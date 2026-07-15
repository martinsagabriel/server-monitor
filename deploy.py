#!/usr/bin/env python3
"""Deploy script - Instala o Server Monitor no servidor remoto via SSH/SFTP"""

import paramiko
import os
import sys
import time
import getpass

def load_env_file(path=None):
    """Carrega variáveis de um arquivo .env (formato KEY=VALUE) para o
    ambiente, sem sobrescrever variáveis já exportadas no shell. Não requer
    a dependência python-dotenv."""
    env_path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(env_path):
        return

    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)

load_env_file()

HOST = os.environ.get("MONITOR_DEPLOY_HOST") or input("Host: ").strip()
PORT = int(os.environ.get("MONITOR_DEPLOY_PORT") or input("Porta [22]: ").strip() or "22")
USER = os.environ.get("MONITOR_DEPLOY_USER") or input(f"Usuario SSH para {HOST}: ").strip()
PASS = os.environ.get("MONITOR_DEPLOY_PASS") or getpass.getpass(f"Senha SSH para {USER}@{HOST}: ")

# Instala dentro da home do usuário (evita precisar de sudo/root em /opt).
REMOTE_DIR = os.environ.get("MONITOR_DEPLOY_REMOTE_DIR") or f"/home/{USER}/server-monitor"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Caminhos locais (neste computador) -> caminho relativo no servidor remoto.
FILES = {
    "app.py":                 os.path.join(BASE_DIR, "app.py"),
    "requirements.txt":       os.path.join(BASE_DIR, "requirements.txt"),
    "templates/index.html":   os.path.join(BASE_DIR, "templates", "index.html"),
}

def banner(msg):
    print(f"\n\033[1;34m{'='*60}\n  {msg}\n{'='*60}\033[0m")

def ok(msg):   print(f"\033[1;32m  ✔ {msg}\033[0m")
def info(msg): print(f"\033[0;36m  ► {msg}\033[0m")
def err(msg):  print(f"\033[1;31m  ✘ {msg}\033[0m")

def render_service_file():
    """Le o monitor.service local e substitui os placeholders @@USER@@ e
    @@REMOTE_DIR@@ pelos valores reais deste deploy."""
    local_path = os.path.join(BASE_DIR, "monitor.service")
    with open(local_path, "r", encoding="utf-8") as f:
        content = f.read()
    return content.replace("@@USER@@", USER).replace("@@REMOTE_DIR@@", REMOTE_DIR)

def run(ssh, cmd, sudo=False, timeout=120):
    display_cmd = cmd
    if sudo:
        cmd = f"echo '{PASS}' | sudo -S sh -c '{cmd}'"
        display_cmd = f"echo '****' | sudo -S sh -c '{display_cmd}'"
    info(f"$ {display_cmd[:100]}{'...' if len(display_cmd)>100 else ''}")
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout, get_pty=True)
    out = stdout.read().decode("utf-8", errors="replace").strip()
    er  = stderr.read().decode("utf-8", errors="replace").strip()
    rc  = stdout.channel.recv_exit_status()
    if out: print(f"    \033[0;37m{out[:500]}\033[0m")
    if er and rc != 0: print(f"    \033[0;33m{er[:300]}\033[0m")
    return rc, out

def main():
    banner("Server Monitor - Deploy")

    # Conectar SSH
    info(f"Conectando a {HOST}:{PORT} como {USER}...")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(HOST, port=PORT, username=USER, password=PASS, timeout=15,
                    allow_agent=False, look_for_keys=False)
    except Exception as e:
        err(f"Falha na conexão SSH: {e}")
        sys.exit(1)
    ok("Conexão SSH estabelecida")

    # ── Criar estrutura de diretórios ────────────────────────────────────────────────────────────────────────────────
    banner("Criando diretórios")
    rc, _ = run(ssh, f"mkdir -p {REMOTE_DIR}/templates")
    ok(f"Diretórios criados em {REMOTE_DIR}")

    # ── Upload dos arquivos via SFTP ────────────────────────────────────────────────────────
    banner("Enviando arquivos")
    sftp = ssh.open_sftp()
    for remote_rel, local_path in FILES.items():
        remote_path = f"{REMOTE_DIR}/{remote_rel}"
        try:
            sftp.put(local_path, remote_path)
            ok(f"Enviado: {remote_rel}")
        except Exception as e:
            err(f"Erro ao enviar {remote_rel}: {e}")

    try:
        service_content = render_service_file()
        with sftp.open(f"{REMOTE_DIR}/monitor.service", "w") as f:
            f.write(service_content)
        ok("Enviado: monitor.service (personalizado com User/WorkingDirectory)")
    except Exception as e:
        err(f"Erro ao enviar monitor.service: {e}")

    sftp.close()

    # ── Verificar/instalar Python3 e pip ──────────────────────────────────────
    banner("Verificando Python3 e pip3")
    rc, _ = run(ssh, "python3 --version")
    if rc != 0:
        info("Instalando python3...")
        run(ssh, "apt-get update -qq && apt-get install -y python3 python3-pip python3-venv", sudo=True, timeout=180)
    else:
        ok("Python3 disponível")

    # Garante o pacote python3-venv (fornece o ensurepip usado por "python3 -m
    # venv"). Em distros como Ubuntu 24.04, python3 pode já estar presente sem
    # esse pacote, o que faz "python3 -m venv" criar um virtualenv sem pip.
    run(ssh, "apt-get update -qq && apt-get install -y python3-venv python3-pip", sudo=True, timeout=180)

    rc, _ = run(ssh, "python3 -m pip --version")
    if rc == 0:
        ok("pip3 disponível")
    else:
        info("pip3 do sistema ainda indisponível (seguindo com o pip do virtualenv)")

    # ── Criar virtualenv e instalar dependências ───────────────────────────────
    banner("Criando virtualenv e instalando dependências")
    rc, _ = run(ssh, f"rm -rf {REMOTE_DIR}/venv && python3 -m venv {REMOTE_DIR}/venv", timeout=60)
    if rc == 0:
        ok("Virtualenv criado")
    else:
        err("Falha ao criar virtualenv")

    rc, _ = run(ssh, f"{REMOTE_DIR}/venv/bin/pip install --upgrade pip -q", timeout=120)
    rc, out = run(ssh, f"{REMOTE_DIR}/venv/bin/pip install -r {REMOTE_DIR}/requirements.txt -q", timeout=180)
    if rc == 0:
        ok("Flask e psutil instalados")
    else:
        err("Erro na instalação de dependências")

    # ── Verificar se docker está disponível ───────────────────────────────────
    banner("Verificando Docker")
    rc, _ = run(ssh, "docker --version")
    if rc == 0:
        ok("Docker disponível")
        run(ssh, f"usermod -aG docker {USER}", sudo=True)
        ok(f"Usuário {USER} adicionado ao grupo docker")
    else:
        info("Docker não encontrado - a aba de containers ficará inativa")

    # ── Instalar serviço systemd ───────────────────────────────────────────────
    banner("Configurando serviço systemd")
    run(ssh, f"cp {REMOTE_DIR}/monitor.service /etc/systemd/system/monitor.service", sudo=True)
    run(ssh, "systemctl daemon-reload", sudo=True)
    run(ssh, "systemctl enable monitor.service", sudo=True)
    run(ssh, "systemctl restart monitor.service", sudo=True)
    ok("Serviço monitor.service ativo e habilitado")

    # ── Aguardar e verificar ───────────────────────────────────────────────────
    info("Aguardando inicialização (3s)...")
    time.sleep(3)

    banner("Verificando status do serviço")
    rc, out = run(ssh, "systemctl is-active monitor.service")
    if "active" in out:
        ok("Serviço está rodando!")
    else:
        err("Serviço pode não ter iniciado")
        run(ssh, "journalctl -u monitor.service -n 20 --no-pager")

    # ── Testar endpoint ────────────────────────────────────────────────────────
    banner("Testando endpoint HTTP")
    rc, out = run(ssh, "curl -s -o /dev/null -w '%{http_code}' http://localhost:5080/ --max-time 5")
    if "200" in out:
        ok("Aplicação respondendo na porta 5080!")
    else:
        info(f"Resposta HTTP: {out}")

    # ── Configurar firewall (ufw) se ativo ────────────────────────────────────
    banner("Configurando firewall")
    rc, out = run(ssh, "ufw status", sudo=True)
    if "Status: active" in out:
        run(ssh, "ufw allow 5080/tcp comment 'Server Monitor'", sudo=True)
        ok("Porta 5080 liberada no UFW")
    else:
        info("UFW não está ativo - nenhuma regra necessária")

    ssh.close()

    # ── Resultado final ────────────────────────────────────────────────────────
    banner("DEPLOY CONCLUÍDO!")
    print(f"""
  \033[1;32m✔ Aplicação instalada com sucesso!\033[0m

  \033[1;33m🌐 Acesse o dashboard em:\033[0m
     \033[1;37mhttp://{HOST}:5080\033[0m

  \033[0;36mComandos úteis no servidor:\033[0m
     systemctl status monitor    → ver status
     systemctl restart monitor   → reiniciar
     journalctl -u monitor -f    → ver logs ao vivo

  \033[0;36mMonitoramento:\033[0m
     ✔ CPU (total + por núcleo)
     ✔ Memória RAM e Swap
     ✔ Discos
     ✔ Containers Docker
     ✔ Top processos
     ✔ Rede
     ✔ Uptime
""")

if __name__ == "__main__":
    main()
