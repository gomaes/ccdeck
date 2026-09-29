import pytest

from ccdeck.config import parse_toml_min, Paths, ensure_files, load_config
from ccdeck.manager import Manager
from ccdeck.proxy import INJECT, _dechunk, inject_script, token_ok
from test_restore import FakeTmux

TOKEN = "test-token-0123456789abcdef"


@pytest.fixture
def client(env):
    flask = pytest.importorskip("flask")  # noqa: F841
    from ccdeck.web import create_app

    m = Manager(tmux=FakeTmux())
    app = create_app(m, lambda: TOKEN)
    app.testing = True
    return app.test_client()


def H(extra=None):
    h = {"Authorization": "Bearer " + TOKEN}
    h.update(extra or {})
    return h


def test_auth_required(client):
    assert client.get("/api/health").status_code == 200
    assert client.get("/").status_code == 200
    assert client.get("/api/sessions").status_code == 401
    assert client.get("/api/sessions", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/sessions", headers=H()).status_code == 200


def test_login_cookie_and_csrf_header(client):
    assert client.post("/api/login", json={"token": "nope"}).status_code == 401
    r = client.post("/api/login", json={"token": TOKEN})
    assert r.status_code == 200 and "HttpOnly" in r.headers["Set-Cookie"] and "SameSite=Strict" in r.headers["Set-Cookie"]
    assert client.get("/api/sessions").status_code == 200  # cookie
    # cookie-authenticated write without the custom header is rejected
    assert client.post("/api/sessions", json={"name": "x"}).status_code == 403


def test_token_query_sets_cookie_and_redirects(client):
    r = client.get("/?token=" + TOKEN)
    assert r.status_code == 302 and "ccdeck_token=" in r.headers.get("Set-Cookie", "")
    r = client.get("/?token=bad")
    assert r.status_code == 302 and "Set-Cookie" not in r.headers


def test_session_crud(client, env):
    r = client.post("/api/sessions", json={"name": "web1", "dir": str(env)}, headers=H())
    assert r.status_code == 201, r.json
    rows = client.get("/api/sessions", headers=H()).json["sessions"]
    assert [s["name"] for s in rows] == ["web1"] and rows[0]["state"] == "running"
    assert client.post("/api/sessions", json={"name": "web1", "dir": str(env)}, headers=H()).status_code == 409
    assert client.post("/api/sessions", json={"name": "a\nb", "dir": str(env)}, headers=H()).status_code == 400
    r = client.post("/api/sessions", json={"name": "日本語", "dir": str(env)}, headers=H())
    assert r.status_code == 201 and r.json["session"]["title"] == "日本語"
    jid = r.json["session"]["name"]
    r = client.post("/api/sessions/%s/rename" % jid, json={"new_name": "名前変更"}, headers=H())
    assert r.json["session"]["name"] == jid and r.json["session"]["title"] == "名前変更"
    assert client.delete("/api/sessions/" + jid, headers=H()).status_code == 200
    r = client.patch("/api/sessions/web1", json={"auto_continue": {"rate_limit": True}}, headers=H())
    assert r.json["session"]["auto_continue"]["rate_limit"] is True
    assert client.post("/api/sessions/web1/keys", json={"key": "Enter"}, headers=H()).status_code == 200
    assert client.post("/api/sessions/web1/keys", json={"key": "kill-server"}, headers=H()).status_code == 400
    assert client.post("/api/sessions/web1/rename", json={"new_name": "web2"}, headers=H()).status_code == 200
    assert client.post("/api/sessions/web2/stop", json={}, headers=H()).json["session"]["state"] == "dead"
    assert client.post("/api/sessions/web2/resume", json={}, headers=H()).json["session"]["state"] == "running"
    assert client.delete("/api/sessions/web2", headers=H()).status_code == 200
    assert client.get("/api/sessions/web2", headers=H()).status_code == 404
    assert client.get("/api/sessions/..%2Fetc", headers=H()).status_code in (400, 404)


# ---------------------------------------------------------------- proxy helpers
def test_token_ok():
    assert token_ok(TOKEN, {"authorization": "Bearer " + TOKEN})
    assert token_ok(TOKEN, {"cookie": "a=b; ccdeck_token=" + TOKEN})
    assert token_ok(TOKEN, {}, {"token": [TOKEN]})
    assert not token_ok(TOKEN, {"cookie": "ccdeck_token=x"})
    assert not token_ok("", {"authorization": "Bearer "})


def test_inject_script():
    body = b"<html><head><title>t</title></head></html>"
    resp = b"HTTP/1.1 200 OK\r\ncontent-type: text/html\r\ncontent-length: %d\r\n\r\n" % len(body) + body
    out = inject_script(resp)
    head, new_body = out.split(b"\r\n\r\n", 1)
    assert new_body.startswith(b"<html><head>" + INJECT)
    assert b"Content-Length: %d" % len(new_body) in head and b"content-length: %d" % len(body) not in head
    # non-HTML is untouched
    js = b"HTTP/1.1 200 OK\r\ncontent-type: application/javascript\r\n\r\nx"
    assert inject_script(js) == js


def test_dechunk():
    assert _dechunk(b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n") == b"hello world"


# ---------------------------------------------------------------- config
def test_min_toml_parser():
    d = parse_toml_min('''
# c
[server]
bind = "127.0.0.1"  # comment
port = 8787
allow_external = false
[ttyd]
extra_args = ["-t", 'x=1']
ratio = 1.5
''')
    assert d == {"server": {"bind": "127.0.0.1", "port": 8787, "allow_external": False},
                 "ttyd": {"extra_args": ["-t", "x=1"], "ratio": 1.5}}


def test_ensure_files_creates_private_config_with_token(tmp_path):
    p = Paths({"CCDECK_CONFIG_DIR": str(tmp_path / "c"), "CCDECK_DATA_DIR": str(tmp_path / "d")})
    ensure_files(p)
    cfg = load_config(p)
    assert len(cfg["server"]["token"]) >= 32 and cfg["server"]["bind"] == "0.0.0.0"
    assert cfg["server"]["allow_external"] is True
    import os
    import stat
    assert stat.S_IMODE(os.stat(p.config_file).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(p.config_dir).st_mode) == 0o700
    assert "exit-empty off" in open(p.tmux_conf).read()
    tok = cfg["server"]["token"]
    ensure_files(p)  # idempotent
    assert load_config(p)["server"]["token"] == tok


LEGACY_CONFIG = '''# ccdeck configuration
# Changes take effect after `systemctl --user restart ccdeck`.

[server]
# Listen address. Keep 127.0.0.1 and use Tailscale / SSH port-forwarding for remote access.
bind = "127.0.0.1"
port = 8787
# Must be true to bind to a non-loopback address (e.g. "0.0.0.0" or a Tailscale IP).
allow_external = false
token = "tok-0123456789abcdef0123"

[ttyd]
port = 7682
'''


def _paths(tmp_path):
    p = Paths({"CCDECK_CONFIG_DIR": str(tmp_path / "c"), "CCDECK_DATA_DIR": str(tmp_path / "d")})
    import os
    os.makedirs(p.config_dir)
    return p


def test_legacy_generated_config_is_migrated_to_0000(tmp_path):
    p = _paths(tmp_path)
    open(p.config_file, "w").write(LEGACY_CONFIG)
    ensure_files(p)
    s = load_config(p)["server"]
    assert (s["bind"], s["allow_external"], s["token"], s["port"]) == ("0.0.0.0", True, "tok-0123456789abcdef0123", 8787)
    assert load_config(p)["ttyd"]["port"] == 7682


def test_user_edited_bind_is_kept(tmp_path):
    p = _paths(tmp_path)
    open(p.config_file, "w").write('[server]\nbind = "127.0.0.1"\ntoken = "tok-0123456789abcdef0123"\n')
    ensure_files(p)
    assert load_config(p)["server"]["bind"] == "127.0.0.1"


def test_set_server_bind(tmp_path):
    from ccdeck.config import set_server_bind

    p = _paths(tmp_path)
    open(p.config_file, "w").write('# x\n[server]\ntoken = "tok-0123456789abcdef0123"\n\n[ttyd]\nport = 1\n')
    set_server_bind(p, "0.0.0.0")
    s = load_config(p)["server"]
    assert (s["bind"], s["allow_external"]) == ("0.0.0.0", True) and load_config(p)["ttyd"]["port"] == 1
    set_server_bind(p, "127.0.0.1")
    assert load_config(p)["server"]["bind"] == "127.0.0.1"
    with pytest.raises(ValueError):
        set_server_bind(p, '0.0.0.0"\nx = "')


def test_default_cmd_migrates_once_to_claude_rc(tmp_path):
    p = _paths(tmp_path)
    open(p.config_file, "w").write('[server]\ntoken = "tok-0123456789abcdef0123"\n\n[defaults]\ncmd = "claude"\ndir = "~"\n')
    ensure_files(p)
    assert load_config(p)["defaults"]["cmd"] == "claude rc"
    # the user deliberately goes back to plain claude: not migrated again
    open(p.config_file, "w").write('[server]\ntoken = "tok-0123456789abcdef0123"\n\n[defaults]\ncmd = "claude"\n')
    ensure_files(p)
    assert load_config(p)["defaults"]["cmd"] == "claude"


def test_new_config_defaults_to_claude_rc(tmp_path):
    p = Paths({"CCDECK_CONFIG_DIR": str(tmp_path / "c"), "CCDECK_DATA_DIR": str(tmp_path / "d")})
    ensure_files(p)
    assert load_config(p)["defaults"]["cmd"] == "claude rc"
