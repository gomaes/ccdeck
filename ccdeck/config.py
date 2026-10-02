"""Paths, config.toml loading and first-run file generation."""
from __future__ import annotations

import copy
import os
import re
import secrets

DEFAULTS = {
    "server": {
        "bind": "0.0.0.0",
        "port": 8787,
        # Binding to anything other than a loopback address requires this to be true.
        "allow_external": True,
        "token": "",
        "cookie_secure": False,
    },
    "ttyd": {
        "bin": "ttyd",
        "port": 7682,
        "ping_interval": 30,
        # Protect ttyd itself with a random basic-auth credential (the proxy injects it).
        "credential": True,
        "font_size": 14,
        "extra_args": [],
    },
    "tmux": {
        "bin": "tmux",
        "socket": "ccdeck",
        "history_limit": 100000,
        # Start the tmux server in its own systemd --user scope so it survives
        # `systemctl --user restart ccdeck` and SSH logout.
        "systemd_scope": True,
    },
    "claude": {
        "bin": "claude",
        # Launch new sessions with `--session-id <uuid>` when claude supports it.
        "use_session_id_flag": True,
        # Pass the ccdeck name to claude: `--name <name>` (interactive display name /
        # `claude rc --name`, the session name shown on claude.ai/code).
        "name_sessions": True,
        # restart / stop / delete: send Ctrl+C and wait up to this many seconds for claude to
        # exit cleanly before killing it (0 = kill immediately)
        "exit_timeout": 10,
        # How the session command is run: [$SHELL, *shell_flags, cmd]
        "shell_flags": ["-lc"],
    },
    "defaults": {
        # default command for new sessions: a Remote Control server, reachable from claude.ai/code
        "cmd": "claude rc",
        "dir": "~",
        # new sessions without an explicit directory get <workspace_root>/<random>
        # (changeable from the web UI; stored in <data>/settings.json)
        "workspace_root": "~/claude",
        "auto_restore": True,
        "auto_continue_rate_limit": False,
        "auto_continue_stall": False,
        "continue_text": "continue",
    },
    "watchdog": {
        "interval": 5,
        "idle_seconds": 20,
        "stall_seconds": 900,
        "rate_limit_margin": 60,
        "rate_limit_unknown_retry": 1800,
        # Also auto-restore sessions that die while `serve` is running
        # (by default only done once at `serve` startup).
        "auto_restore_dead": False,
        "session_id_refresh": 30,
        # size of each session's root directory: recomputed every disk_interval seconds,
        # giving up (shown as "≥ size") after disk_max_seconds per directory
        "disk_interval": 60,
        "disk_max_seconds": 20,
    },
}

CONFIG_TEMPLATE = """\
# ccdeck configuration
# Changes take effect after `systemctl --user restart ccdeck`.

[server]
# Listen address. "0.0.0.0" = all interfaces (LAN / Tailscale). Use "127.0.0.1" for local only.
# ttyd itself always listens on 127.0.0.1 and is reachable only through the token-checking proxy.
bind = "0.0.0.0"
port = 8787
# Must be true to bind to a non-loopback address (safety switch).
allow_external = true
# Access token (Bearer header or cookie). Keep this file private (chmod 600).
token = "{token}"
# Set true when served through HTTPS (e.g. `tailscale serve`).
cookie_secure = false

[ttyd]
bin = "ttyd"
port = 7682
ping_interval = 30
credential = true
font_size = 14

[tmux]
bin = "tmux"
socket = "ccdeck"
history_limit = 100000
systemd_scope = true

[claude]
bin = "claude"
use_session_id_flag = true
# pass the ccdeck name to claude (--name): shown on claude.ai/code for `claude rc`
name_sessions = true
shell_flags = ["-lc"]

[defaults]
# default command for new sessions ("claude rc": usable from claude.ai/code and the Claude app too)
cmd = "claude rc"
dir = "~"
auto_restore = true
auto_continue_rate_limit = false
auto_continue_stall = false
continue_text = "continue"

[watchdog]
interval = 5
idle_seconds = 20
stall_seconds = 900
rate_limit_margin = 60
rate_limit_unknown_retry = 1800
auto_restore_dead = false
"""

TMUX_CONF_TEMPLATE = """\
# ccdeck tmux configuration (used only by `tmux -L {socket}`).
# Keep the server alive even when there are no sessions / clients.
set -g exit-empty off
set -g exit-unattached off
set -g destroy-unattached off
# Keep panes whose command exited, so ccdeck can detect `dead` and respawn in place.
setw -g remain-on-exit on
set -g history-limit {history_limit}
set -g mouse on
set -g default-terminal "tmux-256color"
set -sg escape-time 10
set -g focus-events on
# Browser (PC) and phone may attach at the same time with different sizes.
set -g window-size latest
setw -g aggressive-resize on
set -g set-clipboard on
set -g status-style "bg=colour236,fg=colour250"
set -g status-left "[ccdeck:#S] "
set -g status-left-length 40
set -g status-right "%H:%M"
"""


class Paths:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        home = os.path.expanduser("~")
        xdg_config = env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
        xdg_data = env.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
        self.config_dir = env.get("CCDECK_CONFIG_DIR") or os.path.join(xdg_config, "ccdeck")
        self.data_dir = env.get("CCDECK_DATA_DIR") or os.path.join(xdg_data, "ccdeck")
        self.config_file = os.path.join(self.config_dir, "config.toml")
        self.tmux_conf = os.path.join(self.config_dir, "tmux.conf")
        self.env_file = os.path.join(self.config_dir, "env")
        self.sessions_file = os.path.join(self.data_dir, "sessions.json")
        self.run_dir = os.path.join(self.data_dir, "run")
        self.systemd_unit = os.path.join(xdg_config, "systemd", "user", "ccdeck.service")


# --------------------------------------------------------------------------- TOML

def _load_toml_text(text):
    try:
        import tomllib  # Python 3.11+

        return tomllib.loads(text)
    except ImportError:
        pass
    for mod in ("tomli", "toml"):
        try:
            m = __import__(mod)
            return m.loads(text)
        except ImportError:
            continue
    return parse_toml_min(text)


class TomlError(ValueError):
    pass


_KEY_RE = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=\s*(.*)$")
_TABLE_RE = re.compile(r"^\s*\[\s*([A-Za-z0-9_.-]+)\s*\]\s*(#.*)?$")


def _parse_value(s, lineno):
    s = s.strip()
    if not s:
        raise TomlError("line %d: missing value" % lineno)
    if s[0] == '"':
        out, i = [], 1
        esc = {"n": "\n", "t": "\t", '"': '"', "\\": "\\", "r": "\r"}
        while i < len(s):
            c = s[i]
            if c == "\\" and i + 1 < len(s):
                out.append(esc.get(s[i + 1], s[i + 1]))
                i += 2
                continue
            if c == '"':
                return "".join(out), s[i + 1:]
            out.append(c)
            i += 1
        raise TomlError("line %d: unterminated string" % lineno)
    if s[0] == "'":
        end = s.find("'", 1)
        if end < 0:
            raise TomlError("line %d: unterminated string" % lineno)
        return s[1:end], s[end + 1:]
    if s[0] == "[":
        items, rest = [], s[1:].lstrip()
        while True:
            if rest.startswith("]"):
                return items, rest[1:]
            val, rest = _parse_value(rest, lineno)
            items.append(val)
            rest = rest.lstrip()
            if rest.startswith(","):
                rest = rest[1:].lstrip()
            elif not rest.startswith("]"):
                raise TomlError("line %d: bad array" % lineno)
    m = re.match(r"(true|false)\b", s)
    if m:
        return m.group(1) == "true", s[m.end():]
    m = re.match(r"[+-]?(\d[\d_]*)(\.\d+)?", s)
    if m:
        tok = m.group(0).replace("_", "")
        return (float(tok) if m.group(2) else int(tok)), s[m.end():]
    raise TomlError("line %d: unsupported value %r" % (lineno, s))


def parse_toml_min(text):
    """Tiny TOML subset parser (tables, strings, ints, floats, bools, flat arrays).

    Used only on Python < 3.11 when neither tomli nor toml is installed."""
    root = {}
    cur = root
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _TABLE_RE.match(line)
        if m:
            cur = root
            for part in m.group(1).split("."):
                cur = cur.setdefault(part, {})
            continue
        m = _KEY_RE.match(line)
        if not m:
            raise TomlError("line %d: cannot parse %r" % (lineno, line))
        val, rest = _parse_value(m.group(2), lineno)
        rest = rest.strip()
        if rest and not rest.startswith("#"):
            raise TomlError("line %d: trailing characters %r" % (lineno, rest))
        cur[m.group(1)] = val
    return root


# --------------------------------------------------------------------------- config

def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(paths):
    data = {}
    if os.path.exists(paths.config_file):
        with open(paths.config_file, encoding="utf-8") as f:
            data = _load_toml_text(f.read())
    return _merge(DEFAULTS, data)


def _mkdir_private(path):
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, 0o600)


def ensure_files(paths):
    """Create config dir / data dir / config.toml (with a fresh token) / tmux.conf.

    Existing files are never overwritten, except that an empty token is filled in."""
    _mkdir_private(paths.config_dir)
    _mkdir_private(paths.data_dir)
    _mkdir_private(paths.run_dir)
    created = []
    if not os.path.exists(paths.config_file):
        _write_private(paths.config_file, CONFIG_TEMPLATE.format(token=secrets.token_urlsafe(32)))
        created.append(paths.config_file)
    else:
        with open(paths.config_file, encoding="utf-8") as f:
            text = f.read()
        if not load_config(paths)["server"].get("token"):
            new, n = re.subn(r'(?m)^(\s*token\s*=\s*)(""|\'\')', lambda m: m.group(1) + '"%s"' % secrets.token_urlsafe(32), text, count=1)
            if n:
                _write_private(paths.config_file, new)
                created.append(paths.config_file + " (token)")
        # configs generated by v0.1.0 before the default became 0.0.0.0 (comment line unchanged)
        if LEGACY_BIND_COMMENT in text and re.search(r'(?m)^bind\s*=\s*"127\.0\.0\.1"', text):
            set_server_bind(paths, "0.0.0.0")
            created.append(paths.config_file + " (bind -> 0.0.0.0)")
        if _migrate_default_cmd(paths):
            created.append(paths.config_file + ' (defaults.cmd -> "claude rc")')
    cfg = load_config(paths)
    if not os.path.exists(paths.tmux_conf):
        with open(paths.tmux_conf, "w", encoding="utf-8") as f:
            f.write(TMUX_CONF_TEMPLATE.format(socket=cfg["tmux"]["socket"],
                                              history_limit=int(cfg["tmux"]["history_limit"])))
        created.append(paths.tmux_conf)
    return created


def _migrate_default_cmd(paths):
    """Once: the generated `cmd = "claude"` in [defaults] becomes "claude rc" (new default).

    Recorded in <data>/settings.json so a later deliberate change back is respected."""
    from . import workspace

    st = workspace.load_settings(paths)
    if st.get("migrated_default_cmd_rc"):
        return False
    with open(paths.config_file, encoding="utf-8") as f:
        text = f.read()
    lines, in_defaults, changed = text.splitlines(keepends=True), False, False
    for i, ln in enumerate(lines):
        m = _TABLE_RE.match(ln)
        if m:
            in_defaults = m.group(1) == "defaults"
            continue
        if in_defaults and re.match(r'^cmd\s*=\s*"claude"\s*$', ln):
            lines[i] = 'cmd = "claude rc"\n'
            changed = True
            break
    if changed:
        _write_private(paths.config_file, "".join(lines))
    st["migrated_default_cmd_rc"] = True
    try:
        workspace.save_settings(paths, st)
    except OSError:
        pass
    return changed


LEGACY_BIND_COMMENT = "# Listen address. Keep 127.0.0.1 and use Tailscale / SSH port-forwarding for remote access."


def _set_server_key(text, key, value):
    """Replace `key = ...` inside [server], or insert it right after the [server] header."""
    lines = text.splitlines(keepends=True)
    in_server, header_idx = False, None
    for i, ln in enumerate(lines):
        m = _TABLE_RE.match(ln)
        if m:
            in_server = m.group(1) == "server"
            if in_server:
                header_idx = i
            continue
        if in_server and re.match(r"\s*%s\s*=" % re.escape(key), ln):
            lines[i] = "%s = %s\n" % (key, value)
            return "".join(lines)
    if header_idx is None:
        return text.rstrip("\n") + "\n\n[server]\n%s = %s\n" % (key, value)
    lines.insert(header_idx + 1, "%s = %s\n" % (key, value))
    return "".join(lines)


def set_server_bind(paths, bind):
    """Rewrite [server] bind (and allow_external for non-loopback addresses) in config.toml."""
    if not re.match(r"^[0-9A-Za-z.:_-]+$", bind or ""):
        raise ValueError("invalid bind address: %r" % (bind,))
    with open(paths.config_file, encoding="utf-8") as f:
        text = f.read()
    text = text.replace(LEGACY_BIND_COMMENT + "\n", "")
    text = _set_server_key(text, "bind", '"%s"' % bind)
    if not is_loopback(bind):
        text = _set_server_key(text, "allow_external", "true")
    _write_private(paths.config_file, text)


def is_loopback(addr):
    return addr in ("localhost", "::1") or addr.startswith("127.")
