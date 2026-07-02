#!/usr/bin/env python3
"""
Server Monitor - Backend Flask
Monitora CPU, Memória e Containers Docker
"""

from flask import Flask, jsonify, render_template, Response
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

# Guarda a última amostra para calcular taxa (KB/s) via delta entre chamadas,
# em vez de expor só os contadores acumulados desde o boot.
_net_prev = {"ts": None, "counters": None}
_net_lock = threading.Lock()

def get_network_info():
    now = time.monotonic()
    net_io = psutil.net_io_counters()

    with _net_lock:
        prev, prev_ts = _net_prev["counters"], _net_prev["ts"]
        _net_prev["counters"], _net_prev["ts"] = net_io, now

    sent_rate_kbps = recv_rate_kbps = 0.0
    if prev is not None:
        elapsed = max(now - prev_ts, 1e-6)
        sent_rate_kbps = round(((net_io.bytes_sent - prev.bytes_sent) / elapsed) / 1024, 2)
        recv_rate_kbps = round(((net_io.bytes_recv - prev.bytes_recv) / elapsed) / 1024, 2)

    return {
        "bytes_sent_mb": round(net_io.bytes_sent / (1024**2), 2),
        "bytes_recv_mb": round(net_io.bytes_recv / (1024**2), 2),
        "packets_sent": net_io.packets_sent,
        "packets_recv": net_io.packets_recv,
        "errin": net_io.errin,
        "errout": net_io.errout,
        "sent_rate_kbps": max(sent_rate_kbps, 0.0),
        "recv_rate_kbps": max(recv_rate_kbps, 0.0),
    }

# Mesma ideia para I/O de disco: taxa de leitura/escrita em MB/s.
_diskio_prev = {"ts": None, "counters": None}
_diskio_lock = threading.Lock()

def get_disk_io_info():
    now = time.monotonic()
    io = psutil.disk_io_counters()
    if io is None:
        return {"read_mb": 0, "write_mb": 0, "read_rate_mbps": 0.0, "write_rate_mbps": 0.0,
                "read_count": 0, "write_count": 0}

    with _diskio_lock:
        prev, prev_ts = _diskio_prev["counters"], _diskio_prev["ts"]
        _diskio_prev["counters"], _diskio_prev["ts"] = io, now

    read_rate_mbps = write_rate_mbps = 0.0
    if prev is not None:
        elapsed = max(now - prev_ts, 1e-6)
        read_rate_mbps = round(((io.read_bytes - prev.read_bytes) / elapsed) / (1024**2), 2)
        write_rate_mbps = round(((io.write_bytes - prev.write_bytes) / elapsed) / (1024**2), 2)

    return {
        "read_mb": round(io.read_bytes / (1024**2), 2),
        "write_mb": round(io.write_bytes / (1024**2), 2),
        "read_rate_mbps": max(read_rate_mbps, 0.0),
        "write_rate_mbps": max(write_rate_mbps, 0.0),
        "read_count": io.read_count,
        "write_count": io.write_count,
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

# Valores neutros usados quando um coletor de métrica individual falha, para
# que uma falha isolada (ex.: permissão negada em algum /proc específico) não
# derrube o endpoint inteiro com um 500 e deixe o dashboard travado.
_CPU_DEFAULT = {"total_percent": 0, "per_core": [], "core_count_logical": 0,
                "core_count_physical": 0, "frequency_current": None, "frequency_max": None,
                "user": 0, "system": 0, "idle": 0}
_MEM_DEFAULT = {"total": 0, "available": 0, "used": 0, "free": 0, "percent": 0,
                "total_gb": 0, "available_gb": 0, "used_gb": 0, "free_gb": 0,
                "swap_total": 0, "swap_used": 0, "swap_free": 0, "swap_percent": 0,
                "swap_total_gb": 0, "swap_used_gb": 0}
_DISK_IO_DEFAULT = {"read_mb": 0, "write_mb": 0, "read_rate_mbps": 0.0,
                     "write_rate_mbps": 0.0, "read_count": 0, "write_count": 0}
_NET_DEFAULT = {"bytes_sent_mb": 0, "bytes_recv_mb": 0, "packets_sent": 0, "packets_recv": 0,
                "errin": 0, "errout": 0, "sent_rate_kbps": 0.0, "recv_rate_kbps": 0.0}

def safe_metric(fn, default):
    try:
        return fn()
    except Exception as e:
        app.logger.exception("Falha ao coletar metrica %s", fn.__name__)
        if isinstance(default, dict):
            return {**default, "error": str(e)}
        return default

@app.route('/api/metrics')
def metrics():
    data = {
        "timestamp": datetime.datetime.now().isoformat(),
        "hostname": socket.gethostname(),
        "cpu": safe_metric(get_cpu_info, _CPU_DEFAULT),
        "memory": safe_metric(get_memory_info, _MEM_DEFAULT),
        "disk": safe_metric(get_disk_info, []),
        "disk_io": safe_metric(get_disk_io_info, _DISK_IO_DEFAULT),
        "network": safe_metric(get_network_info, _NET_DEFAULT),
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
        "processes": safe_metric(get_top_processes, [])
    })

@app.route('/api/all')
def all_metrics():
    return jsonify({
        "timestamp": datetime.datetime.now().isoformat(),
        "hostname": socket.gethostname(),
        "cpu": safe_metric(get_cpu_info, _CPU_DEFAULT),
        "memory": safe_metric(get_memory_info, _MEM_DEFAULT),
        "disk": safe_metric(get_disk_info, []),
        "disk_io": safe_metric(get_disk_io_info, _DISK_IO_DEFAULT),
        "network": safe_metric(get_network_info, _NET_DEFAULT),
        "uptime": get_uptime(),
        "containers": safe_metric(get_docker_containers, []),
    })

@app.route('/metrics')
def prometheus_metrics():
    """Exposes metrics in Prometheus text exposition format for scraping."""
    cpu = get_cpu_info()
    mem = get_memory_info()
    disks = get_disk_info()
    disk_io = get_disk_io_info()
    net = get_network_info()

    lines = []

    def gauge(name, value, help_text):
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name} {value}")

    gauge("server_monitor_cpu_percent", cpu["total_percent"], "Total CPU usage percent")
    gauge("server_monitor_memory_percent", mem["percent"], "Memory usage percent")
    gauge("server_monitor_swap_percent", mem["swap_percent"], "Swap usage percent")
    gauge("server_monitor_network_sent_kbps", net["sent_rate_kbps"], "Network upload rate in KB/s")
    gauge("server_monitor_network_recv_kbps", net["recv_rate_kbps"], "Network download rate in KB/s")
    gauge("server_monitor_disk_read_mbps", disk_io["read_rate_mbps"], "Disk read rate in MB/s")
    gauge("server_monitor_disk_write_mbps", disk_io["write_rate_mbps"], "Disk write rate in MB/s")

    lines.append("# HELP server_monitor_disk_usage_percent Disk usage percent per mountpoint")
    lines.append("# TYPE server_monitor_disk_usage_percent gauge")
    for d in disks:
        mountpoint = d["mountpoint"].replace('\\', '\\\\').replace('"', '\\"')
        lines.append(f'server_monitor_disk_usage_percent{{mountpoint="{mountpoint}"}} {d["percent"]}')

    body = "\n".join(lines) + "\n"
    return Response(body, mimetype="text/plain; version=0.0.4")

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
