"""Recovery logic: session-id discovery, launch command planning, startup reconciliation, watchdog."""
import datetime as dt
import os
import time

import pytest

from ccdeck import CCDeckError, claude
from ccdeck.manager import Manager
from ccdeck.tmux import PaneInfo
from ccdeck.watchdog import Watchdog

SID_A = "11111111-1111-4111-8111-111111111111"
SID_B = "22222222-2222-4222-8222-222222222222"
SID_C = "33333333-3333-4333-8333-333333333333"


def transcript(home, cwd, sid, mtime):
    d = os.path.join(home, "projects", claude.encode_cwd(cwd))
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, sid + ".jsonl")
    open(p, "w").write("{}\n")
    os.utime(p, (mtime, mtime))
    return p


# ---------------------------------------------------------------- claude helpers
def test_encode_cwd():
    assert claude.encode_cwd("/home/u/my.proj_x") == "-home-u-my-proj-x"


def test_list_and_guess(tmp_path):
    home, cwd = str(tmp_path), "/work/app"
    transcript(home, cwd, SID_A, 1000)
    transcript(home, cwd, SID_B, 2000)
    assert [s for s, _ in claude.list_transcripts(cwd, home)] == [SID_B, SID_A]
    assert claude.guess_session_id(cwd, home=home) == SID_B
    assert claude.guess_session_id(cwd, exclude=[SID_B], home=home) == SID_A
    # nothing modified since `since` → keep current id
    assert claude.guess_session_id(cwd, since=5000, current=SID_A, home=home) == SID_A
    assert claude.guess_session_id("/nowhere", home=home) is None


def test_strip_resume_flags():
    argv = ["--resume", SID_A, "--model", "opus", "-c", "--session-id", SID_B, "-r", "--verbose"]
    assert claude.strip_resume_flags(argv) == ["--model", "opus", "--verbose"]


def test_with_resume_keeps_other_flags_and_env_prefix():
    cmd = "FOO=1 claude --dangerously-skip-permissions --continue"
    assert claude.with_resume(cmd, SID_A) == "FOO=1 claude --resume %s --dangerously-skip-permissions" % SID_A


def test_with_resume_rejects_garbage():
    with pytest.raises(ValueError):
        claude.with_resume("claude", "x; rm -rf ~")


def test_plan_new_uses_session_id_flag(tmp_path):
    cmd, sid, how = claude.plan_launch("claude --model opus", mode="new", cwd="/w", use_session_id_flag=True,
                                       home=str(tmp_path))
    assert how == "new" and claude.UUID_RE.match(sid) and cmd == "claude --session-id %s --model opus" % sid


def test_plan_new_without_flag_support(tmp_path):
    assert claude.plan_launch("claude", mode="new", cwd="/w", home=str(tmp_path))[:2] == ("claude", None)


def test_plan_resume_known_id(tmp_path):
    home = str(tmp_path)
    transcript(home, "/w", SID_A, 1000)
    transcript(home, "/w", SID_B, 2000)
    cmd, sid, how = claude.plan_launch("claude", mode="resume", cwd="/w", sid=SID_A, home=home)
    assert (cmd, sid, how) == ("claude --resume " + SID_A, SID_A, "resume")


def test_plan_resume_guesses_latest_unclaimed(tmp_path):
    home = str(tmp_path)
    transcript(home, "/w", SID_A, 1000)
    transcript(home, "/w", SID_B, 2000)
    cmd, sid, how = claude.plan_launch("claude", mode="resume", cwd="/w", sid=None, exclude=[SID_B], home=home)
    assert (sid, how) == (SID_A, "resume-guess")


def test_plan_resume_falls_back_to_continue(tmp_path):
    home = str(tmp_path)
    transcript(home, "/w", SID_A, 1000)
    cmd, sid, how = claude.plan_launch("claude -r " + SID_C, mode="resume", cwd="/w", sid=SID_C,
                                       exclude=[SID_A], home=home)
    assert (cmd, sid, how) == ("claude --continue", None, "continue")


def test_plan_resume_without_any_transcript_starts_fresh(tmp_path):
    cmd, sid, how = claude.plan_launch("claude", mode="resume", cwd="/w", sid=SID_C, use_session_id_flag=True,
                                       home=str(tmp_path))
    assert (cmd, how) == ("claude --session-id " + SID_C, "new")


def test_plan_plain_command_is_rerun_as_is(tmp_path):
    assert claude.plan_launch("htop -d 5", mode="resume", cwd="/w", home=str(tmp_path)) == \
        ("htop -d 5", None, "plain")


# ---------------------------------------------------------------- manager with a fake tmux
class FakeTmux:
    socket = "fake"

    def __init__(self):
        self.sessions = {}  # name -> dict(dead, cmd, cwd)
        self.screen = {}
        self.sent = []
        self.launches = []

    def ensure_server(self):
        pass

    def has_session(self, name):
        return name in self.sessions

    def new_session(self, name, cwd, argv, env=None):
        self.sessions[name] = dict(dead=False, argv=argv, cwd=cwd)
        self.launches.append(("new", name, argv[-1]))

    def respawn(self, name, cwd, argv, env=None):
        self.sessions[name] = dict(dead=False, argv=argv, cwd=cwd)
        self.launches.append(("respawn", name, argv[-1]))

    def kill_session(self, name):
        self.sessions.pop(name, None)

    def rename_session(self, old, new):
        self.sessions[new] = self.sessions.pop(old)

    def run(self, *a, **k):
        pass

    def panes(self):
        return {n: PaneInfo(n, s["dead"], 0 if s["dead"] else None, "claude", 1, 0, s["cwd"])
                for n, s in self.sessions.items()}

    def capture(self, name, lines=None, join=False):
        return self.screen.get(name, "")

    def send_text(self, name, text):
        self.sent.append((name, "text", text))

    def send_key(self, name, key):
        self.sent.append((name, "key", key))


@pytest.fixture
def mgr(env):
    m = Manager(tmux=FakeTmux())
    return m


def test_create_records_session_id(mgr, env):
    rec = mgr.create("api", cwd=str(env))
    assert claude.UUID_RE.match(rec["claude_session_id"])
    assert "--session-id " + rec["claude_session_id"] in mgr.tmux.launches[-1][2]
    assert mgr.store.get("api")["cwd"] == str(env)


def test_create_rejects_bad_names_and_dirs(mgr, env):
    for bad in ("a b", "x;rm", "", "a" * 33, "../x"):
        with pytest.raises(CCDeckError):
            mgr.create(bad, cwd=str(env))
    with pytest.raises(CCDeckError):
        mgr.create("ok", cwd=str(env / "missing"))


def test_reconcile_restores_dead_sessions(mgr, env):
    home = os.environ["CLAUDE_CONFIG_DIR"]
    a = mgr.create("a", cwd=str(env))
    b = mgr.create("b", cwd=str(env), auto_restore=False)
    mgr.create("c", cwd=str(env))
    mgr.create("d", cwd=str(env))
    transcript(home, str(env), a["claude_session_id"], time.time())
    mgr.tmux.sessions["a"]["dead"] = True     # claude crashed, pane kept (remain-on-exit)
    mgr.tmux.sessions.pop("b")                 # tmux session vanished, auto_restore off
    mgr.stop("c")                              # stopped by the user
    restored = mgr.reconcile(log=lambda m: None)
    assert restored == ["a"]
    kind, name, cmd = mgr.tmux.launches[-1]
    assert (kind, name) == ("respawn", "a") and "--resume " + a["claude_session_id"] in cmd
    assert "b" not in mgr.tmux.sessions and mgr.store.get("b")["state"] == "dead"
    assert "c" not in mgr.tmux.sessions
    assert mgr.store.get("a")["restarts"] == 1 and not mgr.store.get("a")["stopped"]


def test_reconcile_after_tmux_server_loss_recreates_everything(mgr, env):
    home = os.environ["CLAUDE_CONFIG_DIR"]
    recs = [mgr.create(n, cwd=str(env)) for n in ("x", "y")]
    for r in recs:
        transcript(home, str(env), r["claude_session_id"], time.time())
    mgr.tmux.sessions.clear()  # e.g. machine reboot
    assert sorted(mgr.reconcile(log=lambda m: None)) == ["x", "y"]
    launched = {n: c for k, n, c in mgr.tmux.launches[-2:] if k == "new"}
    for r in recs:
        assert "--resume " + r["claude_session_id"] in launched[r["name"]]


def test_resume_refuses_live_session(mgr, env):
    mgr.create("live", cwd=str(env))
    with pytest.raises(CCDeckError):
        mgr.resume("live")


def test_rename_and_send(mgr, env):
    mgr.create("old", cwd=str(env))
    mgr.rename("old", "new")
    assert mgr.store.get("old") is None and mgr.store.get("new") and "new" in mgr.tmux.sessions
    mgr.send("new", text="continue", enter=True)
    mgr.send("new", key="esc")
    assert mgr.tmux.sent == [("new", "text", "continue"), ("new", "key", "Enter"), ("new", "key", "Escape")]
    with pytest.raises(CCDeckError):
        mgr.send("new", key="kill-server")


# ---------------------------------------------------------------- watchdog
class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def test_watchdog_auto_continue_after_rate_limit_reset(mgr, env):
    mgr.create("rl", cwd=str(env), auto_continue={"rate_limit": True})
    reset = dt.datetime.now().astimezone().replace(microsecond=0) + dt.timedelta(minutes=30)
    mgr.tmux.screen["rl"] = "Claude usage limit reached. Your limit will reset at %s.\n> " % reset.strftime("%H:%M")
    clock = Clock(time.time())
    wd = Watchdog(mgr, log=lambda m: None, clock=clock)
    wd.tick()
    assert mgr.store.get("rl")["state"] == "rate-limited"
    assert mgr.tmux.sent == []
    clock.t = reset.replace(second=0).timestamp() + 61  # margin 60s
    wd.tick()
    assert mgr.tmux.sent == [("rl", "text", "continue"), ("rl", "key", "Enter")]
    wd.tick()  # only once per reset
    assert len(mgr.tmux.sent) == 2


def test_watchdog_does_not_auto_continue_without_opt_in(mgr, env):
    mgr.create("rl2", cwd=str(env))
    mgr.tmux.screen["rl2"] = "Claude usage limit reached. Your limit will reset at 01:00.\n"
    clock = Clock(time.time())
    wd = Watchdog(mgr, log=lambda m: None, clock=clock)
    wd.tick()
    clock.t += 2 * 86400
    wd.tick()
    assert mgr.tmux.sent == []


def test_watchdog_stall_detection_and_opt_in_send(mgr, env):
    mgr.create("st", cwd=str(env), auto_continue={"stall": True, "text": "go on"})
    mgr.tmux.screen["st"] = "> "
    clock = Clock(time.time())
    wd = Watchdog(mgr, log=lambda m: None, clock=clock)
    wd.tick()
    clock.t += 1000
    wd.tick()
    rows = {r["name"]: r for r in mgr.list_status()}
    assert rows["st"]["stalled"] and rows["st"]["state"] == "idle"
    assert mgr.tmux.sent == [("st", "text", "go on"), ("st", "key", "Enter")]
    clock.t += 1000
    wd.tick()  # same silent episode → no second send
    assert len(mgr.tmux.sent) == 2


def test_watchdog_marks_dead(mgr, env):
    mgr.create("dd", cwd=str(env))
    mgr.tmux.sessions["dd"]["dead"] = True
    Watchdog(mgr, log=lambda m: None).tick()
    assert mgr.store.get("dd")["state"] == "dead"


def test_watchdog_stale_limit_message_becomes_idle(mgr, env):
    mgr.create("old", cwd=str(env))
    now = dt.datetime.now().astimezone()
    reset = now + dt.timedelta(minutes=10)
    mgr.tmux.screen["old"] = "5-hour limit reached ∙ resets %s" % reset.strftime("%-I:%M%p").lower()
    clock = Clock(now.timestamp())
    wd = Watchdog(mgr, log=lambda m: None, clock=clock)
    wd.tick()
    assert mgr.store.get("old")["state"] == "rate-limited"
    clock.t += 3600  # message still on screen an hour after the reset
    wd.tick()
    assert mgr.store.get("old")["state"] == "idle"
