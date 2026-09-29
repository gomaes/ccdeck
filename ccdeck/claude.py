"""Claude Code specific helpers: session-id discovery and launch command building."""
from __future__ import annotations

import functools
import glob
import os
import re
import shlex
import subprocess
import uuid

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# flags removed before adding our own resume flags; value = takes an argument
_RESUME_FLAGS = {"--resume": "opt", "-r": "opt", "--continue": False, "-c": False, "--session-id": True}


def claude_home():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def encode_cwd(cwd):
    """Claude Code stores transcripts in ~/.claude/projects/<cwd with non-alnum replaced by '-'>."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def project_dirs(cwd, home=None):
    base = os.path.join(home or claude_home(), "projects")
    cands = [encode_cwd(cwd), cwd.replace("/", "-")]
    seen, out = set(), []
    for c in cands:
        p = os.path.join(base, c)
        if p not in seen and os.path.isdir(p):
            seen.add(p)
            out.append(p)
    return out


def list_transcripts(cwd, home=None):
    """[(session_id, mtime)] newest first."""
    out = {}
    for d in project_dirs(cwd, home):
        for f in glob.glob(os.path.join(d, "*.jsonl")):
            sid = os.path.basename(f)[:-6]
            if not UUID_RE.match(sid):
                continue
            try:
                mt = os.path.getmtime(f)
            except OSError:
                continue
            out[sid] = max(mt, out.get(sid, 0))
    return sorted(out.items(), key=lambda kv: kv[1], reverse=True)


def transcript_exists(cwd, sid, home=None):
    return bool(sid) and any(s == sid for s, _ in list_transcripts(cwd, home))


def guess_session_id(cwd, exclude=(), since=None, current=None, home=None):
    """Estimate the claude session id for a ccdeck session running in `cwd`.

    - ids used by other ccdeck sessions (`exclude`) are skipped
    - if `since` is given, only transcripts modified after it are considered
    - the current id is kept unless a newer unclaimed transcript exists (e.g. after /clear)
    """
    exclude = set(x for x in exclude if x)
    for sid, mt in list_transcripts(cwd, home):
        if sid in exclude:
            continue
        if since is not None and mt < since - 5:
            break
        return sid
    return current if current and transcript_exists(cwd, current, home) else None


def _split(cmd):
    return shlex.split(cmd)


def claude_index(argv):
    """Index of the `claude` executable in argv (skipping VAR=value prefixes and `env`)."""
    for i, tok in enumerate(argv):
        if _ENV_ASSIGN.match(tok) or tok == "env":
            continue
        return i if os.path.basename(tok) == "claude" else None
    return None


def is_claude_cmd(cmd):
    try:
        return claude_index(_split(cmd)) is not None
    except ValueError:
        return False


def strip_resume_flags(argv):
    out, i = [], 0
    while i < len(argv):
        tok = argv[i]
        name = tok.split("=", 1)[0]
        if name in _RESUME_FLAGS:
            kind = _RESUME_FLAGS[name]
            if "=" not in tok and i + 1 < len(argv):
                nxt = argv[i + 1]
                if kind is True or (kind == "opt" and not nxt.startswith("-") and UUID_RE.match(nxt)):
                    i += 1
            i += 1
            continue
        out.append(tok)
        i += 1
    return out


def has_resume_flags(cmd):
    try:
        argv = _split(cmd)
    except ValueError:
        return False
    return any(t.split("=", 1)[0] in _RESUME_FLAGS for t in argv)


def _with(cmd, extra):
    argv = _split(cmd)
    idx = claude_index(argv)
    if idx is None:
        return cmd
    head, tail = argv[: idx + 1], strip_resume_flags(argv[idx + 1:])
    return shlex.join(head + list(extra) + tail)


def with_resume(cmd, sid):
    if not UUID_RE.match(sid or ""):
        raise ValueError("bad session id %r" % (sid,))
    return _with(cmd, ["--resume", sid])


def with_continue(cmd):
    return _with(cmd, ["--continue"])


def with_session_id(cmd, sid):
    if not UUID_RE.match(sid or ""):
        raise ValueError("bad session id %r" % (sid,))
    return _with(cmd, ["--session-id", sid])


def fresh(cmd):
    return _with(cmd, [])


def new_session_id():
    return str(uuid.uuid4())


@functools.lru_cache(maxsize=8)
def supports_session_id_flag(bin="claude"):
    try:
        p = subprocess.run([bin, "--help"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, timeout=20)
        return "--session-id" in p.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False


def plan_launch(cmd, *, mode, cwd, sid=None, exclude=(), use_session_id_flag=False, home=None):
    """Decide the actual command line to run.

    mode = "new":    start fresh (adds --session-id <uuid> when supported)
    mode = "resume": --resume <known id> → --resume <guessed id> → --continue → fresh
    Returns (command, session_id or None, how).
    """
    if not is_claude_cmd(cmd):
        return cmd, sid, "plain"
    if mode == "new":
        if has_resume_flags(cmd) or not use_session_id_flag:
            return cmd, sid, "as-is"
        nsid = new_session_id()
        return with_session_id(cmd, nsid), nsid, "new"
    if sid and transcript_exists(cwd, sid, home):
        return with_resume(cmd, sid), sid, "resume"
    guess = guess_session_id(cwd, exclude=exclude, home=home)
    if guess:
        return with_resume(cmd, guess), guess, "resume-guess"
    if list_transcripts(cwd, home):
        return with_continue(cmd), None, "continue"
    if use_session_id_flag:
        nsid = sid if sid and UUID_RE.match(sid) else new_session_id()
        return with_session_id(cmd, nsid), nsid, "new"
    return fresh(cmd), None, "new"
