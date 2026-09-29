import os
import sys
import textwrap
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAKE_CLAUDE = r'''#!/bin/sh
# Fake `claude` used by tests: records its args, writes a transcript like Claude Code does, then waits.
if [ "$1" = "--help" ]; then echo "  --session-id <uuid>  Use a specific session ID"; exit 0; fi
echo "fake-claude $*" >> "$FAKE_CLAUDE_LOG"
sid=""
prev=""
for a in "$@"; do
  case "$prev" in --session-id|--resume) sid="$a";; esac
  prev="$a"
done
if [ -n "$sid" ]; then
  enc=$(pwd | sed 's/[^A-Za-z0-9]/-/g')
  mkdir -p "$CLAUDE_CONFIG_DIR/projects/$enc"
  echo '{"type":"user"}' >> "$CLAUDE_CONFIG_DIR/projects/$enc/$sid.jsonl"
fi
echo "fake claude running ($*)"
exec sleep 600
'''


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated ccdeck + claude environment (config/data dirs, tmux socket, fake claude)."""
    cfg = tmp_path / "cfg"
    data = tmp_path / "data"
    home = tmp_path / "claude-home"
    bindir = tmp_path / "bin"
    for d in (cfg, data, home, bindir):
        d.mkdir()
    fake = bindir / "claude"
    fake.write_text(FAKE_CLAUDE)
    fake.chmod(0o755)
    (cfg / "config.toml").write_text(textwrap.dedent('''
        [server]
        token = "test-token-0123456789abcdef"
        [tmux]
        systemd_scope = false
        [claude]
        bin = "%s"
        shell_flags = ["-c"]
        [defaults]
        cmd = "%s"
        dir = "%s"
    ''' % (fake, fake, tmp_path)))
    monkeypatch.setenv("CCDECK_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("CCDECK_DATA_DIR", str(data))
    monkeypatch.setenv("CCDECK_TMUX_SOCKET", "ccdeck-test-" + uuid.uuid4().hex[:8])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "claude.log"))
    monkeypatch.setenv("SHELL", "/bin/sh")
    from ccdeck import claude

    claude.supports_session_id_flag.cache_clear()
    return tmp_path
