import json
import asyncio
import aiosqlite
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import uvicorn
from contextlib import asynccontextmanager


DB_FILE = "isl_exchange.db"

DEFAULT_COMPANIES = [
    ("c1", "SolarTech", "Renewable Energy", 100.0, 100.0),
    ("c2", "MedLife", "Healthcare", 75.0, 75.0),
    ("c3", "SkyLink", "Telecommunications", 90.0, 90.0),
    ("c4", "FoodWave", "Consumer Goods", 50.0, 50.0),
    ("c5", "AutoNova", "Electric Vehicles", 120.0, 120.0),
    ("c6", "AquaPure", "Water Solutions", 60.0, 60.0),
    ("c7", "CyberGuard", "Cybersecurity", 80.0, 80.0),
    ("c8", "GlobalAir", "Aviation", 95.0, 95.0)
]

# --- DATABASE SETUP ---
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS system (key TEXT PRIMARY KEY, value TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS companies (id TEXT PRIMARY KEY, name TEXT, industry TEXT, price REAL, prevPrice REAL)")
        await db.execute("CREATE TABLE IF NOT EXISTS teams (username TEXT PRIMARY KEY, password TEXT, teamName TEXT, cash REAL)")
        await db.execute("CREATE TABLE IF NOT EXISTS holdings (username TEXT, company_id TEXT, quantity INTEGER, PRIMARY KEY (username, company_id))")
        
        # Seed default data if system is empty
        async with db.execute("SELECT count(*) FROM system") as cursor:
            if (await cursor.fetchone())[0] == 0:
                await db.execute("INSERT INTO system (key, value) VALUES ('marketOpen', 'false')")
                await db.execute("INSERT INTO system (key, value) VALUES ('round', '1')")
                await db.execute("INSERT INTO system (key, value) VALUES ('adminPassword', '7fedfb67')")
                await db.executemany("INSERT INTO companies (id, name, industry, price, prevPrice) VALUES (?, ?, ?, ?, ?)", DEFAULT_COMPANIES)
        await db.commit()

# --- STATE COMPILER ---
async def get_full_state():
    """Queries the normalized SQL tables and reconstructs the JSON dictionary for the frontend."""
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        
        sys_rows = await db.execute("SELECT key, value FROM system")
        sys_data = {row["key"]: row["value"] for row in await sys_rows.fetchall()}
        
        comp_rows = await db.execute("SELECT * FROM companies")
        companies = [dict(row) for row in await comp_rows.fetchall()]
        
        team_rows = await db.execute("SELECT * FROM teams")
        teams = []
        for trow in await team_rows.fetchall():
            team_dict = dict(trow)
            holdings_rows = await db.execute("SELECT company_id, quantity FROM holdings WHERE username = ?", (team_dict["username"],))
            team_dict["holdings"] = {h["company_id"]: h["quantity"] for h in await holdings_rows.fetchall()}
            teams.append(team_dict)

        return {
            "marketOpen": sys_data.get("marketOpen") == "true",
            "round": int(sys_data.get("round", 1)),
            "adminPassword": sys_data.get("adminPassword", "7fedfb67"),
            "companies": companies,
            "teams": teams
        }

# --- WEBSOCKET MANAGER ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        state = await get_full_state()
        await websocket.send_json(state)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast_state(self):
        state = await get_full_state()
        for connection in self.active_connections:
            await connection.send_json(state)
    
    # Send live trade alerts explicitly to connected screens
    async def broadcast_log(self, text: str, log_type: str):
        msg = {"type": "log", "text": text, "logType": log_type}
        for connection in self.active_connections:
            try:
                await connection.send_json(msg)
            except:
                pass

manager = ConnectionManager()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Everything before 'yield' runs on server startup
    await init_db()
    yield
    # Everything after 'yield' runs on server shutdown (none needed here)

app = FastAPI(lifespan=lifespan)

async def process_action(payload: dict):
    action = payload.get("action")
    auth_pass = payload.get("authPass", "") # Extract the hidden password
    
    async with aiosqlite.connect(DB_FILE) as db:
        
        # --- ZERO-TRUST ADMIN VERIFICATION ---
        async with db.execute("SELECT value FROM system WHERE key = 'adminPassword'") as cursor:
            admin_pw = (await cursor.fetchone())[0]
            
        # Is the sender actually the admin, and did they provide the exact correct password?
        is_admin = (payload.get("admin") is True) and (auth_pass == admin_pw)
        
        # --- ADMIN ACTIONS ---
        if action == "toggle-market" and is_admin:
            await db.execute("UPDATE system SET value = CASE WHEN value = 'true' THEN 'false' ELSE 'true' END WHERE key = 'marketOpen'")
            await db.commit()
            
        elif action == "apply-price" and is_admin:
            cid = payload.get("id")
            modifier = float(payload.get("modifier", 0))
            await db.execute("""
                UPDATE companies 
                SET prevPrice = price, price = MAX(1.0, price + ?) 
                WHERE id = ?
            """, (modifier, cid))
            await db.commit()
            
        elif action == "next-round" and is_admin:
            await db.execute("UPDATE system SET value = CAST(value AS INTEGER) + 1 WHERE key = 'round'")
            await db.commit()

        elif action == "reset-game" and is_admin:
            await db.execute("UPDATE system SET value = '1' WHERE key = 'round'")
            await db.execute("UPDATE system SET value = 'false' WHERE key = 'marketOpen'")
            await db.execute("UPDATE teams SET cash = 100000.0")
            await db.execute("DELETE FROM holdings")
            await db.commit()

        elif action == "add-company" and is_admin:
            import time
            new_id = "c" + str(int(time.time() * 1000))
            name = payload.get("name")
            industry = payload.get("industry")
            price = float(payload.get("price", 10.0))
            if price <= 0: return "INVALID PRICE."
            
            await db.execute(
                "INSERT INTO companies (id, name, industry, price, prevPrice) VALUES (?, ?, ?, ?, ?)",
                (new_id, name, industry, price, price)
            )
            await db.commit()

        elif action == "delete-company" and is_admin:
            cid = payload.get("id")
            await db.execute("DELETE FROM companies WHERE id = ?", (cid,))
            await db.execute("DELETE FROM holdings WHERE company_id = ?", (cid,))
            await db.commit()

        elif action == "add-team" and is_admin:
            username = payload.get("username").lower()
            password = payload.get("password")
            team_name = payload.get("teamName")
            
            if username == "admin":
                return "Reserved system name. Cannot use 'admin'."
            
            async with db.execute("SELECT username FROM teams WHERE username = ?", (username,)) as cursor:
                if await cursor.fetchone():
                    return "Username already exists."
                    
            await db.execute(
                "INSERT INTO teams (username, password, teamName, cash) VALUES (?, ?, ?, 100000.0)",
                (username, password, team_name)
            )
            await db.commit()

        elif action == "delete-team" and is_admin:
            username = payload.get("username")
            await db.execute("DELETE FROM teams WHERE username = ?", (username,))
            await db.execute("DELETE FROM holdings WHERE username = ?", (username,))
            await db.commit()

        elif action == "change-password" and is_admin:
            next_pw = payload.get("next")
            if next_pw:
                await db.execute("UPDATE system SET value = ? WHERE key = 'adminPassword'", (next_pw,))
                await db.commit()

        elif action == "adjust-cash" and is_admin:
            username = payload.get("username")
            amt = float(payload.get("amount", 0))
            if username:
                await db.execute("UPDATE teams SET cash = cash + ? WHERE username = ?", (amt, username))
                await db.commit()

        # --- TEAM ACTIONS ---
        elif action in ["buy", "sell"] and payload.get("username"):
            username = payload.get("username")
            
            # ZERO-TRUST IDENTITY VERIFICATION: Does the provided password match this username?
            async with db.execute("SELECT password, teamName FROM teams WHERE username = ?", (username,)) as cursor:
                row = await cursor.fetchone()
                if not row or row[0] != auth_pass:
                    return "UNAUTHORIZED ACTION: IDENTITY VERIFICATION FAILED."
            
            t_name = row[1]
            cid = payload.get("companyId")
            
            # NEGATIVE NUMBER GLITCH PATCH: Force integers and reject <= 0
            try:
                qty = int(payload.get("quantity", 0))
                if qty <= 0:
                    return "NICE TRY. QUANTITY MUST BE GREATER THAN ZERO."
            except ValueError:
                return "INVALID QUANTITY FORMAT."
            
            async with db.execute("SELECT value FROM system WHERE key = 'marketOpen'") as cursor:
                val = str((await cursor.fetchone())[0]).lower()
                if val not in ["true", "1", "yes"]: 
                    return "MARKET IS CURRENTLY CLOSED."
            
            async with db.execute("SELECT price, name FROM companies WHERE id = ?", (cid,)) as cursor:
                c_row = await cursor.fetchone()
                if not c_row: 
                    return "COMPANY NOT FOUND."
                price = c_row[0]
                c_name = c_row[1]

            if action == "buy":
                cost = price * qty
                async with db.execute("SELECT cash FROM teams WHERE username = ?", (username,)) as cursor:
                    t_cash = (await cursor.fetchone())[0]
                    if t_cash < cost: 
                        return "INSUFFICIENT FUNDS."
                        
                await db.execute("UPDATE teams SET cash = cash - ? WHERE username = ?", (cost, username))
                await db.execute("""
                    INSERT INTO holdings (username, company_id, quantity) 
                    VALUES (?, ?, ?) 
                    ON CONFLICT(username, company_id) 
                    DO UPDATE SET quantity = quantity + ?
                """, (username, cid, qty, qty))
                await db.commit()
                await manager.broadcast_log(f"{t_name} bought {qty} shares of {c_name}", "trade-up")

            elif action == "sell":
                proceeds = price * qty
                async with db.execute("SELECT quantity FROM holdings WHERE username = ? AND company_id = ?", (username, cid)) as cursor:
                    h_row = await cursor.fetchone()
                    if not h_row or h_row[0] < qty: 
                        return "NOT ENOUGH SHARES OWNED."
                    
                await db.execute("UPDATE teams SET cash = cash + ? WHERE username = ?", (proceeds, username))
                await db.execute("UPDATE holdings SET quantity = quantity - ? WHERE username = ? AND company_id = ?", (qty, username, cid))
                await db.execute("DELETE FROM holdings WHERE quantity <= 0")
                await db.commit()
                await manager.broadcast_log(f"{t_name} sold {qty} shares of {c_name}", "trade-down")

    return None

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            payload = json.loads(data)
            
            try:
                # Process the transaction
                error_msg = await process_action(payload)
                
                if error_msg:
                    # Send intended rejections (e.g. "Insufficient Funds")
                    await websocket.send_json({"type": "error", "message": str(error_msg).upper()})
                else:
                    # Success: Broadcast the updated reality
                    await manager.broadcast_state()
                    
            except Exception as e:
                # TRAP CRASHES: If the DB locks or Python fails, tell the UI immediately
                await websocket.send_json({"type": "error", "message": f"SYSTEM CRASH: {str(e)}".upper()})
                
    except WebSocketDisconnect:
        manager.disconnect(websocket)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, ws="websockets")
