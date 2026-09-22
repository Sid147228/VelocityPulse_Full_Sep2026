import threading, time, psutil, paramiko
from flask import Blueprint, request, jsonify
from flask_socketio import SocketIO

socketio = SocketIO()
monitor_bp = Blueprint("monitor", __name__)

monitoring_active = False
monitoring_threads = []

def collect_linux_metrics(host, user, password):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(host, username=user, password=password)
    while monitoring_active:
        stdin, stdout, stderr = ssh.exec_command("vmstat 1 2 | tail -1")
        fields = stdout.read().decode().split()
        cpu = 100 - int(fields[14])   # idle column
        mem = fields[3]               # free memory (KB)
        socketio.emit("server_metrics", {"server": host, "cpu": cpu, "mem": mem})
        time.sleep(5)

def collect_windows_metrics(host="localhost"):
    while monitoring_active:
        cpu = psutil.cpu_percent(interval=1)
        mem = psutil.virtual_memory().percent
        socketio.emit("server_metrics", {"server": host, "cpu": cpu, "mem": mem})
        time.sleep(5)

@monitor_bp.route("/start_monitoring", methods=["POST"])
def start_monitoring():
    global monitoring_active
    monitoring_active = True
    servers = request.json.get("servers", [])
    for srv in servers:
        if srv["os"] == "linux":
            t = threading.Thread(target=collect_linux_metrics,
                                 args=(srv["host"], srv["user"], srv["password"]))
        else:
            t = threading.Thread(target=collect_windows_metrics, args=(srv["host"],))
        t.start()
        monitoring_threads.append(t)
    return jsonify({"status": "monitoring started"})

@monitor_bp.route("/stop_monitoring", methods=["POST"])
def stop_monitoring():
    global monitoring_active
    monitoring_active = False
    for t in monitoring_threads:
        t.join(timeout=1)
    monitoring_threads.clear()
    return jsonify({"status": "monitoring stopped"})