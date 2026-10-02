"""Resource usage of sessions (Linux /proc, stdlib only).

- CPU:    utime+stime of every process in the pane's process tree; percent of one core
          between two samples (100% = one core busy)
- memory: sum of PSS (/proc/<pid>/smaps_rollup; shared pages are split between the
          processes instead of counted twice), falling back to RSS
- disk:   allocated size of the session's root directory (du-like: st_blocks, hard links
          counted once, symlinks not followed, does not cross file systems), computed in a
          background thread because it can be slow
"""
from __future__ import annotations

import os
import threading
import time

CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
NCPU = os.cpu_count() or 1


def cpu_ticks(pid):
    """utime + stime of one process (clock ticks), or None."""
    try:
        with open("/proc/%d/stat" % pid, "rb") as f:
            stat = f.read().decode("utf-8", "replace")
        fields = stat[stat.rindex(")") + 2:].split()
        return int(fields[11]) + int(fields[12])  # utime, stime (fields 14, 15)
    except (OSError, ValueError, IndexError):
        return None


def memory(pid):
    """(bytes, kind) — PSS when available, else RSS."""
    try:
        with open("/proc/%d/smaps_rollup" % pid, "rb") as f:
            for line in f:
                if line.startswith(b"Pss:"):
                    return int(line.split()[1]) * 1024, "pss"
    except (OSError, ValueError, IndexError):
        pass
    try:
        with open("/proc/%d/statm" % pid) as f:
            return int(f.read().split()[1]) * PAGE, "rss"
    except (OSError, ValueError, IndexError):
        return 0, "rss"


class CpuSampler:
    """Keeps the previous tick counts per pid so that consecutive samples give a percentage."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.prev = {}  # key -> (time, {pid: ticks})

    def sample(self, key, pids):
        now = self.clock()
        cur = {}
        for pid in pids:
            t = cpu_ticks(pid)
            if t is not None:
                cur[pid] = t
        prev = self.prev.get(key)
        self.prev[key] = (now, cur)
        if prev is None or now <= prev[0]:
            return None
        # processes that are new since the last sample count from 0 is wrong (their whole
        # lifetime would be attributed to this interval), so only count pids seen before
        used = sum(max(0, t - prev[1][pid]) for pid, t in cur.items() if pid in prev[1])
        return round(100.0 * used / CLK_TCK / (now - prev[0]), 1)

    def forget(self, keep):
        for k in list(self.prev):
            if k not in keep:
                del self.prev[k]


def tree_usage(pids, sampler, key):
    mem, kind = 0, "pss"
    for pid in pids:
        b, k = memory(pid)
        mem += b
        if k == "rss":
            kind = "rss"
    return {"cpu_percent": sampler.sample(key, pids), "mem_bytes": mem, "mem_kind": kind,
            "procs": len(pids)}


def dir_usage(path, max_seconds=20.0, clock=time.monotonic):
    """(bytes, files, complete) like `du -sx`; stops after max_seconds (complete=False)."""
    try:
        root_dev = os.lstat(path).st_dev
    except OSError:
        return None, 0, False
    end = clock() + max_seconds
    total, files, seen, stack = 0, 0, set(), [path]
    while stack:
        if clock() > end:
            return total, files, False
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                if st.st_dev != root_dev:
                    continue
                if st.st_nlink > 1:
                    ino = (st.st_dev, st.st_ino)
                    if ino in seen:
                        continue
                    seen.add(ino)
                total += st.st_blocks * 512
                files += 1
                if e.is_dir(follow_symlinks=False):
                    stack.append(e.path)
    try:
        total += os.lstat(path).st_blocks * 512
    except OSError:
        pass
    return total, files, True


def host_memory():
    """(total, available) bytes from /proc/meminfo."""
    vals = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                vals[k] = int(v.split()[0]) * 1024
    except (OSError, ValueError):
        return None, None
    return vals.get("MemTotal"), vals.get("MemAvailable")


class HostCpu:
    """Whole-machine CPU busy percent (all cores = 100%) from /proc/stat deltas."""

    def __init__(self):
        self.prev = None

    def sample(self):
        try:
            with open("/proc/stat") as f:
                parts = [int(x) for x in f.readline().split()[1:]]
        except (OSError, ValueError):
            return None
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
        total = sum(parts[:8])
        prev, self.prev = self.prev, (total, idle)
        if prev is None or total <= prev[0]:
            return None
        return round(100.0 * (1 - (idle - prev[1]) / (total - prev[0])), 1)


class DiskScanner:
    """Background thread recomputing each session's directory size every `interval` seconds."""

    def __init__(self, manager, interval=60.0, max_seconds=20.0, log=None):
        self.m = manager
        self.interval = float(interval)
        self.max_seconds = float(max_seconds)
        self.log = log or (lambda msg: None)
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._loop, name="ccdeck-disk", daemon=True).start()

    def stop(self):
        self._stop.set()

    TICK = 5.0  # how often to look for sessions that are due (new sessions: within seconds)

    def scan_once(self, only_due=False):
        now = time.time()
        for rec in self.m.store.all():
            if self._stop.is_set():
                return
            cwd = rec.get("cwd")
            if not cwd or not os.path.isdir(cwd):
                continue
            if only_due:
                with self.m.lock:
                    last = (self.m.runtime.get(rec["name"], {}).get("disk") or {}).get("at")
                if last is not None and now - last < self.interval:
                    continue
            size, files, complete = dir_usage(cwd, self.max_seconds)
            with self.m.lock:
                rt = self.m.runtime.setdefault(rec["name"], {})
                rt["disk"] = {"bytes": size, "files": files, "complete": complete, "at": time.time()}

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.scan_once(only_due=True)
            except Exception as e:  # never kill the thread
                self.log("disk scan error: %r" % (e,))
            self._stop.wait(min(self.TICK, self.interval))
