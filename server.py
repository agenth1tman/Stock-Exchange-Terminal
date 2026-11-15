import argparse
import asyncio
import getpass
import hashlib
import hmac
import ipaddress
import itertools
import json
import logging
import math
import os
import re
import secrets
import socket
import sys
import time
from collections import deque
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import aiosqlite
import uvicorn
from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse, JSONResponse


BASE_DIR = Path(__file__).resolve().parent
DB_FILE = Path(os.environ.get("ISL_DB_FILE") or BASE_DIR / "isl_exchange.db")
HTML_FILE = BASE_DIR / "isl-stock-exchange.html"

STARTING_CASH = 100000.0
ADMIN_USERNAME = "admin"
# The old server hard-coded this password and sent it to every browser.
LEGACY_ADMIN_PASSWORD = "7fedfb67"

SESSION_TTL = 12 * 60 * 60          # seconds a login stays valid
MAX_MESSAGE_CHARS = 16 * 1024       # largest message a browser may send
MAX_CLIENTS = 500                   # simultaneous connections
MAX_OUTBOX = 200                    # queued messages before a stuck client is dropped
SEND_TIMEOUT = 10                   # seconds before a stuck client is dropped
BROADCAST_DELAY = 0.05              # bursts of trades are merged into one update
RATE_PER_SEC, RATE_BURST = 15, 40   # messages per connection
LOGIN_FAIL_LIMIT = 20               # failed logins allowed per address...
LOGIN_FAIL_WINDOW = 300             # ...within this many seconds
LOGIN_FAIL_DELAY = 0.6              # pause after every failed login
ACTIVITY_SHOWN = 100                # activity entries sent to the admin console

MAX_QUANTITY = 1_000_000
MIN_PRICE, MAX_PRICE = 1.0, 1_000_000.0
MAX_CASH_ADJUST = 1_000_000_000.0
NAME_MAX = 40
TEAM_PASSWORD_MIN = 4
ADMIN_PASSWORD_MIN = 8
PASSWORD_MAX = 128

DEFAULT_COMPANIES = [
    ("c1", "SolarTech", "Renewable Energy", 100.0, 100.0),
    ("c2", "MedLife", "Healthcare", 75.0, 75.0),
    ("c3", "SkyLink", "Telecommunications", 90.0, 90.0),
    ("c4", "FoodWave", "Consumer Goods", 50.0, 50.0),
    ("c5", "AutoNova", "Electric Vehicles", 120.0, 120.0),
    ("c6", "AquaPure", "Water Solutions", 60.0, 60.0),
    ("c7", "CyberGuard", "Cybersecurity", 80.0, 80.0),
    ("c8", "GlobalAir", "Aviation", 95.0, 95.0),
]

log = logging.getLogger("msx")


# --- PASSWORDS ---
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored) -> bool:
    try:
        algo, n, r, p, salt_hex, digest_hex = str(stored).split("$")
        if algo != "scrypt":
            return False
        expected = bytes.fromhex(digest_hex)
        digest = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                                n=int(n), r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


def is_hashed(value) -> bool:
    return isinstance(value, str) and value.startswith("scrypt$")


# Used for unknown usernames so a wrong username takes as long as a wrong password.
DUMMY_HASH = hash_password(secrets.token_hex(16))
_hash_slots = asyncio.Semaphore(4)


async def hash_password_async(password: str) -> str:
    async with _hash_slots:
        return await asyncio.to_thread(hash_password, password)


async def verify_password_async(password: str, stored) -> bool:
    async with _hash_slots:
        return await asyncio.to_thread(verify_password, password, stored)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --- INPUT VALIDATION ---
class UserError(Exception):
    """A request the server refuses; the message is shown to the user."""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


USERNAME_RE = re.compile(r"[a-z0-9_.-]{2,32}")
ID_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")
INT_RE = re.compile(r"[+-]?\d{1,15}")
_BIDI_CONTROLS = set(range(0x202A, 0x202F)) | set(range(0x2066, 0x206A))


def _bad_char(ch: str) -> bool:
    o = ord(ch)
    return o < 32 or 0x7F <= o < 0xA0 or 0xD800 <= o <= 0xDFFF or o in _BIDI_CONTROLS


def money(value) -> float:
    return round(float(value), 2) + 0.0


def usd(value) -> str:
    return f"${value:,.2f}"


def clean_text(value, field: str, max_len: int = NAME_MAX) -> str:
    if not isinstance(value, str):
        raise UserError(f"{field} IS REQUIRED.")
    text = " ".join(value.split())
    if not text:
        raise UserError(f"{field} IS REQUIRED.")
    if len(text) > max_len:
        raise UserError(f"{field} MUST BE {max_len} CHARACTERS OR FEWER.")
    if any(_bad_char(ch) for ch in text):
        raise UserError(f"{field} CONTAINS INVALID CHARACTERS.")
    return text


def clean_new_username(value) -> str:
    if not isinstance(value, str):
        raise UserError("USERNAME IS REQUIRED.")
    name = value.strip().lower()
    if not USERNAME_RE.fullmatch(name):
        raise UserError("USERNAME MUST BE 2-32 CHARACTERS: LETTERS, NUMBERS, DOT, DASH OR UNDERSCORE.")
    if name == ADMIN_USERNAME:
        raise UserError("RESERVED SYSTEM NAME. CANNOT USE 'ADMIN'.")
    return name


def clean_existing_username(value) -> str:
    # Older versions allowed any characters, so look-ups only check the basics.
    if not isinstance(value, str) or not 1 <= len(value) <= 64 or any(_bad_char(ch) for ch in value):
        raise UserError("TEAM NOT FOUND.")
    return value


def clean_password(value, field: str, min_len: int) -> str:
    if not isinstance(value, str) or not value:
        raise UserError(f"{field} IS REQUIRED.")
    if len(value) < min_len:
        raise UserError(f"{field} MUST BE AT LEAST {min_len} CHARACTERS.")
    if len(value) > PASSWORD_MAX:
        raise UserError(f"{field} MUST BE {PASSWORD_MAX} CHARACTERS OR FEWER.")
    if any(_bad_char(ch) for ch in value):
        raise UserError(f"{field} CONTAINS INVALID CHARACTERS.")
    return value


def clean_id(value, field: str = "COMPANY") -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise UserError(f"{field} NOT FOUND.")
    return value


def clean_int(value, field: str, lo: int, hi: int) -> int:
    if isinstance(value, bool):
        raise UserError(f"INVALID {field}.")
    if isinstance(value, float):
        if not value.is_integer():
            raise UserError(f"{field} MUST BE A WHOLE NUMBER.")
        value = int(value)
    elif isinstance(value, str):
        if not INT_RE.fullmatch(value.strip()):
            raise UserError(f"{field} MUST BE A WHOLE NUMBER.")
        value = int(value.strip())
    elif not isinstance(value, int):
        raise UserError(f"INVALID {field}.")
    if not lo <= value <= hi:
        raise UserError(f"{field} MUST BE BETWEEN {lo:,} AND {hi:,}.")
    return value


def clean_amount(value, field: str, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise UserError(f"INVALID {field}.")
    try:
        number = float(value.strip() if isinstance(value, str) else value)
    except (ValueError, OverflowError):
        raise UserError(f"{field} MUST BE A NUMBER.") from None
    if not math.isfinite(number):
        raise UserError(f"{field} MUST BE A NUMBER.")
    number = money(number)
    if not lo <= number <= hi:
        raise UserError(f"{field} MUST BE BETWEEN {usd(lo)} AND {usd(hi)}.")
    return number


# --- DATABASE ---
class Store:
    """One SQLite connection shared by everything, guarded by one lock."""

    def __init__(self, path: Path):
        self.path = path
        self.conn: aiosqlite.Connection | None = None
        self.lock = asyncio.Lock()

    async def open(self):
        self.conn = await aiosqlite.connect(self.path, isolation_level=None)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA busy_timeout = 5000")
        # The default journal mode syncs to disk several times per commit, which
        # costs 60-130 ms on Windows and capped the whole game at ~10 trades a
        # second. WAL commits take ~0.1 ms and still survive a crashed server.
        await self.conn.execute("PRAGMA journal_mode = WAL")
        await self.conn.execute("PRAGMA synchronous = NORMAL")

    async def close(self):
        if self.conn is not None:
            await self.conn.close()
            self.conn = None

    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                await self.conn.execute("ROLLBACK")
                raise
            await self.conn.execute("COMMIT")

    @asynccontextmanager
    async def reading(self):
        async with self.lock:
            yield self.conn


store = Store(DB_FILE)

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS system (key TEXT PRIMARY KEY, value TEXT)",
    "CREATE TABLE IF NOT EXISTS companies (id TEXT PRIMARY KEY, name TEXT, industry TEXT, price REAL, prevPrice REAL)",
    "CREATE TABLE IF NOT EXISTS teams (username TEXT PRIMARY KEY, password TEXT, teamName TEXT, cash REAL)",
    "CREATE TABLE IF NOT EXISTS holdings (username TEXT, company_id TEXT, quantity INTEGER, PRIMARY KEY (username, company_id))",
    "CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, role TEXT NOT NULL, username TEXT NOT NULL, expires REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS activity (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL)",
)


async def fetch_one(db, sql: str, params=()):
    async with db.execute(sql, params) as cursor:
        return await cursor.fetchone()


async def get_setting(db, key: str):
    row = await fetch_one(db, "SELECT value FROM system WHERE key = ?", (key,))
    return None if row is None else row[0]


async def put_setting(db, key: str, value):
    await db.execute(
        "INSERT INTO system (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


async def record(db, kind: str, text: str):
    await db.execute("INSERT INTO activity (ts, kind, text) VALUES (?, ?, ?)", (time.time(), kind, text))
    log.info("%s", text)


async def market_is_open(db) -> bool:
    return str(await get_setting(db, "marketOpen")).lower() in ("true", "1", "yes")


async def current_round(db) -> int:
    try:
        return int(await get_setting(db, "round"))
    except (TypeError, ValueError):
        return 1


async def require_team(db, username: str):
    row = await fetch_one(db, "SELECT username, teamName, cash FROM teams WHERE username = ?", (username,))
    if row is None:
        raise UserError("TEAM NOT FOUND.")
    return row


async def require_company(db, company_id: str):
    row = await fetch_one(db, "SELECT id, name, price FROM companies WHERE id = ?", (company_id,))
    if row is None:
        raise UserError("COMPANY NOT FOUND.")
    return row


async def init_db():
    """Create tables, convert data left by older versions, and print start-up notes."""
    notes = []
    async with store.transaction() as db:
        for statement in SCHEMA:
            await db.execute(statement)

        if (await fetch_one(db, "SELECT count(*) FROM system"))[0] == 0:
            await put_setting(db, "marketOpen", "false")
            await put_setting(db, "round", "1")
            await db.executemany(
                "INSERT INTO companies (id, name, industry, price, prevPrice) VALUES (?, ?, ?, ?, ?)",
                DEFAULT_COMPANIES,
            )
        if await get_setting(db, "marketOpen") is None:
            await put_setting(db, "marketOpen", "false")
        if await get_setting(db, "round") is None:
            await put_setting(db, "round", "1")

        # Repair rows the old server could leave behind.
        await db.execute("DELETE FROM holdings WHERE quantity IS NULL OR quantity <= 0")
        await db.execute("DELETE FROM holdings WHERE username NOT IN (SELECT username FROM teams) "
                         "OR company_id NOT IN (SELECT id FROM companies)")
        await db.execute("UPDATE teams SET cash = ? WHERE cash IS NULL", (STARTING_CASH,))
        await db.execute("UPDATE companies SET prevPrice = price WHERE prevPrice IS NULL")
        await db.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))

        # Plain-text team passwords -> hashes.
        migrated = 0
        for row in await db.execute_fetchall("SELECT username, password FROM teams"):
            if not is_hashed(row["password"]):
                plain = str(row["password"]) if row["password"] else secrets.token_urlsafe(16)
                await db.execute("UPDATE teams SET password = ? WHERE username = ?",
                                 (await hash_password_async(plain), row["username"]))
                migrated += 1
        if migrated:
            notes.append(f"Converted {migrated} plain-text team password(s) to secure hashes. The old server "
                         "sent every password to every browser, so consider resetting them from the admin "
                         "Teams tab.")

        # Plain-text admin password -> hash.
        admin_hash = await get_setting(db, "adminPasswordHash")
        legacy_admin = await get_setting(db, "adminPassword")
        if admin_hash is None:
            if legacy_admin:
                admin_hash = await hash_password_async(legacy_admin)
                notes.append("Converted the admin password to a secure hash.")
            else:
                chosen = os.environ.get("ISL_ADMIN_PASSWORD")
                password = chosen or secrets.token_urlsafe(9)
                admin_hash = await hash_password_async(password)
                if not chosen:
                    notes.append(f"NEW ADMIN PASSWORD (write it down, it will not be shown again): {password}")
            await put_setting(db, "adminPasswordHash", admin_hash)
        await db.execute("DELETE FROM system WHERE key = 'adminPassword'")

    if await verify_password_async(LEGACY_ADMIN_PASSWORD, admin_hash):
        notes.append("WARNING: the admin password is still the old default, which was public. "
                     "Change it in Admin Console > Settings.")
    return notes


# --- STATE SNAPSHOTS ---
async def load_snapshot(include_activity: bool) -> dict:
    async with store.reading() as db:
        settings = {r["key"]: r["value"] for r in await db.execute_fetchall(
            "SELECT key, value FROM system WHERE key IN ('marketOpen', 'round')")}
        companies = [dict(r) for r in await db.execute_fetchall(
            "SELECT id, name, industry, price, prevPrice FROM companies ORDER BY rowid")]
        teams = [dict(r) for r in await db.execute_fetchall(
            "SELECT username, teamName, cash FROM teams ORDER BY rowid")]
        holdings = await db.execute_fetchall(
            "SELECT username, company_id, quantity FROM holdings WHERE quantity > 0")
        activity = [dict(r) for r in await db.execute_fetchall(
            "SELECT id, ts, kind, text FROM activity ORDER BY id DESC LIMIT ?", (ACTIVITY_SHOWN,))
        ] if include_activity else []

    prices = {c["id"]: c["price"] or 0.0 for c in companies}
    owned: dict[str, dict[str, int]] = {}
    for h in holdings:
        if h["company_id"] in prices:
            owned.setdefault(h["username"], {})[h["company_id"]] = h["quantity"]
    for team in teams:
        team["cash"] = money(team["cash"] or 0)
        team["holdings"] = owned.get(team["username"], {})
        team["portfolio"] = money(sum(prices[cid] * qty for cid, qty in team["holdings"].items()))
        team["equity"] = money(team["cash"] + team["portfolio"])
    try:
        round_no = int(settings.get("round", 1))
    except (TypeError, ValueError):
        round_no = 1
    return {
        "marketOpen": str(settings.get("marketOpen")).lower() in ("true", "1", "yes"),
        "round": round_no,
        "companies": companies,
        "teams": teams,
        "activity": activity,
    }


def admin_view(snap: dict, devices: list) -> dict:
    return {
        "marketOpen": snap["marketOpen"],
        "round": snap["round"],
        "startingCash": STARTING_CASH,
        "companies": snap["companies"],
        "teams": snap["teams"],
        "activity": snap["activity"],
        "devices": devices,
    }


def describe_device(user_agent: str) -> str:
    """A short label such as "iPhone · Safari" from the browser's User-Agent."""
    ua = user_agent or ""
    if "iPhone" in ua:
        system = "iPhone"
    elif "iPad" in ua:
        system = "iPad"
    elif "Android" in ua:
        system = "Android phone" if "Mobile" in ua else "Android tablet"
    elif "CrOS" in ua:
        system = "Chromebook"
    elif "Windows" in ua:
        system = "Windows"
    elif "Macintosh" in ua or "Mac OS X" in ua:
        system = "Mac"
    elif "Linux" in ua:
        system = "Linux"
    else:
        return "Script / unknown" if ua else "Unknown device"
    for marker, name in (("OPR/", "Opera"), ("OPT/", "Opera"), ("Edg", "Edge"), ("SamsungBrowser", "Samsung Internet"),
                         ("CriOS", "Chrome"), ("FxiOS", "Firefox"), ("Firefox/", "Firefox"),
                         ("Chrome/", "Chrome"), ("Safari/", "Safari")):
        if marker in ua:
            return f"{system} · {name}"
    return system


def team_view(snap: dict, username: str) -> dict | None:
    mine = next((t for t in snap["teams"] if t["username"] == username), None)
    if mine is None:
        return None
    ranked = sorted(snap["teams"], key=lambda t: t["equity"], reverse=True)
    return {
        "marketOpen": snap["marketOpen"],
        "round": snap["round"],
        "startingCash": STARTING_CASH,
        "companies": snap["companies"],
        "me": {k: mine[k] for k in ("username", "teamName", "cash", "holdings", "portfolio", "equity")},
        # Other teams are shown by display name only - no usernames, no holdings.
        "leaderboard": [
            {"teamName": t["teamName"], "cash": t["cash"], "portfolio": t["portfolio"],
             "equity": t["equity"], "me": t is mine}
            for t in ranked
        ],
    }


# --- CONNECTIONS ---
class Client:
    """One browser connection. Only its own writer task ever sends on the socket."""

    _ids = itertools.count(1)

    def __init__(self, ws: WebSocket, address: str):
        self.id = next(Client._ids)
        self.ws = ws
        self.address = address
        self.device = describe_device(ws.headers.get("user-agent", ""))
        self.connected_at = time.time()
        self.role: str | None = None          # "admin", "team" or None
        self.username: str | None = None
        self.token_hash: str | None = None
        self.session_expires = 0.0
        self.alive = True
        self.reader_done = False
        self.outbox: deque[str] = deque()
        self.pending_state: str | None = None  # only the newest state is worth sending
        self.wake = asyncio.Event()
        self.tokens = float(RATE_BURST)
        self.refilled = time.monotonic()
        self.throttled = 0
        self.last_throttle_notice = 0.0

    def send(self, payload: dict):
        if not self.alive:
            return
        if len(self.outbox) >= MAX_OUTBOX:
            log.warning("Dropping connection %s from %s: it stopped reading updates", self.id, self.address)
            self.close()
            return
        self.outbox.append(json.dumps(payload))
        self.wake.set()

    def send_state(self, text: str):
        if self.alive:
            self.pending_state = text
            self.wake.set()

    def close(self):
        self.alive = False
        self.wake.set()

    def sign_in(self, role: str, username: str, token_hash: str, expires: float):
        self.role, self.username, self.token_hash, self.session_expires = role, username, token_hash, expires
        hub.request_admin_broadcast()  # the admin Devices tab shows who is signed in where

    def sign_out(self):
        self.role = self.username = self.token_hash = None
        self.session_expires = 0.0
        self.pending_state = None
        hub.request_admin_broadcast()

    def allow_message(self) -> bool:
        now = time.monotonic()
        self.tokens = min(RATE_BURST, self.tokens + (now - self.refilled) * RATE_PER_SEC)
        self.refilled = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        self.throttled += 1
        return False

    async def run_writer(self):
        try:
            while self.alive:
                await self.wake.wait()
                self.wake.clear()
                while self.alive:
                    if self.outbox:
                        text = self.outbox.popleft()
                    elif self.pending_state is not None:
                        text, self.pending_state = self.pending_state, None
                    else:
                        break
                    await asyncio.wait_for(self.ws.send_text(text), SEND_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass  # the browser went away or stopped reading
        finally:
            self.alive = False
            if not self.reader_done:
                with suppress(Exception):
                    await self.ws.close(code=1011)


class Hub:
    def __init__(self):
        self.clients: set[Client] = set()
        self.dirty = asyncio.Event()
        self.everyone_dirty = False
        self.version = 0

    def request_broadcast(self):
        """The game changed: update every signed-in screen."""
        self.everyone_dirty = True
        self.dirty.set()

    def request_admin_broadcast(self):
        """Only the device list changed: update admin screens."""
        self.dirty.set()

    async def run(self):
        while True:
            await self.dirty.wait()
            await asyncio.sleep(BROADCAST_DELAY)
            self.dirty.clear()
            everyone, self.everyone_dirty = self.everyone_dirty, False
            try:
                await self.broadcast_now(None if everyone else [c for c in self.clients if c.role == "admin"])
            except Exception:
                log.exception("Broadcast failed")

    def device_list(self, snap: dict) -> list:
        team_names = {t["username"]: t["teamName"] for t in snap["teams"]}
        order = {"admin": 0, "team": 1, "login": 2}
        devices = []
        for c in self.clients:
            if not c.alive:
                continue
            if c.role == "admin":
                kind, account = "admin", "Admin"
            elif c.role == "team":
                kind, account = "team", team_names.get(c.username, c.username)
            else:
                kind, account = "login", "On Login Page"
            devices.append({"id": c.id, "kind": kind, "account": account,
                            "username": c.username if kind == "team" else None,
                            "device": c.device, "address": c.address, "since": c.connected_at})
        devices.sort(key=lambda d: (order[d["kind"]], d["account"].lower(), d["id"]))
        return devices

    async def broadcast_now(self, only=None):
        pool = list(only if only is not None else self.clients)
        if not any(c.alive and c.role for c in pool):
            return
        snap = await load_snapshot(include_activity=any(c.role == "admin" for c in pool))
        # No awaits from here on, so roles cannot change while we fan out.
        self.version += 1
        devices = self.device_list(snap) if any(c.role == "admin" for c in pool) else []
        cache: dict[tuple, str | None] = {}
        for client in pool:
            if not (client.alive and client.role):
                continue
            key = ("admin",) if client.role == "admin" else ("team", client.username)
            if key not in cache:
                view = admin_view(snap, devices) if client.role == "admin" else team_view(snap, client.username)
                cache[key] = None if view is None else json.dumps(
                    {"type": "state", "version": self.version, "state": view})
            if cache[key] is not None:
                client.send_state(cache[key])

    def sign_out_where(self, predicate, reason: str):
        for client in list(self.clients):
            if client.role and predicate(client):
                client.sign_out()
                client.send({"type": "signed_out", "reason": reason})


hub = Hub()


class FailedLogins:
    """Counts failed logins per network address in a sliding window."""

    def __init__(self):
        self.events: dict[str, deque] = {}

    def _recent(self, key: str) -> int:
        queue = self.events.get(key)
        if not queue:
            return 0
        cutoff = time.monotonic() - LOGIN_FAIL_WINDOW
        while queue and queue[0] < cutoff:
            queue.popleft()
        if not queue:
            del self.events[key]
        return len(queue)

    def blocked(self, key: str) -> bool:
        return self._recent(key) >= LOGIN_FAIL_LIMIT

    def record(self, key: str):
        if len(self.events) > 10000:
            self.events.clear()
        self.events.setdefault(key, deque()).append(time.monotonic())


failed_logins = FailedLogins()


def client_address(ws: WebSocket) -> str:
    peer = ws.client.host if ws.client else ""
    try:
        via_local_proxy = ipaddress.ip_address(peer).is_loopback
    except ValueError:
        via_local_proxy = False
    if via_local_proxy:
        # cloudflared / ngrok run on this machine and pass the real address along.
        forwarded = ws.headers.get("cf-connecting-ip") or ws.headers.get("x-forwarded-for", "").split(",")[0]
        if forwarded.strip():
            return forwarded.strip()[:64]
    return peer or "unknown"


# --- REQUEST HANDLERS ---
ACTIONS: dict[str, tuple] = {}


def action(name: str, role: str | None = None):
    def register(func):
        ACTIONS[name] = (func, role)
        return func
    return register


async def start_session(client: Client, role: str, username: str):
    token = secrets.token_urlsafe(32)
    token_hash = token_digest(token)
    expires = time.time() + SESSION_TTL
    async with store.transaction() as db:
        await db.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
        if client.token_hash:
            await db.execute("DELETE FROM sessions WHERE token_hash = ?", (client.token_hash,))
        await db.execute("INSERT INTO sessions (token_hash, role, username, expires) VALUES (?, ?, ?, ?)",
                         (token_hash, role, username, expires))
        team_name = None
        if role == "team":
            team_name = (await require_team(db, username))["teamName"]
    client.sign_in(role, username, token_hash, expires)
    client.send({"type": "auth", "role": role, "username": username, "teamName": team_name, "token": token})
    await hub.broadcast_now(only=[client])


@action("login")
async def do_login(client: Client, msg: dict):
    if failed_logins.blocked(client.address):
        raise UserError("TOO MANY FAILED SIGN-IN ATTEMPTS. WAIT A FEW MINUTES AND TRY AGAIN.")
    username, password = msg.get("username"), msg.get("password")
    if not isinstance(username, str) or not isinstance(password, str) or not username.strip() or not password:
        raise UserError("USERNAME AND PASSWORD ARE REQUIRED.")
    username = username.strip().lower()

    stored = None
    if len(username) <= 64 and len(password) <= PASSWORD_MAX:
        async with store.reading() as db:
            if username == ADMIN_USERNAME:
                stored = await get_setting(db, "adminPasswordHash")
            else:
                row = await fetch_one(db, "SELECT password FROM teams WHERE username = ?", (username,))
                stored = row["password"] if row else None
    ok = await verify_password_async(password, stored or DUMMY_HASH)
    if not (ok and stored):
        failed_logins.record(client.address)
        log.info("Failed sign-in for %r from %s", username[:64], client.address)
        await asyncio.sleep(LOGIN_FAIL_DELAY)
        raise UserError("INVALID USERNAME OR PASSWORD.")
    await start_session(client, "admin" if username == ADMIN_USERNAME else "team", username)


@action("resume")
async def do_resume(client: Client, msg: dict):
    token = msg.get("token")
    expired = UserError("SESSION EXPIRED. PLEASE SIGN IN AGAIN.", code="session_invalid")
    if not isinstance(token, str) or not 20 <= len(token) <= 100:
        raise expired
    token_hash = token_digest(token)
    async with store.reading() as db:
        row = await fetch_one(db, "SELECT role, username, expires FROM sessions WHERE token_hash = ?", (token_hash,))
        team = None
        if row and row["role"] == "team":
            team = await fetch_one(db, "SELECT teamName FROM teams WHERE username = ?", (row["username"],))
    if row is None or row["expires"] < time.time() or (row["role"] == "team" and team is None):
        raise expired
    client.sign_in(row["role"], row["username"], token_hash, row["expires"])
    client.send({"type": "auth", "role": row["role"], "username": row["username"],
                 "teamName": team["teamName"] if team else None})
    await hub.broadcast_now(only=[client])


@action("logout")
async def do_logout(client: Client, msg: dict):
    if client.token_hash:
        async with store.transaction() as db:
            await db.execute("DELETE FROM sessions WHERE token_hash = ?", (client.token_hash,))
    client.sign_out()


def check_expected_price(expected, price: float, name: str):
    # The browser sends the price it showed; refuse if it changed in the meantime.
    if expected is None:
        return
    seen = clean_amount(expected, "PRICE", 0, MAX_PRICE)
    if abs(seen - price) > 0.004:
        raise UserError(f"PRICE OF {name.upper()} CHANGED TO {usd(price)}. CHECK AND TRY AGAIN.")


@action("buy", role="team")
async def do_buy(client: Client, msg: dict):
    company_id = clean_id(msg.get("companyId"))
    qty = clean_int(msg.get("quantity"), "QUANTITY", 1, MAX_QUANTITY)
    async with store.transaction() as db:
        team = await require_team(db, client.username)
        if not await market_is_open(db):
            raise UserError("MARKET IS CURRENTLY CLOSED.")
        company = await require_company(db, company_id)
        price = company["price"]
        check_expected_price(msg.get("expectedPrice"), price, company["name"])
        cost = money(price * qty)
        cash = money(team["cash"] or 0)
        if cost > cash:
            raise UserError(f"INSUFFICIENT FUNDS. COST {usd(cost)}, CASH {usd(cash)}.")
        await db.execute("UPDATE teams SET cash = ? WHERE username = ?", (money(cash - cost), client.username))
        await db.execute(
            "INSERT INTO holdings (username, company_id, quantity) VALUES (?, ?, ?) "
            "ON CONFLICT(username, company_id) DO UPDATE SET quantity = quantity + excluded.quantity",
            (client.username, company_id, qty),
        )
        await record(db, "trade-up", f"{team['teamName']} bought {qty:,} {company['name']} @ {usd(price)}")
    hub.request_broadcast()
    return f"BOUGHT {qty:,} SHARES OF {company['name'].upper()} @ {usd(price)}"


@action("sell", role="team")
async def do_sell(client: Client, msg: dict):
    company_id = clean_id(msg.get("companyId"))
    qty = clean_int(msg.get("quantity"), "QUANTITY", 1, MAX_QUANTITY)
    async with store.transaction() as db:
        team = await require_team(db, client.username)
        if not await market_is_open(db):
            raise UserError("MARKET IS CURRENTLY CLOSED.")
        company = await require_company(db, company_id)
        price = company["price"]
        check_expected_price(msg.get("expectedPrice"), price, company["name"])
        row = await fetch_one(db, "SELECT quantity FROM holdings WHERE username = ? AND company_id = ?",
                              (client.username, company_id))
        owned = row["quantity"] if row else 0
        if owned < qty:
            raise UserError(f"NOT ENOUGH SHARES. YOU OWN {owned:,} {company['name'].upper()}.")
        if owned == qty:
            await db.execute("DELETE FROM holdings WHERE username = ? AND company_id = ?",
                             (client.username, company_id))
        else:
            await db.execute("UPDATE holdings SET quantity = quantity - ? WHERE username = ? AND company_id = ?",
                             (qty, client.username, company_id))
        proceeds = money(price * qty)
        await db.execute("UPDATE teams SET cash = ? WHERE username = ?",
                         (money((team["cash"] or 0) + proceeds), client.username))
        await record(db, "trade-down", f"{team['teamName']} sold {qty:,} {company['name']} @ {usd(price)}")
    hub.request_broadcast()
    return f"SOLD {qty:,} SHARES OF {company['name'].upper()} @ {usd(price)}"


@action("set-market", role="admin")
async def do_set_market(client: Client, msg: dict):
    want_open = msg.get("open")
    if not isinstance(want_open, bool):
        raise UserError("INVALID REQUEST.")
    async with store.transaction() as db:
        if await market_is_open(db) == want_open:
            return f"MARKET IS ALREADY {'OPEN' if want_open else 'CLOSED'}."
        await put_setting(db, "marketOpen", "true" if want_open else "false")
        await record(db, "market", "Market opened by admin" if want_open else "Market closed by admin")
    hub.request_broadcast()
    return "MARKET OPENED." if want_open else "MARKET CLOSED."


@action("next-round", role="admin")
async def do_next_round(client: Client, msg: dict):
    async with store.transaction() as db:
        round_no = await current_round(db)
        if msg.get("expectedRound") is not None:
            # Stops a double click (or two admins) from skipping a round.
            if clean_int(msg.get("expectedRound"), "ROUND", 0, 10 ** 9) != round_no:
                raise UserError(f"ALREADY ON ROUND {round_no}.")
        await put_setting(db, "round", round_no + 1)
        await record(db, "round", f"Advanced to Round {round_no + 1}")
    hub.request_broadcast()
    return f"ADVANCED TO ROUND {round_no + 1}."


@action("reset-game", role="admin")
async def do_reset_game(client: Client, msg: dict):
    async with store.transaction() as db:
        await put_setting(db, "round", "1")
        await put_setting(db, "marketOpen", "false")
        await db.execute("UPDATE teams SET cash = ?", (STARTING_CASH,))
        await db.execute("DELETE FROM holdings")
        await record(db, "reset", "Game reset by admin")
    hub.request_broadcast()
    return "GAME RESET."


@action("apply-price", role="admin")
async def do_apply_price(client: Client, msg: dict):
    company_id = clean_id(msg.get("id"))
    modifier = clean_amount(msg.get("modifier"), "PRICE CHANGE", -MAX_PRICE, MAX_PRICE)
    if modifier == 0:
        raise UserError("ENTER A NON-ZERO PRICE CHANGE.")
    async with store.transaction() as db:
        company = await require_company(db, company_id)
        new_price = money(min(MAX_PRICE, max(MIN_PRICE, (company["price"] or 0) + modifier)))
        await db.execute("UPDATE companies SET prevPrice = price, price = ? WHERE id = ?", (new_price, company_id))
        direction = "increased" if modifier > 0 else "decreased"
        await record(db, "trade-up" if modifier > 0 else "trade-down",
                     f"{company['name']} price {direction} by {usd(abs(modifier))} to {usd(new_price)}")
    hub.request_broadcast()
    return f"{company['name'].upper()} IS NOW {usd(new_price)}."


@action("add-company", role="admin")
async def do_add_company(client: Client, msg: dict):
    name = clean_text(msg.get("name"), "COMPANY NAME")
    industry = clean_text(msg.get("industry") or "General", "INDUSTRY")
    price = clean_amount(msg.get("price"), "PRICE", MIN_PRICE, MAX_PRICE)
    async with store.transaction() as db:
        if await fetch_one(db, "SELECT 1 FROM companies WHERE lower(name) = lower(?)", (name,)):
            raise UserError("A COMPANY WITH THAT NAME ALREADY EXISTS.")
        company_id = "c" + secrets.token_hex(6)
        await db.execute("INSERT INTO companies (id, name, industry, price, prevPrice) VALUES (?, ?, ?, ?, ?)",
                         (company_id, name, industry, price, price))
        await record(db, "admin", f"Listed {name} at {usd(price)}")
    hub.request_broadcast()
    return f"{name.upper()} LISTED AT {usd(price)}."


@action("delete-company", role="admin")
async def do_delete_company(client: Client, msg: dict):
    company_id = clean_id(msg.get("id"))
    async with store.transaction() as db:
        company = await require_company(db, company_id)
        await db.execute("DELETE FROM holdings WHERE company_id = ?", (company_id,))
        await db.execute("DELETE FROM companies WHERE id = ?", (company_id,))
        await record(db, "admin", f"Removed {company['name']}")
    hub.request_broadcast()
    return f"{company['name'].upper()} REMOVED."


@action("add-team", role="admin")
async def do_add_team(client: Client, msg: dict):
    team_name = clean_text(msg.get("teamName"), "TEAM NAME")
    username = clean_new_username(msg.get("username"))
    password = clean_password(msg.get("password"), "PASSWORD", TEAM_PASSWORD_MIN)
    password_hash = await hash_password_async(password)
    async with store.transaction() as db:
        if await fetch_one(db, "SELECT 1 FROM teams WHERE username = ?", (username,)):
            raise UserError("USERNAME ALREADY EXISTS.")
        await db.execute("INSERT INTO teams (username, password, teamName, cash) VALUES (?, ?, ?, ?)",
                         (username, password_hash, team_name, STARTING_CASH))
        await record(db, "admin", f"Created team {team_name} ({username})")
    hub.request_broadcast()
    return f"TEAM {team_name.upper()} CREATED."


@action("delete-team", role="admin")
async def do_delete_team(client: Client, msg: dict):
    username = clean_existing_username(msg.get("username"))
    async with store.transaction() as db:
        team = await require_team(db, username)
        await db.execute("DELETE FROM holdings WHERE username = ?", (username,))
        await db.execute("DELETE FROM sessions WHERE username = ? AND role = 'team'", (username,))
        await db.execute("DELETE FROM teams WHERE username = ?", (username,))
        await record(db, "admin", f"Removed team {team['teamName']}")
    hub.sign_out_where(lambda c: c.role == "team" and c.username == username,
                       "YOUR TEAM ACCOUNT WAS REMOVED BY THE ADMIN.")
    hub.request_broadcast()
    return f"TEAM {team['teamName'].upper()} REMOVED."


@action("reset-team-password", role="admin")
async def do_reset_team_password(client: Client, msg: dict):
    username = clean_existing_username(msg.get("username"))
    password = clean_password(msg.get("password"), "PASSWORD", TEAM_PASSWORD_MIN)
    password_hash = await hash_password_async(password)
    async with store.transaction() as db:
        team = await require_team(db, username)
        await db.execute("UPDATE teams SET password = ? WHERE username = ?", (password_hash, username))
        await db.execute("DELETE FROM sessions WHERE username = ? AND role = 'team'", (username,))
        await record(db, "admin", f"Password reset for {team['teamName']}")
    hub.sign_out_where(lambda c: c.role == "team" and c.username == username,
                       "YOUR PASSWORD WAS CHANGED BY THE ADMIN. PLEASE SIGN IN AGAIN.")
    return f"PASSWORD RESET FOR {team['teamName'].upper()}. THEIR DEVICES WERE SIGNED OUT."


@action("change-password", role="admin")
async def do_change_password(client: Client, msg: dict):
    current = msg.get("current")
    new_password = clean_password(msg.get("next"), "NEW PASSWORD", ADMIN_PASSWORD_MIN)
    if failed_logins.blocked(client.address):
        raise UserError("TOO MANY FAILED ATTEMPTS. WAIT A FEW MINUTES AND TRY AGAIN.")
    async with store.reading() as db:
        stored = await get_setting(db, "adminPasswordHash")
    if not isinstance(current, str) or len(current) > PASSWORD_MAX or not await verify_password_async(current, stored):
        failed_logins.record(client.address)
        await asyncio.sleep(LOGIN_FAIL_DELAY)
        raise UserError("CURRENT PASSWORD IS INCORRECT.")
    new_hash = await hash_password_async(new_password)
    async with store.transaction() as db:
        await put_setting(db, "adminPasswordHash", new_hash)
        await db.execute("DELETE FROM sessions WHERE role = 'admin' AND token_hash != ?", (client.token_hash,))
        await record(db, "admin", "Admin password changed")
    hub.sign_out_where(lambda c: c.role == "admin" and c is not client,
                       "THE ADMIN PASSWORD WAS CHANGED. PLEASE SIGN IN AGAIN.")
    return "ADMIN PASSWORD UPDATED. OTHER ADMIN SESSIONS WERE SIGNED OUT."


@action("sign-out-device", role="admin")
async def do_sign_out_device(client: Client, msg: dict):
    device_id = clean_int(msg.get("id"), "DEVICE", 1, 10 ** 12)
    if device_id == client.id:
        raise UserError("THAT IS THIS DEVICE. USE LOG OUT INSTEAD.")
    target = next((c for c in hub.clients if c.id == device_id and c.alive), None)
    if target is None:
        raise UserError("THAT DEVICE IS NO LONGER CONNECTED.")
    if not target.role:
        raise UserError("THAT DEVICE IS NOT SIGNED IN.")
    token_hash, role, username = target.token_hash, target.role, target.username
    async with store.transaction() as db:
        label = "Admin"
        if role == "team":
            row = await fetch_one(db, "SELECT teamName FROM teams WHERE username = ?", (username,))
            label = row["teamName"] if row else username
        await db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
        await record(db, "admin", f"Signed out {label} on {target.device}")
    # Also catches a duplicated browser tab that shares the same login.
    hub.sign_out_where(lambda c: c.token_hash == token_hash, "YOU WERE SIGNED OUT BY THE ADMIN.")
    return f"SIGNED OUT {label.upper()} ON {target.device.upper()}."


@action("adjust-cash", role="admin")
async def do_adjust_cash(client: Client, msg: dict):
    username = clean_existing_username(msg.get("username"))
    amount = clean_amount(msg.get("amount"), "AMOUNT", -MAX_CASH_ADJUST, MAX_CASH_ADJUST)
    if amount == 0:
        raise UserError("ENTER A NON-ZERO AMOUNT.")
    async with store.transaction() as db:
        team = await require_team(db, username)
        cash = money(team["cash"] or 0)
        new_cash = money(cash + amount)
        if new_cash < 0:
            raise UserError(f"{team['teamName'].upper()} ONLY HAS {usd(cash)} IN CASH.")
        await db.execute("UPDATE teams SET cash = ? WHERE username = ?", (new_cash, username))
        verb = f"Added {usd(amount)} to" if amount > 0 else f"Deducted {usd(-amount)} from"
        await record(db, "trade-up" if amount > 0 else "trade-down", f"{verb} {team['teamName']}")
    hub.request_broadcast()
    return f"{verb.upper()} {team['teamName'].upper()}."


def request_id(msg: dict):
    req_id = msg.get("reqId")
    if isinstance(req_id, bool) or not isinstance(req_id, (int, str)) or len(str(req_id)) > 40:
        return None
    return req_id


async def handle_message(client: Client, msg: dict):
    act = msg.get("action")
    req_id = request_id(msg)

    def reply(ok: bool, message=None, code=None):
        client.send({"type": "result", "reqId": req_id, "ok": ok, "message": message, "code": code})

    if act == "ping":
        client.send({"type": "pong"})
        return
    if not isinstance(act, str) or act not in ACTIONS:
        reply(False, "UNKNOWN ACTION.")
        return
    handler, needed_role = ACTIONS[act]
    if client.role and time.time() > client.session_expires:
        client.sign_out()
        client.send({"type": "signed_out", "reason": "SESSION EXPIRED. PLEASE SIGN IN AGAIN."})
    if needed_role and client.role != needed_role:
        reply(False, "NOT AUTHORIZED.")
        return
    try:
        reply(True, await handler(client, msg))
    except UserError as exc:
        reply(False, str(exc), exc.code)
    except Exception:
        log.exception("Error while handling %r", act)
        reply(False, "SERVER ERROR. PLEASE TRY AGAIN.")


# --- APP ---
def quiet_disconnects(loop, context):
    # On Windows, asyncio prints a scary traceback whenever a device drops off
    # the network without saying goodbye. That is normal here, so skip it.
    if isinstance(context.get("exception"), (ConnectionResetError, ConnectionAbortedError)):
        return
    loop.default_exception_handler(context)


@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.get_running_loop().set_exception_handler(quiet_disconnects)
    await store.open()
    for note in await init_db():
        log.warning(note)
    log.info("Database: %s", store.path)
    broadcaster = asyncio.create_task(hub.run())
    try:
        yield
    finally:
        broadcaster.cancel()
        with suppress(asyncio.CancelledError):
            await broadcaster
        await store.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

PAGE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        # static.cloudflareinsights.com: Cloudflare Web Analytics, which Cloudflare adds to the page itself.
        "default-src 'self'; script-src 'self' 'unsafe-inline' https://static.cloudflareinsights.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
        "img-src 'self' data: https://www.google.com https://*.gstatic.com; "
        "connect-src 'self' ws: wss: https://cloudflareinsights.com; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'none'; object-src 'none'"
    ),
}


@app.get("/", include_in_schema=False)
async def index():
    if not HTML_FILE.is_file():
        return JSONResponse({"error": "frontend file missing"}, status_code=404)
    return FileResponse(HTML_FILE, media_type="text/html", headers=PAGE_HEADERS)


@app.get("/healthz", include_in_schema=False)
async def healthz():
    return {"ok": True}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    if len(hub.clients) >= MAX_CLIENTS:
        await ws.close(code=1013)
        return
    await ws.accept()
    client = Client(ws, client_address(ws))
    hub.clients.add(client)
    hub.request_admin_broadcast()
    writer = asyncio.create_task(client.run_writer())
    client.send({"type": "hello", "id": client.id})
    try:
        while client.alive:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break
            text = message.get("text")
            if text is None or len(text) > MAX_MESSAGE_CHARS:
                client.send({"type": "error", "message": "MESSAGE REJECTED."})
                continue
            try:
                payload = json.loads(text)
            except (ValueError, RecursionError):
                client.send({"type": "error", "message": "MESSAGE REJECTED."})
                continue
            if not isinstance(payload, dict):
                client.send({"type": "error", "message": "MESSAGE REJECTED."})
                continue
            if not client.allow_message():
                if client.throttled > 300:
                    log.warning("Dropping connection %s from %s: flooding", client.id, client.address)
                    break
                now = time.monotonic()
                if now - client.last_throttle_notice > 1 or request_id(payload) is not None:
                    client.last_throttle_notice = now
                    client.send({"type": "result", "reqId": request_id(payload), "ok": False,
                                 "message": "TOO MANY REQUESTS. SLOW DOWN."})
                continue
            await handle_message(client, payload)
    except Exception:
        pass  # abrupt disconnects surface as assorted network errors
    finally:
        client.reader_done = True
        client.close()
        hub.clients.discard(client)
        hub.request_admin_broadcast()
        writer.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await writer


async def reset_admin_password():
    first = getpass.getpass("New admin password: ")
    if first != getpass.getpass("Repeat new admin password: "):
        print("Passwords do not match. Nothing changed.")
        return
    try:
        clean_password(first, "PASSWORD", ADMIN_PASSWORD_MIN)
    except UserError as exc:
        print(str(exc).capitalize())
        return
    await store.open()
    try:
        await init_db()
        new_hash = await hash_password_async(first)
        async with store.transaction() as db:
            await put_setting(db, "adminPasswordHash", new_hash)
            await db.execute("DELETE FROM sessions WHERE role = 'admin'")
            await record(db, "admin", "Admin password reset from the server console")
    finally:
        await store.close()
    print("Admin password updated. Restart the server if it is running.")


def keep_console_from_freezing():
    """Clicking inside a Windows console window starts a text selection, and
    Windows then pauses any program that prints to it - freezing the whole game.
    Turn that click-to-select mode off for this window."""
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-10)  # console input
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            ENABLE_QUICK_EDIT_MODE, ENABLE_EXTENDED_FLAGS = 0x40, 0x80
            kernel32.SetConsoleMode(handle, (mode.value | ENABLE_EXTENDED_FLAGS) & ~ENABLE_QUICK_EDIT_MODE)
    except Exception:
        pass


def local_network_addresses() -> list[str]:
    """IPv4 addresses that other devices on the same Wi-Fi / network can use to reach this PC."""
    found = []
    with suppress(OSError):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 80))  # picks the main network adapter; sends nothing
            found.append(probe.getsockname()[0])
    with suppress(OSError):
        found += [info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
    usable = []
    for address in found:
        ip = ipaddress.ip_address(address)
        reachable = ip.is_private or ip in ipaddress.ip_network("100.64.0.0/10")  # LAN or Tailscale
        if reachable and not ip.is_loopback and not ip.is_link_local and address not in usable:
            usable.append(address)
    return usable


def print_addresses(host: str, port: int):
    log.info("On this PC:            http://localhost:%s", port)
    if host in ("127.0.0.1", "localhost", "::1"):
        log.info("(Started with --host %s, so other devices cannot connect directly.)", host)
        return
    addresses = local_network_addresses()
    for address in addresses:
        log.info("Same Wi-Fi / network:  http://%s:%s", address, port)
    if addresses:
        log.info("(If other devices cannot open that, allow port %s in Windows Firewall.)", port)
    else:
        log.info("No network connection found, so only this PC can connect directly.")
    log.info("Anywhere else:         your Cloudflare tunnel address, e.g. https://taurusmatrix.bid")


def main():
    parser = argparse.ArgumentParser(description="MSX stock exchange server")
    parser.add_argument("--host", default=os.environ.get("ISL_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("ISL_PORT", "8000")))
    parser.add_argument("--db", help="database file to use, e.g. a separate one for testing (default: isl_exchange.db)")
    parser.add_argument("--reset-admin-password", action="store_true", help="set a new admin password and exit")
    args = parser.parse_args()
    if args.db:
        store.path = Path(args.db).resolve()

    for stream in (sys.stdout, sys.stderr):
        with suppress(Exception):
            stream.reconfigure(errors="backslashreplace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")

    if args.reset_admin_password:
        asyncio.run(reset_admin_password())
        return
    keep_console_from_freezing()
    print_addresses(args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, ws="websockets-sansio",
                ws_max_size=4 * MAX_MESSAGE_CHARS, ws_ping_interval=20.0, ws_ping_timeout=20.0,
                server_header=False)


if __name__ == "__main__":
    main()
