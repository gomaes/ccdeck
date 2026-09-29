"""Command line interface."""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time

from . import CCDeckError, __version__, validate_name
from .config import Paths, ensure_files, is_loopback

UNIT_TEMPLATE = """\
[Unit]
Description=ccdeck - web deck for Claude Code sessions
After=default.target

[Service]
Type=simple
EnvironmentFile=-{env_file}
ExecStart={exec_start}
Restart=always
RestartSec=3
# tmux runs in its own scope; only stop the ccdeck process itself (it stops ttyd on exit).
KillMode=process
TimeoutStopSec=15

[Install]
WantedBy=default.target
"""


def _name(s):
    try:
        return validate_name(s)
    except CCDeckError as e:
        raise argparse.ArgumentTypeError(str(e))


def _manager():
    from .manager import Manager

    return Manager()


def _age(sec):
    if sec is None:
        return "-"
    sec = int(sec)
    if sec < 60:
        return "%ds" % sec
    if sec < 3600:
        return "%dm" % (sec // 60)
    if sec < 86400:
        return "%dh%02dm" % (sec // 3600, sec % 3600 // 60)
    return "%dd" % (sec // 86400)


# ------------------------------------------------------------------------ commands

def cmd_new(args):
    m = _manager()
    ac = {"rate_limit": True} if args.auto_continue else None
    rec = m.create(args.name, cwd=args.dir, cmd=args.cmd,
                   auto_restore=False if args.no_auto_restore else None, auto_continue=ac)
    print("created %s in %s (%s)" % (rec["name"], rec["cwd"], rec["cmd"]))
    if args.attach:
        return cmd_attach(argparse.Namespace(name=args.name))
    print("attach: ccdeck attach %s" % rec["name"])
    return 0


def cmd_ls(args):
    m = _manager()
    rows = m.list_status()
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False, default=str))
        return 0
    if not rows:
        print("no sessions (create one with: ccdeck new <name> --dir PATH)")
        return 0
    home = os.path.expanduser("~")
    fmt = "%-20s %-14s %-7s %-9s %s"
    print(fmt % ("NAME", "STATE", "SILENT", "SESSION", "CWD"))
    for r in rows:
        state = r["state"] + ("*" if r.get("stalled") else "") + (" (stopped)" if r.get("stopped") else "")
        cwd = r["cwd"].replace(home, "~", 1) if r["cwd"].startswith(home) else r["cwd"]
        print(fmt % (r["name"], state, _age(r.get("silent_seconds")),
                     (r.get("claude_session_id") or "-")[:8], cwd))
        if r["state"] == "rate-limited" and r.get("rate_limit_reset"):
            print("    resets at %s" % r["rate_limit_reset"])
    return 0


def cmd_attach(args):
    m = _manager()
    name = args.name
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}
    announced = False
    try:
        while True:
            if m.tmux.has_session(name):
                announced = False
                rc = subprocess.call(m.tmux.attach_argv(name), env=env)
                if m.store.get(name) is None:
                    print("\r\n[ccdeck] session %s was removed." % name)
                    return rc
                if m.tmux.has_session(name):
                    return rc  # detached by the user
                continue
            rec = m.store.get(name)
            if rec is None:
                print("[ccdeck] no such session: %s" % name)
                time.sleep(2)
                return 1
            if not announced:
                print("\r\n[ccdeck] session %s is not running%s. Waiting for it to be restored "
                      "(Web UI: 復旧 / CLI: ccdeck resume %s) ... Ctrl+C to quit"
                      % (name, " (stopped)" if rec.get("stopped") else "", name))
                announced = True
            time.sleep(1)
    except KeyboardInterrupt:
        return 130


def cmd_kill(args):
    m = _manager()
    if args.keep:
        m.stop(args.name)
        print("stopped %s (record kept; `ccdeck resume %s` to start again)" % (args.name, args.name))
    else:
        m.delete(args.name)
        print("killed %s" % args.name)
    return 0


def cmd_restart(args):
    rec = _manager().restart(args.name, fresh=args.fresh)
    print("restarted %s: %s" % (rec["name"], rec.get("last_launch")))
    return 0


def cmd_resume(args):
    m = _manager()
    if args.all:
        names = m.resume_all()
        print("restored: %s" % (", ".join(names) or "(none)"))
        return 0
    if not args.name:
        print("usage: ccdeck resume <name> | --all", file=sys.stderr)
        return 2
    rec = m.resume(args.name, force=args.force)
    print("resumed %s: %s" % (rec["name"], rec.get("last_launch")))
    return 0


def cmd_rename(args):
    _manager().rename(args.old, args.new)
    print("renamed %s -> %s" % (args.old, args.new))
    return 0


def cmd_send(args):
    _manager().send(args.name, key=args.key, text=args.text, enter=args.enter)
    return 0


def cmd_log(args):
    sys.stdout.write(_manager().log(args.name, lines=args.lines))
    return 0


def cmd_doctor(args):
    from . import doctor

    return doctor.main(_manager())


def _self_exec():
    arg0 = os.path.abspath(sys.argv[0])
    if os.path.basename(arg0) == "__main__.py":
        pkg_parent = os.path.dirname(os.path.dirname(arg0))
        return "/usr/bin/env PYTHONPATH=%s %s -m ccdeck" % (pkg_parent, sys.executable)
    return "%s %s" % (sys.executable, arg0)


def cmd_setup(args):
    paths = Paths()
    created = ensure_files(paths)
    for c in created:
        print("created " + c)
    if args.force or not os.path.exists(paths.env_file):
        path = os.environ.get("PATH", "")
        extra = [os.path.expanduser("~/.local/bin"), os.path.expanduser("~/.claude/local")]
        parts = path.split(os.pathsep)
        for e in extra:
            if e not in parts:
                parts.insert(0, e)
        with open(paths.env_file, "w") as f:
            f.write("# Environment for ccdeck.service (generated by `ccdeck setup`)\n")
            f.write("PATH=%s\n" % os.pathsep.join(parts))
            f.write("SHELL=%s\n" % (os.environ.get("SHELL") or "/bin/bash"))
            f.write("LANG=%s\n" % (os.environ.get("LANG") or "C.UTF-8"))
        os.chmod(paths.env_file, 0o600)
        print("wrote " + paths.env_file)
    os.makedirs(os.path.dirname(paths.systemd_unit), exist_ok=True)
    unit = UNIT_TEMPLATE.format(env_file=paths.env_file, exec_start=_self_exec() + " serve")
    old = open(paths.systemd_unit).read() if os.path.exists(paths.systemd_unit) else None
    if old != unit:
        with open(paths.systemd_unit, "w") as f:
            f.write(unit)
        print("wrote " + paths.systemd_unit)
    if args.enable:
        for argv in (["systemctl", "--user", "daemon-reload"],
                     ["systemctl", "--user", "enable", "ccdeck.service"],
                     ["systemctl", "--user", "restart", "ccdeck.service"]):
            rc = subprocess.call(argv)
            if rc != 0:
                print("warning: %s failed (%d)" % (" ".join(argv), rc), file=sys.stderr)
    return 0


def cmd_url(args):
    from .config import load_config

    paths = Paths()
    ensure_files(paths)
    s = load_config(paths)["server"]
    host = s["bind"]
    if host in ("0.0.0.0", "::"):
        host = socket.gethostname()
    if ":" in host:
        host = "[%s]" % host
    print("http://%s:%d/?token=%s" % (host, int(s["port"]), s["token"]))
    return 0


def cmd_serve(args):
    from werkzeug.serving import make_server

    from .proxy import FrontServer
    from .ttyd import TtydSupervisor
    from .watchdog import Watchdog
    from .web import create_app

    def log(msg):
        print(msg, file=sys.stderr, flush=True)

    m = _manager()
    cfg = m.cfg
    s = cfg["server"]
    bind = args.bind or s["bind"]
    port = int(args.port or s["port"])
    if not is_loopback(bind) and not s.get("allow_external"):
        log("refusing to bind %s: set [server] allow_external = true in %s" % (bind, m.paths.config_file))
        return 2
    token = s.get("token")
    if not token:
        log("no token configured in %s" % m.paths.config_file)
        return 2
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    restored = m.reconcile(restore=True, log=log)
    log("ccdeck %s: %d session(s), restored %d" % (__version__, len(m.store.all()), len(restored)))

    ttyd = TtydSupervisor(cfg, m.paths, log=log)
    try:
        ttyd.start()
    except OSError as e:
        log("warning: cannot start ttyd (%s); web terminals disabled" % e)

    app = create_app(m, lambda: token, cookie_secure=s.get("cookie_secure"))
    inner = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=inner.serve_forever, name="ccdeck-flask", daemon=True).start()

    wd = Watchdog(m, log=log)
    wd.start()

    ctx = {"token": lambda: token, "ttyd_port": ttyd.port, "ttyd_cred": ttyd.credential,
           "app_port": lambda: inner.server_port}
    front = FrontServer(bind, port, ctx)
    log("ccdeck listening on http://%s:%d/ (ttyd on 127.0.0.1:%d)" % (bind, port, ttyd.port))

    def _shutdown(signum, frame):
        threading.Thread(target=front.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    try:
        front.serve_forever()
    finally:
        wd.stop()
        ttyd.stop()
        inner.shutdown()
        front.server_close()
        log("ccdeck stopped (tmux sessions keep running)")
    return 0


# ------------------------------------------------------------------------ parser

def build_parser():
    p = argparse.ArgumentParser(prog="ccdeck", description="Manage Claude Code sessions (tmux + ttyd + web UI)")
    p.add_argument("--version", action="version", version="ccdeck " + __version__)
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    s = sub.add_parser("new", help="create a session")
    s.add_argument("name", type=_name)
    s.add_argument("--dir", "-d", help="working directory (default: config defaults.dir)")
    s.add_argument("--cmd", help='command (default: "claude")')
    s.add_argument("--no-auto-restore", action="store_true", help="do not restore automatically on serve start")
    s.add_argument("--auto-continue", action="store_true", help='send "continue" after a usage-limit reset')
    s.add_argument("--attach", "-a", action="store_true", help="attach right away")
    s.set_defaults(func=cmd_new)

    s = sub.add_parser("ls", help="list sessions")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_ls)

    s = sub.add_parser("attach", help="attach to a session (waits while it is dead)")
    s.add_argument("name", type=_name)
    s.set_defaults(func=cmd_attach)

    s = sub.add_parser("kill", help="kill a session and forget it")
    s.add_argument("name", type=_name)
    s.add_argument("--keep", action="store_true", help="keep the record (stop only)")
    s.set_defaults(func=cmd_kill)

    s = sub.add_parser("restart", help="restart claude (resuming the conversation)")
    s.add_argument("name", type=_name)
    s.add_argument("--fresh", action="store_true", help="start a new conversation instead of resuming")
    s.set_defaults(func=cmd_restart)

    s = sub.add_parser("resume", help="restore a dead session (claude --resume / --continue)")
    s.add_argument("name", type=_name, nargs="?")
    s.add_argument("--all", action="store_true", help="restore every dead session")
    s.add_argument("--force", action="store_true", help="even if it looks alive")
    s.set_defaults(func=cmd_resume)

    s = sub.add_parser("rename", help="rename a session")
    s.add_argument("old", type=_name)
    s.add_argument("new", type=_name)
    s.set_defaults(func=cmd_rename)

    s = sub.add_parser("send", help="send keys / text to a session")
    s.add_argument("name", type=_name)
    s.add_argument("text", nargs="?")
    s.add_argument("--key", "-k", help="Enter, Escape, C-c, Up, Down, Tab, BTab ...")
    s.add_argument("--enter", "-e", action="store_true", help="press Enter after the text")
    s.set_defaults(func=cmd_send)

    s = sub.add_parser("log", help="print pane history (tmux capture-pane)")
    s.add_argument("name", type=_name)
    s.add_argument("-n", "--lines", default=2000, type=lambda v: v if v == "all" else int(v))
    s.set_defaults(func=cmd_log)

    s = sub.add_parser("serve", help="run web UI + watchdog (used by the systemd service)")
    s.add_argument("--bind")
    s.add_argument("--port", type=int)
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("doctor", help="diagnose the environment")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("setup", help="create config, tmux.conf and the systemd user unit")
    s.add_argument("--enable", action="store_true", help="daemon-reload + enable + restart the service")
    s.add_argument("--force", action="store_true", help="rewrite the env file")
    s.set_defaults(func=cmd_setup)

    s = sub.add_parser("url", help="print the login URL (contains the token!)")
    s.set_defaults(func=cmd_url)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        return args.func(args) or 0
    except CCDeckError as e:
        print("ccdeck: %s" % e, file=sys.stderr)
        return 1
