# Clash / mihomo external-controller API endpoints

Reference catalog of the REST API that `clash_api.py` talks to. **Read this when
you need an operation the script does not wrap** (streaming traffic/logs,
providers, rule-providers, DNS query, storage) and want to hit the controller
with raw `curl`.

- Base URL: `http://<external-controller>` (e.g. `http://127.0.0.1:9090`). Always `http://`, not https.
- Auth: `Authorization: Bearer <secret>` when a secret is set. Some web dashboards also accept `?token=<secret>`, but the header is canonical.
- Names in a path (proxy, group, provider) must be URL-encoded (`urllib.parse.quote(safe="")`).
- Source of truth: https://wiki.metacubex.one/en/api/ — mihomo (Clash.Meta). Classic Clash Premium supports a subset (no `/group/:name/delay`, `/providers`, `/restart`, `/memory`).

## Wrapped by `clash_api.py`

| Method | Path | Purpose | Script command |
|---|---|---|---|
| GET | `/version` | Version + `meta` flag (mihomo vs Premium) | `status`, `doctor` (probe) |
| GET | `/configs` | Running config: `mode`, `tun`, ports, `allow-lan` | `status`, `config` |
| PATCH | `/configs` | Runtime override: `{"mode":…}`, `{"tun":{"enable":…}}`, `{"allow-lan":…}` | `mode`, `tun`, `allow-lan` |
| PUT | `/configs?force=true` | Reload config file: `{"path":"/abs/path"}` | `reload` |
| POST | `/restart` | Restart the core (needed after enabling TUN) | `tun --restart` |
| GET | `/proxies` | All proxies + groups (`all`, `now`, `type`) | `status`, `groups`, `proxies` |
| PUT | `/proxies/{name}` | Select group member: `{"name":"JP-01"}` | `switch` |
| GET | `/proxies/{name}/delay` | Latency of one proxy: `?url=…&timeout=ms` | `delay` |
| GET | `/group/{name}/delay` | Latency of every member of a group (mihomo only) | `group-delay` |
| GET | `/rules` | Routing rules (`type`/`payload`/`proxy`) | `rules` |
| GET | `/connections` | Active connections + `uploadTotal`/`downloadTotal` | `connections` |
| DELETE | `/connections` | Close all connections | `connections close --all --yes` |
| DELETE | `/connections/{id}` | Close one connection | `connections close --id <id>` |

## Not wrapped — use raw curl

| Method | Path | Purpose |
|---|---|---|
| GET | `/traffic` | Real-time up/down stream (WebSocket-friendly; `curl` streams NDJSON) |
| GET | `/memory` | Real-time memory stream (mihomo) |
| GET | `/logs?level=info` | Real-time log stream; `?format=structured` for JSON lines |
| GET | `/group` , `/group/{name}` | Policy-group listing (mihomo) |
| DELETE | `/proxies/{name}` | Clear a fixed selection (non-Selector groups) |
| PATCH | `/rules/disable` | Temporarily disable rules by index: `{"3":true}` |
| GET/PUT | `/providers/proxies[/{name}]` | List / update proxy providers (subscriptions) |
| GET | `/providers/proxies/{name}/healthcheck` | Health-check a whole provider |
| GET/PUT | `/providers/rules[/{name}]` | List / update rule providers |
| POST | `/cache/fakeip/flush` , `/cache/dns/flush` | Flush fake-IP / DNS cache (HTTP 204) |
| GET | `/dns/query?name=…&type=A` | Resolve a name through the core's DNS |
| GET/PUT/DELETE | `/storage/{key}` | Key/value storage (≤1 MB) |

## Raw curl recipes

```sh
CTRL=127.0.0.1:9090 ; SECRET=xxxxx        # discover with: clash_api.py doctor
AUTH=(-H "Authorization: Bearer $SECRET")  # omit if no secret

# switch a group
curl -sS "${AUTH[@]}" -X PUT "http://$CTRL/proxies/PROXY" -d '{"name":"JP-01"}'

# go global mode (runtime override)
curl -sS "${AUTH[@]}" -X PATCH "http://$CTRL/configs" -d '{"mode":"global"}'

# enable TUN then restart the core
curl -sS "${AUTH[@]}" -X PATCH "http://$CTRL/configs" -d '{"tun":{"enable":true}}'
curl -sS "${AUTH[@]}" -X POST  "http://$CTRL/restart" -d '{}'

# stream traffic (Ctrl-C to stop)
curl -sN "${AUTH[@]}" "http://$CTRL/traffic"

# health-check a subscription provider
curl -sS "${AUTH[@]}" "http://$CTRL/providers/proxies/MyProvider/healthcheck"
```

## Gotchas specific to the API

- **PATCH `/configs` returns HTTP 204** (no body) on success — treat `< 300` as OK.
- **Enabling TUN via PATCH does not create the interface by itself.** The core must be elevated (Service Mode / root / `cap_net_admin`) and usually needs `POST /restart`. On a non-elevated core the toggle silently no-ops.
- **`/group/:name/delay`, `/providers/*`, `/restart`, `/memory` are mihomo-only.** On classic Clash Premium they 404 — `clash_api.py group-delay` surfaces this with a "needs a mihomo core" note.
- **`PUT /configs?force=true` `path` is resolved on the controller host**, not the caller. For a remote/container controller the path must exist *there*.
