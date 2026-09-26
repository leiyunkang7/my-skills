"""Bounded, filtered routing observations. No configuration changes or traffic replay."""
from __future__ import annotations

import datetime
import ipaddress
import json
import re
import threading
import time
import urllib.error


def hostname(value):
    value = value.rstrip(".").lower()
    try:
        value = value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("expected a hostname, without a URL or credentials") from exc
    if len(value) > 253 or not all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
        for part in value.split(".")
    ):
        raise ValueError("expected a hostname, without a URL or credentials")
    return value


def utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def parse_log(payload, domain, client):
    # Only retain the routing sentence. Never persist arbitrary log payloads.
    match = re.fullmatch(
        r"\[(TCP|UDP)\]\s+(\S+):(\d+)\s+-+>\s+([^ :]+):(\d+)\s+"
        r"match\s+(.+?)\s+using\s+(.+)", payload
    )
    if not match:
        return None
    network, source, source_port, target, port, rule, chain = match.groups()
    if target.lower().rstrip(".") != domain or (client and source != client):
        return None
    if len(rule) > 1024 or len(chain) > 1024:
        return None
    return {"observed_at": utc(), "network": network, "source_ip": source,
            "source_port": int(source_port), "host": domain, "destination_port": int(port),
            "matched_rule": rule, "chain_log": chain}


def observe(api, controller, secret, domain, *, client=None, duration=15, trigger=None):
    """Return filtered evidence; trigger runs while background observers are active."""
    try:
        domain = hostname(domain)
        if client:
            client = str(ipaddress.ip_address(client))
        if not 1 <= duration <= 60:
            raise ValueError("duration must be 1-60 seconds")
    except ValueError as exc:
        raise api.ClashError(str(exc)) from exc
    status, version = api.request_json(controller, secret, "GET", "version", timeout=3)
    if status != 200 or not isinstance(version, dict):
        raise api.ControllerUnreachable(f"controller identity unavailable: HTTP {status}")
    config = api.get_configs(controller, secret)
    started = utc()
    deadline = time.monotonic() + duration
    stop = threading.Event()
    snapshots_ready = threading.Event()
    logs_ready = threading.Event()
    records, logs, errors = {}, [], []
    lock = threading.Lock()
    limit = 500
    capped = False

    def connections():
        nonlocal capped
        while not stop.is_set() and time.monotonic() < deadline:
            try:
                status, snapshot = api.request_json(controller, secret, "GET", "connections",
                                                    timeout=min(2, max(.1, deadline - time.monotonic())))
                if status != 200 or not isinstance(snapshot, dict):
                    raise ValueError("connections-unavailable")
                for item in snapshot.get("connections") or []:
                    meta = item.get("metadata") or {}
                    if str(meta.get("host", "")).lower().rstrip(".") != domain:
                        continue
                    if client and meta.get("sourceIP") != client:
                        continue
                    key = item.get("id")
                    if not isinstance(key, str):
                        continue
                    record = {"id": key, "observed_at": utc(),
                              "metadata": {k: meta.get(k) for k in (
                                  "network", "type", "host", "sourceIP", "sourcePort",
                                  "destinationIP", "destinationPort")},
                              "rule": item.get("rule"), "rule_payload": item.get("rulePayload"),
                              "chains_core_order": item.get("chains") or []}
                    with lock:
                        if key in records or len(records) < limit:
                            records[key] = record
                        else:
                            capped = True
            except Exception:
                with lock:
                    if "connections-unavailable" not in errors:
                        errors.append("connections-unavailable")
            finally:
                snapshots_ready.set()
            stop.wait(min(.5, max(0, deadline - time.monotonic())))

    def stream_logs():
        nonlocal capped
        try:
            with api.controller_open(controller, secret, "GET", "logs?level=info",
                                     timeout=min(duration, 2)) as response:
                logs_ready.set()
                while not stop.is_set() and time.monotonic() < deadline:
                    raw = response.readline(16385)
                    if not raw:
                        break
                    if len(raw) > 16384:
                        raise ValueError("log-line-too-large")
                    item = json.loads(raw)
                    record = parse_log(str(item.get("payload", "")), domain, client)
                    if record:
                        with lock:
                            if len(logs) < limit:
                                logs.append(record)
                            else:
                                capped = True
        except Exception:
            with lock:
                errors.append("log-stream-unavailable-or-idle")
        finally:
            logs_ready.set()

    threads = [threading.Thread(target=connections, daemon=True),
               threading.Thread(target=stream_logs, daemon=True)]
    for thread in threads:
        thread.start()
    probe = None
    try:
        if trigger:
            snapshots_ready.wait(timeout=min(2, max(0, deadline - time.monotonic())))
            logs_ready.wait(timeout=min(2, max(0, deadline - time.monotonic())))
            probe = trigger()
        stop.wait(max(0, deadline - time.monotonic()))
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=2.5)
    with lock:
        result = {
            "schema": "clash-routing-observation-v1", "domain": domain, "client": client,
            "started_at": started, "finished_at": utc(), "duration_requested": duration,
            "controller": controller, "core_version": version.get("version"),
            "runtime": {"mode": config.get("mode"), "tun_enabled": (config.get("tun") or {}).get("enable")},
            "connections": list(records.values()), "routing_logs": list(logs),
            "collection_warnings": list(errors), "capped": capped,
            "evidence": "observed-route" if records or logs else "insufficient",
            "limitations": ["Active snapshots can miss short connections.",
                           "A matched route does not prove HTTP/application success.",
                           "No evidence is not proof that traffic bypassed this controller."],
        }
    if probe is not None:
        result["client_probe"] = probe
    return result
