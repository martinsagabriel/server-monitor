# Server Monitor

A lightweight, self-hosted dashboard for real-time monitoring of a Linux server's **CPU**, **memory**, **disk**, **network**, **Docker containers**, and **top processes** — built with Flask and [psutil](https://github.com/giampaolo/psutil), with zero external dependencies on the frontend (vanilla HTML/CSS/JS).

Designed to run directly on the host (outside Docker) as a `systemd` service, with minimal CPU and memory footprint.

![Python](https://img.shields.io/badge/python-3.8%2B-blue)
![Flask](https://img.shields.io/badge/flask-backend-black)
![License](https://img.shields.io/badge/license-MIT-green)

## Features

- **CPU** — total and per-core usage, logical/physical core count, frequency, user/system/idle times
- **Memory** — RAM and swap usage (used/free/available, percentages)
- **Disk** — usage per mounted partition
- **Network** — bytes/packets sent and received, errors
- **Docker** — container list (status, ports, size) plus live CPU/memory/network/block I/O stats
- **Processes** — top 15 processes by CPU usage
- **Uptime** — system uptime and boot time
- Single-page dashboard, auto-refreshing via polling (pauses automatically when the browser tab is in the background)

## Project structure

```
app.py               # Flask backend and REST API
templates/index.html # Dashboard frontend (HTML/CSS/JS, no build step)
requirements.txt      # Python dependencies
monitor.service       # systemd unit file
deploy.py             # Remote deploy script (SSH/SFTP via paramiko)
```

## API endpoints

| Endpoint          | Description                                      |
|--------------------|---------------------------------------------------|
| `GET /`            | Dashboard UI                                       |
| `GET /api/metrics` | CPU, memory, disk, network, uptime                 |
| `GET /api/containers` | Docker container list + stats                   |
| `GET /api/processes`  | Top 15 processes by CPU usage                   |
| `GET /api/all`     | All of the above combined (used by the dashboard)  |

## Requirements

- Python 3.8+
- Linux host (uses `psutil` and, optionally, the `docker` CLI)
- Docker CLI available in `PATH` if you want the containers tab populated (optional — the app runs fine without it)

## Local setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

The dashboard will be available at `http://<server-ip>:5080`.

## Running as a systemd service

1. Copy the project to `/opt/monitor` on the target server.
2. Create a virtual environment and install dependencies inside `/opt/monitor/venv`.
3. Install the unit file:

   ```bash
   sudo cp monitor.service /etc/systemd/system/monitor.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now monitor.service
   ```

4. Check status/logs:

   ```bash
   systemctl status monitor
   journalctl -u monitor -f
   ```

The provided unit file also caps resource usage (`MemoryMax`, `CPUQuota`, `Nice`) so the monitor never competes with the services it watches.

## Remote deploy script

`deploy.py` automates the whole setup above over SSH/SFTP: it uploads the files, creates the virtualenv, installs dependencies, installs the systemd service, and opens the firewall port if `ufw` is active.

It runs **locally** (on your machine, not on the server) and requires `paramiko`:

```bash
pip install -r requirements-deploy.txt
```

Configure the target via environment variables (no credentials are hardcoded):

```bash
export MONITOR_DEPLOY_HOST=192.168.15.101
export MONITOR_DEPLOY_USER=gabe
export MONITOR_DEPLOY_PORT=22
export MONITOR_DEPLOY_PASS=your_password   # optional — omit to be prompted securely
python deploy.py
```

On Windows (PowerShell):

```powershell
$env:MONITOR_DEPLOY_HOST = "192.168.15.101"
$env:MONITOR_DEPLOY_USER = "gabe"
$env:MONITOR_DEPLOY_PASS = "your_password"
python deploy.py
```

## Security notes

- Never commit SSH credentials to the repository; use environment variables as shown above.
- The dashboard has no authentication — it is intended for a trusted local network. If exposing it beyond that, put it behind a reverse proxy with authentication (e.g. Nginx + basic auth) or a VPN.

## License

MIT
