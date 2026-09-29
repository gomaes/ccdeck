"""`ccdeck start / stop / status`: control the web UI server (`ccdeck serve`).

Uses the systemd --user unit when it is installed and usable; otherwise runs `ccdeck serve`
as a detached background process (pid file: <data>/run/serve.pid, log: <data>/serve.log)."""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

from .config import is_loopback

UNIT = "ccdeck.service"


# ------------------------------------------------------------------ pid file

def pid_file(paths):
    return os.path.join(paths.run_dir, "serve.pid")


def log_file(paths):
    return os.path.join(paths.data_dir, "serve.log")


def _is_serve_process(pid):
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            argv = f.read().split(b"\0")
    except OSError:
        return False
    return b"serve" in argv and any(b"ccdeck" in a for a in argv)


def read_pid(paths):
    """pid of a running `ccdeck serve` recorded in the pid file, else None."""
    try:
        with open(pid_file(paths)) as f:
            pid = int(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    return pid if _is_serve_process(pid) else None


def write_pid(paths):
    os.makedirs(paths.run_dir, mode=0o700, exist_ok=True)
    with open(pid_file(paths), "w") as f:
        f.write("%d\n" % os.getpid())


def remove_pid(paths):
    try:
        if read_pid(paths) in (None, os.getpid()):
            os.unlink(pid_file(paths))
    except OSError:
        pass


# ------------------------------------------------------------------ systemd

def _systemctl(*args, timeout=30):
    try:
        p = subprocess.run(["systemctl", "--user", *args], stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
        return p.returncode, (p.stdout.strip() or p.stderr.strip())
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, str(e)


def systemd_usable(paths):
    if not shutil.which("systemctl") or not os.path.exists(paths.systemd_unit):
        return False
    rc, _ = _systemctl("show-environment", timeout=10)
    return rc == 0


def systemd_state():
    """(active, enabled) strings, e.g. ("active", "enabled")."""
    return _systemctl("is-active", UNIT, timeout=10)[1], _systemctl("is-enabled", UNIT, timeout=10)[1]


def systemd_main_pid():
    rc, out = _systemctl("show", "-p", "MainPID", "--value", UNIT, timeout=10)
    try:
        pid = int(out)
    except ValueError:
        return None
    return pid or None


# ------------------------------------------------------------------ health

def local_host(bind):
    if bind in ("0.0.0.0", "", "localhost") or is_loopback(bind):
        return "127.0.0.1"
    if bind == "::":
        return "::1"
    return bind


def health(cfg, timeout=2):
    s = cfg["server"]
    host = local_host(s["bind"])
    if ":" in host:
        host = "[%s]" % host
    try:
        with urllib.request.urlopen("http://%s:%d/api/health" % (host, int(s["port"])), timeout=timeout) as r:
            return bool(json.loads(r.read()).get("ok"))
    except Exception:
        return False


def wait_health(cfg, want=True, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        if health(cfg, timeout=1) == want:
            return True
        time.sleep(0.3)
    return health(cfg, timeout=1) == want


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ------------------------------------------------------------------ state

def server_state(paths, cfg):
    """dict(running, mode, pid, systemd_active, systemd_enabled)."""
    st = {"running": False, "mode": None, "pid": None, "systemd": False,
          "systemd_active": None, "systemd_enabled": None}
    if systemd_usable(paths):
        st["systemd"] = True
        st["systemd_active"], st["systemd_enabled"] = systemd_state()
        if st["systemd_active"] in ("active", "activating", "reloading"):
            st.update(running=True, mode="systemd", pid=systemd_main_pid())
            return st
    pid = read_pid(paths)
    if pid:
        st.update(running=True, mode="background", pid=pid)
    elif health(cfg):
        st.update(running=True, mode="unknown")  # e.g. started by hand from another checkout
    return st


# ------------------------------------------------------------------ actions

def _spawn_background(paths):
    from .ttyd import self_argv

    env = dict(os.environ)
    argv = self_argv()
    if argv[1:3] == ["-m", "ccdeck"]:
        pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env["PYTHONPATH"] = pkg_parent + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    os.makedirs(paths.data_dir, mode=0o700, exist_ok=True)
    log = open(log_file(paths), "ab")
    log.write(("\n--- ccdeck start %s ---\n" % time.strftime("%Y-%m-%d %H:%M:%S")).encode())
    log.flush()
    proc = subprocess.Popen(argv + ["serve"], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                            env=env, start_new_session=True, close_fds=True)
    log.close()
    return proc


def _tail(path, n=15):
    try:
        with open(path, "rb") as f:
            lines = f.read().decode("utf-8", "replace").splitlines()
        return "\n".join(lines[-n:])
    except OSError:
        return ""


def start(paths, cfg, out=print):
    st = server_state(paths, cfg)
    if st["running"]:
        out("ccdeck is already running (%s%s)" % (st["mode"], ", pid %s" % st["pid"] if st["pid"] else ""))
        return 0
    if st["systemd"]:
        rc, msg = _systemctl("start", UNIT)
        if rc != 0:
            out("systemctl --user start %s failed: %s" % (UNIT, msg))
            return 1
        mode, logs = "systemd", "journalctl --user -u ccdeck -n 30"
        proc = None
    else:
        proc = _spawn_background(paths)
        mode, logs = "background", log_file(paths)
    ok = wait_health(cfg, True)
    if not ok and proc is not None and proc.poll() is not None:
        out("ccdeck failed to start (exit %s). Last log lines (%s):" % (proc.returncode, logs))
        out(_tail(log_file(paths)))
        return 1
    if not ok:
        out("ccdeck was started (%s) but does not answer yet; check: %s" % (mode, logs))
        return 1
    out("ccdeck started (%s)" % mode)
    return 0


def stop(paths, cfg, out=print, timeout=15, sessions_note=True):
    st = server_state(paths, cfg)
    if not st["running"]:
        out("ccdeck is not running")
        return 0
    if st["mode"] == "systemd":
        rc, msg = _systemctl("stop", UNIT)
        if rc != 0:
            out("systemctl --user stop %s failed: %s" % (UNIT, msg))
            return 1
    elif st["pid"]:
        pid = st["pid"]
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        end = time.time() + timeout
        while time.time() < end and _pid_alive(pid) and _is_serve_process(pid):
            time.sleep(0.2)
        if _pid_alive(pid) and _is_serve_process(pid):
            os.kill(pid, signal.SIGKILL)
    else:
        out("ccdeck answers on its port but was not started by `ccdeck start` or systemd; "
            "stop that process by hand")
        return 1
    wait_health(cfg, False, timeout=5)
    out("ccdeck stopped (web UI)." + (" Claude sessions keep running in tmux." if sessions_note else ""))
    return 0


def status(paths, cfg, manager=None, out=print):
    """Print server + session status. Returns 0 when the server is running, 3 otherwise (LSB style)."""
    from .cli import _url_hosts

    s = cfg["server"]
    st = server_state(paths, cfg)
    if st["running"]:
        extra = " (%s%s)" % (st["mode"], ", pid %s" % st["pid"] if st["pid"] else "")
        answering = health(cfg)
        out("server  : running%s%s" % (extra, "" if answering else " — NOT answering on port %s" % s["port"]))
        for h in _url_hosts(s["bind"]):
            out("          http://%s:%d/" % ("[%s]" % h if ":" in h else h, int(s["port"])))
    else:
        out("server  : stopped   (start: ccdeck start)")
    if st["systemd"]:
        out("service : %s / %s   (systemctl --user ... %s)" % (st["systemd_active"], st["systemd_enabled"], UNIT))
    else:
        out("service : systemd --user not available -> background mode (log: %s)" % log_file(paths))
    if manager is not None:
        try:
            rows = manager.list_status()
        except Exception as e:  # tmux problems must not hide the server status
            out("sessions: error: %s" % e)
            rows = None
        if rows is not None:
            counts = {}
            for r in rows:
                counts[r["state"]] = counts.get(r["state"], 0) + 1
            summary = ", ".join("%s %d" % kv for kv in sorted(counts.items())) or "none"
            out("sessions: %d (%s)" % (len(rows), summary))
    return 0 if st["running"] else 3
