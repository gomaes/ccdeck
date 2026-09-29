"""High level session operations shared by the CLI, the web API and the watchdog."""
from __future__ import annotations

import os
import sys
import threading
import time

from . import CCDeckError, claude, detect, validate_name
from .config import Paths, ensure_files, load_config
from .store import Store, new_record
from .tmux import ALLOWED_KEYS, Tmux

KEY_ALIASES = {
    "enter": "Enter", "esc": "Escape", "escape": "Escape", "ctrl-c": "C-c", "c-c": "C-c",
    "ctrl+c": "C-c", "tab": "Tab", "shift-tab": "BTab", "btab": "BTab", "up": "Up", "down": "Down",
    "left": "Left", "right": "Right", "backspace": "BSpace", "ctrl-d": "C-d", "ctrl-l": "C-l",
}


def resolve_key(key):
    key = KEY_ALIASES.get(str(key).lower(), key)
    if key not in ALLOWED_KEYS:
        raise CCDeckError("key not allowed: %r" % (key,))
    return key


class Manager:
    def __init__(self, paths=None, cfg=None, store=None, tmux=None, setup=True):
        self.paths = paths or Paths()
        if setup:
            ensure_files(self.paths)
        self.cfg = cfg or load_config(self.paths)
        self.store = store or Store(self.paths.sessions_file)
        t = self.cfg["tmux"]
        self.tmux = tmux or Tmux(socket=os.environ.get("CCDECK_TMUX_SOCKET") or t["socket"],
                                 conf=self.paths.tmux_conf, bin=t["bin"],
                                 systemd_scope=bool(t.get("systemd_scope")))
        self.lock = threading.RLock()
        # in-memory runtime info maintained by the watchdog: {name: dict}
        self.runtime = {}

    # -- helpers ----------------------------------------------------------------
    def _shell_argv(self, cmd):
        shell = os.environ.get("SHELL") or "/bin/bash"
        if os.path.basename(shell) not in detect.SHELLS or not os.path.exists(shell):
            shell = "/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh"
        flags = self.cfg["claude"].get("shell_flags") or ["-lc"]
        return [shell] + list(flags) + [cmd]

    def _use_sid_flag(self):
        c = self.cfg["claude"]
        return bool(c.get("use_session_id_flag")) and claude.supports_session_id_flag(c.get("bin", "claude"))

    @staticmethod
    def resolve_dir(d):
        path = os.path.abspath(os.path.expanduser(d or "~"))
        if not os.path.isdir(path):
            raise CCDeckError("directory does not exist: %s" % path)
        return path

    def _other_ids(self, name, cwd):
        return [r.get("claude_session_id") for r in self.store.all()
                if r["name"] != name and r.get("cwd") == cwd]

    def _launch(self, rec, command):
        name = rec["name"]
        self.tmux.ensure_server()
        env = {"CCDECK_SESSION": name}
        argv = self._shell_argv(command)
        if self.tmux.has_session(name):
            self.tmux.respawn(name, rec["cwd"], argv, env)
        else:
            self.tmux.new_session(name, rec["cwd"], argv, env)
        now = time.time()
        with self.lock:
            rt = self.runtime.setdefault(name, {})
            rt.update(last_change=now, hash=None, stall_sent_for=None, rl_sent_for=None)
            rt.pop("status", None)

    def _update(self, name, **fields):
        return self.store.update(name, **fields)

    # -- operations ----------------------------------------------------------------
    def create(self, name, cwd=None, cmd=None, auto_restore=None, auto_continue=None):
        validate_name(name)
        d = self.cfg["defaults"]
        cwd = self.resolve_dir(cwd or d["dir"])
        cmd = (cmd or d["cmd"]).strip()
        if not cmd:
            raise CCDeckError("empty command")
        if self.tmux.has_session(name):
            raise CCDeckError("a tmux session named %s already exists" % name, 409)
        ac = {"rate_limit": bool(d.get("auto_continue_rate_limit")), "stall": bool(d.get("auto_continue_stall")),
              "text": d.get("continue_text") or "continue"}
        ac.update({k: v for k, v in (auto_continue or {}).items() if k in ac})
        rec = new_record(name, cwd, cmd, d["auto_restore"] if auto_restore is None else auto_restore, ac)
        command, sid, _ = claude.plan_launch(cmd, mode="new", cwd=cwd, use_session_id_flag=self._use_sid_flag())
        rec["claude_session_id"] = sid
        self.store.add(rec)
        try:
            self._launch(rec, command)
        except Exception:
            self.store.delete(name)
            raise
        return rec

    def resume(self, name, force=False):
        """Recover a dead session in the same cwd, resuming the claude conversation."""
        rec = self.store.require(name)
        if not force and self.is_alive(name):
            raise CCDeckError("session %s is still running (use restart)" % name, 409)
        return self._relaunch(rec, fresh=False)

    def restart(self, name, fresh=False):
        rec = self.store.require(name)
        return self._relaunch(rec, fresh=fresh)

    def _relaunch(self, rec, fresh):
        name = rec["name"]
        if not os.path.isdir(rec["cwd"]):
            raise CCDeckError("working directory vanished: %s" % rec["cwd"], 409)
        mode = "new" if fresh else "resume"
        command, sid, how = claude.plan_launch(
            rec["cmd"], mode=mode, cwd=rec["cwd"], sid=None if fresh else rec.get("claude_session_id"),
            exclude=self._other_ids(name, rec["cwd"]), use_session_id_flag=self._use_sid_flag())
        self._launch(rec, command)
        now = time.time()
        return self._update(name, claude_session_id=sid, launched_at=now, last_output_at=now,
                            stopped=False, state="running", restarts=int(rec.get("restarts", 0)) + 1,
                            last_launch=command, last_launch_how=how)

    def stop(self, name):
        self.store.require(name)
        self.tmux.kill_session(name)
        with self.lock:
            self.runtime.get(name, {}).pop("status", None)
        return self._update(name, stopped=True, state="dead")

    def delete(self, name):
        validate_name(name)
        self.tmux.kill_session(name)
        rec = self.store.delete(name)
        with self.lock:
            self.runtime.pop(name, None)
        if rec is None:
            raise CCDeckError("no such session: %s" % name, 404)
        return rec

    def rename(self, old, new):
        validate_name(old)
        validate_name(new)
        self.store.require(old)
        if self.store.get(new) or self.tmux.has_session(new):
            raise CCDeckError("session already exists: %s" % new, 409)
        if self.tmux.has_session(old):
            self.tmux.rename_session(old, new)
            self.tmux.run("set-environment", "-t", "=" + new, "CCDECK_SESSION", new, check=False)
        rec = self.store.rename(old, new)
        with self.lock:
            if old in self.runtime:
                self.runtime[new] = self.runtime.pop(old)
        return rec

    def set_options(self, name, auto_restore=None, auto_continue=None):
        rec = self.store.require(name)
        fields = {}
        if auto_restore is not None:
            fields["auto_restore"] = bool(auto_restore)
        if auto_continue is not None:
            ac = dict(rec.get("auto_continue") or {})
            for k in ("rate_limit", "stall"):
                if k in auto_continue:
                    ac[k] = bool(auto_continue[k])
            if "text" in auto_continue:
                text = str(auto_continue["text"])
                if not text or len(text) > 500:
                    raise CCDeckError("continue text must be 1-500 chars")
                ac["text"] = text
            fields["auto_continue"] = ac
        return self._update(name, **fields) if fields else rec

    def send(self, name, key=None, text=None, enter=False):
        self.store.require(name)
        key = resolve_key(key) if key else None
        if not self.tmux.has_session(name):
            raise CCDeckError("session %s is not running" % name, 409)
        if text:
            if len(text) > 10000:
                raise CCDeckError("text too long")
            self.tmux.send_text(name, text)
            if enter:
                time.sleep(0.15)
        if key:
            self.tmux.send_key(name, key)
        if enter:
            self.tmux.send_key(name, "Enter")

    def log(self, name, lines=2000):
        self.store.require(name)
        if not self.tmux.has_session(name):
            raise CCDeckError("session %s has no tmux pane" % name, 409)
        text = self.tmux.capture(name, lines=lines, join=True)
        return "\n".join(ln.rstrip() for ln in text.splitlines()).rstrip("\n") + "\n"

    def is_alive(self, name):
        pane = self.tmux.panes().get(name)
        rec = self.store.get(name)
        st = detect.classify(exists=pane is not None, pane_dead=bool(pane and pane.dead),
                             current_command=pane.current_command if pane else None,
                             cmd=(rec or {}).get("cmd", ""), text="", now=time.time(),
                             last_change=time.time(), launched_at=(rec or {}).get("launched_at"))
        return st.state != "dead"

    # -- status ---------------------------------------------------------------------
    def status(self, rec, pane, text=None, now=None, last_change=None):
        now = now or time.time()
        w = self.cfg["watchdog"]
        if last_change is None:
            with self.lock:
                last_change = self.runtime.get(rec["name"], {}).get("last_change")
        if last_change is None:
            last_change = rec.get("last_output_at") or now
        if text is None and pane is not None and not pane.dead:
            try:
                text = self.tmux.capture(rec["name"])
            except CCDeckError:
                text = ""
        return detect.classify(
            exists=pane is not None, pane_dead=bool(pane and pane.dead),
            current_command=pane.current_command if pane else None, cmd=rec.get("cmd", ""),
            text=text or "", now=now, last_change=last_change, launched_at=rec.get("launched_at"),
            idle_seconds=w["idle_seconds"], stall_seconds=w["stall_seconds"])

    def list_status(self):
        panes = self.tmux.panes()
        now = time.time()
        out = []
        for rec in sorted(self.store.all(), key=lambda r: r.get("created_at", "")):
            with self.lock:
                cached = dict(self.runtime.get(rec["name"], {}))
            st = cached.get("status")
            if st is None or now - cached.get("checked_at", 0) > 15:
                st = self.status(rec, panes.get(rec["name"]), now=now)
            out.append(self.to_json(rec, st, panes.get(rec["name"]), now, cached))
        return out

    @staticmethod
    def to_json(rec, st, pane, now, runtime=None):
        runtime = runtime or {}
        last_change = runtime.get("last_change") or rec.get("last_output_at")
        d = dict(rec)
        d.update(
            state=st.state,
            state_reason=st.reason,
            stopped=bool(rec.get("stopped")),
            silent_seconds=int(max(0, now - last_change)) if last_change else None,
            last_output_at=last_change,
            stalled=bool(st.stalled),
            rate_limit_reset=st.rate_limit_reset.isoformat() if st.rate_limit_reset else None,
            attached=pane.attached if pane else 0,
            pid=pane.pid if pane else None,
            exit_status=pane.dead_status if pane and pane.dead else None,
            auto_continue_next=runtime.get("auto_continue_next"),
        )
        return d

    # -- startup reconciliation ------------------------------------------------------
    def reconcile(self, restore=True, log=None):
        """Compare sessions.json with the real tmux state; resume dead auto_restore sessions."""
        log = log or (lambda msg: print(msg, file=sys.stderr))
        self.tmux.ensure_server()
        panes = self.tmux.panes()
        restored = []
        for rec in self.store.all():
            name = rec["name"]
            st = self.status(rec, panes.get(name), text="")
            if st.state != "dead":
                continue
            if rec.get("stopped"):
                continue
            if not (restore and rec.get("auto_restore")):
                self._update(name, state="dead")
                log("ccdeck: %s is dead (%s); auto_restore off" % (name, st.reason))
                continue
            try:
                self.resume(name, force=True)
                restored.append(name)
                log("ccdeck: restored %s (%s)" % (name, st.reason))
            except CCDeckError as e:
                self._update(name, state="dead")
                log("ccdeck: failed to restore %s: %s" % (name, e))
        return restored

    def resume_all(self, include_manual=True):
        restored = []
        panes = self.tmux.panes()
        for rec in self.store.all():
            st = self.status(rec, panes.get(rec["name"]), text="")
            if st.state == "dead" and (include_manual or rec.get("auto_restore")):
                self.resume(rec["name"], force=True)
                restored.append(rec["name"])
        return restored
