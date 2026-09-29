"""`ccdeck doctor`: environment diagnostics."""
from __future__ import annotations

import getpass
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import urllib.request

from . import claude
from .config import is_loopback
from .ttyd import ttyd_version

OK, WARN, FAIL = "ok", "warn", "fail"
MARK = {OK: "\033[32m✔\033[0m", WARN: "\033[33m!\033[0m", FAIL: "\033[31m✘\033[0m"}
PLAIN = {OK: "[ok]  ", WARN: "[warn]", FAIL: "[FAIL]"}


def _run(argv, timeout=10):
    try:
        p = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, timeout=timeout)
        return p.returncode, p.stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, str(e)


def _port_state(host, port):
    """'free' | 'in-use'"""
    try:
        with socket.create_connection((host, port), timeout=1):
            return "in-use"
    except OSError:
        return "free"


def _mode(path):
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return None


def run_checks(manager):
    cfg, paths = manager.cfg, manager.paths
    res = []

    def add(level, name, detail=""):
        lines = str(detail).strip().splitlines()
        res.append((level, name, lines[0] if lines else ""))

    # python / flask
    add(OK if sys.version_info >= (3, 10) else WARN, "python", sys.version.split()[0])
    try:
        from importlib.metadata import version

        add(OK, "flask", version("flask"))
    except Exception:
        try:
            import flask  # noqa: F401

            add(OK, "flask", "installed")
        except ImportError:
            add(FAIL, "flask", "missing: sudo apt install python3-flask")

    # tmux
    tbin = cfg["tmux"]["bin"]
    if shutil.which(tbin):
        rc, out = _run([tbin, "-V"])
        m = re.search(r"(\d+)\.(\d+)", out or "")
        ok = m and (int(m.group(1)), int(m.group(2))) >= (3, 0)
        add(OK if ok else FAIL, "tmux", out + ("" if ok else " (3.0+ required)"))
        running = manager.tmux.server_running()
        n = len(manager.tmux.panes()) if running else 0
        add(OK if running else WARN, "tmux server (-L %s)" % manager.tmux.socket,
            "running, %d session(s)" % n if running else "not running (started by `ccdeck serve`/`new`)")
    else:
        add(FAIL, "tmux", "missing: sudo apt install tmux")

    # ttyd
    ybin = cfg["ttyd"]["bin"]
    if shutil.which(ybin):
        v = ttyd_version(ybin)
        add(OK if v else WARN, "ttyd", ".".join(map(str, v)) if v else "unknown version")
    else:
        add(FAIL, "ttyd", "missing: sudo apt install ttyd")
    rc, out = _run(["systemctl", "is-enabled", "ttyd.service"])
    if rc == 0 and out.strip() == "enabled":
        add(WARN, "system ttyd.service", "enabled (the Debian/Ubuntu package runs `ttyd -O login` on "
            "localhost:7681). Not needed by ccdeck: sudo systemctl disable --now ttyd")

    # claude
    cbin = cfg["claude"]["bin"]
    path = claude.find_bin(cbin, claude.env_file_path(paths.env_file))
    if path:
        cbin = path
        rc, out = _run([cbin, "--version"], timeout=20)
        add(OK, "claude", "%s (%s)" % (out.splitlines()[0] if out else "?", path))
        add(OK if claude.supports_session_id_flag(cbin) else WARN, "claude --session-id",
            "supported" if claude.supports_session_id_flag(cbin) else "not supported (ids are guessed from ~/.claude)")
    else:
        add(FAIL, "claude", "not found in PATH (%s). Sessions are started via `$SHELL -lc`, so make sure "
            "your login shell PATH contains it" % os.environ.get("PATH", ""))
    proj = os.path.join(claude.claude_home(), "projects")
    add(OK if os.path.isdir(proj) else WARN, "claude projects dir", proj)

    # systemd / linger
    user = getpass.getuser()
    if shutil.which("loginctl"):
        rc, out = _run(["loginctl", "show-user", user, "-p", "Linger", "--value"])
        if rc == 0:
            add(OK if out.strip() == "yes" else WARN, "systemd linger",
                "enabled" if out.strip() == "yes" else "disabled: sudo loginctl enable-linger %s" % user)
        else:
            add(WARN, "systemd linger", out or "unknown")
    else:
        add(WARN, "systemd linger", "loginctl not found")
    if shutil.which("systemctl"):
        rc, out = _run(["systemctl", "--user", "is-active", "ccdeck.service"])
        rc2, out2 = _run(["systemctl", "--user", "is-enabled", "ccdeck.service"])
        add(OK if out == "active" else WARN, "ccdeck.service",
            "%s / %s" % (out or "?", out2 or "?") + ("" if out == "active" else
                                                       " (systemctl --user enable --now ccdeck)"))
    add(OK if os.path.exists(paths.systemd_unit) else WARN, "unit file", paths.systemd_unit)

    # ports
    s = cfg["server"]
    host = "127.0.0.1" if s["bind"] in ("0.0.0.0", "::") else s["bind"]
    st = _port_state(host, int(s["port"]))
    if st == "in-use":
        try:
            with urllib.request.urlopen("http://%s:%d/api/health" % (host, int(s["port"])), timeout=2) as r:
                ok = json.loads(r.read()).get("ok")
            add(OK if ok else WARN, "web port %d" % s["port"], "ccdeck serve is answering")
        except Exception:
            add(FAIL, "web port %d" % s["port"], "in use by another program")
    else:
        add(WARN, "web port %d" % s["port"], "free (ccdeck serve not running)")
    tp = int(cfg["ttyd"]["port"])
    add(OK, "ttyd port %d" % tp, _port_state("127.0.0.1", tp))
    if not is_loopback(s["bind"]):
        add(WARN if s.get("allow_external") else FAIL, "bind address",
            "%s (external exposure%s)" % (s["bind"], "" if s.get("allow_external") else
                                          " but allow_external=false; serve will refuse"))
    else:
        add(OK, "bind address", s["bind"])

    # permissions / files
    for p, want in ((paths.config_dir, 0o700), (paths.data_dir, 0o700), (paths.config_file, 0o600)):
        mode = _mode(p)
        if mode is None:
            add(FAIL, "exists " + p, "missing (run `ccdeck setup`)")
        elif mode & 0o077:
            add(WARN, "permissions " + p, "%o (expected %o): chmod %o %s" % (mode, want, want, p))
        else:
            add(OK, "permissions " + p, "%o" % mode)
    add(OK if s.get("token") and len(s["token"]) >= 16 else FAIL, "token", "set" if s.get("token") else "empty")
    add(OK if os.path.exists(paths.tmux_conf) else WARN, "tmux.conf", paths.tmux_conf)
    try:
        n = len(manager.store.all())
        add(OK, "sessions.json", "%d session(s)" % n)
    except Exception as e:
        add(FAIL, "sessions.json", str(e))
    return res


def main(manager):
    res = run_checks(manager)
    color = sys.stdout.isatty()
    for level, name, detail in res:
        mark = MARK[level] if color else PLAIN[level]
        print("%s %-32s %s" % (mark, name, detail))
    fails = sum(1 for r in res if r[0] == FAIL)
    warns = sum(1 for r in res if r[0] == WARN)
    print("\n%d problem(s), %d warning(s)" % (fails, warns))
    return 1 if fails else 0
