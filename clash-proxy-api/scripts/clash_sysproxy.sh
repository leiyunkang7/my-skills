#!/usr/bin/env bash
# clash_sysproxy.sh — inspect/toggle the OS system proxy for Clash/mihomo setups
# that don't have their own toggle (e.g. mihomo CLI on Ubuntu, headless boxes).
#
# The Clash external-controller API CANNOT set the OS system proxy — that is an
# OS-level setting. This wraps macOS `networksetup` and GNOME `gsettings`, and
# always prints shell `export`/`unset` lines to stdout for headless/other shells.
#
# set -euo pipefail: -e exit on error, -u error on unset var, pipefail propagate
# failures through pipes.
set -euo pipefail

log() { printf '%s\n' "$*" >&2; }        # diagnostics -> stderr
die() { log "error: $1"; exit "${2:-1}"; }

DRY_RUN=0
YES=0
SERVICE=""     # macOS network service override
SOCKS=""       # optional HOST:PORT for SOCKS

usage() {
  cat <<'EOF'
clash_sysproxy.sh — inspect/toggle the OS system proxy

USAGE:
  clash_sysproxy.sh detect
  clash_sysproxy.sh on  HOST:PORT [--socks HOST:PORT] [--yes] [--dry-run] [--service NAME]
  clash_sysproxy.sh off [--yes] [--dry-run] [--service NAME]

COMMANDS:
  detect   Show current system-proxy state (and current $http_proxy).
  on       Point the OS system proxy at HOST:PORT (http + https). Add --socks
           for a separate SOCKS endpoint (a Clash mixed-port serves both, so
           pass the same HOST:PORT as --socks for mixed-port setups).
  off      Disable the OS system proxy.

FLAGS:
  --yes         Actually apply the change. Without it, `on`/`off` only preview.
  --dry-run     Preview the exact commands; change nothing (same as omitting --yes).
  --service N   macOS network service to target (default: the one backing the
                default route, else "Wi-Fi"). See `networksetup -listallnetworkservices`.
  --socks H:P   Also set the SOCKS proxy (HOST:PORT).
  -h, --help    This help.

BEHAVIOR:
  macOS   -> networksetup -setwebproxy/-setsecurewebproxy[/-setsocksfirewallproxy]
  GNOME   -> gsettings org.gnome.system.proxy (mode manual/none + host/port)
  other   -> no OS toggle available; still prints export/unset lines to stdout.

  stdout carries shell lines you can eval:  eval "$(clash_sysproxy.sh on 127.0.0.1:7890 --yes)"
  Diagnostics/plan go to stderr.

EXIT CODES:
  0 ok (or preview)   1 usage error   2 no OS toggler on this platform   3 apply failed
EOF
}

# --------------------------------------------------------------------------- #
# arg parsing
# --------------------------------------------------------------------------- #
[ $# -ge 1 ] || { usage; exit 1; }
CMD="$1"; shift || true
TARGET=""
case "$CMD" in
  on)
    [ $# -ge 1 ] || die "on requires HOST:PORT" 1
    TARGET="$1"; shift || true
    ;;
  detect|off) : ;;
  -h|--help) usage; exit 0 ;;
  *) die "unknown command: $CMD (use detect|on|off)" 1 ;;
esac

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --yes) YES=1 ;;
    --service) shift || true; SERVICE="${1:-}" ;;
    --socks) shift || true; SOCKS="${1:-}" ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown flag: $1" 1 ;;
  esac
  shift || true
done

split_host() { printf '%s' "${1%%:*}"; }
split_port() { printf '%s' "${1##*:}"; }

validate_target() {
  case "$1" in
    *:*) : ;;
    *) die "expected HOST:PORT, got: $1" 1 ;;
  esac
}

# apply(): run a mutating command, or echo it under dry-run / no --yes.
apply() {
  if [ "$DRY_RUN" -eq 1 ] || [ "$YES" -eq 0 ]; then
    log "[plan] $*"
    return 0
  fi
  log "+ $*"
  "$@" || die "command failed: $*" 3
}

confirm_note() {
  if [ "$DRY_RUN" -eq 0 ] && [ "$YES" -eq 0 ]; then
    log "(preview only — re-run with --yes to apply)"
  fi
}

# --------------------------------------------------------------------------- #
# macOS backend
# --------------------------------------------------------------------------- #
macos_service() {
  if [ -n "$SERVICE" ]; then printf '%s' "$SERVICE"; return 0; fi
  local dev name svc=""
  dev=$(route -n get default 2>/dev/null | awk '/interface:/{print $2; exit}') || true
  if [ -n "$dev" ]; then
    svc=$(networksetup -listnetworkserviceorder 2>/dev/null | awk -v dev="$dev" '
      /^\([0-9]+\)/ { name=$0; sub(/^\([0-9]+\) /,"",name); next }
      $0 ~ ("Device: " dev ")") { print name; exit }') || true
  fi
  [ -n "$svc" ] && { printf '%s' "$svc"; return 0; }
  printf '%s' "Wi-Fi"
}

macos_detect() {
  local svc; svc=$(macos_service)
  log "service: $svc"
  echo "# HTTP proxy:";  networksetup -getwebproxy "$svc" 2>/dev/null || true
  echo "# HTTPS proxy:"; networksetup -getsecurewebproxy "$svc" 2>/dev/null || true
  echo "# SOCKS proxy:"; networksetup -getsocksfirewallproxy "$svc" 2>/dev/null || true
}

macos_on() {
  local host port svc; host=$(split_host "$TARGET"); port=$(split_port "$TARGET"); svc=$(macos_service)
  log "macOS service: $svc  ->  http/https ${host}:${port}${SOCKS:+  socks ${SOCKS}}"
  apply networksetup -setwebproxy "$svc" "$host" "$port"
  apply networksetup -setsecurewebproxy "$svc" "$host" "$port"
  if [ -n "$SOCKS" ]; then
    apply networksetup -setsocksfirewallproxy "$svc" "$(split_host "$SOCKS")" "$(split_port "$SOCKS")"
  fi
}

macos_off() {
  local svc; svc=$(macos_service)
  log "macOS service: $svc  ->  disable web/secure/socks proxy"
  apply networksetup -setwebproxystate "$svc" off
  apply networksetup -setsecurewebproxystate "$svc" off
  apply networksetup -setsocksfirewallproxystate "$svc" off
}

# --------------------------------------------------------------------------- #
# GNOME backend
# --------------------------------------------------------------------------- #
gnome_available() {
  command -v gsettings >/dev/null 2>&1 &&
    gsettings writable org.gnome.system.proxy mode >/dev/null 2>&1
}

gnome_detect() {
  echo "# mode:";  gsettings get org.gnome.system.proxy mode 2>/dev/null || true
  echo "# http:";  gsettings get org.gnome.system.proxy.http host 2>/dev/null || true
                   gsettings get org.gnome.system.proxy.http port 2>/dev/null || true
  echo "# https:"; gsettings get org.gnome.system.proxy.https host 2>/dev/null || true
                   gsettings get org.gnome.system.proxy.https port 2>/dev/null || true
}

gnome_on() {
  local host port; host=$(split_host "$TARGET"); port=$(split_port "$TARGET")
  log "GNOME gsettings -> manual http/https ${host}:${port}${SOCKS:+  socks ${SOCKS}}"
  apply gsettings set org.gnome.system.proxy mode 'manual'
  apply gsettings set org.gnome.system.proxy.http host "$host"
  apply gsettings set org.gnome.system.proxy.http port "$port"
  apply gsettings set org.gnome.system.proxy.https host "$host"
  apply gsettings set org.gnome.system.proxy.https port "$port"
  if [ -n "$SOCKS" ]; then
    apply gsettings set org.gnome.system.proxy.socks host "$(split_host "$SOCKS")"
    apply gsettings set org.gnome.system.proxy.socks port "$(split_port "$SOCKS")"
  fi
}

gnome_off() {
  log "GNOME gsettings -> mode none"
  apply gsettings set org.gnome.system.proxy mode 'none'
}

# --------------------------------------------------------------------------- #
# shell env lines (always emitted to stdout — the parseable "data")
# --------------------------------------------------------------------------- #
emit_env_on() {
  local url="http://$TARGET"
  printf 'export http_proxy=%s https_proxy=%s all_proxy=%s\n' "$url" "$url" "${SOCKS:+socks5://$SOCKS}"
  printf 'export HTTP_PROXY=%s HTTPS_PROXY=%s ALL_PROXY=%s\n' "$url" "$url" "${SOCKS:+socks5://$SOCKS}"
}
emit_env_off() {
  printf 'unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY\n'
}

# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #
OS=$(uname -s 2>/dev/null || echo unknown)

case "$CMD" in
  detect)
    log "current shell env: http_proxy=${http_proxy:-} https_proxy=${https_proxy:-} all_proxy=${all_proxy:-}"
    case "$OS" in
      Darwin) command -v networksetup >/dev/null 2>&1 && macos_detect || log "networksetup unavailable" ;;
      Linux)  if gnome_available; then gnome_detect; else log "no GNOME gsettings proxy schema (headless/non-GNOME)"; fi ;;
      *)      log "unsupported platform: $OS (shell env shown above)" ;;
    esac
    ;;

  on)
    validate_target "$TARGET"
    [ -z "$SOCKS" ] || validate_target "$SOCKS"
    case "$OS" in
      Darwin)
        command -v networksetup >/dev/null 2>&1 || die "networksetup not found" 2
        macos_on ;;
      Linux)
        if gnome_available; then gnome_on
        else log "no OS system-proxy toggle here — use the shell env lines below (exported to child processes only)"
        fi ;;
      *) log "unsupported platform: $OS — use the shell env lines below" ;;
    esac
    emit_env_on
    confirm_note ;;

  off)
    case "$OS" in
      Darwin)
        command -v networksetup >/dev/null 2>&1 || die "networksetup not found" 2
        macos_off ;;
      Linux)
        if gnome_available; then gnome_off
        else log "no OS system-proxy toggle here — unset the shell vars below"
        fi ;;
      *) log "unsupported platform: $OS — unset the shell vars below" ;;
    esac
    emit_env_off
    confirm_note ;;
esac
