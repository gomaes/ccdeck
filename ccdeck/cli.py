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


def _width(s):
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s, n):
    return s + " " * max(0, n - _width(s))


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
    perm = {}
    for key in ("mode", "read_scope", "bash"):
        if getattr(args, key):
            perm[key] = getattr(args, key)
    if args.add_dir:
        perm["extra_dirs"] = args.add_dir
    if args.deny_path is not None:
        perm["deny_paths"] = m.default_permissions()["deny_paths"] + args.deny_path
    if args.no_web:
        perm["web"] = False
    rec = m.create(args.name, cwd=args.dir, cmd=args.cmd,
                   auto_restore=False if args.no_auto_restore else None, auto_continue=ac, title=args.title,
                   permissions_=perm)
    label = rec["name"] if rec["title"] == rec["name"] else "%s (id: %s)" % (rec["title"], rec["name"])
    print("created %s in %s (%s)" % (label, rec["cwd"], rec["cmd"]))
    if args.attach:
        return cmd_attach(argparse.Namespace(name=rec["name"]))
    print("attach: ccdeck attach %s" % rec["name"])
    return 0


def cmd_ls(args):
    m = _manager()
    rows = m.list_status()
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False, default=str))
        return 0
    print_sessions(rows)
    return 0


def print_sessions(rows):
    if not rows:
        print("no sessions (create one with: ccdeck new <name> --dir PATH)")
        return 0
    home = os.path.expanduser("~")
    labels = [r["name"] if r.get("title", r["name"]) == r["name"] else "%s (%s)" % (r["title"], r["name"])
              for r in rows]
    w = max([20] + [_width(x) + 1 for x in labels])
    fmt = "%s %-14s %-7s %-9s %s"
    print(fmt % (_pad("NAME", w), "STATE", "SILENT", "SESSION", "CWD"))
    for label, r in zip(labels, rows):
        state = r["state"] + ("*" if r.get("stalled") else "") + (" (stopped)" if r.get("stopped") else "")
        cwd = r["cwd"].replace(home, "~", 1) if r["cwd"].startswith(home) else r["cwd"]
        print(fmt % (_pad(label, w), state, _age(r.get("silent_seconds")),
                     (r.get("claude_session_id") or "-")[:8], cwd))
        if r["state"] == "rate-limited" and r.get("rate_limit_reset"):
            print("    resets at %s" % r["rate_limit_reset"])
    return 0


def cmd_start(args):
    from . import service

    m = _manager()
    rc = service.start(m.paths, m.cfg)
    if rc == 0:
        print()
        service.status(m.paths, m.cfg, m)
    return rc


def cmd_stop(args):
    from . import service

    m = _manager()
    rc = service.stop(m.paths, m.cfg, sessions_note=not args.all)
    if args.all:
        names = []
        for rec in m.store.all():
            if m.tmux.has_session(rec["name"]) or not rec.get("stopped"):
                m.stop(rec["name"])
                names.append(rec["name"])
        print("stopped sessions: %s (restore with: ccdeck resume --all)" % (", ".join(names) or "(none)"))
    return rc


def cmd_status(args):
    from . import service

    m = _manager()
    if args.json:
        st = service.server_state(m.paths, m.cfg)
        st["answering"] = service.health(m.cfg)
        st["sessions"] = m.list_status()
        print(json.dumps(st, indent=2, ensure_ascii=False, default=str))
        return 0 if st["running"] else 3
    rc = service.status(m.paths, m.cfg, m)
    rows = m.list_status()
    if rows:
        print()
        print_sessions(rows)
    return rc


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
        rec = m.delete(args.name, keep_dir=args.keep_dir)
        print("killed %s" % args.name)
        if rec.get("workspace_deleted"):
            print("deleted working directory %s" % rec["cwd"])
        elif rec.get("workspace"):
            print("kept working directory %s (%s)" % (rec["cwd"], rec.get("workspace_note")))
    return 0


def cmd_workspace(args):
    m = _manager()
    if args.path:
        print("workspace root: %s" % m.set_workspace_root(args.path))
    else:
        print(m.workspace_root())
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


def find_claude_dir():
    """Directory containing the `claude` executable, looked up like the user's own shell would."""
    import glob
    import shutil

    found = shutil.which("claude")
    if not found:
        shell = os.environ.get("SHELL") or "/bin/bash"
        try:
            # interactive login shell: picks up PATH set in ~/.bashrc (nvm, npm prefix, ...)
            p = subprocess.run([shell, "-lic", "command -v claude"], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=15)
            cand = (p.stdout.strip().splitlines() or [""])[-1]
            if cand.startswith("/") and os.access(cand, os.X_OK):
                found = cand
        except (OSError, subprocess.TimeoutExpired):
            pass
    if not found:
        pats = ["~/.claude/local/claude", "~/.local/bin/claude", "~/.npm-global/bin/claude", "~/.bun/bin/claude",
                "~/.volta/bin/claude", "~/.local/share/pnpm/claude", "~/.nvm/versions/node/*/bin/claude"]
        for pat in pats:
            hits = sorted(glob.glob(os.path.expanduser(pat)))
            if hits and os.access(hits[-1], os.X_OK):
                found = hits[-1]
                break
    return os.path.dirname(found) if found else None


def cmd_setup(args):
    from .config import set_server_bind

    paths = Paths()
    created = ensure_files(paths)
    for c in created:
        print("created " + c)
    if args.bind:
        try:
            set_server_bind(paths, args.bind)
        except ValueError as e:
            raise CCDeckError(str(e))
        print("set [server] bind = %s in %s" % (args.bind, paths.config_file))
    if args.force or not os.path.exists(paths.env_file):
        path = os.environ.get("PATH", "")
        extra = [os.path.expanduser("~/.local/bin"), os.path.expanduser("~/.claude/local")]
        cdir = find_claude_dir()
        if cdir:
            extra.append(cdir)
            print("claude found in " + cdir)
        else:
            print("warning: claude not found; add its directory to PATH in %s" % paths.env_file, file=sys.stderr)
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
    for host in _url_hosts(s["bind"]):
        if ":" in host:
            host = "[%s]" % host
        print("http://%s:%d/?token=%s" % (host, int(s["port"]), s["token"]))
    return 0


def _url_hosts(bind):
    if bind not in ("0.0.0.0", "::"):
        return [bind]
    hosts = []
    try:
        out = subprocess.run(["hostname", "-I"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True, timeout=5).stdout.split()
        hosts = [h for h in out if ":" not in h]  # IPv4 only, keeps the list short
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ["127.0.0.1"] + (hosts or [socket.gethostname()])


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

    from . import service

    other = service.read_pid(m.paths)
    if other and other != os.getpid():
        log("ccdeck serve is already running (pid %d); use `ccdeck stop` first" % other)
        return 1
    service.write_pid(m.paths)

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
    from .resources import DiskScanner

    disk = DiskScanner(m, interval=cfg["watchdog"].get("disk_interval", 60),
                       max_seconds=cfg["watchdog"].get("disk_max_seconds", 20), log=log)
    disk.start()

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
        disk.stop()
        ttyd.stop()
        inner.shutdown()
        front.server_close()
        service.remove_pid(m.paths)
        log("ccdeck stopped (tmux sessions keep running)")
    return 0


# ------------------------------------------------------------------------ parser

def build_parser():
    p = argparse.ArgumentParser(prog="ccdeck", description="Manage Claude Code sessions (tmux + ttyd + web UI)")
    p.add_argument("--version", action="version", version="ccdeck " + __version__)
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    s = sub.add_parser("new", help="create a session")
    s.add_argument("name")
    s.add_argument("--dir", "-d", help="use this existing directory (default: create <workspace root>/<random>, "
                                        "deleted together with the session)")
    s.add_argument("--cmd", help='command (default: "claude")')
    s.add_argument("--no-auto-restore", action="store_true", help="do not restore automatically on serve start")
    s.add_argument("--auto-continue", action="store_true", help='send "continue" after a usage-limit reset')
    s.add_argument("--attach", "-a", action="store_true", help="attach right away")
    s.add_argument("--title", help="display name (default: NAME). Any language, e.g. Japanese")
    g = s.add_argument_group("permissions (default: config [defaults.permissions])")
    g.add_argument("--mode", choices=["default", "acceptEdits", "plan", "dontAsk", "bypassPermissions"],
                   help="claude permission mode")
    g.add_argument("--read-scope", choices=["workdir", "home", "any"], help="where files may be read without asking")
    g.add_argument("--add-dir", action="append", metavar="PATH", help="extra read/write directory (repeatable)")
    g.add_argument("--deny-path", action="append", metavar="PATH", help="additional never-readable path (repeatable)")
    g.add_argument("--bash", choices=["ask", "sandbox", "deny", "allow"], help="how Bash commands run")
    g.add_argument("--no-web", action="store_true", help="deny WebFetch / WebSearch")
    s.set_defaults(func=cmd_new)

    s = sub.add_parser("ls", help="list sessions")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_ls)

    s = sub.add_parser("attach", help="attach to a session (waits while it is dead)")
    s.add_argument("name")
    s.set_defaults(func=cmd_attach)

    s = sub.add_parser("kill", help="kill a session and forget it")
    s.add_argument("name")
    s.add_argument("--keep", action="store_true", help="keep the record (stop only)")
    s.add_argument("--keep-dir", action="store_true", help="do not delete the working directory created by ccdeck")
    s.set_defaults(func=cmd_kill)

    s = sub.add_parser("workspace", help="show / set the root for new working directories (default ~/claude)")
    s.add_argument("path", nargs="?")
    s.set_defaults(func=cmd_workspace)

    s = sub.add_parser("restart", help="restart claude (resuming the conversation)")
    s.add_argument("name")
    s.add_argument("--fresh", action="store_true", help="start a new conversation instead of resuming")
    s.set_defaults(func=cmd_restart)

    s = sub.add_parser("resume", help="restore a dead session (claude --resume / --continue)")
    s.add_argument("name", nargs="?")
    s.add_argument("--all", action="store_true", help="restore every dead session")
    s.add_argument("--force", action="store_true", help="even if it looks alive")
    s.set_defaults(func=cmd_resume)

    s = sub.add_parser("rename", help="rename a session")
    s.add_argument("old")
    s.add_argument("new")
    s.set_defaults(func=cmd_rename)

    s = sub.add_parser("send", help="send keys / text to a session")
    s.add_argument("name")
    s.add_argument("text", nargs="?")
    s.add_argument("--key", "-k", help="Enter, Escape, C-c, Up, Down, Tab, BTab ...")
    s.add_argument("--enter", "-e", action="store_true", help="press Enter after the text")
    s.set_defaults(func=cmd_send)

    s = sub.add_parser("log", help="print pane history (tmux capture-pane)")
    s.add_argument("name")
    s.add_argument("-n", "--lines", default=2000, type=lambda v: v if v == "all" else int(v))
    s.set_defaults(func=cmd_log)

    s = sub.add_parser("start", help="start the web UI server (systemd service, or in the background)")
    s.set_defaults(func=cmd_start)

    s = sub.add_parser("stop", help="stop the web UI server (Claude sessions keep running)")
    s.add_argument("--all", action="store_true", help="also stop every Claude session (kept in sessions.json)")
    s.set_defaults(func=cmd_stop)

    s = sub.add_parser("status", help="show server and session status")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("serve", help="run web UI + watchdog in the foreground (used by start / systemd)")
    s.add_argument("--bind")
    s.add_argument("--port", type=int)
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("doctor", help="diagnose the environment")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("setup", help="create config, tmux.conf and the systemd user unit")
    s.add_argument("--enable", action="store_true", help="daemon-reload + enable + restart the service")
    s.add_argument("--force", action="store_true", help="rewrite the env file")
    s.add_argument("--bind", help='set [server] bind in config.toml (e.g. "0.0.0.0" or "127.0.0.1")')
    s.set_defaults(func=cmd_setup)

    s = sub.add_parser("url", help="print the login URL (contains the token!)")
    s.set_defaults(func=cmd_url)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        # bare `ccdeck`: show the status and the most common commands
        try:
            rc = cmd_status(argparse.Namespace(json=False))
        except CCDeckError as e:
            print("ccdeck: %s" % e, file=sys.stderr)
            rc = 1
        print("\ncommands: ccdeck start | stop | status | new <name> | attach <name> | url | --help")
        return rc
    try:
        # commands taking an existing session accept its id or its display name
        if args.command in ("attach", "kill", "restart", "resume", "send", "log") and getattr(args, "name", None):
            try:
                args.name = _manager().resolve(args.name)
            except CCDeckError:
                if args.command != "attach":
                    raise
                validate_name(args.name)
        return args.func(args) or 0
    except BrokenPipeError:  # e.g. `ccdeck status | head`
        sys.stderr.close()
        return 0
    except CCDeckError as e:
        print("ccdeck: %s" % e, file=sys.stderr)
        return 1
