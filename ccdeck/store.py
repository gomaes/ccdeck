"""sessions.json persistence (flock + atomic replace + .bak fallback)."""
from __future__ import annotations

import contextlib
import copy
import fcntl
import json
import os
import threading
import time

from . import CCDeckError

EMPTY = {"version": 1, "sessions": {}}


def now_iso(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() if ts is None else ts))


def new_record(name, cwd, cmd, auto_restore=True, auto_continue=None):
    ac = {"rate_limit": False, "stall": False, "text": "continue"}
    ac.update(auto_continue or {})
    now = time.time()
    return {
        "name": name,
        "cwd": cwd,
        "cmd": cmd,
        "claude_session_id": None,
        "created_at": now_iso(now),
        "launched_at": now,
        "last_output_at": now,
        "state": "running",
        "stopped": False,
        "auto_restore": bool(auto_restore),
        "auto_continue": ac,
        "restarts": 0,
    }


class Store:
    def __init__(self, path):
        self.path = path
        self.lock_path = path + ".lock"
        self._tlock = threading.RLock()

    @contextlib.contextmanager
    def _locked(self):
        with self._tlock:
            os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
            fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def _read_file(self, path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("sessions"), dict):
            raise ValueError("bad format")
        return data

    def _read(self):
        if not os.path.exists(self.path):
            return copy.deepcopy(EMPTY)
        try:
            return self._read_file(self.path)
        except (ValueError, OSError):
            bak = self.path + ".bak"
            if os.path.exists(bak):
                try:
                    return self._read_file(bak)
                except (ValueError, OSError):
                    pass
            raise CCDeckError("%s is corrupted (and no usable .bak)" % self.path, 500)

    def _write(self, data):
        tmp = "%s.tmp.%d" % (self.path, os.getpid())
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(self.path):
            try:
                os.replace(self.path, self.path + ".bak")
            except OSError:
                pass
        os.replace(tmp, self.path)

    # -- public API ---------------------------------------------------------
    def load(self):
        with self._locked():
            return self._read()

    @contextlib.contextmanager
    def transaction(self):
        with self._locked():
            data = self._read()
            yield data
            self._write(data)

    def all(self):
        return list(self.load()["sessions"].values())

    def get(self, name):
        return self.load()["sessions"].get(name)

    def require(self, name):
        rec = self.get(name)
        if rec is None:
            raise CCDeckError("no such session: %s" % name, 404)
        return rec

    def add(self, rec):
        with self.transaction() as data:
            if rec["name"] in data["sessions"]:
                raise CCDeckError("session already exists: %s" % rec["name"], 409)
            data["sessions"][rec["name"]] = rec

    def update(self, name, **fields):
        with self.transaction() as data:
            rec = data["sessions"].get(name)
            if rec is None:
                raise CCDeckError("no such session: %s" % name, 404)
            rec.update(fields)
            return copy.deepcopy(rec)

    def delete(self, name):
        with self.transaction() as data:
            return data["sessions"].pop(name, None)

    def rename(self, old, new):
        with self.transaction() as data:
            s = data["sessions"]
            if old not in s:
                raise CCDeckError("no such session: %s" % old, 404)
            if new in s:
                raise CCDeckError("session already exists: %s" % new, 409)
            rec = s.pop(old)
            rec["name"] = new
            s[new] = rec
            return rec
