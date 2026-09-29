"""ccdeck - manage many Claude Code sessions (tmux + ttyd) from a web UI."""
import re

__version__ = "0.1.0"

NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")


class CCDeckError(Exception):
    """User-facing error. ``status`` is used as the HTTP status code by the API."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


TITLE_MAX = 64


def validate_title(title):
    """Display name: any language (e.g. Japanese), 1-64 chars, no control characters."""
    import unicodedata

    if not isinstance(title, str):
        raise CCDeckError("invalid name")
    title = title.strip()
    if not title or len(title) > TITLE_MAX:
        raise CCDeckError("name must be 1-%d characters" % TITLE_MAX)
    if any(unicodedata.category(c).startswith("C") for c in title):
        raise CCDeckError("name must not contain control characters")
    return title


def validate_name(name):
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise CCDeckError("invalid session name %r (allowed: ^[a-zA-Z0-9_-]{1,32}$)" % (name,))
    return name
