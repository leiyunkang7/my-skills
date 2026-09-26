#!/usr/bin/env python3
"""Portable Clash/mihomo external-controller helper for agents.

Talks to the Clash/mihomo REST API ("external-controller") to inspect and
control a running proxy: current node, proxy groups, latency, proxy mode
(rule/global/direct), TUN, config reload, connections, and egress IP.

Stdlib only — no `uv`, no third-party packages required. If PyYAML happens to
be importable it is used for richer local-config parsing; otherwise a small
regex scanner reads the two keys we need (external-controller, secret).

Controller discovery order (first REACHABLE wins for discovered sources):
  1. --controller / --secret flags            (trusted, not probed)
  2. $CLASH_CONTROLLER / $CLASH_SECRET env     (trusted, not probed)
  3. ~/.config/television/clash-source.sh controller   (optional TV hook)
  4. Local Clash/mihomo config file(s): external-controller + secret
  5. Common defaults: 127.0.0.1:9090, then 127.0.0.1:9097 (Clash Verge Rev)

Run `clash_api.py doctor` when nothing answers — it reports what was tried and
which client to enable the API on. See references/enable-api-by-client.md.

Exit codes:
  0  success
  1  usage / generic error
  2  group or proxy not found
  3  controller unreachable / discovery failed
  4  controller rejected the operation (HTTP >= 300 on a write)
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import ssl
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_DELAY_URL = "http://www.gstatic.com/generate_204"
DEFAULT_EGRESS_URL = "https://ipinfo.io/json"
DEFAULT_PORTS = (9090, 9097)  # 9090 classic clash/mihomo, 9097 Clash Verge Rev
TV_SOURCE = Path.home() / ".config" / "television" / "clash-source.sh"
CA_CERT: str | None = None
READ_ONLY = False
MAX_RESPONSE = 8 * 1024 * 1024


# --------------------------------------------------------------------------- #
# Errors (each maps to a distinct exit code in main())
# --------------------------------------------------------------------------- #
class ClashError(RuntimeError):
    """Generic failure (exit 1)."""


class NotFound(ClashError):
    """A named group/proxy does not exist (exit 2)."""


class ControllerUnreachable(ClashError):
    """No controller answered (exit 3)."""


class OpRejected(ClashError):
    """Controller returned HTTP >= 300 on a write (exit 4)."""


def log(msg: str) -> None:
    """Diagnostics go to stderr so stdout stays parseable."""
    print(msg, file=sys.stderr)


# --------------------------------------------------------------------------- #
# HTTP core
# --------------------------------------------------------------------------- #
def strip_scheme(value: str) -> str:
    """Legacy name; preserve the scheme and accept only a controller origin."""
    value = value.strip()
    parsed = urllib.parse.urlsplit(value if "://" in value else "http://" + value)
    try:
        valid = parsed.port is not None and 1 <= parsed.port <= 65535
    except ValueError:
        valid = False
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or not valid
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            or any(c.isspace() or ord(c) < 32 for c in value)):
        raise ClashError("controller must be an http(s)://host:port origin without credentials, path, query or fragment")
    return f"{parsed.scheme}://{parsed.netloc}"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def configure_transport(args: argparse.Namespace) -> None:
    global CA_CERT, READ_ONLY
    CA_CERT = getattr(args, "ca_cert", None) or os.environ.get("CLASH_CA_CERT")
    READ_ONLY = bool(getattr(args, "read_only", False))
    if CA_CERT:
        try:
            ssl.create_default_context(cafile=CA_CERT)
        except (OSError, ssl.SSLError) as exc:
            raise ClashError("cannot load CA certificate; supply a readable PEM CA file") from exc


def controller_open(host: str, secret: str, method: str, endpoint: str,
                    *, payload=None, timeout: float = 5):
    if READ_ONLY and method != "GET":
        raise OpRejected("read-only mode rejects controller mutations")
    if any(c in secret for c in "\r\n\0"):
        raise ClashError("controller secret must be a single token")
    if not endpoint or endpoint.startswith(("/", "http:", "https:")):
        raise ClashError("controller endpoint must be relative")
    headers = {"Accept": "application/json"}
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=CA_CERT)),
    )
    req = urllib.request.Request(f"{strip_scheme(host)}/{endpoint}", data=data,
                                 headers=headers, method=method)
    return opener.open(req, timeout=timeout)


def read_secret_file(path: str) -> str:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_size > 8192):
                raise ClashError("secret file must be a single-link mode-0600 regular file, at most 8192 bytes")
            value = stream.read(8193).decode("utf-8").strip()
            after = os.fstat(stream.fileno())
            if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                raise ClashError("secret file changed during reading")
            if not value or any(c in value for c in "\r\n\0"):
                raise ClashError("secret file must contain one non-empty token")
            return value
    except (OSError, UnicodeError) as exc:
        raise ClashError("cannot safely read secret file; check its path, ownership and mode") from exc


def decode_json(body: bytes) -> Any:
    if not body:
        return None
    text = body.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def request_json(
    host: str,
    secret: str,
    method: str,
    endpoint: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: float = 5,
) -> tuple[int, Any]:
    try:
        with controller_open(host, secret, method, endpoint, payload=payload, timeout=timeout) as resp:
            body = resp.read(MAX_RESPONSE + 1)
            if len(body) > MAX_RESPONSE:
                raise ClashError("controller response exceeds 8 MiB")
            return resp.status, decode_json(body)
    except urllib.error.HTTPError as exc:
        # Error bodies can echo credentials or subscription URLs.
        exc.close()
        return exc.code, None
    except urllib.error.URLError as exc:
        raise ControllerUnreachable(f"controller {host} unreachable: {exc.reason}") from exc
    except (TimeoutError, OSError) as exc:
        raise ControllerUnreachable(f"controller {host} unreachable: {exc}") from exc


def probe(host: str, secret: str, timeout: float = 1.5) -> bool:
    """True if GET /version answers with 2xx."""
    try:
        status, _ = request_json(host, secret, "GET", "version", timeout=timeout)
    except ClashError:
        return False
    return status < 300


# --------------------------------------------------------------------------- #
# Local config discovery (for controller creds + reload path)
# --------------------------------------------------------------------------- #
def _config_dirs() -> list[Path]:
    home = Path.home()
    return [
        home / ".config" / "clash",
        home / ".config" / "mihomo",
        home / ".config" / "clash-verge",
        home / ".config" / "clash-verge-rev",
        home / "Library" / "Application Support" / "clash",
        home / "Library" / "Application Support" / "mihomo",
        home / "Library" / "Application Support" / "io.github.clash-verge-rev.clash-verge-rev",
        home / "Library" / "Application Support" / "com.github.zzzgydi.clashverge",
        home / "Library" / "Application Support" / "ClashX",
        home / "Library" / "Application Support" / "ClashMetaX",
    ]


def _runtime_config_paths() -> list[Path]:
    paths: list[Path] = []
    for base in _config_dirs():
        for name in ("config.yaml", "config.yml"):
            paths.append(base / name)
    return paths


def _yaml_scalar(raw: str) -> str:
    """Extract a scalar from `key: value`'s value part (regex-parse fallback)."""
    v = raw.strip()
    if not v:
        return ""
    if v[0] in "'\"":
        quote = v[0]
        end = v.find(quote, 1)
        return v[1:end] if end > 0 else v[1:]
    for i, ch in enumerate(v):  # strip inline comment (space-prefixed #)
        if ch == "#" and (i == 0 or v[i - 1] in " \t"):
            v = v[:i]
            break
    return v.strip()


def _scan_controller(path: Path) -> tuple[str, str]:
    """Return (external-controller, secret) from a config file (top-level keys)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", ""
    controller, secret = "", ""
    for line in text.splitlines():
        if line[:1] in (" ", "\t"):  # only top-level (unindented) keys
            continue
        if not controller and line.startswith("external-controller:"):
            controller = _yaml_scalar(line.split(":", 1)[1])
        elif not secret and line.startswith("secret:"):
            secret = _yaml_scalar(line.split(":", 1)[1])
    return controller, secret


def config_path() -> tuple[Path | None, str | None]:
    """Resolve the active config file. $CLASH_CONFIG set-but-missing is an error
    (never silently fall back — could grab another config and leak its secret)."""
    env = os.environ.get("CLASH_CONFIG", "").strip()
    if env:
        p = Path(env).expanduser()
        try:
            if p.is_file():
                return p, None
        except OSError as exc:
            return None, f"CLASH_CONFIG unreadable: {p} ({exc})"
        return None, f"CLASH_CONFIG path not found: {p}"
    for p in _runtime_config_paths():
        try:
            if p.is_file():
                return p, None
        except OSError:
            continue
    return None, "no config found (set CLASH_CONFIG or place under ~/.config/clash/)"


def _config_controller_candidates() -> list[tuple[str, str, str]]:
    """(host, secret, source) parsed from local config files."""
    out: list[tuple[str, str, str]] = []
    env = os.environ.get("CLASH_CONFIG", "").strip()
    files = [Path(env).expanduser()] if env else _runtime_config_paths()
    for p in files:
        try:
            if not p.is_file():
                continue
        except OSError:
            continue
        host, secret = _scan_controller(p)
        if host:
            out.append((strip_scheme(host), secret, f"config: {p}"))
    return out


def _tv_controller() -> tuple[str, str] | None:
    if not (TV_SOURCE.exists() and os.access(TV_SOURCE, os.X_OK)):
        return None
    try:
        proc = subprocess.run(
            [str(TV_SOURCE), "controller"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    lines = proc.stdout.splitlines()
    if lines and lines[0].strip():
        secret = lines[1].strip() if len(lines) > 1 else ""
        return strip_scheme(lines[0].strip()), secret
    return None


# --------------------------------------------------------------------------- #
# Controller resolution
# --------------------------------------------------------------------------- #
def _env_secret() -> str:
    return os.environ.get("CLASH_SECRET", "").strip()


def discover(args: argparse.Namespace) -> tuple[str, str, str]:
    """Return (host, secret, source). Raises ControllerUnreachable with the
    list of things tried if nothing answers."""
    override = getattr(args, "secret", None)  # --secret always wins when given
    secret_file = getattr(args, "secret_file", None) or os.environ.get("CLASH_SECRET_FILE")
    if override is None and secret_file:
        override = read_secret_file(secret_file)

    if getattr(args, "controller", None):
        host = strip_scheme(args.controller)
        return host, override if override is not None else _env_secret(), "flag: --controller"

    env_host = os.environ.get("CLASH_CONTROLLER", "").strip()
    if env_host:
        return strip_scheme(env_host), override if override is not None else _env_secret(), "env: CLASH_CONTROLLER"

    candidates: list[tuple[str, str, str]] = []
    tv = _tv_controller()
    if tv:
        candidates.append((tv[0], tv[1], "tv: clash-source.sh"))
    candidates.extend(_config_controller_candidates())
    for port in DEFAULT_PORTS:
        candidates.append((f"127.0.0.1:{port}", "", f"default: 127.0.0.1:{port}"))

    tried: list[str] = []
    seen: set[tuple[str, str]] = set()
    for host, secret, source in candidates:
        if not host:
            continue
        secret = override if override is not None else secret
        key = (host, secret)
        if key in seen:
            continue
        seen.add(key)
        if probe(host, secret):
            return host, secret, source
        tried.append(f"{source} ({host})")

    raise ControllerUnreachable(
        "no reachable controller. Tried: "
        + ("; ".join(tried) if tried else "(no candidates)")
        + ". Run `clash_api.py doctor`, set CLASH_CONTROLLER=host:port, "
        "or see references/enable-api-by-client.md."
    )


# --------------------------------------------------------------------------- #
# Shared getters
# --------------------------------------------------------------------------- #
def get_configs(host: str, secret: str) -> dict[str, Any]:
    status, data = request_json(host, secret, "GET", "configs", timeout=3)
    if status >= 300 or not isinstance(data, dict):
        return {}
    return data


def get_proxies(host: str, secret: str) -> dict[str, Any]:
    status, data = request_json(host, secret, "GET", "proxies")
    if status >= 300:
        raise ControllerUnreachable(f"GET /proxies failed with HTTP {status}: {data}")
    proxies = data.get("proxies") if isinstance(data, dict) else None
    if not isinstance(proxies, dict):
        raise ClashError("GET /proxies returned an unexpected payload")
    return proxies


def selector_items(proxies: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    items = [
        (name, value)
        for name, value in proxies.items()
        if isinstance(value, dict) and isinstance(value.get("all"), list)
    ]
    # GLOBAL last, otherwise case-insensitive by name.
    return sorted(items, key=lambda kv: (kv[0] == "GLOBAL", kv[0].lower()))


def emit_json(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


# Credential-bearing keys that GET /configs (or nested config) may expose.
_SECRET_KEYS = {
    "secret", "authentication", "users", "password", "uuid", "psk",
    "token", "private-key", "privatekey", "auth", "ca-str", "key",
}


def _redact_secrets(obj: Any) -> Any:
    """Recursively mask credential values so `config` never echoes them."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in _SECRET_KEYS and v not in (None, "", [], {}, 0, False):
                out[k] = "<redacted>"
            else:
                out[k] = _redact_secrets(v)
        return out
    if isinstance(obj, list):
        return [_redact_secrets(x) for x in obj]
    return obj


def _ports(configs: dict[str, Any]) -> dict[str, int]:
    out = {}
    for key in ("mixed-port", "port", "socks-port", "redir-port", "tproxy-port"):
        val = configs.get(key)
        if isinstance(val, int) and val > 0:
            out[key] = val
    return out


def _default_proxy(host: str, secret: str) -> str:
    """A remote controller does not imply a reachable local mixed port."""
    hostname = urllib.parse.urlsplit(strip_scheme(host)).hostname
    if hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ClashError("remote controller: supply --proxy explicitly, or use the device's proxy-health command")
    configs = get_configs(host, secret)
    for key in ("mixed-port", "port"):
        val = configs.get(key)
        if isinstance(val, int) and val > 0:
            return f"http://127.0.0.1:{val}"
    raise ClashError("no HTTP/mixed port advertised; supply --proxy explicitly")


# --------------------------------------------------------------------------- #
# Commands — read
# --------------------------------------------------------------------------- #
def cmd_status(args: argparse.Namespace) -> int:
    host, secret, source = discover(args)
    vstatus, version = request_json(host, secret, "GET", "version", timeout=3)
    if vstatus >= 300:
        raise ControllerUnreachable(f"GET /version failed with HTTP {vstatus}: {version}")
    configs = get_configs(host, secret)
    proxies = get_proxies(host, secret)

    ver = version.get("version", "?") if isinstance(version, dict) else str(version)
    meta = bool(version.get("meta")) if isinstance(version, dict) else False
    mode = configs.get("mode", "?")
    tun = configs.get("tun") if isinstance(configs.get("tun"), dict) else {}
    tun_on = bool(tun.get("enable"))
    ports = _ports(configs)

    groups = [
        {
            "name": name,
            "now": value.get("now"),
            "type": value.get("type"),
            "members": len(value.get("all") or []),
        }
        for name, value in selector_items(proxies)
    ]

    if args.json:
        emit_json(
            {
                "controller": host,
                "source": source,
                "secret": bool(secret),
                "version": ver,
                "meta": meta,
                "mode": mode,
                "tun": tun_on,
                "ports": ports,
                "groups": groups,
            }
        )
        return 0

    print(f"controller: {host}  ({source})")
    print(f"secret: {'yes' if secret else 'no'}")
    print(f"version: {ver}{' (mihomo/meta)' if meta else ''}")
    print(f"mode: {mode}    tun: {'on' if tun_on else 'off'}")
    if ports:
        print("ports: " + ", ".join(f"{k}={v}" for k, v in ports.items()))
    print()
    print("groups:")
    for g in groups:
        print(f"- {g['name']}: now={g['now']} type={g['type']} members={g['members']}")
    return 0


def cmd_config_get(args: argparse.Namespace) -> int:
    host, secret, _ = discover(args)
    status, data = request_json(host, secret, "GET", "configs", timeout=3)
    if status >= 300:
        raise OpRejected(f"GET /configs failed with HTTP {status}: {data}")
    if isinstance(data, (dict, list)) and not args.show_secrets:
        data = _redact_secrets(data)
    emit_json(data)
    return 0


def cmd_groups(args: argparse.Namespace) -> int:
    host, secret, _ = discover(args)
    proxies = get_proxies(host, secret)
    items = selector_items(proxies)
    if args.json:
        emit_json(
            [
                {
                    "name": n,
                    "now": v.get("now"),
                    "type": v.get("type"),
                    "all": v.get("all") or [],
                }
                for n, v in items
            ]
        )
        return 0
    for name, value in items:
        print(f"{name}\tnow={value.get('now', '-')}\ttype={value.get('type', '-')}")
        if args.members:
            for member in value.get("all") or []:
                print(f"  - {member}")
    return 0


def cmd_proxies(args: argparse.Namespace) -> int:
    host, secret, _ = discover(args)
    proxies = get_proxies(host, secret)
    group_names = {name for name, _ in selector_items(proxies)}
    rows = []
    for name in sorted(proxies, key=str.lower):
        if name in group_names:
            continue
        value = proxies[name]
        if not isinstance(value, dict):
            continue
        if args.filter and args.filter.lower() not in name.lower():
            continue
        rows.append((name, value))
    if args.json:
        emit_json(
            [
                {
                    "name": n,
                    "type": v.get("type"),
                    "alive": v.get("alive"),
                    "udp": v.get("udp"),
                }
                for n, v in rows
            ]
        )
        return 0
    for name, value in rows:
        print(
            f"{name}\ttype={value.get('type', '-')}"
            f"\talive={value.get('alive', '-')}\tudp={value.get('udp', '-')}"
        )
    return 0


def cmd_rules(args: argparse.Namespace) -> int:
    host, secret, _ = discover(args)
    status, data = request_json(host, secret, "GET", "rules")
    if status >= 300:
        raise OpRejected(f"GET /rules failed with HTTP {status}: {data}")
    rules = data.get("rules") if isinstance(data, dict) else None
    if not isinstance(rules, list):
        raise ClashError("GET /rules returned an unexpected payload")
    if args.filter:
        needle = args.filter.lower()
        rules = [
            r
            for r in rules
            if isinstance(r, dict)
            and (needle in str(r.get("payload", "")).lower() or needle in str(r.get("proxy", "")).lower())
        ]
    if args.json:
        emit_json(rules)
        return 0
    for r in rules:
        if isinstance(r, dict):
            print(f"{r.get('type', '-')}\t{r.get('payload', '-')}\t-> {r.get('proxy', '-')}")
    return 0


def cmd_delay(args: argparse.Namespace) -> int:
    host, secret, _ = discover(args)
    encoded = urllib.parse.quote(args.proxy, safe="")
    query = urllib.parse.urlencode({"url": args.url, "timeout": str(args.timeout_ms)})
    status, data = request_json(
        host, secret, "GET", f"proxies/{encoded}/delay?{query}", timeout=(args.timeout_ms / 1000) + 2
    )
    if status >= 300:
        raise OpRejected(f"delay test for {args.proxy!r} failed with HTTP {status}: {data}")
    if args.json:
        emit_json(data)
        return 0
    if isinstance(data, dict) and "delay" in data:
        print(f"{args.proxy}: {data['delay']} ms")
    else:
        emit_json(data)
    return 0


def cmd_group_delay(args: argparse.Namespace) -> int:
    host, secret, _ = discover(args)
    encoded = urllib.parse.quote(args.group, safe="")
    query = urllib.parse.urlencode({"url": args.url, "timeout": str(args.timeout_ms)})
    status, data = request_json(
        host, secret, "GET", f"group/{encoded}/delay?{query}", timeout=(args.timeout_ms / 1000) + 3
    )
    if status >= 300:
        raise OpRejected(
            f"group-delay for {args.group!r} failed with HTTP {status}: {data}. "
            "This endpoint needs a mihomo (Clash.Meta) core."
        )
    if args.json:
        emit_json(data)
        return 0
    if isinstance(data, dict):
        for name, delay in sorted(data.items(), key=lambda kv: (kv[1] == 0, kv[1])):
            print(f"{name}: {delay} ms" if delay else f"{name}: timeout")
    else:
        emit_json(data)
    return 0


def cmd_egress(args: argparse.Namespace) -> int:
    proxy = args.proxy
    if proxy is None:
        host, secret, _ = discover(args)
        proxy = _default_proxy(host, secret)
    log(f"probing egress through {proxy}")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    req = urllib.request.Request(args.url, headers={"Accept": "application/json"})
    try:
        with opener.open(req, timeout=args.timeout) as resp:
            body = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ControllerUnreachable(f"egress probe via {proxy} failed: {exc}") from exc
    data = decode_json(body)
    if isinstance(data, (dict, list)):
        emit_json(data)
    else:
        print(data)
    return 0


# --------------------------------------------------------------------------- #
# Commands — write
# --------------------------------------------------------------------------- #
def _patch_configs(host: str, secret: str, payload: dict[str, Any], what: str) -> None:
    status, data = request_json(host, secret, "PATCH", "configs", payload=payload, timeout=5)
    if status >= 300:
        raise OpRejected(f"{what} failed with HTTP {status}: {data}")


def cmd_switch(args: argparse.Namespace) -> int:
    host, secret, _ = discover(args)
    proxies = get_proxies(host, secret)
    group = proxies.get(args.group)
    if not isinstance(group, dict) or not isinstance(group.get("all"), list):
        raise NotFound(f"group not found or not selectable: {args.group!r}")
    if args.proxy not in group["all"]:
        members = ", ".join(group["all"][:20])
        raise NotFound(
            f"proxy {args.proxy!r} is not a member of group {args.group!r}. Members: {members}"
        )
    if args.dry_run:
        log(f"[dry-run] would PUT /proxies/{args.group} -> {args.proxy} (now: {group.get('now')})")
        return 0
    encoded = urllib.parse.quote(args.group, safe="")
    status, data = request_json(host, secret, "PUT", f"proxies/{encoded}", payload={"name": args.proxy}, timeout=5)
    if status >= 300:
        raise OpRejected(f"switch failed with HTTP {status}: {data}")
    print(f"switched {args.group} -> {args.proxy}")
    return 0


def cmd_mode(args: argparse.Namespace) -> int:
    host, secret, _ = discover(args)
    if args.dry_run:
        log(f"[dry-run] would PATCH /configs {{'mode': {args.value!r}}}")
        return 0
    _patch_configs(host, secret, {"mode": args.value}, f"set mode={args.value}")
    print(f"mode -> {args.value}")
    return 0


def cmd_tun(args: argparse.Namespace) -> int:
    host, secret, _ = discover(args)
    enable = args.state == "on"
    if args.dry_run:
        log(f"[dry-run] would PATCH /configs {{'tun': {{'enable': {enable}}}}}"
            + (" then POST /restart" if args.restart else ""))
        return 0
    _patch_configs(host, secret, {"tun": {"enable": enable}}, f"set tun.enable={enable}")
    print(f"tun -> {'on' if enable else 'off'}")
    if args.restart:
        status, data = request_json(host, secret, "POST", "restart", payload={}, timeout=8)
        if status >= 300:
            raise OpRejected(f"core restart failed with HTTP {status}: {data}")
        print("core restarted")
    else:
        log("note: enabling TUN needs an elevated core (Service Mode / root). "
            "If it does not take effect, restart the core (add --restart) and enable Service Mode.")
    return 0


def cmd_allow_lan(args: argparse.Namespace) -> int:
    host, secret, _ = discover(args)
    enable = args.state == "on"
    if args.dry_run:
        log(f"[dry-run] would PATCH /configs {{'allow-lan': {enable}}}")
        return 0
    _patch_configs(host, secret, {"allow-lan": enable}, f"set allow-lan={enable}")
    print(f"allow-lan -> {'on' if enable else 'off'}")
    return 0


def cmd_reload(args: argparse.Namespace) -> int:
    host, secret, source = discover(args)
    if args.path:
        path = str(Path(args.path).expanduser())
    else:
        found, err = config_path()
        if found is None:
            raise ClashError(f"cannot resolve config path to reload: {err}. Pass --path.")
        path = str(found)
        if source.startswith("env") or source.startswith("flag"):
            log("note: remote/explicit controller — --path must be a path the CONTROLLER host can read.")
    if args.dry_run:
        log(f"[dry-run] would PUT /configs?force=true {{'path': {path!r}}} on {host}")
        return 0
    status, data = request_json(
        host, secret, "PUT", "configs?force=true", payload={"path": path}, timeout=10
    )
    if status >= 300:
        raise OpRejected(f"reload failed with HTTP {status}: {data}")
    print(f"reloaded config from {path}")
    return 0


def cmd_connections(args: argparse.Namespace) -> int:
    host, secret, _ = discover(args)
    if args.close:
        if args.id:
            if args.dry_run:
                log(f"[dry-run] would DELETE /connections/{args.id}")
                return 0
            status, data = request_json(host, secret, "DELETE", f"connections/{urllib.parse.quote(args.id, safe='')}", timeout=5)
            target = f"connection {args.id}"
        elif args.all:
            if args.dry_run:
                log("[dry-run] would DELETE /connections (close all)")
                return 0
            if not args.yes:
                raise ClashError("refusing to close ALL connections without --yes (pass `close --all --yes`)")
            status, data = request_json(host, secret, "DELETE", "connections", timeout=5)
            target = "all connections"
        else:
            raise ClashError("`connections close` needs --id <id> or --all --yes")
        if status >= 300:
            raise OpRejected(f"close {target} failed with HTTP {status}: {data}")
        print(f"closed {target}")
        return 0

    status, data = request_json(host, secret, "GET", "connections", timeout=5)
    if status >= 300:
        raise OpRejected(f"GET /connections failed with HTTP {status}: {data}")
    if args.json:
        emit_json(data)
        return 0
    conns = data.get("connections") if isinstance(data, dict) else None
    count = len(conns) if isinstance(conns, list) else 0
    up = data.get("uploadTotal") if isinstance(data, dict) else None
    down = data.get("downloadTotal") if isinstance(data, dict) else None
    print(f"connections: {count}  uploadTotal={up}  downloadTotal={down}")
    return 0


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #
def _detect_clients() -> list[str]:
    found: list[str] = []
    for d in _config_dirs():
        try:
            if d.is_dir():
                found.append(f"config dir: {d}")
        except OSError:
            continue
    if shutil.which("pgrep"):
        for proc in ("mihomo", "clash", "clash-verge", "Clash Verge", "ClashX", "clash-meta"):
            try:
                rc = subprocess.run(["pgrep", "-if", proc], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=2, check=False).returncode
            except (OSError, subprocess.SubprocessError):
                continue
            if rc == 0:
                found.append(f"process: {proc}")
    return found


def cmd_doctor(args: argparse.Namespace) -> int:
    report: dict[str, Any] = {
        "os": platform.system(),
        "python": platform.python_version(),
        "curl": bool(shutil.which("curl")),
        "yaml_module": _has_yaml(),
        "tv_hook": TV_SOURCE.exists(),
        "env": {
            "CLASH_CONTROLLER": os.environ.get("CLASH_CONTROLLER", ""),
            "CLASH_SECRET_set": bool(os.environ.get("CLASH_SECRET")),
            "CLASH_CONFIG": os.environ.get("CLASH_CONFIG", ""),
        },
        "detected_clients": _detect_clients(),
    }
    cfg_path, cfg_err = config_path()
    report["config_file"] = str(cfg_path) if cfg_path else None
    report["config_error"] = cfg_err

    try:
        host, secret, source = discover(args)
        configs = get_configs(host, secret)
        report["controller"] = {
            "reachable": True,
            "host": host,
            "source": source,
            "secret": bool(secret),
            "mode": configs.get("mode"),
            "tun": bool(configs.get("tun", {}).get("enable")) if isinstance(configs.get("tun"), dict) else None,
            "ports": _ports(configs),
        }
        report["ok"] = True
    except ControllerUnreachable as exc:
        report["controller"] = {"reachable": False, "detail": str(exc)}
        report["ok"] = False
        report["next"] = (
            "No controller answered. Enable the external-controller API on your client "
            "(see references/enable-api-by-client.md), or set "
            "CLASH_CONTROLLER=host:port [CLASH_SECRET=...]."
        )

    if args.json:
        emit_json(report)
        return 0 if report.get("ok") else 3

    print(f"os: {report['os']}   python: {report['python']}   curl: {report['curl']}   yaml: {report['yaml_module']}")
    if report["env"]["CLASH_CONTROLLER"]:
        print(f"env CLASH_CONTROLLER: {report['env']['CLASH_CONTROLLER']}")
    print(f"config file: {report['config_file'] or '(none) — ' + (cfg_err or '')}")
    if report["detected_clients"]:
        print("detected: " + "; ".join(report["detected_clients"]))
    ctrl = report["controller"]
    if ctrl.get("reachable"):
        print(f"\ncontroller OK: {ctrl['host']} ({ctrl['source']})  "
              f"mode={ctrl['mode']} tun={ctrl['tun']} secret={'yes' if ctrl['secret'] else 'no'}")
        print("try: status | groups --members | mode rule | tun on --restart")
        return 0
    print(f"\ncontroller: UNREACHABLE\n  {ctrl['detail']}")
    print(f"\n{report['next']}")
    return 3


def _has_yaml() -> bool:
    try:
        import yaml  # noqa: F401
        return True
    except ImportError:
        return False


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
EPILOG = """\
examples:
  clash_api.py doctor                 # discover controller / diagnose
  clash_api.py status                 # node, mode, tun, ports, groups
  clash_api.py groups --members
  clash_api.py delay "JP-01"
  clash_api.py switch PROXY "JP-01"
  clash_api.py mode global
  clash_api.py tun on --restart
  clash_api.py reload --dry-run
  clash_api.py connections close --all --yes
  CLASH_CONTROLLER=192.168.1.9:9090 CLASH_SECRET=xxx clash_api.py status

exit codes: 0 ok | 1 usage | 2 group/proxy not found | 3 controller unreachable | 4 op rejected
"""


def _add_dry_run(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dry-run", action="store_true", help="Show what would change; make no request.")


def _add_json(p: argparse.ArgumentParser) -> None:
    p.add_argument("--json", action="store_true", help="Emit JSON on stdout.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect and control a Clash/mihomo external-controller API.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--controller", help="Controller host:port or URL. Default: discovery.")
    credentials = parser.add_mutually_exclusive_group()
    credentials.add_argument("--secret", help="Controller secret (prefer --secret-file to avoid process-argument exposure).")
    credentials.add_argument("--secret-file", help="Mode-0600 file containing the controller secret. Env: CLASH_SECRET_FILE.")
    parser.add_argument("--ca-cert", help="PEM CA for HTTPS; certificate and hostname verification stay enabled. Env: CLASH_CA_CERT.")
    parser.add_argument("--read-only", action="store_true", help="Reject mutations and unredacted config output.")
    sub = parser.add_subparsers(dest="command", required=True)

    dc = sub.add_parser("doctor", help="Diagnose discovery + reachability; guide enabling the API.")
    _add_json(dc)
    dc.set_defaults(func=cmd_doctor)
    st = sub.add_parser("status", help="Controller, version, mode, tun, ports, groups.")
    _add_json(st)
    st.set_defaults(func=cmd_status)
    cf = sub.add_parser("config", help="GET /configs (credential fields redacted).")
    cf.add_argument("--show-secrets", action="store_true", help="Do NOT redact secret/authentication/credential fields.")
    cf.set_defaults(func=cmd_config_get)

    g = sub.add_parser("groups", help="List selectable proxy groups.")
    g.add_argument("--members", action="store_true", help="Print group members.")
    _add_json(g)
    g.set_defaults(func=cmd_groups)

    px = sub.add_parser("proxies", help="List leaf proxies.")
    px.add_argument("--filter", help="Case-insensitive proxy name substring.")
    _add_json(px)
    px.set_defaults(func=cmd_proxies)

    rl = sub.add_parser("rules", help="List routing rules.")
    rl.add_argument("--filter", help="Case-insensitive payload/proxy substring.")
    _add_json(rl)
    rl.set_defaults(func=cmd_rules)

    dl = sub.add_parser("delay", help="Latency-test one proxy via /proxies/:name/delay.")
    dl.add_argument("proxy")
    dl.add_argument("--url", default=DEFAULT_DELAY_URL)
    dl.add_argument("--timeout-ms", type=int, default=5000)
    _add_json(dl)
    dl.set_defaults(func=cmd_delay)

    gd = sub.add_parser("group-delay", help="Latency-test all members of a group (mihomo).")
    gd.add_argument("group")
    gd.add_argument("--url", default=DEFAULT_DELAY_URL)
    gd.add_argument("--timeout-ms", type=int, default=5000)
    _add_json(gd)
    gd.set_defaults(func=cmd_group_delay)

    sw = sub.add_parser("switch", help="Point a selectable group at a member proxy.")
    sw.add_argument("group")
    sw.add_argument("proxy")
    _add_dry_run(sw)
    sw.set_defaults(func=cmd_switch)

    md = sub.add_parser("mode", help="Set proxy mode.")
    md.add_argument("value", choices=["rule", "global", "direct"])
    _add_dry_run(md)
    md.set_defaults(func=cmd_mode)

    tn = sub.add_parser("tun", help="Enable/disable TUN via PATCH /configs.")
    tn.add_argument("state", choices=["on", "off"])
    tn.add_argument("--restart", action="store_true", help="POST /restart afterwards (TUN often needs it).")
    _add_dry_run(tn)
    tn.set_defaults(func=cmd_tun)

    al = sub.add_parser("allow-lan", help="Enable/disable allow-lan via PATCH /configs.")
    al.add_argument("state", choices=["on", "off"])
    _add_dry_run(al)
    al.set_defaults(func=cmd_allow_lan)

    rd = sub.add_parser("reload", help="Reload config file via PUT /configs?force=true.")
    rd.add_argument("--path", help="Config path the controller host should load. Default: discovery.")
    _add_dry_run(rd)
    rd.set_defaults(func=cmd_reload)

    cn = sub.add_parser("connections", help="Show connections, or close them.")
    cn.add_argument("close", nargs="?", choices=["close"], help="Close connections instead of listing.")
    cn.add_argument("--id", help="Close a single connection id.")
    cn.add_argument("--all", dest="all", action="store_true", help="Close every connection (needs --yes).")
    cn.add_argument("--yes", action="store_true", help="Confirm closing all connections.")
    _add_dry_run(cn)
    _add_json(cn)
    cn.set_defaults(func=cmd_connections)

    eg = sub.add_parser("egress", help="Fetch an IP-info URL through the local proxy.")
    eg.add_argument("--proxy", default=None, help="Proxy URL. Default: derived from live config (mixed/http port).")
    eg.add_argument("--url", default=DEFAULT_EGRESS_URL)
    eg.add_argument("--timeout", type=float, default=10)
    eg.set_defaults(func=cmd_egress)

    ob = sub.add_parser("observe", help="Observe one hostname's routing for a bounded window; reproduce the request during capture.")
    ob.add_argument("domain")
    ob.add_argument("--client", help="Filter by source IP (recommended for shared controllers).")
    ob.add_argument("--duration", type=float, default=15, help="Observation seconds (1-60).")
    ob.set_defaults(func=cmd_observe)

    return parser


def cmd_observe(args: argparse.Namespace) -> int:
    from clash_observe import observe
    host, secret, _ = discover(args)
    emit_json(observe(sys.modules[__name__], host, secret, args.domain,
                      client=args.client, duration=args.duration))
    return 0


def main() -> int:
    args = build_parser().parse_args()
    # normalize: `connections close` sets close truthy
    if getattr(args, "close", None) == "close":
        args.close = True
    try:
        configure_transport(args)
        if READ_ONLY and (args.command in {"switch", "mode", "tun", "allow-lan", "reload"}
                          or getattr(args, "close", False) or getattr(args, "show_secrets", False)):
            raise OpRejected("read-only mode rejects this command")
        return args.func(args)
    except NotFound as exc:
        log(f"error: {exc}")
        return 2
    except ControllerUnreachable as exc:
        log(f"error: {exc}")
        return 3
    except OpRejected as exc:
        log(f"error: {exc}")
        return 4
    except ClashError as exc:
        log(f"error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
