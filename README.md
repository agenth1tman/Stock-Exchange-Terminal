
# Stock Exchange Terminal (Simulator) 📈

[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![License: GPL v3](https://img.shields.io/badge/License-GPL_v3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Release](https://img.shields.io/badge/Release-v1.2.0--stable-orange.svg)](https://github.com/agenth1tman/Stock-Exchange-Terminal/releases)

A real-time stock exchange simulator built to power an **interschool fintech competition**. Teams trade shares from their phones or laptops while an admin runs the market (opening and closing trading, moving prices and advancing rounds) and every screen updates live.

> ### 🌐 Nothing to download for clients
> The server delivers the trading terminal itself. Clients **just open the server's address in any browser** (a phone, tablet or laptop) and sign in.

---

## ✨ Features

**For teams**
*   **Live trading:** buy and sell shares at the current price; cash, holdings, portfolio value and gain/loss update instantly.
*   **Market watch & leaderboard:** live prices with % change, plus a ranking of every team by total equity.
*   **Works on phones:** a layout made for small screens, so every team can trade from a phone.

**For the admin**
*   **Market control:** open/close the market, advance rounds, change prices, reset the game.
*   **Companies & teams:** list or remove companies, create team accounts, reset team passwords, add or deduct cash.
*   **Live activity log:** every trade and admin action as it happens.
*   **Devices tab:** every connected device, what it is (e.g. "iPhone · Safari"), which account it's signed in to (or "On Login Page"), which teams have no device online, and a button to sign any device out.


## 🛠️ Tech Stack

*   **Backend:** Python, FastAPI, Uvicorn (ASGI web server), WebSockets
*   **Database:** SQLite via aiosqlite
*   **Frontend:** a single self-contained HTML5 / CSS3 / JavaScript page, served by the backend

---

## 🚀 Getting Started

### Prerequisites
**Python 3.10 or higher.**

### 1. Clone the Repository
```bash
git clone https://github.com/agenth1tman/Stock-Exchange-Terminal.git
cd Stock-Exchange-Terminal
```

### 2. Set Up a Virtual Environment (optional)
```bash
# Create environment
python -m venv venv

# Activate environment (Windows)
.\venv\Scripts\activate

# Activate environment (macOS/Linux)
source venv/bin/activate
```

### 3. Install Dependencies
```bash
pip install -r requirements.txt
```

### 4. Start the Server
Run `run.cmd` (Windows) or:
```bash
python server.py
```
The console prints the addresses to use, for example:
```
On this PC:            http://localhost:8000
Same Wi-Fi / network:  http://192.168.1.18:8000
```

### 5. First Sign-In
On the very first start the server creates a random **admin password and prints it in the console** — write it down. Sign in with username `admin` and that password, then create team accounts in **Admin Console → Teams**.

Forgot the admin password? Stop the server and run:
```bash
python server.py --reset-admin-password
```

---

## 📱 Connecting Clients

Clients only need a browser — nothing to install or download.

### On the same Wi-Fi / network
Open the **"Same Wi-Fi / network"** address the server prints (e.g. `http://192.168.1.18:8000`).

On Windows, allow the port through the firewall once (run in **cmd as Administrator**):
```bash
netsh advfirewall firewall add rule name="MSX Stock Exchange" dir=in action=allow protocol=TCP localport=8000 profile=private
```
> Some school and guest Wi-Fi networks block devices from reaching each other. If so, use a Cloudflare tunnel instead.

### From anywhere (Cloudflare Tunnel)
Install [`cloudflared`](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/), keep the server running, and in a second window run:
```bash
cloudflared tunnel --url http://localhost:8000
```
It prints a link like `https://random-words.trycloudflare.com` — send that to everyone; opening it connects automatically. For a permanent address on your own domain, set up a named Cloudflare Tunnel pointing to `http://localhost:8000`.

---

## ⚙️ Server Options

| Option | What it does |
|---|---|
| `--port 8000` | Port to listen on (default `8000`) |
| `--host 0.0.0.0` | Address to listen on; `127.0.0.1` allows this PC only |
| `--db file.db` | Use a different database file, e.g. a separate one for testing |
| `--reset-admin-password` | Choose a new admin password, then exit |

Set the environment variable `ISL_ADMIN_PASSWORD` before the **first** start to pick the initial admin password yourself instead of a random one.

---

## 🧭 Event-Day Tips

*   **Keep the server window open** and set the PC to **never sleep** — if the server stops, every device disconnects.
*   **Change the admin password** before the event if it's still one you've shared.
*   **Backups:** stop the server, then copy `isl_exchange.db` (and `isl_exchange.db-wal` / `-shm` if present).
*   **"SERVER OFFLINE"** on a device → check the server is running and the PC is online.
*   **"CONNECTION BLOCKED"** on a device → the server is fine, but something in that browser blocks the live connection: turn off its VPN (e.g. Opera's built-in VPN), ad blocker or antivirus "web shield" for this site, or use another browser.

---

## ⚖️ License

Distributed under the **GNU GPL v3**. See `LICENSE` for more details. This tool includes a limitation of liability clause, making it open for educational reuse and adaptation by other school fintech clubs, but it remains open-source in the process.

---

## 📺 Preview
<img width="1878" height="964" alt="image_2026-07-03_033819966" src="https://github.com/user-attachments/assets/7ec9ccf9-11a2-4211-baa2-c8b7a3d77695" />

**Login Page**

<img width="1878" height="964" alt="Opera Snapshot_2026-07-03_032200_isl-stock-exchange" src="https://github.com/user-attachments/assets/c616cd07-2ea9-4042-b789-090f687ef59d" />

**Team Panel - Dashboard**

<img width="1878" height="964" alt="image" src="https://github.com/user-attachments/assets/75a8ebb3-ab3c-4159-8324-23744826c682" />

**Admin Console - Overview**
