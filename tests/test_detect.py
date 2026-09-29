import datetime as dt

from ccdeck.detect import classify, parse_rate_limit, parse_reset_time

JST = dt.timezone(dt.timedelta(hours=9))
NOW = 1_000_000.0


def cls(text="", **kw):
    args = dict(exists=True, pane_dead=False, current_command="claude", cmd="claude", text=text,
                now=NOW, last_change=NOW - 100, launched_at=NOW - 1000, idle_seconds=20, stall_seconds=900,
                now_dt=dt.datetime(2026, 9, 29, 10, 0, tzinfo=JST))
    args.update(kw)
    return classify(**args)


# ---------------------------------------------------------------- dead
def test_dead_when_tmux_session_missing():
    st = cls(exists=False)
    assert st.state == "dead" and "missing" in st.reason


def test_dead_when_pane_process_exited():
    assert cls(pane_dead=True).state == "dead"


def test_dead_when_returned_to_shell():
    assert cls(current_command="bash").state == "dead"


def test_shell_right_after_launch_is_not_dead():
    assert cls(current_command="bash", launched_at=NOW - 3).state != "dead"


def test_shell_is_fine_when_the_command_itself_is_a_shell():
    assert cls(current_command="bash", cmd="bash").state != "dead"


# ---------------------------------------------------------------- running / idle / stalled
def test_running_marker():
    text = "some output\n✻ Thinking… (12s · ↑ 1.2k tokens · esc to interrupt)\n"
    st = cls(text, last_change=NOW - 5000)
    assert st.state == "running" and st.stalled


def test_recent_output_is_running():
    assert cls("hello", last_change=NOW - 5).state == "running"


def test_idle_after_threshold():
    st = cls("╭────╮\n│ >  │\n╰────╯\n  ? for shortcuts", last_change=NOW - 60)
    assert st.state == "idle" and not st.stalled and st.silent_seconds == 60


def test_stalled_flag():
    st = cls("> ", last_change=NOW - 901)
    assert st.state == "idle" and st.stalled


# ---------------------------------------------------------------- waiting-input
def test_permission_prompt_is_waiting_input():
    text = """
 Bash command
   rm -rf build
 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and don't ask again for rm commands in /work
   3. No, and tell Claude what to do differently (esc)
"""
    assert cls(text).state == "waiting-input"


def test_yn_prompt_is_waiting_input():
    assert cls("Overwrite file? (y/n)").state == "waiting-input"


def test_answered_yn_prompt_is_not_waiting():
    # claude rc after answering its questions (screen from a real session)
    text = """
Trust /home/claude? [y/N] y

Enable Remote Control? (y/n) y

·✓· Connected · claude · HEAD
    Capacity: 1/32 · New sessions will be created in the current directory
Continue coding in the Claude mobile app or https://claude.ai/code?environment=env_x
space to show QR code
"""
    assert cls(text).state == "idle"
    assert cls("Enable Remote Control? (y/n) ").state == "waiting-input"
    assert cls("Trust /home/claude? [y/N]").state == "waiting-input"


def test_old_prompt_scrolled_away_is_not_waiting():
    text = "Do you want to proceed?\n" + "\n".join("line %d" % i for i in range(30))
    assert cls(text).state == "idle"


# ---------------------------------------------------------------- rate limit
def test_rate_limited_with_reset_time_and_tz():
    text = "⎿  Claude usage limit reached. Your limit will reset at 3pm (Asia/Tokyo).\n\n> "
    st = cls(text)
    assert st.state == "rate-limited"
    assert st.rate_limit_reset == dt.datetime(2026, 9, 29, 15, 0, tzinfo=JST)


def test_running_marker_wins_over_old_limit_text():
    text = "Claude usage limit reached. Your limit will reset at 3pm\n✻ Working… (esc to interrupt)"
    assert cls(text).state == "running"


def test_stale_limit_message_is_ignored():
    # reset was 1 hour ago (09:00 < 10:00 - 15min)
    text = "Weekly limit reached ∙ resets Sep 29, 9am (Asia/Tokyo)"
    assert cls(text).state == "idle"


def test_normal_text_is_not_rate_limited():
    text = "I added exponential backoff to handle the rate limiter config\n> "
    assert cls(text).state == "idle"


def now_jst(h=10, m=0):
    return dt.datetime(2026, 9, 29, h, m, tzinfo=JST)


def test_parse_reset_pm_same_day():
    rl = parse_rate_limit("Your limit will reset at 3pm (Asia/Tokyo).", now_jst())
    assert rl.reset_at == dt.datetime(2026, 9, 29, 15, 0, tzinfo=JST)


def test_parse_reset_rolls_to_next_day():
    rl = parse_rate_limit("5-hour limit reached ∙ resets 3am", now_jst(22))
    assert rl.reset_at == dt.datetime(2026, 9, 30, 3, 0, tzinfo=JST)


def test_parse_reset_just_passed_stays_today():
    rl = parse_rate_limit("5-hour limit reached ∙ resets 3pm", now_jst(15, 5))
    assert rl.reset_at == dt.datetime(2026, 9, 29, 15, 0, tzinfo=JST)


def test_parse_reset_with_minutes_and_other_tz():
    rl = parse_rate_limit("You've hit your limit · resets 4:30pm (America/New_York)", now_jst())
    assert rl.reset_at.utcoffset() == dt.timedelta(hours=-4)
    assert (rl.reset_at.hour, rl.reset_at.minute) == (16, 30)
    # 16:30 EDT on 9/29 == 05:30 JST on 9/30
    assert rl.reset_at.astimezone(JST) == dt.datetime(2026, 9, 30, 5, 30, tzinfo=JST)


def test_parse_reset_with_date():
    rl = parse_rate_limit("Weekly limit reached ∙ resets Oct 7, 1am (Asia/Tokyo)", now_jst())
    assert rl.reset_at == dt.datetime(2026, 10, 7, 1, 0, tzinfo=JST)


def test_parse_reset_date_next_year():
    rl = parse_rate_limit("Weekly limit reached ∙ resets Jan 3 at 9:00", now_jst())
    assert rl.reset_at == dt.datetime(2027, 1, 3, 9, 0, tzinfo=JST)


def test_parse_epoch_format():
    rl = parse_rate_limit("Claude AI usage limit reached|1790700000", now_jst())
    assert rl.reset_at.timestamp() == 1790700000


def test_parse_relative():
    rl = parse_rate_limit("API Error: Rate limit reached. Please try again in 1h 30m", now_jst())
    assert rl.reset_at == now_jst(11, 30)


def test_parse_24h():
    assert parse_reset_time("resets at 18:45", now_jst()) == now_jst(18, 45)


def test_limit_without_time():
    rl = parse_rate_limit("API Error: 429 Too Many Requests", now_jst())
    assert rl is not None and rl.reset_at is None


def test_reset_time_on_next_line():
    rl = parse_rate_limit("Claude usage limit reached.\nYour limit will reset at 5pm.", now_jst())
    assert rl.reset_at == now_jst(17)


def test_ambiguous_number_is_not_a_time():
    assert parse_reset_time("resets 5", now_jst()) is None
