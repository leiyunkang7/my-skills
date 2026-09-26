# Enabling & controlling Clash/mihomo per client and OS

Read this when `clash_api.py doctor` reports **no reachable controller**, or when
the user asks about **System Proxy / TUN / Service Mode / Mixin** — things the
controller API can't fully do. Menu labels drift between client versions; treat
exact wording as approximate and prefer the config-file facts.

## The one thing the API needs: `external-controller`

Every operation in this skill needs the core's REST API ("external-controller")
listening. In `config.yaml` that is:

```yaml
external-controller: 127.0.0.1:9090   # host:port the API listens on
secret: "some-long-random-string"      # optional bearer token; set it if exposed
external-ui: ui                         # optional: serves a dashboard at /ui
```

After editing the file, restart the core (or the GUI client). Then:

```sh
clash_api.py doctor
# or point at it explicitly:
CLASH_CONTROLLER=127.0.0.1:9090 CLASH_SECRET=some-long-random-string clash_api.py status
```

**The controller port is NOT always 9090.** Discovery probes `9090` then `9097`
(Clash Verge Rev), and scans local config files — but if the client uses another
port, read it from that client's settings and pass `CLASH_CONTROLLER`.

## Three layers of control

| Want to… | Layer | How |
|---|---|---|
| switch node, mode, reload, connections, latency, **TUN enable** | **controller API** | `clash_api.py` (works on every client with the API on) |
| turn the API on | **client setting / config file** | this doc, per client |
| **System Proxy** on/off | **OS or client toggle** | client toggle, else `clash_sysproxy.sh` |
| **TUN** actually routing | **elevated core** | Service Mode / root (per client below) |
| **Mixin / Merge** extra config | **config file** | client's merge feature; not a runtime API |

## Per-client cheatsheet

### mihomo core / CLI (Linux, macOS, Windows) — the "no GUI" case
- **API:** add `external-controller`/`secret` to `config.yaml` (see above). Default config dir: `~/.config/mihomo/` (or `~/.config/clash/`); macOS also `~/Library/Application Support/{mihomo,clash}/`.
- **System Proxy:** there is **no toggle** — mihomo doesn't touch OS settings. Use `clash_sysproxy.sh on <host:port> --yes` (macOS `networksetup` / GNOME `gsettings`), or `export http_proxy=…` (the script prints these), or use TUN instead.
- **TUN:** set `tun: {enable: true, stack: system}` in config, and run the core with privileges: `sudo mihomo …` or grant `sudo setcap cap_net_admin,cap_net_bind_service=ep $(which mihomo)` (Linux). TUN needs no system proxy — it captures traffic transparently.
- **Mixin:** none — edit `config.yaml` directly and `clash_api.py reload`.

### Clash Verge / Clash Verge Rev (Windows, macOS, Linux) — mainstream GUI
- **API:** Settings → **Clash Setting** shows/sets *External Controller* + *Secret*. Default is **`127.0.0.1:9097`** (proxy mixed-port `7897`). If discovery misses it, copy the address/secret from here into `CLASH_CONTROLLER`/`CLASH_SECRET`.
- **System Proxy:** the big **System Proxy** toggle on the home/Settings screen (flips OS proxy for you).
- **TUN:** **TUN Mode** toggle — requires **Service Mode** first (Settings → *Service Mode* → Install). Without the installed helper, TUN won't stay on. `clash_api.py tun on --restart` works once Service Mode is installed and the core is elevated.
- **Mixin:** Profiles → a **Merge** (YAML) or **Script** profile layered onto the subscription. It's a config-file merge, applied on profile activation — not a runtime API call.

### ClashX / ClashX Meta / ClashX Pro (macOS menubar)
- **API:** menubar shows the config; controller defaults to **`127.0.0.1:9090`**, secret usually empty. Config lives in `~/.config/clash/`; the dashboard opens from the menu.
- **System Proxy:** menubar → **Set as system proxy**.
- **TUN:** ClashX Meta → **Enhanced Mode / TUN**; installs a privileged helper the first time (prompts for password).
- **Mixin:** none built-in — edit the config profile.

### mihomo-party / FlClash / Clash Nyanpasu (newer GUIs)
- **API:** in Settings there's an *External Controller* / *API* field (often `127.0.0.1:9090`, sometimes a random port). Copy it to `CLASH_CONTROLLER`.
- **System Proxy + TUN:** each has its own toggles; TUN installs a helper/Service like Verge.
- Treat these like Verge Rev; the controller API commands are identical once the address is known.

### Legacy Clash for Windows (CFW) — discontinued (repo deleted ~late 2023)
- Still works if installed. General tab has **Allow LAN / System Proxy / TUN Mode / Service Mode** toggles; **Settings → Clash API** (or the General "API" line) reveals the controller `host:port` + secret; the **Mixin** tab injects extra YAML.
- Recommend migrating to **Clash Verge Rev** (actively maintained, bundles the mihomo core).

### Remote / router / container (OpenClash, Docker mihomo)
- **OpenClash (OpenWrt):** API is on the router — `http://<router-lan-ip>:9090`; find the secret in LuCI (OpenClash → Settings). Run `CLASH_CONTROLLER=<router-ip>:9090 CLASH_SECRET=… clash_api.py status`.
- **Docker mihomo:** set `external-controller: 0.0.0.0:9090` in the mounted config and publish the port (`-p 9090:9090`). Reach it via `CLASH_CONTROLLER=127.0.0.1:9090`.
- For remote controllers, `reload --path` must reference a path **on the controller host** (see api-endpoints gotchas).

## If discovery still fails

1. Confirm the core is running and the API is on: `curl -s http://<host>:<port>/version` (add `-H "Authorization: Bearer <secret>"` if set).
2. Firewall/LAN: a controller bound to `127.0.0.1` is only reachable locally; bind `0.0.0.0` (and set a `secret`) to reach it across the LAN.
3. Still nothing → the client's API is off. Enable it in that client's settings (above) or add `external-controller` to its config and restart.
