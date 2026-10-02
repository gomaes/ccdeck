"""High level session operations shared by the CLI, the web API and the watchdog."""
from __future__ import annotations

import os
import sys
import threading
import time

import re
import secrets

from . import NAME_RE, CCDeckError, claude, detect, permissions, procs, validate_name, validate_title, workspace
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
        # host CPU / memory totals (set by the watchdog)
        self.host = None

    # -- helpers ----------------------------------------------------------------
    def _shell_argv(self, cmd):
        shell = os.environ.get("SHELL") or "/bin/bash"
        if os.path.basename(shell) not in detect.SHELLS or not os.path.exists(shell):
            shell = "/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh"
        flags = self.cfg["claude"].get("shell_flags") or ["-lc"]
        return [shell] + list(flags) + [cmd]

    @staticmethod
    def title_of(rec):
        return rec.get("title") or rec["name"]

    def _launch_title(self):
        c = self.cfg["claude"]
        return bool(c.get("name_sessions", True)) and claude.supports_name_flag(c.get("bin", "claude"))

    def resolve(self, ref):
        """Session id from an id or a display name (title)."""
        if isinstance(ref, str) and NAME_RE.match(ref) and self.store.get(ref):
            return ref
        hits = [r["name"] for r in self.store.all() if self.title_of(r) == ref]
        if len(hits) == 1:
            return hits[0]
        raise CCDeckError("no such session: %s" % ref, 404)

    def _check_title_free(self, title, own=None):
        for r in self.store.all():
            if r["name"] != own and (self.title_of(r) == title or (r["name"] == title and own != title)):
                raise CCDeckError("session already exists: %s" % title, 409)

    def _new_id(self, title):
        slug = re.sub(r"[^A-Za-z0-9_-]+", "-", title).strip("-")[:32]
        if slug and NAME_RE.match(slug) and not self.store.get(slug) and not self.tmux.has_session(slug):
            return slug
        while True:
            sid = "s-" + secrets.token_hex(3)
            if not self.store.get(sid) and not self.tmux.has_session(sid):
                return sid

    def _set_window_title(self, name, title):
        target = "=%s:" % name
        self.tmux.run("set-option", "-w", "-t", target, "automatic-rename", "off", check=False)
        self.tmux.run("rename-window", "-t", target, "--", title, check=False)

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

    def graceful_exit(self, name, timeout=None):
        """Ask the program in the pane to exit (Ctrl+C, twice for interactive claude) and wait.

        Killing claude outright skips its cleanup; e.g. `claude rc` then keeps a stale Remote
        Control session id in ~/.claude/projects/<cwd>/bridge-pointer.json and later runs fail
        with "CCR v2 worker registration failed ... 404". Returns True if the process exited."""
        if timeout is None:
            timeout = float(self.cfg["claude"].get("exit_timeout", 10))
        pane = self.tmux.panes().get(name)
        if pane is None or pane.dead or timeout <= 0:
            return True
        end = time.time() + timeout
        next_key = 0.0
        while time.time() < end:
            if time.time() >= next_key:
                self.tmux.send_key(name, "C-c")
                next_key = time.time() + 1.0
            time.sleep(0.2)
            pane = self.tmux.panes().get(name)
            if pane is None or pane.dead:
                return True
        return False

    def _shutdown_pane(self, name):
        """graceful_exit + remember the pane's process tree, so that processes surviving the
        tmux kill/respawn (e.g. detached children) can be terminated with finish_shutdown()."""
        pane = self.tmux.panes().get(name)
        snap = {}
        if pane is not None and not pane.dead and pane.pid:
            snap = procs.snapshot(procs.tree(pane.pid))
        self.graceful_exit(name)
        return snap

    @staticmethod
    def _finish_shutdown(snap):
        if snap:
            procs.terminate(snap)

    def _check_remote_control_free(self, rec, command):
        """`claude rc` refuses to start when the folder is already served by another rc."""
        if not claude.is_claude_cmd(command) or claude.subcommand(command) not in claude.REMOTE_CONTROL_SUBCOMMANDS:
            return
        allp = procs.all_procs()
        pane = self.tmux.panes().get(rec["name"])
        own = set(procs.tree(pane.pid, allp)) if pane is not None and pane.pid else set()
        for p in procs.remote_control_servers(allp):
            if p.pid not in own and p.cwd == rec["cwd"]:
                raise CCDeckError(
                    "%s is already served by another claude remote-control (pid %d: %s). "
                    "Stop it first (kill %d)" % (rec["cwd"], p.pid, " ".join(p.argv)[:120], p.pid), 409)

    def _launch(self, rec, command):
        name = rec["name"]
        self.tmux.ensure_server()
        env = {"CCDECK_SESSION": name}
        cbin = claude.find_bin(self.cfg["claude"].get("bin", "claude"),
                               claude.env_file_path(self.paths.env_file))
        command = self._apply_permissions(rec, command)
        argv = self._shell_argv(claude.absolutize(command, cbin))
        self._check_remote_control_free(rec, command)
        if self.tmux.has_session(name):
            snap = self._shutdown_pane(name)
            self.tmux.respawn(name, rec["cwd"], argv, env)
            self._finish_shutdown(snap)
        else:
            self.tmux.new_session(name, rec["cwd"], argv, env)
        self._set_window_title(name, self.title_of(rec))
        now = time.time()
        with self.lock:
            rt = self.runtime.setdefault(name, {})
            rt.update(last_change=now, hash=None, stall_sent_for=None, rl_sent_for=None)
            rt.pop("status", None)

    def _update(self, name, **fields):
        return self.store.update(name, **fields)

    # -- operations ----------------------------------------------------------------
    # -- workspace / permissions -------------------------------------------------------
    def workspace_root(self):
        return workspace.get_root(self.paths, self.cfg)

    def set_workspace_root(self, root):
        return workspace.set_root(self.paths, root)

    def default_permissions(self):
        return permissions.normalize(self.cfg["defaults"].get("permissions") or {})

    def _perm_settings_path(self, name):
        return os.path.join(self.paths.data_dir, "permissions", "%s.json" % name)

    def _apply_permissions(self, rec, command):
        """Generate the settings for rec's permission profile and add the flags to command."""
        if "permissions" not in rec or not claude.is_claude_cmd(command):
            return command
        perm = permissions.normalize(rec["permissions"])
        cbin = self.cfg["claude"].get("bin", "claude")
        supported = claude.supported_permission_modes(cbin)
        if perm["mode"] != "default" and supported is not None and perm["mode"] not in supported:
            raise CCDeckError("this claude does not support permission mode %s (supported: %s)"
                              % (perm["mode"], ", ".join(sorted(supported))))
        sub = claude.subcommand(command)
        if sub is None:
            path = permissions.write_settings(self._perm_settings_path(rec["name"]), perm)
            return claude.with_permissions(command, perm["mode"], path)
        if sub in claude.REMOTE_CONTROL_SUBCOMMANDS:
            # sessions spawned by `claude rc` read the folder's project settings
            if rec.get("workspace") and workspace.removable(rec) in (None, "used by another session"):
                permissions.write_settings(os.path.join(rec["cwd"], ".claude", "settings.local.json"), perm)
            return claude.with_permissions(command, perm["mode"], None)
        return command

    def create(self, name, cwd=None, cmd=None, auto_restore=None, auto_continue=None, title=None,
               permissions_=None):
        """`name` may be any display name (e.g. Japanese). If it is not a valid tmux/URL id
        (^[a-zA-Z0-9_-]{1,32}$), it becomes the title and an ASCII id is generated.

        cwd=None creates a fresh working directory <workspace_root>/<random> that is deleted
        together with the session; otherwise cwd must be an existing directory."""
        if isinstance(name, str) and NAME_RE.match(name):
            title = validate_title(title) if title else name
        else:
            title = validate_title(title or name)
            name = self._new_id(title)
        self._check_title_free(title)
        if self.store.get(name):
            raise CCDeckError("session already exists: %s" % name, 409)
        d = self.cfg["defaults"]
        cmd = (cmd or d["cmd"]).strip()
        if not cmd:
            raise CCDeckError("empty command")
        if self.tmux.has_session(name):
            raise CCDeckError("a tmux session named %s already exists" % name, 409)
        perm = permissions.normalize(permissions_, self.default_permissions())
        ws = None
        if cwd:
            cwd = self.resolve_dir(cwd)
        else:
            ws = workspace.create(self.workspace_root())
            cwd = ws["path"]
        ac = {"rate_limit": bool(d.get("auto_continue_rate_limit")), "stall": bool(d.get("auto_continue_stall")),
              "text": d.get("continue_text") or "continue"}
        ac.update({k: v for k, v in (auto_continue or {}).items() if k in ac})
        rec = new_record(name, cwd, cmd, d["auto_restore"] if auto_restore is None else auto_restore, ac)
        rec["title"] = title
        rec["permissions"] = perm
        if ws:
            rec["workspace"] = ws
        try:
            command, sid, _ = claude.plan_launch(cmd, mode="new", cwd=cwd, use_session_id_flag=self._use_sid_flag(),
                                                 title=title if self._launch_title() else None)
            rec["claude_session_id"] = sid
            self.store.add(rec)
        except Exception:
            if ws:
                workspace.remove(rec)
            raise
        try:
            self._launch(rec, command)
        except Exception:
            self.store.delete(name)
            if ws:
                workspace.remove(rec)
            raise
        return self.store.get(name) or rec

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
            exclude=self._other_ids(name, rec["cwd"]), use_session_id_flag=self._use_sid_flag(),
            title=self.title_of(rec) if self._launch_title() else None)
        self._launch(rec, command)
        now = time.time()
        return self._update(name, claude_session_id=sid, launched_at=now, last_output_at=now,
                            stopped=False, state="running", restarts=int(rec.get("restarts", 0)) + 1,
                            last_launch=command, last_launch_how=how)

    def stop(self, name):
        self.store.require(name)
        snap = self._shutdown_pane(name)
        self.tmux.kill_session(name)
        self._finish_shutdown(snap)
        with self.lock:
            self.runtime.get(name, {}).pop("status", None)
        return self._update(name, stopped=True, state="dead")

    def delete(self, name, keep_dir=False):
        """Kill the session and forget it. Its working directory is deleted as well when
        ccdeck created it (see workspace.removable for the safety checks)."""
        validate_name(name)
        snap = self._shutdown_pane(name)
        self.tmux.kill_session(name)
        self._finish_shutdown(snap)
        rec = self.store.delete(name)
        with self.lock:
            self.runtime.pop(name, None)
        if rec is None:
            raise CCDeckError("no such session: %s" % name, 404)
        try:
            os.unlink(self._perm_settings_path(name))
        except OSError:
            pass
        rec = dict(rec)
        if keep_dir:
            rec["workspace_deleted"], rec["workspace_note"] = False, "kept"
        else:
            rec["workspace_deleted"], rec["workspace_note"] = workspace.remove(rec, self.store.all())
        return rec

    def rename(self, old, new):
        """Change the display name. When the new name is also a valid id, the id (tmux session
        name, URL, CLI name) follows it; otherwise only the display name changes.
        claude picks up the new name (--name) on its next restart."""
        old = self.resolve(old)
        new = validate_title(new)
        self._check_title_free(new, own=old)
        if not NAME_RE.match(new) or new == old or self.store.get(new) or self.tmux.has_session(new):
            if NAME_RE.match(new) and new != old and (self.store.get(new) or self.tmux.has_session(new)):
                raise CCDeckError("session already exists: %s" % new, 409)
            rec = self._update(old, title=new)
            if self.tmux.has_session(old):
                self._set_window_title(old, new)
            return rec
        if self.tmux.has_session(old):
            self.tmux.rename_session(old, new)
            self.tmux.run("set-environment", "-t", "=" + new, "CCDECK_SESSION", new, check=False)
        self.store.rename(old, new)
        rec = self._update(new, title=new)
        if self.tmux.has_session(new):
            self._set_window_title(new, new)
        with self.lock:
            if old in self.runtime:
                self.runtime[new] = self.runtime.pop(old)
        return rec

    def set_options(self, name, auto_restore=None, auto_continue=None, permissions_=None):
        """Permission changes take effect on the next (re)start of claude."""
        rec = self.store.require(name)
        fields = {}
        if permissions_ is not None:
            fields["permissions"] = permissions.normalize(permissions_, rec.get("permissions") or
                                                          self.default_permissions())
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
        d.setdefault("title", rec["name"])
        d["workspace_managed"] = bool(rec.get("workspace"))
        if rec.get("permissions"):
            d["permissions_summary"] = permissions.summary(permissions.normalize(rec["permissions"]))
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
            usage=runtime.get("usage") if pane is not None and not pane.dead else None,
            disk=runtime.get("disk"),
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
