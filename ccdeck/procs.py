"""Process helpers (Linux /proc): process trees of tmux panes and Remote Control servers."""
from __future__ import annotations

import os
import signal
import time
from collections import namedtuple

Proc = namedtuple("Proc", "pid ppid argv cwd")


def _read(pid):
    try:
        with open("/proc/%d/stat" % pid, "rb") as f:
            stat = f.read().decode("utf-8", "replace")
        # "pid (comm) state ppid ..." - comm may contain spaces/parentheses
        ppid = int(stat[stat.rindex(")") + 2:].split()[1])
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            argv = [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
        try:
            cwd = os.readlink("/proc/%d/cwd" % pid)
        except OSError:
            cwd = None
        return Proc(pid, ppid, argv, cwd)
    except (OSError, ValueError, IndexError):
        return None


def all_procs():
    out = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return out
    for n in names:
        if n.isdigit():
            p = _read(int(n))
            if p is not None:
                out[p.pid] = p
    return out


def _protected():
    """pids that must never be signalled: init, this process and its ancestors."""
    prot = {0, 1, os.getpid()}
    pid = os.getpid()
    for _ in range(64):
        p = _read(pid)
        if p is None or p.ppid in prot:
            break
        prot.add(p.ppid)
        pid = p.ppid
    return prot


def _same_user(pid):
    try:
        return os.stat("/proc/%d" % pid).st_uid == os.getuid()
    except OSError:
        return False


def tree(root, procs=None):
    """root pid + all descendants (pids). Empty for pid <= 1 (would be the whole system)."""
    if not isinstance(root, int) or root <= 1:
        return []
    procs = procs if procs is not None else all_procs()
    children = {}
    for p in procs.values():
        children.setdefault(p.ppid, []).append(p.pid)
    out, stack = [], [root]
    while stack:
        pid = stack.pop()
        if pid in out:
            continue
        out.append(pid)
        stack.extend(children.get(pid, []))
    return out


def snapshot(pids):
    """{pid: argv} used to make sure we only signal the same processes later.

    Never includes init, ccdeck itself / its ancestors, or other users' processes."""
    snap = {}
    prot = _protected()
    for pid in pids:
        if pid in prot or not _same_user(pid):
            continue
        p = _read(pid)
        if p is not None:
            snap[pid] = p.argv
    return snap


def terminate(snap, timeout=3.0):
    """SIGTERM (then SIGKILL) processes from `snap` that are still alive. Returns pids signalled."""
    def alive():
        return [pid for pid, argv in snap.items() if (_read(pid) or Proc(0, 0, None, None)).argv == argv]

    prot = _protected()
    snap = {pid: argv for pid, argv in snap.items() if pid not in prot}
    left = alive()
    for pid in left:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    end = time.time() + timeout
    while time.time() < end and alive():
        time.sleep(0.1)
    for pid in alive():
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return left


def is_remote_control(argv):
    """`claude rc` / `claude remote-control` server (also `claude --rc` interactive)."""
    for i, tok in enumerate(argv):
        base = os.path.basename(tok)
        if base == "claude" or (base in ("node", "bun") and i + 1 < len(argv) and "claude" in argv[i + 1]):
            rest = argv[i + 1:]
            return any(t in ("rc", "remote-control", "--rc", "--remote-control") or
                       t.startswith("--remote-control=") or t.startswith("--rc=") for t in rest)
    return False


def remote_control_servers(procs=None):
    procs = procs if procs is not None else all_procs()
    found = [p for p in procs.values() if is_remote_control(p.argv)]
    # report only the top-most rc process of each tree
    pids = {p.pid for p in found}
    return [p for p in found if p.ppid not in pids]
