#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CrossBay 一键启动 / 停止 / 状态检查

用法（由同目录下的 start-globex.bat / stop-globex.bat 调用）：
    python globex_ctl.py start
    python globex_ctl.py stop
    python globex_ctl.py status
"""
import os
import sys
import time
import json
import socket
import subprocess
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
VENV_PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
FRONTEND_DIR = os.path.join(ROOT, "frontend")
LOG_DIR = os.path.join(ROOT, "logs")

BACKEND_PORT = 8000
FRONTEND_PORT = 5173

DETACHED = 0x00000008          # DETACHED_PROCESS
NEW_GROUP = 0x00000200         # CREATE_NEW_PROCESS_GROUP


def out(msg=""):
    print(msg, flush=True)


def port_open(port, timeout=0.5):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex(("127.0.0.1", port)) == 0


def backend_health():
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:%d/health" % BACKEND_PORT, timeout=4
        ) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def pids_on_port(port):
    """返回监听指定端口的 PID 列表"""
    pids = []
    try:
        raw = subprocess.check_output(
            ["netstat", "-ano"], stderr=subprocess.DEVNULL
        ).decode("utf-8", "ignore")
    except Exception:
        return pids
    for line in raw.splitlines():
        line = line.strip()
        if "LISTENING" not in line.upper():
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        addr, pid = parts[1], parts[-1]
        if addr.endswith(":%d" % port) and pid.isdigit() and pid != "0":
            if pid not in pids:
                pids.append(pid)
    return pids


def find_node():
    """优先用系统 PATH 里的 node；兜底 WorkBuddy 内置版本。"""
    from shutil import which
    import glob
    p = which("node")
    if p:
        return p
    cands = glob.glob(os.path.expanduser(
        r"~\.workbuddy\binaries\node\versions\*\node.exe"))
    cands += glob.glob(os.path.join(
        os.environ.get("ProgramFiles", r"C:\Program Files"), "nodejs", "node.exe"))
    if cands:
        return sorted(cands)[-1]
    return None


def start():
    os.makedirs(LOG_DIR, exist_ok=True)

    # ---------- 后端 ----------
    if port_open(BACKEND_PORT):
        h = backend_health()
        if h:
            out("[后端] 已在运行 (%s)，跳过" % h.get("model", "?"))
        else:
            out("[后端] 端口 %d 被占用但健康检查失败，请先执行停止" % BACKEND_PORT)
            return 1
    else:
        if not os.path.isfile(VENV_PY):
            out("[后端] 找不到虚拟环境: %s" % VENV_PY)
            return 1
        env = os.environ.copy()
        env["PYTHONPATH"] = ROOT
        env.setdefault("PYTHONIOENCODING", "utf-8")
        blog = open(os.path.join(LOG_DIR, "backend.log"), "ab")
        subprocess.Popen(
            [VENV_PY, "-m", "uvicorn", "app.presentation.server:app",
             "--host", "127.0.0.1", "--port", str(BACKEND_PORT)],
            cwd=ROOT, env=env, stdout=blog, stderr=subprocess.STDOUT,
            creationflags=DETACHED | NEW_GROUP, close_fds=True,
        )
        out("[后端] 已启动，日志: logs\\backend.log")

    # ---------- 前端 ----------
    if port_open(FRONTEND_PORT):
        out("[前端] 已在运行，跳过")
    else:
        node = find_node()
        vite_js = os.path.join(FRONTEND_DIR, "node_modules", "vite", "bin", "vite.js")
        if not node:
            out("[前端] 找不到 node.exe，请确认已安装 Node.js")
            return 1
        if not os.path.isfile(vite_js):
            out("[前端] 找不到 vite，请先在 frontend 目录执行 pnpm install")
            return 1
        flog = open(os.path.join(LOG_DIR, "frontend.log"), "ab")
        subprocess.Popen(
            [node, vite_js, "--host", "0.0.0.0", "--port", str(FRONTEND_PORT)],
            cwd=FRONTEND_DIR, stdout=flog, stderr=subprocess.STDOUT,
            creationflags=DETACHED | NEW_GROUP, close_fds=True, shell=False,
        )
        out("[前端] 已启动（%s），日志: logs\\frontend.log" % node)

    # ---------- 等待就绪 ----------
    out("")
    out("等待服务就绪...（首次启动后端需要给商品建向量索引，约 1-3 分钟）")
    be_ok = fe_ok = False
    for i in range(120):          # 最多 240 秒
        if not be_ok and backend_health():
            be_ok = True
            out("  [后端] 就绪")
        if not fe_ok and port_open(FRONTEND_PORT):
            fe_ok = True
            out("  [前端] 就绪")
        if be_ok and fe_ok:
            break
        time.sleep(2)
        if i % 15 == 14 and not be_ok:
            out("  ...仍在启动，已等待 %d 秒" % ((i + 1) * 2))

    out("")
    if be_ok and fe_ok:
        h = backend_health() or {}
        out("=" * 46)
        out("  全部就绪")
        out("  前端  http://localhost:%d" % FRONTEND_PORT)
        out("  后端  http://127.0.0.1:%d/docs" % BACKEND_PORT)
        out("  模型  %s" % h.get("model", "?"))
        out("=" * 46)
        try:
            os.startfile("http://localhost:%d" % FRONTEND_PORT)
            out("已为你打开浏览器")
        except Exception:
            pass
        return 0

    out("启动超时。请查看 logs\\backend.log 或 logs\\frontend.log")
    out("  后端 %s / 前端 %s" % ("OK" if be_ok else "未就绪", "OK" if fe_ok else "未就绪"))
    return 1


def stop():
    stopped = []
    for port, label in ((FRONTEND_PORT, "前端"), (BACKEND_PORT, "后端")):
        pids = pids_on_port(port)
        if not pids:
            out("[%s] 未在运行" % label)
            continue
        for pid in pids:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", pid],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=NEW_GROUP,
                )
                stopped.append((label, pid))
            except Exception as e:
                out("[%s] 结束 PID %s 失败: %s" % (label, pid, e))
    out("")
    for label, pid in stopped:
        out("已停止 %s (PID %s)" % (label, pid))
    if not stopped:
        out("没有需要停止的服务")
    else:
        time.sleep(1)
        out("")
        out("剩余端口状态: 8000=%s  5173=%s" % (
            "占用" if port_open(BACKEND_PORT) else "空闲",
            "占用" if port_open(FRONTEND_PORT) else "空闲",
        ))
    return 0


def status():
    h = backend_health()
    out("=" * 46)
    if h:
        out("后端: 运行中  http://127.0.0.1:%d" % BACKEND_PORT)
        out("      模型 %s | 数据库 %s | Redis %s" % (
            h.get("model", "?"), h.get("database", "?"), h.get("redis", "?")))
    elif port_open(BACKEND_PORT):
        out("后端: 端口占用但健康检查失败（可能仍在启动）")
    else:
        out("后端: 未运行")

    if port_open(FRONTEND_PORT):
        out("前端: 运行中  http://localhost:%d" % FRONTEND_PORT)
    else:
        out("前端: 未运行")
    out("=" * 46)
    return 0


def main():
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()
    table = {"start": start, "stop": stop, "status": status}
    fn = table.get(cmd)
    if not fn:
        out("用法: globex_ctl.py [start|stop|status]")
        return 2
    return fn()


if __name__ == "__main__":
    sys.exit(main())
