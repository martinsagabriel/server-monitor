#!/usr/bin/env python3
"""
Server Monitor - Backend Flask
Monitora CPU, Memória e Containers Docker
"""

from flask import Flask, jsonify, render_template
import psutil
import subprocess
import threading
import time
import json
import datetime
import socket

app = Flask(__name__)

# Prime psutil's internal counters once at startup so every subsequent call
# to cpu_percent()/cpu_times_percent() is non-blocking (interval=None) instead
# of sleeping the request thread for 0.5s on every single API call.
psutil.cpu_percent(percpu=True)
psutil.cpu_percent()
psutil.cpu_times_percent()

def get_cpu_info():
    cpu_percent = psutil.cpu_percent(percpu=True)  # non-blocking: delta since last call
    cpu_freq = psutil.cpu_freq()
    cpu_count = psutil.cpu_count(logical=True)
    cpu_count_physical = psutil.cpu_count(logical=False)
    cpu_times = psutil.cpu_times_percent()

    return {
        "total_percent": round(sum(cpu_percent) / len(cpu_percent), 2) if cpu_percent else 0,
        "per_core": [round(p, 2) for p in cpu_percent],
        "core_count_logical": cpu_count,
        "core_count_physical": cpu_count_physical,
        "frequency_current": round(cpu_freq.current, 2) if cpu_freq else None,
        "frequency_max": round(cpu_freq.max, 2) if cpu_freq else None,
        "user": round(cpu_times.user, 2),
        "system": round(cpu_times.system, 2),
        "idle": round(cpu_times.idle, 2),
    }

def get_memory_info():
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()

    return {
        "total": mem.total,
        "available": mem.available,
        "used": mem.used,
        "free": mem.free,
        "percent": mem.percent,
        "total_gb": round(mem.total / (1024**3), 2),
        "available_gb": round(mem.available / (1024**3), 2),
        "used_gb": round(mem.used / (1024**3), 2),
        "free_gb": round(mem.free / (1024**3), 2),
        "swap_total": swap.total,
        "swap_used": swap.used,
        "swap_free": swap.free,
        "swap_percent": swap.percent,
        "swap_total_gb": round(swap.total / (1024**3), 2),
        "swap_used_gb": round(swap.used / (1024**3), 2),
    }

def get_disk_info():
    disks = []
    for partition in psutil.disk_partitions():
        try:
            usage = psutil.disk_usage(partition.mountpoint)
            disks.append({
                "device": partition.device,
                "mountpoint": partition.mountpoint,
                "fstype": partition.fstype,
                "total_gb": round(usage.total / (1024**3), 2),
                "used_gb": round(usage.used / (1024**3), 2),
                "free_gb": round(usage.free / (1024**3), 2),
                "percent": usage.percent,
            })
        except PermissionError:
            continue
    return disks

def get_network_info():
    net_io = psutil.net_io_counters()
    return {
        "bytes_sent_mb": round(net_io.bytes_sent / (1024**2), 2),
        "bytes_recv_mb": round(net_io.bytes_recv / (1024**2), 2),
        "packets_sent": net_io.packets_sent,
        "packets_recv": net_io.packets_recv,
        "errin": net_io.errin,
        "errout": net_io.errout,
    }

# Cache dos dados do Docker: "docker stats" é a chamada mais pesada do app
# (spawna processo + aguarda amostragem). Reaproveitar o resultado por alguns
# segundos evita repetir esse custo a cada request/aba aberta no dashboard.
_docker_cache = {"data": None, "ts": 0.0}
_docker_lock = threading.Lock()
DOCKER_CACHE_TTL = 4  # segundos

def get_docker_containers():
    now = time.monotonic()
    with _docker_lock:
        if _docker_cache["data"] is not None and (now - _docker_cache["ts"]) < DOCKER_CACHE_TTL:
            return _docker_cache["data"]

    data = _fetch_docker_containers()

    with _docker_lock:
        _docker_cache["data"] = data
        _docker_cache["ts"] = time.monotonic()
    return data

def _fetch_docker_containers():
    containers = []
    try:
        result = subprocess.run(
            [
                "docker", "ps", "-a",
                "--format",
                '{"id":"{{.ID}}","name":"{{.Names}}","image":"{{.Image}}","status":"{{.Status}}","state":"{{.State}}","ports":"{{.Ports}}","size":"{{.Size}}","created":"{{.CreatedAt}}"}'
            ],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split('\n'):
                if line.strip():
                    try:
                        containers.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass

        # Get CPU/Memory stats for running containers
        stats_result = subprocess.run(
            ["docker", "stats", "--no-stream", "--format",
             '{{.Container}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.NetIO}}\t{{.BlockIO}}'],
            capture_output=True,
            text=True,
            timeout=15
        )
        stats_map = {}
        if stats_result.returncode == 0:
            for line in stats_result.stdout.strip().split('\n'):
                if line.strip():
                    parts = line.split('\t')
                    if len(parts) >= 6:
                        stats_map[parts[0][:12]] = {
                            "cpu_percent": parts[1],
                            "mem_usage": parts[2],
                            "mem_percent": parts[3],
                            "net_io": parts[4],
                            "block_io": parts[5],
                        }

        for c in containers:
            cid = c["id"][:12]
            if cid in stats_map:
                c.update(stats_map[cid])
            else:
                c.setdefault("cpu_percent", "0%")
                c.setdefault("mem_usage", "0B / 0B")
                c.setdefault("mem_percent", "0%")
                c.setdefault("net_io", "0B / 0B")
                c.setdefault("block_io", "0B / 0B")

    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        containers = [{"error": str(e), "docker_available": False}]

    return containers

# Mantemos os objetos psutil.Process entre chamadas: assim cpu_percent()
# reflete o uso real desde a última leitura (não desde a criação do processo)
# sem precisar bloquear a requisição com um intervalo de amostragem.
_proc_cache = {}
_proc_lock = threading.Lock()

def get_top_processes():
    with _proc_lock:
        current_pids = set(psutil.pids())

        # Remove processos que já terminaram
        for pid in list(_proc_cache):
            if pid not in current_pids:
                del _proc_cache[pid]

        # Registra e "prepara" (prime) processos novos
        for pid in current_pids - _proc_cache.keys():
            try:
                proc = psutil.Process(pid)
                proc.cpu_percent(None)
                _proc_cache[pid] = proc
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        procs = []
        for pid, proc in list(_proc_cache.items()):
            try:
                with proc.oneshot():
                    procs.append({
                        "pid": pid,
                        "name": proc.name(),
                        "cpu_percent": proc.cpu_percent(None),
                        "memory_percent": round(proc.memory_percent(), 2),
                        "status": proc.status(),
                        "username": proc.username(),
                    })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                _proc_cache.pop(pid, None)

    procs.sort(key=lambda x: x.get('cpu_percent', 0) or 0, reverse=True)
    return procs[:15]

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/metrics')
def metrics():
    data = {
        "timestamp": datetime.datetime.now().isoformat(),
        "hostname": socket.gethostname(),
        "cpu": get_cpu_info(),
        "memory": get_memory_info(),
        "disk": get_disk_info(),
        "network": get_network_info(),
        "uptime": get_uptime(),
    }
    return jsonify(data)

@app.route('/api/containers')
def containers():
    return jsonify({
        "timestamp": datetime.datetime.now().isoformat(),
        "containers": get_docker_containers()
    })

@app.route('/api/processes')
def processes():
    return jsonify({
        "timestamp": datetime.datetime.now().isoformat(),
        "processes": get_top_processes()
    })

@app.route('/api/all')
def all_metrics():
    return jsonify({
        "timestamp": datetime.datetime.now().isoformat(),
        "hostname": socket.gethostname(),
        "cpu": get_cpu_info(),
        "memory": get_memory_info(),
        "disk": get_disk_info(),
        "network": get_network_info(),
        "uptime": get_uptime(),
        "containers": get_docker_containers(),
    })

def get_uptime():
    try:
        boot_time = psutil.boot_time()
        uptime_seconds = datetime.datetime.now().timestamp() - boot_time
        hours = int(uptime_seconds // 3600)
        minutes = int((uptime_seconds % 3600) // 60)
        seconds = int(uptime_seconds % 60)
        return {
            "seconds": int(uptime_seconds),
            "formatted": f"{hours}h {minutes}m {seconds}s",
            "boot_time": datetime.datetime.fromtimestamp(boot_time).isoformat()
        }
    except Exception:
        return {"seconds": 0, "formatted": "N/A", "boot_time": "N/A"}

if __name__ == '__main__':
    # threaded=True permite que /api/all e /api/processes sejam atendidas
    # concorrentemente sem bloquear a outra requisição.
    app.run(host='0.0.0.0', port=5080, debug=False, threaded=True)
