---
name: clash-proxy-api
description: Use when diagnosing an unreachable website through Clash/mihomo, observing matched rules and proxy chains, checking node/mode/latency, or controlling Clash Verge, ClashX, TUN, Mixin and the system proxy. Supports explicit HTTPS controllers, private CA verification and bounded read-only routing observations; respects managed-router transaction workflows.
---

# Clash Proxy API

Operate a running Clash / mihomo proxy from natural-language requests — "what's my
current node?", "switch to the JP node", "am I in global or rule mode?", "turn on
TUN", "reload my config", "the proxy isn't working", "there's no system-proxy
toggle on my Ubuntu box" — without re-discovering controller details or
hand-writing `curl`.

The reliable core is the **external-controller REST API**. Bundled scripts wrap
it, bounded routing observation and the OS system proxy. On a managed router,
read its project contract before choosing a command.

> Invocations below are relative to this skill's directory. Run `python3
> scripts/clash_api.py …` from here, or use the absolute path your agent resolves
> for the skill. `clash_api.py` is **stdlib-only** — no `uv`, no pip installs.

## When to use

- "What node/mode am I on?" → `status`. "List my groups / nodes" → `groups` / `proxies`.
- "Switch PROXY to the Japan node" → `delay` then `switch`.
- "Go global / rule / direct mode" → `mode`.
- "Turn TUN on/off", "enable transparent proxy" → `tun`.
- "Reload my config", "close all connections", "show my egress IP" → `reload` / `connections` / `egress`.
- "Is the Clash API even on? / it's not working" → `doctor`, then `references/enable-api-by-client.md`.
- "There's no system-proxy toggle" (mihomo CLI, headless, Ubuntu) → `clash_sysproxy.sh`.
- The user mentions Clash, mihomo, Clash Verge (Rev), ClashX, external-controller, `9090`/`9097`, or a proxy node by name.

## When NOT to use

- **Authoring subscriptions or publishing rules** — use the policy repository's review/build workflow. Managed routers deploy through their own transactions.
- **Mixin / Merge config** — a client-side config-file feature (Clash Verge / CFW), not a runtime API. Guide the user to the client; don't script it.
- **Picking/buying nodes, subscription management** — out of scope.
- **A one-off `curl` the user already wrote** — just run it.

## Mental model — three layers

| Layer | What it controls | Tool |
|---|---|---|
| **Controller API** | current node, groups, latency, **mode**, **TUN enable**, reload, connections, rules | `clash_api.py` (works on any client with the API on) |
| **Enable / discover** | turning the API on; finding its address+secret | `clash_api.py doctor` → `references/enable-api-by-client.md` |
| **OS / client toggles** | **System Proxy**, Service Mode (for TUN), Mixin | `clash_sysproxy.sh` for system proxy; guidance for the rest |

**Never hardcode the controller.** It is not always `127.0.0.1:9090` — Clash Verge
Rev defaults to `9097`, GUIs pick random ports, routers live on the LAN. Always
discover (or set `CLASH_CONTROLLER`).

## Quick start

```sh
python3 scripts/clash_api.py doctor    # discover controller + diagnose; ALWAYS start here if unsure
python3 scripts/clash_api.py status    # node, mode, tun, ports, groups
```

`doctor`/`status` resolve the controller from, in order: `--controller`/`--secret`
flags → `CLASH_CONTROLLER`/`CLASH_SECRET` env → the optional Television hook
(`~/.config/television/clash-source.sh`) → local config files → probe `127.0.0.1:9090`
then `:9097`. First reachable wins. Auth is `Authorization: Bearer <secret>` when a
secret is known.

## Intent → command map

| User says | Command |
|---|---|
| "what's my node / status" | `status` (add `--json` for machine output) |
| "list groups / show members" | `groups --members` |
| "list / search nodes" | `proxies [--filter jp]` |
| "test latency of node X" | `delay "X"` |
| "test all nodes in group G" | `group-delay "G"` (mihomo cores only) |
| "switch group G to node X" | `switch G "X"` (validates X ∈ G first) |
| "go global / rule / direct" | `mode global\|rule\|direct` |
| "turn TUN on/off" | `tun on\|off [--restart]` |
| "allow LAN on/off" | `allow-lan on\|off` |
| "reload my config" | `reload` (or `reload --path /abs/config.yaml`) |
| "show/close connections" | `connections` / `connections close --all --yes` |
| "what's my egress IP" | `egress` |
| "show my routing rules" | `rules [--filter netflix]` |
| "is the API on / it's broken" | `doctor` → guide via `references/enable-api-by-client.md` |
| "toggle the system proxy" | `clash_sysproxy.sh detect\|on <host:port> --yes\|off --yes` |

Every mutating command takes `--dry-run` to preview without changing anything.

## Workflow A — switch node safely

```sh
python3 scripts/clash_api.py status
# controller: 127.0.0.1:9097  (config: ~/.config/clash/config.yaml)
# mode: rule    tun: off
# groups:
# - PROXY: now=US-01 type=Selector members=8

python3 scripts/clash_api.py delay "JP-01" --timeout-ms 3000
# JP-01: 142 ms                 # if this fails/timeouts, report it and do NOT switch unless asked

python3 scripts/clash_api.py switch PROXY "JP-01"
# switched PROXY -> JP-01

python3 scripts/clash_api.py status          # verify now=JP-01
```

Test latency before switching. `switch` pre-validates the node is a member and, on
a bad name, prints the actual members so you can self-correct. Don't switch
URL-test/fallback groups unless asked — they reselect automatically.

## Workflow B — the API isn't reachable

```sh
python3 scripts/clash_api.py doctor
# controller: UNREACHABLE ... Tried: default (127.0.0.1:9090); default (127.0.0.1:9097)
# detected: config dir: ~/.config/clash; process: mihomo
```

`doctor` reports OS, detected client, config file, and what it probed. Then open
`references/enable-api-by-client.md`, find the user's client, and walk them through
enabling `external-controller` (or copy its address/secret into `CLASH_CONTROLLER`
/`CLASH_SECRET`). Re-run `doctor` to confirm.

## Workflow C — change mode / enable TUN

```sh
python3 scripts/clash_api.py mode global          # PATCH /configs {"mode":"global"}
python3 scripts/clash_api.py tun on --restart     # PATCH tun.enable, then POST /restart
```

TUN needs an **elevated core** (Service Mode / root). If it doesn't take effect,
the core isn't privileged — point the user at Service Mode in
`references/enable-api-by-client.md`. Preview first with `--dry-run`.

## Workflow D — system proxy where the client has no toggle

For mihomo CLI / headless / Ubuntu (no client toggle):

```sh
bash scripts/clash_sysproxy.sh detect                        # current OS proxy + $http_proxy
bash scripts/clash_sysproxy.sh on 127.0.0.1:7890 --yes       # macOS networksetup / GNOME gsettings
#   also prints:  export http_proxy=http://127.0.0.1:7890 ...   (for headless shells)
bash scripts/clash_sysproxy.sh off --yes
```

Without `--yes` it previews the exact commands and changes nothing. On a platform
with no toggler it still prints `export`/`unset` lines for the shell. Use the
proxy's **mixed-port** (see `status` → ports) as `host:port`. TUN mode is the
alternative that needs no system proxy at all.

## Available scripts

- **`scripts/clash_api.py`** — Controller API client. Stdlib-only Python 3. Discovers the controller, speaks `Authorization: Bearer`, URL-encodes names.
  - Read: `doctor`, `status`, `config`, `groups [--members]`, `proxies [--filter]`, `rules [--filter]`, `delay <proxy>`, `group-delay <group>`, `connections`, `egress`. Add `--json` to read commands for structured stdout.
  - Write (all support `--dry-run`): `switch <group> <proxy>`, `mode <rule|global|direct>`, `tun <on|off> [--restart]`, `allow-lan <on|off>`, `reload [--path P]`, `connections close [--id X | --all --yes]`.
  - Global: `--controller http[s]://host:port`, `--ca-cert PATH` / `CLASH_CA_CERT`, `--secret-file PATH` / `CLASH_SECRET_FILE`, `--read-only`. Keep secrets out of command arguments; the legacy `--secret` flag remains compatible.
  - Exit: `0` ok, `1` usage, `2` group/proxy not found (message lists real members), `3` controller unreachable, `4` op rejected (HTTP ≥300 on a write).
- **`scripts/clash_sysproxy.sh`** — OS system-proxy toggle (bash 3.2). macOS `networksetup` / GNOME `gsettings`; always prints shell `export`/`unset` lines to stdout.
  - `detect` | `on <host:port> [--socks H:P]` | `off`. Flags: `--yes` (apply; else preview), `--dry-run`, `--service NAME` (macOS), `-h`.
  - Exit: `0` ok/preview, `1` usage, `2` no OS toggler on this platform, `3` apply failed.

## Reference files

- `references/managed-router-diagnosis.md` — Read **first** for single-site diagnosis, a remote TLS controller, Nikki/ImmortalWrt, missing routing evidence, or rule candidates. Includes the report contract and links to the reusable policy knowledge base.
- `references/enable-api-by-client.md` — Read **when** `doctor` can't reach a controller, or the user asks about System Proxy / TUN / Service Mode / Mixin. Per-client × OS matrix (mihomo CLI, Clash Verge Rev, ClashX, mihomo-party/FlClash, legacy CFW, OpenClash/Docker) for turning the API on and where the OS-level toggles live.
- `references/api-endpoints.md` — Read **when** you need an operation the script doesn't wrap (streaming `/traffic` `/logs` `/memory`, `/providers`, rule-providers, `/dns/query`, `/storage`) and want raw `curl`. Full endpoint catalog with a "wrapped by clash_api.py" column.

## Gotchas

- **Managed Pi commands take precedence.** Use its `just diagnose-site` wrapper and verified private CA. Do not use API writes, reload, mode/TUN changes or system-proxy toggles to bypass `proxy-test` / `proxy-enable` transactions. Its policy uses TPROXY with TUN off and fail-open behavior.
- **Explicit HTTPS targets never fall back to another controller.** Certificate/hostname errors are failures; do not switch to HTTP or disable verification. Requests ignore environment proxy settings and reject redirects before forwarding credentials.
- **A remote controller is not an egress proxy.** `egress` requires an explicit `--proxy` for remote controllers. A management API port is never a proxy data port.
- **`observe` needs traffic during its 1–60 second window.** It filters exact domain and optional source IP, combines connection snapshots and bounded log lines, and can still miss short connections. Empty evidence cannot prove bypass; a matching rule cannot prove application access.
- **Reports are private browsing metadata.** Domain, source IP, node names, controller and timestamps may identify usage. Persist below ignored private storage with mode 0600; share a reviewed conclusion, not raw reports/configs.
- **The controller is not always `127.0.0.1:9090`.** Clash Verge Rev defaults to `9097`; GUIs pick random ports; routers are on the LAN. Discovery probes 9090+9097 and scans config files — but when it fails, read the address from the client and set `CLASH_CONTROLLER`. Never assume 9090.
- **The proxy port is not always `7890`.** Derive it from live config: `status` prints `ports` (Clash Verge uses mixed-port `7897`). `egress` and `clash_sysproxy.sh` should use the reported mixed/http port, not a guess.
- **Enabling TUN via the API needs an elevated core.** `PATCH /configs {"tun":{"enable":true}}` returns 204 but TUN won't route without Service Mode / root, and usually a `POST /restart` (`tun on --restart`). If it "succeeds" but nothing changes, that's why.
- **System Proxy is an OS setting, not an API concept.** The controller API cannot toggle it. Use the client's toggle, or `clash_sysproxy.sh`.
- **Mixin / Merge is a config-file feature** (Clash Verge / CFW), applied on profile activation — not a runtime call. Guide the user in the client; then `reload`.
- **`group-delay`, `/providers`, `/restart`, `/memory` are mihomo-only.** On classic Clash Premium they 404 (the script says so). `status` shows `(mihomo/meta)` when the core supports them.
- **Switching a selector reroutes all traffic using that proxy** — ongoing downloads, other shells' `http_proxy`, browser sessions. It's reversible but live. URL-test/fallback groups may immediately reselect, so a manual switch there may not "stick".
- **Never print the `secret`.** `status` reports `secret: yes/no`, never the value. Say a secret was used, not what it is.
- **`$CLASH_CONFIG` set-but-missing is a hard error, by design.** The tool won't silently fall back to another config (which could load a different profile and leak its secret). Fix the path or unset it.
- **Prefer the runtime `config.yaml` over a subscription profile** for controller creds — subscription profiles usually omit `external-controller`/`secret`. Discovery already scans runtime configs first.
- **`reload --path` resolves on the controller host.** For a remote/container controller, the path must exist *there*, not on the calling machine.
