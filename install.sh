#!/usr/bin/env bash
# ccdeck installer
#   - apt install tmux / ttyd / python3 / python3-flask (via sudo)
#   - build ccdeck as a single-file zipapp and install it to ~/.local/bin/ccdeck
#   - create ~/.config/ccdeck/{config.toml,tmux.conf,env} and the systemd --user unit
#   - enable linger + start the service
#
# Usage: ./install.sh [--bind ADDR] [--no-apt] [--no-service] [--no-linger] [--keep-system-ttyd] [--uninstall]
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
TARGET="${BIN_DIR}/ccdeck"
PY=/usr/bin/python3

DO_APT=1
DO_SERVICE=1
DO_LINGER=1
KEEP_SYSTEM_TTYD=0
UNINSTALL=0
BIND=""
while [ $# -gt 0 ]; do
  case "$1" in
    --bind) [ $# -ge 2 ] || { echo "--bind needs an address" >&2; exit 2; }; BIND="$2"; shift ;;
    --bind=*) BIND="${1#--bind=}" ;;
    --no-apt) DO_APT=0 ;;
    --no-service) DO_SERVICE=0 ;;
    --no-linger) DO_LINGER=0 ;;
    --keep-system-ttyd) KEEP_SYSTEM_TTYD=1 ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

if [ "$(id -u)" -eq 0 ]; then
  warn "running as root: ccdeck will be installed for root. Run as your normal user (sudo is used only for apt / linger)."
fi

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  if command -v sudo >/dev/null 2>&1; then SUDO="sudo"; else SUDO=""; fi
fi

have_systemd_user() {
  command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1
}

# ------------------------------------------------------------------ uninstall
if [ "$UNINSTALL" -eq 1 ]; then
  if have_systemd_user; then
    systemctl --user disable --now ccdeck.service 2>/dev/null || true
    rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/ccdeck.service"
    systemctl --user daemon-reload || true
  fi
  rm -f "$TARGET"
  info "removed $TARGET and the systemd unit."
  info "kept: ~/.config/ccdeck (config/token), ~/.local/share/ccdeck (sessions.json)."
  info "running tmux sessions were not touched (list: tmux -L ccdeck ls)."
  exit 0
fi

# ------------------------------------------------------------------ apt
# bubblewrap + socat: OS sandbox for Bash commands (per-session "Bash: sandbox" permission)
PKGS=(tmux ttyd python3 python3-flask bubblewrap socat)
if [ "$DO_APT" -eq 1 ]; then
  command -v apt-get >/dev/null 2>&1 || die "apt-get not found (Ubuntu 22.04+ / Debian 12+ required). Use --no-apt to skip."
  missing=()
  for p in "${PKGS[@]}"; do
    dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "install ok installed" || missing+=("$p")
  done
  ttyd_was_installed=1
  [[ " ${missing[*]} " == *" ttyd "* ]] && ttyd_was_installed=0
  if [ "${#missing[@]}" -gt 0 ]; then
    info "installing: ${missing[*]}"
    $SUDO apt-get update
    if ! $SUDO apt-get install -y "${missing[@]}"; then
      die "apt-get install failed. On Ubuntu, ttyd is in 'universe': sudo add-apt-repository universe"
    fi
  else
    info "apt packages already installed: ${PKGS[*]}"
  fi
  # The Debian/Ubuntu ttyd package enables a system-wide `ttyd -O login` on localhost:7681.
  # ccdeck runs its own ttyd, so disable that service when we were the ones who installed it.
  if [ "$ttyd_was_installed" -eq 0 ] && [ "$KEEP_SYSTEM_TTYD" -eq 0 ] && \
     systemctl is-enabled ttyd.service >/dev/null 2>&1; then
    info "disabling the system ttyd.service installed by the package (use --keep-system-ttyd to keep it)"
    $SUDO systemctl disable --now ttyd.service || warn "could not disable ttyd.service"
  fi
fi

[ -x "$PY" ] || die "$PY not found"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || die "Python 3.10+ required"
"$PY" -c 'import flask' 2>/dev/null || die "python3-flask is not importable by $PY (sudo apt install python3-flask)"
command -v tmux >/dev/null || die "tmux not found"
command -v ttyd >/dev/null || warn "ttyd not found: web terminals will not work"

# ------------------------------------------------------------------ build zipapp
info "building ccdeck (zipapp) -> $TARGET"
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
mkdir -p "$BUILD/app"
cp -r "$SRC_DIR/ccdeck" "$BUILD/app/ccdeck"
find "$BUILD/app" -name '__pycache__' -prune -exec rm -rf {} +
cat > "$BUILD/app/__main__.py" <<'EOF'
import sys

from ccdeck.cli import main

sys.exit(main())
EOF
mkdir -p "$BIN_DIR"
"$PY" -m zipapp "$BUILD/app" -p "$PY" -o "$BUILD/ccdeck" -c
install -m 0755 "$BUILD/ccdeck" "$TARGET"
"$TARGET" --version

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) warn "$BIN_DIR is not in PATH. Add to ~/.profile:  export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac
if ! command -v claude >/dev/null 2>&1; then
  warn "claude not found in PATH. Install Claude Code first (ccdeck uses the existing installation)."
fi

# ------------------------------------------------------------------ config + systemd
export PATH="$BIN_DIR:$PATH"
info "creating config (~/.config/ccdeck) and the systemd user unit"
if [ -n "$BIND" ]; then "$TARGET" setup --force --bind "$BIND"; else "$TARGET" setup --force; fi

if [ "$DO_SERVICE" -eq 1 ]; then
  if [ "$DO_LINGER" -eq 1 ] && command -v loginctl >/dev/null 2>&1; then
    if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || echo no)" != "yes" ]; then
      info "enabling linger for $USER (services keep running after logout / start at boot)"
      $SUDO loginctl enable-linger "$USER" || warn "could not enable linger: sudo loginctl enable-linger $USER"
    fi
  fi
  if have_systemd_user; then
    info "starting ccdeck.service"
    systemctl --user daemon-reload
    systemctl --user enable ccdeck.service
    systemctl --user restart ccdeck.service
    sleep 2
    systemctl --user --no-pager --lines=5 status ccdeck.service || true
  else
    warn "systemd --user is not available in this shell (no user bus). Log in again, or run: ccdeck serve"
  fi
fi

echo
"$TARGET" doctor || true
echo
info "done."
echo "  Web UI : (login URLs with token: ccdeck url)"
"$TARGET" url | sed 's/?token=.*//; s/^/           /'
echo "  Server : ccdeck start | ccdeck status | ccdeck stop"
echo "  CLI    : ccdeck new myproj --dir ~/src/myproj && ccdeck ls"
BIND_NOW="$(sed -n 's/^bind[[:space:]]*=[[:space:]]*"\(.*\)".*/\1/p' "${XDG_CONFIG_HOME:-$HOME/.config}/ccdeck/config.toml" | head -1)"
echo "  Listen : ${BIND_NOW:-?} (change: ./install.sh --bind 127.0.0.1 | --bind 0.0.0.0)"
if [ "$BIND_NOW" = "0.0.0.0" ]; then
  echo "           all interfaces: if the page does not open from another PC, check the firewall"
  echo "           (e.g. sudo ufw allow 8787/tcp). Do not expose it to the internet (see README)."
fi
