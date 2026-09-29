"""ccdeck - manage many Claude Code sessions (tmux + ttyd) from a web UI."""
import re

__version__ = "0.1.0"

NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")


class CCDeckError(Exception):
    """User-facing error. ``status`` is used as the HTTP status code by the API."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def validate_name(name):
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise CCDeckError("invalid session name %r (allowed: ^[a-zA-Z0-9_-]{1,32}$)" % (name,))
    return name
