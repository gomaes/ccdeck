"""Integration test against a real tmux server (dedicated socket) with a fake long-running `claude`."""
import os
import shutil
import signal
import time

import pytest

from ccdeck import claude
from ccdeck.manager import Manager
from ccdeck.watchdog import Watchdog

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")


def wait_for(pred, timeout=10.0, step=0.1):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return pred()


def state(m, name):
    return {r["name"]: r for r in m.list_status()}[name]["state"]


@pytest.fixture
def mgr(env):
    m = Manager()
    yield m
    m.tmux.run("kill-server", check=False)


def test_dead_and_restore_with_real_tmux(mgr, env):
    work = env / "project"
    work.mkdir()
    log = env / "claude.log"
    rec = mgr.create("itest", cwd=str(work))
    sid = rec["claude_session_id"]
    assert claude.UUID_RE.match(sid)

    # the fake claude started in the right cwd with --session-id and wrote its transcript
    assert wait_for(lambda: log.exists() and "--session-id " + sid in log.read_text())
    assert wait_for(lambda: claude.transcript_exists(str(work), sid))
    assert wait_for(lambda: state(mgr, "itest") == "running")
    pane = mgr.tmux.panes()["itest"]
    assert pane.current_path == str(work)

    # 1) the process dies (crash) → pane kept by remain-on-exit → dead
    os.kill(pane.pid, signal.SIGKILL)
    assert wait_for(lambda: state(mgr, "itest") == "dead")
    assert mgr.tmux.panes()["itest"].dead

    # watchdog persists the state
    Watchdog(mgr, log=lambda m: None).tick()
    assert mgr.store.get("itest")["state"] == "dead"

    # resume → same cwd, `claude --resume <id>`
    rec = mgr.resume("itest")
    assert rec["last_launch_how"] == "resume" and rec["claude_session_id"] == sid
    assert wait_for(lambda: "--resume " + sid in log.read_text())
    assert wait_for(lambda: state(mgr, "itest") == "running")
    new_pane = mgr.tmux.panes()["itest"]
    assert new_pane.pid != pane.pid and new_pane.current_path == str(work)

    # 2) the whole tmux server disappears (e.g. reboot) → restored from sessions.json on serve start
    mgr.tmux.run("kill-server", check=False)
    assert wait_for(lambda: state(mgr, "itest") == "dead")
    assert mgr.reconcile(log=lambda m: None) == ["itest"]
    assert wait_for(lambda: log.read_text().count("--resume " + sid) == 2)
    assert wait_for(lambda: state(mgr, "itest") == "running")

    # capture / send keys work on the restored pane
    assert wait_for(lambda: "fake claude running" in mgr.log("itest", lines=100))
    mgr.send("itest", key="C-c")  # sleep gets SIGINT → sh exits → dead again
    assert wait_for(lambda: state(mgr, "itest") == "dead")

    # 3) a stopped session is not auto-restored
    mgr.stop("itest")
    assert mgr.reconcile(log=lambda m: None) == []
    assert not mgr.tmux.has_session("itest")

    mgr.delete("itest")
    assert mgr.store.get("itest") is None


def test_session_names_cannot_inject(mgr, env):
    from ccdeck import CCDeckError

    from ccdeck import NAME_RE

    # arbitrary display names are accepted, but ids stay ^[a-zA-Z0-9_-]{1,32}$ and nothing is executed
    for bad in ("x;touch pwned", "$(touch pwned2)", "`touch pwned3`", "a b", "../x", "x" * 40, "日本語"):
        rec = mgr.create(bad, cwd=str(env))
        assert NAME_RE.match(rec["name"]) and rec["title"] == bad
        assert mgr.tmux.has_session(rec["name"])
        mgr.delete(rec["name"])
    for bad in ("", "a\nb", "\x1b]0;x\x07"):
        with pytest.raises(CCDeckError):
            mgr.create(bad, cwd=str(env))
    for f in ("pwned", "pwned2", "pwned3"):
        assert not (env / f).exists()
    # names that look like options are passed safely as arguments
    mgr.create("-t", cwd=str(env))
    mgr.rename("-t", "-x")
    assert mgr.tmux.has_session("-x") and mgr.store.get("-x")
    mgr.delete("-x")
