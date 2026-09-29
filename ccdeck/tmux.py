"""Thin wrapper around `tmux -L <socket> -f <conf>`. Arguments are always passed as a list."""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections import namedtuple

from . import CCDeckError, validate_name

PaneInfo = namedtuple("PaneInfo", "session dead dead_status current_command pid attached current_path")

_FMT = "\t".join([
    "#{session_name}", "#{pane_dead}", "#{pane_dead_status}", "#{pane_current_command}",
    "#{pane_pid}", "#{session_attached}", "#{pane_current_path}", "#{window_active}", "#{pane_active}",
])

# Key names accepted by send_key (tmux key names). Anything else must be sent as literal text.
ALLOWED_KEYS = {
    "Enter", "Escape", "C-c", "C-d", "C-z", "C-l", "C-r", "C-o", "C-t", "C-b",
    "Up", "Down", "Left", "Right", "Tab", "BTab", "BSpace", "PageUp", "PageDown", "Home", "End", "Space",
}


def _target(name):
    # "=name:" = exact session name match, its current window (active pane).
    return "=%s:" % validate_name(name)


class Tmux:
    def __init__(self, socket="ccdeck", conf=None, bin="tmux", systemd_scope=False):
        self.socket = socket
        self.conf = conf
        self.bin = bin
        self.systemd_scope = systemd_scope

    def base(self):
        argv = [self.bin, "-L", self.socket]
        if self.conf and os.path.exists(self.conf):
            argv += ["-f", self.conf]
        return argv

    def run(self, *args, check=True, timeout=15):
        try:
            p = subprocess.run(self.base() + list(args), stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=timeout, text=True, errors="replace")
        except FileNotFoundError:
            raise CCDeckError("tmux not found (%s)" % self.bin, 500)
        except subprocess.TimeoutExpired:
            raise CCDeckError("tmux %s timed out" % args[0], 500)
        if check and p.returncode != 0:
            raise CCDeckError("tmux %s failed: %s" % (args[0], p.stderr.strip() or p.returncode), 500)
        return p

    # -- server ---------------------------------------------------------------
    def server_running(self):
        p = self.run("list-sessions", "-F", "#{session_name}", check=False)
        if p.returncode == 0:
            return True
        err = p.stderr.lower()
        return not ("no server" in err or "error connecting" in err or "no such file" in err)

    def ensure_server(self):
        """Start the tmux server (in its own systemd scope when possible) and (re)load our conf."""
        if not self.server_running():
            argv = self.base() + ["start-server"]
            if self.systemd_scope and shutil.which("systemd-run") and os.environ.get("XDG_RUNTIME_DIR"):
                scoped = ["systemd-run", "--user", "--scope", "--quiet", "--collect",
                          "--unit", "ccdeck-tmux-%d" % int(time.time() * 1000)] + argv
                p = subprocess.run(scoped, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, text=True, timeout=15)
                if p.returncode != 0:
                    subprocess.run(argv, stdin=subprocess.DEVNULL, timeout=15)
            else:
                subprocess.run(argv, stdin=subprocess.DEVNULL, timeout=15)
        if self.conf and os.path.exists(self.conf):
            self.run("source-file", self.conf, check=False)
        # Safety net even if the conf could not be loaded.
        self.run("set-option", "-g", "exit-empty", "off", check=False)
        self.run("set-option", "-wg", "remain-on-exit", "on", check=False)

    # -- sessions -------------------------------------------------------------
    def has_session(self, name):
        return self.run("has-session", "-t", "=" + validate_name(name), check=False).returncode == 0

    @staticmethod
    def _env_args(env):
        out = []
        for k, v in (env or {}).items():
            out += ["-e", "%s=%s" % (k, v)]
        return out

    def new_session(self, name, cwd, argv, env=None, width=200, height=50):
        validate_name(name)
        self.run("new-session", "-d", "-s", name, "-c", cwd, "-x", str(width), "-y", str(height),
                 *self._env_args(env), "--", *argv)
        self.run("set-option", "-w", "-t", _target(name), "remain-on-exit", "on", check=False)

    def respawn(self, name, cwd, argv, env=None):
        self.run("respawn-pane", "-k", "-t", _target(name), "-c", cwd, *self._env_args(env), "--", *argv)

    def kill_session(self, name):
        self.run("kill-session", "-t", "=" + validate_name(name), check=False)

    def rename_session(self, old, new):
        validate_name(new)
        self.run("rename-session", "-t", "=" + validate_name(old), "--", new)

    def panes(self):
        """Active pane of the active window for every session: {name: PaneInfo}."""
        p = self.run("list-panes", "-a", "-F", _FMT, check=False)
        if p.returncode != 0:
            return {}
        out = {}
        for line in p.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 9:
                continue
            name, dead, dstat, cmd, pid, att, path, wact, pact = parts
            if wact != "1" or pact != "1":
                continue
            out[name] = PaneInfo(name, dead == "1", int(dstat) if dstat.lstrip("-").isdigit() else None,
                                 cmd, int(pid) if pid.isdigit() else None,
                                 int(att) if att.isdigit() else 0, path)
        return out

    def capture(self, name, lines=None, join=False):
        """Visible screen (lines=None) or the last `lines` lines of history + screen."""
        args = ["capture-pane", "-p", "-t", _target(name)]
        if join:
            args.append("-J")
        if lines is not None:
            args += ["-S", "-" if lines == "all" else str(-abs(int(lines)))]
        return self.run(*args).stdout

    def send_key(self, name, key):
        if key not in ALLOWED_KEYS:
            raise CCDeckError("key not allowed: %r" % (key,))
        self.run("send-keys", "-t", _target(name), key)

    def send_text(self, name, text):
        if text:
            self.run("send-keys", "-t", _target(name), "-l", "--", text)

    def attach_argv(self, name):
        return self.base() + ["attach-session", "-t", "=" + validate_name(name)]
