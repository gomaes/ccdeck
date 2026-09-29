"""Per-session working directories and permission profiles."""
import json
import os

import pytest

from ccdeck import CCDeckError, claude, permissions, workspace
from ccdeck.manager import Manager
from test_restore import FakeTmux


@pytest.fixture
def mgr(env):
    return Manager(tmux=FakeTmux())


# ---------------------------------------------------------------- workspace
def test_random_name():
    names = {workspace.random_name() for _ in range(200)}
    assert len(names) == 200
    assert all(workspace.NAME_RE.match(n) for n in names)
    assert not any(c in "".join(names) for c in "0o1li")


def test_create_session_makes_and_delete_removes_workspace(mgr, env):
    rec = mgr.create("ws1")
    root = str(env / "ws")
    assert rec["workspace"] == {"root": root, "path": rec["cwd"]}
    assert os.path.dirname(rec["cwd"]) == root and workspace.NAME_RE.match(os.path.basename(rec["cwd"]))
    assert oct(os.stat(rec["cwd"]).st_mode & 0o777) == "0o700"
    open(os.path.join(rec["cwd"], "file.txt"), "w").write("x")
    os.makedirs(os.path.join(rec["cwd"], "sub", "deep"))
    out = mgr.delete("ws1")
    assert out["workspace_deleted"] is True and not os.path.exists(rec["cwd"])
    assert os.path.isdir(root)  # the root itself stays


def test_explicit_dir_is_never_deleted(mgr, env):
    d = env / "mine"
    d.mkdir()
    mgr.create("own", cwd=str(d))
    out = mgr.delete("own")
    assert out["workspace_deleted"] is False and d.is_dir()


def test_keep_dir(mgr, env):
    rec = mgr.create("keep")
    assert mgr.delete("keep", keep_dir=True)["workspace_deleted"] is False
    assert os.path.isdir(rec["cwd"])


def test_removal_safety_checks(env):
    root = env / "wsroot"
    ws = workspace.create(str(root))
    rec = {"cwd": ws["path"], "workspace": ws}
    assert workspace.removable(rec) is None
    assert workspace.removable(rec, [{"cwd": ws["path"]}]) == "used by another session"
    assert workspace.removable({"cwd": str(env), "workspace": {"root": str(env.parent), "path": str(env)}})
    # tampered record pointing outside the root / at the root / at a non-random name
    assert workspace.removable({"cwd": str(root), "workspace": {"root": str(root), "path": str(root)}})
    other = root / "not-random"
    other.mkdir()
    assert workspace.removable({"cwd": str(other), "workspace": {"root": str(root), "path": str(other)}})
    # symlink named like a workspace pointing elsewhere
    target = env / "precious"
    target.mkdir()
    link = root / workspace.random_name()
    os.symlink(target, link)
    assert workspace.removable({"cwd": str(link), "workspace": {"root": str(root), "path": str(link)}}) == "is a symlink"
    assert workspace.remove({"cwd": str(link), "workspace": {"root": str(root), "path": str(link)}})[0] is False
    assert target.is_dir()


def test_workspace_root_setting(mgr, env):
    new = env / "elsewhere" / "roots"
    assert mgr.set_workspace_root(str(new)) == str(new) and new.is_dir()
    assert mgr.workspace_root() == str(new)
    rec = mgr.create("rooted")
    assert os.path.dirname(rec["cwd"]) == str(new)
    for bad in ("/", "~", ""):
        with pytest.raises(CCDeckError):
            mgr.set_workspace_root(bad)


# ---------------------------------------------------------------- permissions
def test_normalize_and_validation():
    p = permissions.normalize({"mode": "acceptEdits", "extra_dirs": "~/a, /b/", "bash": "sandbox"})
    assert p["mode"] == "acceptEdits" and p["extra_dirs"] == ["~/a", "/b"] and p["bash"] == "sandbox"
    assert p["deny_paths"] == permissions.DEFAULT_DENY
    for bad in ({"mode": "godmode"}, {"read_scope": "x"}, {"bash": "x"}, {"extra_dirs": ["relative/path"]},
                {"deny_paths": ["/a(b)"]}):
        with pytest.raises(CCDeckError):
            permissions.normalize(bad)


def test_to_settings():
    s = permissions.to_settings(permissions.normalize({
        "read_scope": "home", "extra_dirs": ["/data"], "deny_paths": ["~/.ssh", "/etc/secret"],
        "bash": "sandbox", "web": False}))
    perm = s["permissions"]
    assert "Read(~/**)" in perm["allow"]
    assert {"Read(~/.ssh/**)", "Edit(~/.ssh/**)", "Read(//etc/secret/**)", "Read(~/.config/ccdeck/**)"} <= set(perm["deny"])
    assert {"WebFetch", "WebSearch"} <= set(perm["deny"])
    assert perm["additionalDirectories"] == ["/data"]
    assert perm["disableBypassPermissionsMode"] == "disable" and "defaultMode" not in perm
    sb = s["sandbox"]
    assert sb["enabled"] and sb["autoAllowBashIfSandboxed"] and not sb["allowUnsandboxedCommands"]
    assert os.path.expanduser("~/.ssh") in sb["filesystem"]["denyRead"]
    assert sb["network"] == {"allowedDomains": []}
    s = permissions.to_settings(permissions.normalize({"mode": "bypassPermissions", "bash": "deny"}))
    assert s["permissions"]["defaultMode"] == "bypassPermissions" and "Bash" in s["permissions"]["deny"]
    assert "disableBypassPermissionsMode" not in s["permissions"] and "sandbox" not in s


def test_with_permissions_flags():
    assert claude.with_permissions("claude --model x", "acceptEdits", "/p.json") == \
        "claude --permission-mode acceptEdits --settings /p.json --model x"
    assert claude.with_permissions("claude", "default", "/p.json") == "claude --settings /p.json"
    assert claude.with_permissions("claude rc --name a", "plan", "/p.json") == "claude rc --permission-mode plan --name a"
    assert claude.with_permissions("claude --dangerously-skip-permissions --settings /mine.json", "plan", "/p.json") == \
        "claude --dangerously-skip-permissions --settings /mine.json"
    assert claude.with_permissions("htop", "plan", "/p.json") == "htop"


def test_session_launch_applies_permissions(mgr, env):
    rec = mgr.create("perm", permissions_={"mode": "acceptEdits", "read_scope": "any", "web": False})
    cmd = mgr.tmux.launches[-1][2]
    path = mgr._perm_settings_path("perm")
    assert "--permission-mode acceptEdits" in cmd and "--settings " + path in cmd
    data = json.load(open(path))
    assert "Read(//**)" in data["permissions"]["allow"] and "WebFetch" in data["permissions"]["deny"]
    # edit → applied on the next restart
    mgr.set_options("perm", permissions_={"mode": "plan"})
    mgr.restart("perm")
    assert "--permission-mode plan" in mgr.tmux.launches[-1][2]
    assert mgr.store.get("perm")["permissions"]["read_scope"] == "any"  # partial update keeps the rest
    mgr.delete("perm")
    assert not os.path.exists(path) and not os.path.exists(rec["cwd"])


def test_remote_control_workspace_gets_project_settings(mgr, env):
    fake = claude.find_bin(mgr.cfg["claude"]["bin"])
    rec = mgr.create("rcws", cmd="%s rc" % fake, permissions_={"mode": "acceptEdits", "bash": "deny"})
    cmd = mgr.tmux.launches[-1][2]
    assert "rc --permission-mode acceptEdits" in cmd and "--settings" not in cmd
    local = json.load(open(os.path.join(rec["cwd"], ".claude", "settings.local.json")))
    assert "Bash" in local["permissions"]["deny"]
