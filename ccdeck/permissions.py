"""Per-session permission profile → Claude Code settings / flags.

Profile (stored in the session record as "permissions"):
  mode        permission mode: default | acceptEdits | plan | dontAsk | bypassPermissions
  read_scope  where file tools may read without asking: workdir | home | any
  extra_dirs  additional directories with read/write access (permissions.additionalDirectories)
  deny_paths  never readable / writable (Read/Edit deny rules + sandbox denyRead/denyWrite)
  bash        ask | sandbox (run in the OS sandbox, auto-allowed) | deny | allow
  web         allow WebFetch / WebSearch

Writes are limited to the working directory + extra_dirs (Claude Code's working-directory
model); deny rules always win over allow rules.

Applied as:
  interactive claude   --permission-mode <mode> --settings <generated file>
  claude rc            --permission-mode <mode> (for the sessions it spawns) and, for
                       directories created by ccdeck, <cwd>/.claude/settings.local.json
                       (spawned sessions read the folder's project settings)
"""
from __future__ import annotations

import json
import os
import re

from . import CCDeckError

MODES = ("default", "acceptEdits", "plan", "dontAsk", "bypassPermissions")
READ_SCOPES = ("workdir", "home", "any")
BASH = ("ask", "sandbox", "deny", "allow")

# always denied: ccdeck's own config holds the web UI token
ALWAYS_DENY = ["~/.config/ccdeck"]
DEFAULT_DENY = ["~/.ssh", "~/.aws", "~/.gnupg", "~/.config/gh", "~/.netrc", "~/.docker/config.json"]

DEFAULT = {
    "mode": "default",
    "read_scope": "workdir",
    "extra_dirs": [],
    "deny_paths": list(DEFAULT_DENY),
    "bash": "ask",
    "web": True,
}

_PATH_RE = re.compile(r"^[^\x00-\x1f\x7f()]+$")


def _paths(value, what):
    if value is None:
        return []
    if isinstance(value, str):
        value = [v for v in re.split(r"[\n,]", value)]
    if not isinstance(value, list):
        raise CCDeckError("%s must be a list" % what)
    out = []
    for v in value:
        v = str(v).strip()
        if not v:
            continue
        if not _PATH_RE.match(v) or not (v.startswith("/") or v.startswith("~")):
            raise CCDeckError("%s: %r must be an absolute path or start with ~ (no parentheses)" % (what, v))
        v = v.rstrip("/") or "/"
        if v not in out:
            out.append(v)
    if len(out) > 50:
        raise CCDeckError("%s: too many entries" % what)
    return out


def normalize(p, base=None):
    """Validate a (partial) profile and merge it over `base` (default: DEFAULT)."""
    out = dict(DEFAULT if base is None else base)
    out["extra_dirs"] = list(out.get("extra_dirs") or [])
    out["deny_paths"] = list(out.get("deny_paths") or [])
    p = p or {}
    if not isinstance(p, dict):
        raise CCDeckError("permissions must be an object")
    if "mode" in p:
        if p["mode"] not in MODES:
            raise CCDeckError("mode must be one of %s" % ", ".join(MODES))
        out["mode"] = p["mode"]
    if "read_scope" in p:
        if p["read_scope"] not in READ_SCOPES:
            raise CCDeckError("read_scope must be one of %s" % ", ".join(READ_SCOPES))
        out["read_scope"] = p["read_scope"]
    if "bash" in p:
        if p["bash"] not in BASH:
            raise CCDeckError("bash must be one of %s" % ", ".join(BASH))
        out["bash"] = p["bash"]
    if "web" in p:
        out["web"] = bool(p["web"])
    if "extra_dirs" in p:
        out["extra_dirs"] = _paths(p["extra_dirs"], "extra_dirs")
    if "deny_paths" in p:
        out["deny_paths"] = _paths(p["deny_paths"], "deny_paths")
    return out


def _expand(path):
    return os.path.expanduser(path)


def _rule_path(path):
    """Claude Code permission rule path: `//abs` for absolute, `~/x` stays."""
    if path.startswith("~"):
        return path
    return "/" + path  # "/etc" -> "//etc"


def to_settings(perm):
    """Claude Code settings dict for this profile."""
    allow, deny = [], []
    if perm["read_scope"] == "home":
        allow.append("Read(~/**)")
    elif perm["read_scope"] == "any":
        allow.append("Read(//**)")
    denied = list(perm["deny_paths"]) + [d for d in ALWAYS_DENY if d not in perm["deny_paths"]]
    for d in denied:
        rp = _rule_path(d)
        deny += ["Read(%s)" % rp, "Read(%s/**)" % rp, "Edit(%s)" % rp, "Edit(%s/**)" % rp]
    if perm["bash"] == "deny":
        deny.append("Bash")
    elif perm["bash"] == "allow":
        allow.append("Bash")
    if not perm["web"]:
        deny += ["WebFetch", "WebSearch"]
    settings = {"permissions": {"allow": allow, "deny": deny,
                                "additionalDirectories": [_expand(d) for d in perm["extra_dirs"]]}}
    if perm["mode"] != "default":
        settings["permissions"]["defaultMode"] = perm["mode"]
    if perm["mode"] != "bypassPermissions":
        settings["permissions"]["disableBypassPermissionsMode"] = "disable"
    if perm["bash"] == "sandbox":
        settings["sandbox"] = {
            "enabled": True,
            "autoAllowBashIfSandboxed": True,
            "allowUnsandboxedCommands": False,
            "filesystem": {
                "allowWrite": [_expand(d) for d in perm["extra_dirs"]],
                "denyRead": [_expand(d) for d in denied],
                "denyWrite": [_expand(d) for d in denied],
            },
        }
        if not perm["web"]:
            settings["sandbox"]["network"] = {"allowedDomains": []}
    return settings


def write_settings(path, perm):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(to_settings(perm), f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    return path


def summary(perm):
    """Short Japanese description for the UI / ls."""
    mode = {"default": "都度確認", "acceptEdits": "編集は自動許可", "plan": "計画のみ",
            "dontAsk": "許可済み以外は拒否", "bypassPermissions": "全許可"}[perm["mode"]]
    read = {"workdir": "作業dir", "home": "ホーム", "any": "全体"}[perm["read_scope"]]
    bash = {"ask": "確認", "sandbox": "サンドボックス", "deny": "禁止", "allow": "許可"}[perm["bash"]]
    return "%s / 読取:%s / Bash:%s%s" % (mode, read, bash, "" if perm["web"] else " / Web禁止")
