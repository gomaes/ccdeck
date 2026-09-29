"""ttyd process supervision."""
from __future__ import annotations

import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time


def ttyd_version(bin="ttyd"):
    try:
        p = subprocess.run([bin, "--version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", p.stdout)
    return tuple(int(x) for x in m.groups()) if m else None


def self_argv():
    """Command that re-invokes this ccdeck (zipapp, script or `python -m ccdeck`)."""
    exe = os.environ.get("CCDECK_SELF")
    if exe:
        return [exe]
    arg0 = os.path.abspath(sys.argv[0])
    if os.path.basename(arg0) == "__main__.py":
        return [sys.executable, "-m", "ccdeck"]
    return [sys.executable, arg0]


class TtydSupervisor:
    BASE_PATH = "/tty"

    def __init__(self, cfg, paths, log=None):
        self.cfg = cfg["ttyd"]
        self.paths = paths
        self.log = log or (lambda msg: print(msg, file=sys.stderr, flush=True))
        self.port = int(self.cfg["port"])
        self.user = "ccdeck"
        self.password = secrets.token_urlsafe(24) if self.cfg.get("credential", True) else None
        self.proc = None
        self._stop = threading.Event()
        self.pidfile = os.path.join(paths.run_dir, "ttyd.pid")

    @property
    def credential(self):
        return "%s:%s" % (self.user, self.password) if self.password else None

    def argv(self):
        bin = self.cfg.get("bin", "ttyd")
        argv = [bin, "-i", "127.0.0.1", "-p", str(self.port), "-b", self.BASE_PATH,
                "-P", str(int(self.cfg.get("ping_interval", 30))), "-a",
                "-t", "disableReconnect=true", "-t", "disableLeaveAlert=true",
                "-t", "titleFixed=ccdeck", "-t", "fontSize=%d" % int(self.cfg.get("font_size", 14))]
        ver = ttyd_version(bin)
        if ver is None or ver >= (1, 7, 0):
            argv.append("-W")  # 1.7+ is read-only unless -W
        if self.password:
            argv += ["-c", self.credential]
        argv += [str(a) for a in self.cfg.get("extra_args") or []]
        return argv + ["--"] + self_argv() + ["attach"]

    def _kill_stale(self):
        try:
            with open(self.pidfile) as f:
                pid = int(f.read().strip())
            with open("/proc/%d/cmdline" % pid, "rb") as f:
                cmdline = f.read()
            if b"ttyd" in cmdline and b"attach" in cmdline:
                os.kill(pid, signal.SIGTERM)
                time.sleep(0.5)
        except (OSError, ValueError):
            pass

    def _spawn(self):
        env = dict(os.environ)
        # the ttyd child runs `ccdeck attach`; make sure it finds this package
        pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if os.path.isdir(pkg_parent):
            env["PYTHONPATH"] = pkg_parent + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        self.proc = subprocess.Popen(self.argv(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, env=env, start_new_session=True)
        with open(self.pidfile, "w") as f:
            f.write(str(self.proc.pid))
        threading.Thread(target=self._drain, args=(self.proc,), daemon=True).start()

    def _drain(self, proc):
        for line in proc.stderr:
            line = line.decode("utf-8", "replace").rstrip()
            if line and ("ERROR" in line or "error" in line.lower()):
                self.log("ttyd: " + line)

    def start(self):
        self._kill_stale()
        self._spawn()
        threading.Thread(target=self._watch, name="ccdeck-ttyd", daemon=True).start()

    def _watch(self):
        backoff = 1
        while not self._stop.is_set():
            rc = self.proc.poll()
            if rc is None:
                self._stop.wait(1)
                backoff = 1
                continue
            if self._stop.is_set():
                break
            self.log("ttyd exited with %s; restarting in %ds" % (rc, backoff))
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 60)
            if not self._stop.is_set():
                try:
                    self._spawn()
                except OSError as e:
                    self.log("ttyd start failed: %s" % e)

    def stop(self):
        self._stop.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        try:
            os.unlink(self.pidfile)
        except OSError:
            pass
