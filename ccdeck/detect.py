"""Pure functions: session state classification and rate-limit message parsing."""
from __future__ import annotations

import datetime as dt
import os
import re
from collections import namedtuple

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

STATES = ("running", "idle", "waiting-input", "rate-limited", "dead")
SHELLS = {"bash", "zsh", "sh", "dash", "fish", "ksh", "tcsh", "csh", "login"}

Status = namedtuple("Status", "state reason silent_seconds stalled rate_limit_reset")
RateLimit = namedtuple("RateLimit", "line reset_at")

TAIL_LINES = 12

_RUNNING = [re.compile(p, re.I) for p in (
    r"esc to interrupt",
    r"ctrl\+c to interrupt",
)]

_WAITING = [re.compile(p, re.I) for p in (
    r"do you want to (proceed|make this edit|create|run|allow|continue|overwrite|delete|use)",
    r"^\s*[❯>›]\s*1\.\s*yes\b",
    r"no, and tell claude",
    r"\(y/n\)|\[y/n\]",
    r"press enter to (continue|confirm|retry)",
    r"would you like to (proceed|continue)",
    r"waiting for (your )?(input|confirmation|approval)",
)]

_RATE = [re.compile(p, re.I) for p in (
    r"usage limit reached",
    r"(5-hour|five-hour|weekly|opus|daily|session)\s+limit\s+reached",
    r"\blimit reached\b.*\breset",
    r"hit your (usage |session |weekly )?limit",
    r"limit will reset",
    r"rate[ _-]?limit(ed| reached| exceeded|_error)",
    r"\b(api error|error)\b.*\b429\b",
    r"too many requests",
    r"out of (extra )?usage",
)]

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

_EPOCH = re.compile(r"\|\s*(\d{10})\b")
_RELATIVE = re.compile(
    r"(?:try again|retry|resets?|available)\s+in\s+"
    r"(?:(?P<h>\d+)\s*h(?:ours?|rs?)?\s*)?(?:(?P<m>\d+)\s*m(?:in(?:ute)?s?)?\s*)?(?:(?P<s>\d+)\s*s(?:ec(?:ond)?s?)?)?",
    re.I)
_RESET = re.compile(
    r"reset(?:s|ting)?\s*(?:at|on)?\s*"
    r"(?:(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s*(?:at\s+)?)?"
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ampm>[ap]\.?\s?m\b\.?)?"
    r"(?:\s*\((?P<tz>[A-Za-z][A-Za-z0-9_+\-]*(?:/[A-Za-z0-9_+\-]+)*)\))?",
    re.I)


def tail(text, n=TAIL_LINES):
    lines = [ln.rstrip() for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln.strip()]
    return lines[-n:]


def _tz(name, fallback):
    if name and ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return fallback


def parse_reset_time(line, now):
    """Parse the reset time out of a limit message. `now` must be timezone-aware."""
    m = _EPOCH.search(line)
    if m:
        return dt.datetime.fromtimestamp(int(m.group(1)), tz=now.tzinfo)
    m = _RELATIVE.search(line)
    if m and any(m.group(k) for k in ("h", "m", "s")):
        return now + dt.timedelta(hours=int(m.group("h") or 0), minutes=int(m.group("m") or 0),
                                  seconds=int(m.group("s") or 0))
    for m in _RESET.finditer(line):
        ampm, minute = m.group("ampm"), m.group("m")
        if not ampm and minute is None:
            continue  # "resets 5" is ambiguous
        h, mi = int(m.group("h")), int(minute or 0)
        if ampm:
            if not 1 <= h <= 12:
                continue
            h = h % 12 + (12 if ampm.lower().startswith("p") else 0)
        if h > 23 or mi > 59:
            continue
        tz = _tz(m.group("tz"), now.tzinfo)
        base = now.astimezone(tz)
        if m.group("mon"):
            mon, day = _MONTHS[m.group("mon").lower()[:3]], int(m.group("day"))
            try:
                cand = base.replace(month=mon, day=day, hour=h, minute=mi, second=0, microsecond=0)
            except ValueError:
                continue
            if cand < base - dt.timedelta(days=1):
                cand = cand.replace(year=cand.year + 1)
        else:
            cand = base.replace(hour=h, minute=mi, second=0, microsecond=0)
            if cand < base - dt.timedelta(minutes=15):
                cand += dt.timedelta(days=1)
        return cand
    return None


def parse_rate_limit(text, now, n=TAIL_LINES):
    """Look for a usage / rate limit message in the last `n` non-empty lines.

    Returns RateLimit(line, reset_at|None) or None."""
    lines = tail(text, n)
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i]
        if any(p.search(line) for p in _RATE):
            # the reset time is sometimes on the following line
            ctx = " ".join(lines[i:i + 3])
            return RateLimit(line.strip(), parse_reset_time(ctx, now))
    return None


def is_running_marker(text):
    return any(p.search(ln) for ln in tail(text, 8) for p in _RUNNING)


def is_waiting(text):
    return any(p.search(ln) for ln in tail(text, TAIL_LINES) for p in _WAITING)


def launched_cmd_is_shell(cmd):
    tok = (cmd or "").strip().split(" ", 1)[0]
    return os.path.basename(tok) in SHELLS


def classify(*, exists, pane_dead, current_command, cmd, text, now, last_change, launched_at,
             idle_seconds=20, stall_seconds=900, now_dt=None):
    """Return Status for one session.

    exists          tmux session exists
    pane_dead       tmux reports the pane's process exited (remain-on-exit)
    current_command pane_current_command (foreground process name)
    cmd             the command ccdeck launched (used to decide whether a bare shell means dead)
    text            visible pane text
    now             epoch seconds;  last_change / launched_at epoch seconds or None
    """
    silent = max(0.0, now - last_change) if last_change else 0.0
    if not exists:
        return Status("dead", "tmux session missing", silent, False, None)
    if pane_dead:
        return Status("dead", "process exited", silent, False, None)
    if (current_command in SHELLS and not launched_cmd_is_shell(cmd)
            and launched_at is not None and now - launched_at > 15):
        return Status("dead", "returned to shell", silent, False, None)
    stalled = silent >= stall_seconds
    now_dt = now_dt or dt.datetime.fromtimestamp(now).astimezone()
    running_marker = is_running_marker(text)
    if not running_marker:
        rl = parse_rate_limit(text, now_dt)
        if rl is not None:
            stale = rl.reset_at is not None and rl.reset_at < now_dt - dt.timedelta(minutes=15)
            if not stale:
                return Status("rate-limited", rl.line[:200], silent, stalled, rl.reset_at)
        if is_waiting(text):
            return Status("waiting-input", "prompt detected", silent, stalled, None)
    if running_marker or silent < idle_seconds:
        return Status("running", "working" if running_marker else "recent output", silent, stalled, None)
    return Status("idle", "no output for %ds" % silent, silent, stalled, None)
