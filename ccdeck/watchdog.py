"""Periodic monitor: output hashing, state tracking, auto-continue and optional auto-restore."""
from __future__ import annotations

import hashlib
import sys
import threading
import time

from . import CCDeckError, claude


class Watchdog:
    def __init__(self, manager, log=None, clock=time.time, sleeper=time.sleep):
        self.m = manager
        self.log = log or (lambda msg: print(msg, file=sys.stderr, flush=True))
        self.clock = clock
        self.sleeper = sleeper
        self._stop = threading.Event()
        self._thread = None
        self._restart_times = {}

    # -- thread control --------------------------------------------------------
    def start(self):
        self._thread = threading.Thread(target=self._loop, name="ccdeck-watchdog", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        interval = float(self.m.cfg["watchdog"]["interval"])
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:  # keep running whatever happens
                self.log("watchdog error: %r" % (e,))
            self._stop.wait(interval)

    # -- one iteration ---------------------------------------------------------
    def tick(self):
        m = self.m
        w = m.cfg["watchdog"]
        now = self.clock()
        panes = m.tmux.panes()
        records = m.store.all()
        for rec in records:
            name = rec["name"]
            pane = panes.get(name)
            text = ""
            if pane is not None and not pane.dead:
                try:
                    text = m.tmux.capture(name)
                except CCDeckError:
                    pane = None
            h = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()
            with m.lock:
                rt = m.runtime.setdefault(name, {})
                if rt.get("last_change") is None:
                    rt["last_change"] = rec.get("last_output_at") or now
                if rt.get("hash") is not None and rt["hash"] != h:
                    rt["last_change"] = now
                rt["hash"] = h
                last_change = rt["last_change"]
            st = m.status(rec, pane, text=text, now=now, last_change=last_change)
            st = self._pin_reset_time(rec, rt, st, now)
            with m.lock:
                rt.update(status=st, checked_at=now)
            self._persist(rec, rt, st, now)
            if st.state == "dead":
                self._maybe_auto_restore(rec, st, now)
                continue
            self._auto_continue(rec, rt, st, now, w)
            self._refresh_session_id(rec, rt, now, w, records)

    def _pin_reset_time(self, rec, rt, st, now):
        """A reset time like "resets 9am" is relative to when the message was printed.

        Remember the value parsed at first sight of a limit line, so a stale message that is
        still on screen after the reset is not re-interpreted as "tomorrow 9am"."""
        if st.state != "rate-limited":
            rt.pop("rl_line", None)
            rt.pop("rl_reset", None)
            return st
        if rt.get("rl_line") != st.reason:
            rt["rl_line"] = st.reason
            rt["rl_reset"] = st.rate_limit_reset
            return st
        pinned = rt.get("rl_reset")
        if pinned is not None and pinned.timestamp() < now - 15 * 60:
            return st._replace(state="idle", reason="limit message is stale", rate_limit_reset=None)
        return st._replace(rate_limit_reset=pinned)

    def _persist(self, rec, rt, st, now):
        fields = {}
        if rec.get("state") != st.state:
            fields["state"] = st.state
        lc = rt.get("last_change")
        if lc and (lc - (rec.get("last_output_at") or 0) > 30 or fields):
            fields["last_output_at"] = lc
        if fields:
            try:
                self.m.store.update(rec["name"], **fields)
            except CCDeckError:
                pass

    def _send_continue(self, rec, why):
        text = (rec.get("auto_continue") or {}).get("text") or "continue"
        self.log("ccdeck: auto-continue %s (%s)" % (rec["name"], why))
        self.m.send(rec["name"], text=text, enter=True)

    def _auto_continue(self, rec, rt, st, now, w):
        ac = rec.get("auto_continue") or {}
        rt["auto_continue_next"] = None
        if st.state == "rate-limited" and ac.get("rate_limit"):
            if st.rate_limit_reset is not None:
                due = st.rate_limit_reset.timestamp() + float(w["rate_limit_margin"])
                key = st.rate_limit_reset.isoformat()
            else:
                # unknown reset time: retry periodically
                period = float(w["rate_limit_unknown_retry"])
                base = rt.get("rl_unknown_since") or now
                rt["rl_unknown_since"] = base
                n = int((now - base) // period)
                due, key = base + period * max(1, n), "unknown-%d" % max(1, n)
            rt["auto_continue_next"] = due
            if now >= due and rt.get("rl_sent_for") != key:
                rt["rl_sent_for"] = key
                self._send_continue(rec, "rate limit reset")
            return
        rt.pop("rl_unknown_since", None)
        if st.stalled and ac.get("stall") and st.state in ("idle", "running"):
            key = rt.get("last_change")
            if rt.get("stall_sent_for") != key:
                rt["stall_sent_for"] = key
                self._send_continue(rec, "no output for %ds" % st.silent_seconds)

    def _maybe_auto_restore(self, rec, st, now):
        w = self.m.cfg["watchdog"]
        if not w.get("auto_restore_dead") or rec.get("stopped") or not rec.get("auto_restore"):
            return
        hist = [t for t in self._restart_times.get(rec["name"], []) if now - t < 3600]
        if len(hist) >= 5:  # crash loop protection
            return
        if rec.get("launched_at") and now - rec["launched_at"] < 30:
            return
        try:
            self.m.resume(rec["name"], force=True)
            hist.append(now)
            self.log("ccdeck: auto-restored %s (%s)" % (rec["name"], st.reason))
        except CCDeckError as e:
            self.log("ccdeck: auto-restore of %s failed: %s" % (rec["name"], e))
        self._restart_times[rec["name"]] = hist

    def _refresh_session_id(self, rec, rt, now, w, records):
        if now - rt.get("sid_checked", 0) < float(w.get("session_id_refresh", 30)):
            return
        rt["sid_checked"] = now
        if not claude.is_claude_cmd(rec.get("cmd", "")):
            return
        others = [r.get("claude_session_id") for r in records
                  if r["name"] != rec["name"] and r.get("cwd") == rec["cwd"]]
        sid = claude.guess_session_id(rec["cwd"], exclude=others, since=rec.get("launched_at"),
                                      current=rec.get("claude_session_id"))
        if sid and sid != rec.get("claude_session_id"):
            try:
                self.m.store.update(rec["name"], claude_session_id=sid)
            except CCDeckError:
                pass
