"""Per-session working directories: <workspace_root>/<random> created on session creation
and removed together with the session."""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil

from . import CCDeckError

# 10 chars from an alphabet without look-alikes (0/o, 1/l/i): 31^10 ≈ 8e14 (~49 bits)
ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
LENGTH = 10
NAME_RE = re.compile(r"^[%s]{%d}$" % (ALPHABET, LENGTH))
DEFAULT_ROOT = "~/claude"


def random_name():
    return "".join(secrets.choice(ALPHABET) for _ in range(LENGTH))


# ------------------------------------------------------------------ UI settings (settings.json)

def settings_file(paths):
    return os.path.join(paths.data_dir, "settings.json")


def load_settings(paths):
    try:
        with open(settings_file(paths), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(paths, data):
    path = settings_file(paths)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def get_root(paths, cfg):
    return load_settings(paths).get("workspace_root") or cfg["defaults"].get("workspace_root") or DEFAULT_ROOT


def normalize_root(root):
    if not isinstance(root, str) or not root.strip():
        raise CCDeckError("workspace root is empty")
    path = os.path.realpath(os.path.expanduser(root.strip()))
    home = os.path.realpath(os.path.expanduser("~"))
    if path in ("/", home) or len(path.rstrip("/").split("/")) < 2:
        raise CCDeckError("workspace root must be a dedicated directory, not %s" % path)
    return path


def set_root(paths, root):
    path = normalize_root(root)
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
    except OSError as e:
        raise CCDeckError("cannot create %s: %s" % (path, e))
    if not os.access(path, os.W_OK | os.X_OK):
        raise CCDeckError("%s is not writable" % path)
    data = load_settings(paths)
    data["workspace_root"] = path
    save_settings(paths, data)
    return path


# ------------------------------------------------------------------ create / remove

def create(root):
    """Create <root>/<random> (0700). Returns {"root": realroot, "path": realpath}."""
    root = normalize_root(root)
    try:
        os.makedirs(root, mode=0o700, exist_ok=True)
    except OSError as e:
        raise CCDeckError("cannot create workspace root %s: %s" % (root, e))
    for _ in range(20):
        path = os.path.join(root, random_name())
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            continue
        except OSError as e:
            raise CCDeckError("cannot create %s: %s" % (path, e))
        return {"root": root, "path": path}
    raise CCDeckError("could not allocate a workspace directory in %s" % root)


def removable(rec, others=()):
    """Reason string if rec's workspace must NOT be deleted, else None."""
    ws = rec.get("workspace")
    if not isinstance(ws, dict) or not ws.get("path") or not ws.get("root"):
        return "not created by ccdeck"
    path, root = ws["path"], ws["root"]
    if rec.get("cwd") != path:
        return "cwd differs from the created directory"
    if not os.path.lexists(path):
        return "already gone"
    if os.path.islink(path):
        return "is a symlink"
    real = os.path.realpath(path)
    if real != path or os.path.dirname(real) != os.path.realpath(root):
        return "not directly under the workspace root"
    if not NAME_RE.match(os.path.basename(real)):
        return "name is not a ccdeck random name"
    if any(o.get("cwd") == path for o in others):
        return "used by another session"
    return None


def remove(rec, others=()):
    """Delete the session's workspace directory. Returns (deleted: bool, reason)."""
    why = removable(rec, others)
    if why:
        return False, why
    shutil.rmtree(rec["workspace"]["path"])
    return True, "deleted"
