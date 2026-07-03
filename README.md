
# Stock Exchange Terminal (Simulator) 📈

[![Python](https://img.shields.io/badge/Python-3.9+-blue.svg)](https://www.python.org/)
[![License: GPL v3](https://img.shields.io/badge/License-GPL_v3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Release](https://img.shields.io/badge/Release-v0.5.9--alpha-orange.svg)]()

An asynchronous, high-performance stock exchange simulator built to power an **interschool fintech competition**. The engine mimics real-world exchange environments, combining a rapid asynchronous backend with a highly responsive, minimal, and beautifully animated frontend dashboard.

---

## ✨ Features

*   **Asynchronous Processing:** Built on `FastAPI` and `aiosqlite` to handle concurrent order placements, cancellations, and live state evaluations efficiently.
*   **Modern, Minimal UI:** A clean interface designed to maximize screen real estate and prioritize data visibility for high-speed decision making.
*   **Dynamic Animations:** Fluid transition effects for shifting market charts, updated order books, and real-time portfolio value fluctuations.
*   **Persistent SQLite Backend:** Low-friction, non-blocking disk storage to ensure trading logs and account balances remain safe during system restarts.

## 🛠️ Tech Stack

*   **Backend:** Python, FastAPI, Uvicorn (ASGI Web Server)
*   **Database:** aiosqlite (Asynchronous SQLite wrapper)
*   **Frontend:** HTML5, CSS3, Modern JavaScript (ES6+), Animation Engines

---

## 🚀 Getting Started

Follow these steps to spin up the exchange terminal locally on your machine.

### Prerequisites
Make sure you have **Python 3.9 or higher** installed.

### 1. Clone the Repository
```bash
git clone [https://github.com/agenth1tman/Stock-Exchange-Terminal.git](https://github.com/agenth1tman/Stock-Exchange-Terminal.git)
cd Stock-Exchange-Terminal

```

### 2. Set Up a Virtual Environment

```bash
# Create environment
python -m venv venv

# Activate environment (Windows)
.\venv\Scripts\activate

# Activate environment (macOS/Linux)
source venv/bin/activate

```

### 3. Install Dependencies

Ensure you have python installed and the following dependencies:
```bash
aiosqlite
fastapi
uvicorn[standard]
websockets
```

### 4. Run the Engine

Run the "run.cmd" to run the server.

Open your browser and connect to your local ip to access the terminal dashboard.

Admin Username is "admin" and initial password is "7fedfb67".

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
