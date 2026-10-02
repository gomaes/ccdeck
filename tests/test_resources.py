import os
import subprocess
import sys
import time

from ccdeck import resources


def test_cpu_sampler_measures_a_busy_process():
    p = subprocess.Popen([sys.executable, "-c", "while True: pass"])
    try:
        s = resources.CpuSampler()
        assert s.sample("k", [p.pid]) is None  # first sample has no baseline
        time.sleep(1.0)
        pct = s.sample("k", [p.pid])
        assert 50 <= pct <= 110, pct
        mem, kind = resources.memory(p.pid)
        assert mem > 1024 * 1024 and kind in ("pss", "rss")
    finally:
        p.kill()
        p.wait()


def test_idle_process_and_new_pids_do_not_spike():
    idle = subprocess.Popen(["sleep", "30"])
    busy = subprocess.Popen([sys.executable, "-c", "import time\nt=time.time()\nwhile time.time()-t<0.5: pass\nimport time; time.sleep(30)"])
    try:
        s = resources.CpuSampler()
        s.sample("k", [idle.pid])
        time.sleep(0.8)
        # busy appears only now: its earlier CPU time must not be counted in this interval
        assert s.sample("k", [idle.pid, busy.pid]) < 10
    finally:
        for p in (idle, busy):
            p.kill()
            p.wait()


def test_dir_usage(tmp_path):
    (tmp_path / "a").write_bytes(b"x" * 100_000)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b").write_bytes(b"y" * 50_000)
    os.link(tmp_path / "a", tmp_path / "a-hardlink")        # counted once
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    (outside / "big").write_bytes(b"z" * 1_000_000)
    os.symlink(outside, tmp_path / "link")                   # not followed
    size, files, complete = resources.dir_usage(str(tmp_path))
    assert complete and files == 4  # a, sub, sub/b, link (the hard link is the same file as a)
    assert 150_000 <= size < 400_000
    assert resources.dir_usage(str(tmp_path / "missing"))[0] is None


def test_dir_usage_time_limit(tmp_path):
    for i in range(50):
        (tmp_path / str(i)).mkdir()
    clock = iter(range(1000)).__next__  # every call advances 1s
    size, files, complete = resources.dir_usage(str(tmp_path), max_seconds=3, clock=clock)
    assert complete is False


def test_host_stats():
    total, avail = resources.host_memory()
    assert total > avail > 0
    h = resources.HostCpu()
    assert h.sample() is None
    time.sleep(0.2)
    v = h.sample()
    assert v is None or 0 <= v <= 100


def test_disk_scanner_only_rescans_when_due(tmp_path):
    import threading

    class M:
        lock = threading.RLock()
        runtime = {}

        class store:
            @staticmethod
            def all():
                return [{"name": "a", "cwd": str(tmp_path)}]

    sc = resources.DiskScanner(M, interval=60)
    sc.scan_once(only_due=True)
    first = M.runtime["a"]["disk"]["at"]
    (tmp_path / "f").write_bytes(b"x" * 10000)
    sc.scan_once(only_due=True)      # not due yet: unchanged
    assert M.runtime["a"]["disk"]["at"] == first
    sc.scan_once()                   # forced
    assert M.runtime["a"]["disk"]["bytes"] >= 10000
