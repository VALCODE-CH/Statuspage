#!/usr/bin/env python3
"""
Health-check monitor for Atlassian Statuspage.

Checks one or more HTTP endpoints and mirrors the outcome into Statuspage
components using the public Statuspage API v1:

    GET   https://api.statuspage.io/v1/pages/{page_id}/components/{component_id}
    PATCH https://api.statuspage.io/v1/pages/{page_id}/components/{component_id}
          Authorization: OAuth <API_KEY>
          Content-Type: application/json
          {"component": {"status": "operational"}}

Standard library only - no third-party dependencies, no database, no server.
Credentials are read from environment variables (GitHub Secrets) and are
never written to the log.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

API_BASE = "https://api.statuspage.io/v1"
USER_AGENT = "statuspage-monitor/1.0"

# Component status values accepted by the Statuspage API.
VALID_COMPONENT_STATUSES = (
    "operational",
    "under_maintenance",
    "degraded_performance",
    "partial_outage",
    "major_outage",
)

MAX_BODY_BYTES = 64 * 1024
STATUSPAGE_MIN_INTERVAL = 1.1  # Statuspage allows roughly 1 request/second per API key
STATUSPAGE_MAX_ATTEMPTS = 3
PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
SENSITIVE_NAME_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "AUTH")

# Per-monitor defaults. Anything here can be overridden globally in the
# config's "defaults" block or per monitor in the "monitors" list.
DEFAULTS = {
    "enabled": True,
    "method": "GET",
    "timeout_seconds": 10,
    "expected_status": [200],
    "expected_body_contains": None,
    "headers": {},
    "failure_threshold": 3,
    "success_threshold": 2,
    "retry_delay_seconds": 15,
    "up_status": "operational",
    "down_status": "major_outage",
    "flapping_status": None,
    "allow_http": False,
    "page_id": "${STATUSPAGE_PAGE_ID}",
    "component_id": "${STATUSPAGE_COMPONENT_ID}",
}

UP, DOWN, FLAPPING = "UP", "DOWN", "FLAPPING"


class ConfigError(Exception):
    """The configuration file is invalid or references a missing secret."""


class StatuspageError(Exception):
    """The Statuspage API could not be reached or rejected the request."""


# --------------------------------------------------------------------------
# logging / redaction
# --------------------------------------------------------------------------

_SENSITIVE_VALUES: list[str] = []


def collect_sensitive_values() -> None:
    """Remember secret-looking environment values so they can be scrubbed from output."""
    values = set()
    for name, value in os.environ.items():
        if value and len(value) >= 8 and any(h in name.upper() for h in SENSITIVE_NAME_HINTS):
            values.add(value)
    # Longest first so overlapping values are replaced completely.
    _SENSITIVE_VALUES[:] = sorted(values, key=len, reverse=True)


def redact(text: str) -> str:
    for value in _SENSITIVE_VALUES:
        text = text.replace(value, "***")
    return text


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(message: str = "") -> None:
    print(f"[{now()}] {redact(message)}" if message else "", flush=True)


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

def resolve(value, where: str, required: bool = True):
    """Replace ${ENV_VAR} placeholders with environment values (GitHub Secrets)."""
    if not isinstance(value, str):
        return value

    missing: list[str] = []

    def substitute(match: re.Match) -> str:
        name = match.group(1)
        env_value = os.environ.get(name, "")
        if not env_value:
            missing.append(name)
        return env_value

    resolved = PLACEHOLDER_RE.sub(substitute, value)
    if missing and required:
        names = sorted(set(missing))
        lines = [f"{where}: environment variable(s) {', '.join(names)} are not set.",
                 "  A GitHub Secret only reaches this script once it is mapped in the",
                 "  workflow. Add to the env: block of .github/workflows/monitor.yml:"]
        for name in names:
            lines.append("      " + name + ": ${{ secrets." + name + " }}")
        lines.append("  Component ids are not secret (they are published on the status page),")
        lines.append("  so you can also write the id directly into monitors.json instead.")
        raise ConfigError("\n".join(lines))
    return resolved


def display(value) -> str:
    """Config form of a value - placeholders stay unresolved so no secret is logged."""
    return value if isinstance(value, str) else str(value)


def load_config(path: str) -> tuple[dict, list[dict]]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}")
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}")

    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a JSON object")

    defaults = {**DEFAULTS, **(raw.get("defaults") or {})}
    monitors_raw = raw.get("monitors")
    if not isinstance(monitors_raw, list) or not monitors_raw:
        raise ConfigError(f"{path}: \"monitors\" must be a non-empty list")

    monitors = []
    seen_names = set()
    for index, entry in enumerate(monitors_raw):
        if not isinstance(entry, dict):
            raise ConfigError(f"{path}: monitors[{index}] must be an object")
        monitor = {**defaults, **entry}
        monitor["name"] = str(monitor.get("name") or f"monitor-{index + 1}")
        validate_monitor(monitor, f"{path}: monitor \"{monitor['name']}\"")
        if monitor["name"] in seen_names:
            raise ConfigError(f"{path}: duplicate monitor name \"{monitor['name']}\"")
        seen_names.add(monitor["name"])
        monitors.append(monitor)

    return raw, monitors


def validate_monitor(monitor: dict, where: str) -> None:
    url = monitor.get("url")
    if not isinstance(url, str) or not url.strip():
        raise ConfigError(f"{where}: \"url\" is required")
    if not url.startswith("https://") and not (url.startswith("http://") and monitor["allow_http"]):
        raise ConfigError(
            f"{where}: url must use https:// (set \"allow_http\": true to deliberately allow http)"
        )

    if str(monitor["method"]).upper() not in ("GET", "HEAD", "OPTIONS"):
        raise ConfigError(f"{where}: \"method\" must be GET, HEAD or OPTIONS")

    expected = monitor["expected_status"]
    if isinstance(expected, int):
        expected = [expected]
    if not isinstance(expected, list) or not expected or not all(
        isinstance(code, int) and 100 <= code <= 599 for code in expected
    ):
        raise ConfigError(f"{where}: \"expected_status\" must be a status code or a list of them")
    monitor["expected_status"] = expected

    for field in ("timeout_seconds", "failure_threshold", "success_threshold"):
        value = monitor[field]
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 1:
            raise ConfigError(f"{where}: \"{field}\" must be a number >= 1")
    if not isinstance(monitor["retry_delay_seconds"], (int, float)) or monitor["retry_delay_seconds"] < 0:
        raise ConfigError(f"{where}: \"retry_delay_seconds\" must be >= 0")

    for field in ("up_status", "down_status"):
        if monitor[field] not in VALID_COMPONENT_STATUSES:
            raise ConfigError(
                f"{where}: \"{field}\" must be one of {', '.join(VALID_COMPONENT_STATUSES)}"
            )
    if monitor["flapping_status"] is not None and monitor["flapping_status"] not in VALID_COMPONENT_STATUSES:
        raise ConfigError(
            f"{where}: \"flapping_status\" must be null or one of {', '.join(VALID_COMPONENT_STATUSES)}"
        )

    if not isinstance(monitor["headers"], dict):
        raise ConfigError(f"{where}: \"headers\" must be an object")
    for field in ("page_id", "component_id"):
        if not isinstance(monitor[field], str) or not monitor[field].strip():
            raise ConfigError(f"{where}: \"{field}\" is required")


# --------------------------------------------------------------------------
# health check
# --------------------------------------------------------------------------

class CheckResult:
    def __init__(self, ok: bool, status: int | None, elapsed_ms: int, detail: str):
        self.ok = ok
        self.status = status
        self.elapsed_ms = elapsed_ms
        self.detail = detail

    def __str__(self) -> str:
        status = f"HTTP {self.status}" if self.status is not None else "no response"
        return f"{status} in {self.elapsed_ms} ms - {self.detail}"


def check_once(monitor: dict, url: str, headers: dict) -> CheckResult:
    request = urllib.request.Request(url, method=str(monitor["method"]).upper())
    request.add_header("User-Agent", USER_AGENT)
    for name, value in headers.items():
        request.add_header(name, value)

    timeout = float(monitor["timeout_seconds"])
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(MAX_BODY_BYTES)
            status = response.status
    except urllib.error.HTTPError as exc:  # server answered with 4xx/5xx
        elapsed = int((time.monotonic() - started) * 1000)
        try:
            exc.read(MAX_BODY_BYTES)
        except Exception:
            pass
        return CheckResult(False, exc.code, elapsed, f"unexpected status (expected {monitor['expected_status']})")
    except urllib.error.URLError as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        reason = getattr(exc, "reason", exc)
        is_timeout = isinstance(reason, TimeoutError) or "timed out" in str(reason).lower()
        label = f"timeout after {timeout:g}s" if is_timeout else f"network error: {reason}"
        return CheckResult(False, None, elapsed, label)
    except TimeoutError:
        elapsed = int((time.monotonic() - started) * 1000)
        return CheckResult(False, None, elapsed, f"timeout after {timeout:g}s")
    except (OSError, ValueError) as exc:  # TLS failures, bad URL, ...
        elapsed = int((time.monotonic() - started) * 1000)
        return CheckResult(False, None, elapsed, f"request failed: {exc}")

    elapsed = int((time.monotonic() - started) * 1000)
    if status not in monitor["expected_status"]:
        return CheckResult(False, status, elapsed, f"unexpected status (expected {monitor['expected_status']})")

    needle = monitor["expected_body_contains"]
    if needle:
        if needle not in body.decode("utf-8", errors="replace"):
            return CheckResult(False, status, elapsed, f"body does not contain {needle!r}")
        return CheckResult(True, status, elapsed, "status and body OK")
    return CheckResult(True, status, elapsed, "status OK")


def evaluate(monitor: dict, url: str, headers: dict, currently_up: bool, sleeper=time.sleep) -> tuple[str, list[CheckResult]]:
    """Run consecutive checks until an UP or DOWN verdict is reached.

    A component that is already operational only needs a single success, so a
    healthy run stays fast. Recovering from a non-operational status requires
    "success_threshold" consecutive successes, and going down requires
    "failure_threshold" consecutive failures - one short blip never flips the
    component.
    """
    required_ok = 1 if currently_up else int(monitor["success_threshold"])
    required_fail = int(monitor["failure_threshold"])
    max_attempts = max(required_ok, required_fail)
    delay = float(monitor["retry_delay_seconds"])

    results: list[CheckResult] = []
    consecutive_ok = consecutive_fail = 0

    for attempt in range(1, max_attempts + 1):
        result = check_once(monitor, url, headers)
        results.append(result)
        outcome = "OK" if result.ok else "FAIL"
        log(f"  attempt {attempt}/{max_attempts}: {result} -> {outcome}")

        if result.ok:
            consecutive_ok, consecutive_fail = consecutive_ok + 1, 0
        else:
            consecutive_fail, consecutive_ok = consecutive_fail + 1, 0

        if consecutive_ok >= required_ok:
            return UP, results
        if consecutive_fail >= required_fail:
            return DOWN, results
        if attempt < max_attempts and delay > 0:
            log(f"  waiting {delay:g}s before the next attempt")
            sleeper(delay)

    return FLAPPING, results


# --------------------------------------------------------------------------
# Statuspage API client
# --------------------------------------------------------------------------

class StatuspageClient:
    def __init__(self, api_key: str, timeout: float = 15.0):
        self._api_key = api_key
        self._timeout = timeout
        self._last_call = 0.0

    def _throttle(self) -> None:
        wait = STATUSPAGE_MIN_INTERVAL - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _request(self, method: str, path: str, payload: dict | None = None):
        url = f"{API_BASE}{path}"
        if not url.startswith("https://"):
            raise StatuspageError("refusing to talk to the Statuspage API over plain HTTP")
        data = json.dumps(payload).encode("utf-8") if payload is not None else None

        last_error = ""
        for attempt in range(1, STATUSPAGE_MAX_ATTEMPTS + 1):
            request = urllib.request.Request(url, data=data, method=method)
            request.add_header("Authorization", f"OAuth {self._api_key}")
            request.add_header("User-Agent", USER_AGENT)
            if data is not None:
                request.add_header("Content-Type", "application/json")

            self._throttle()
            try:
                with urllib.request.urlopen(request, timeout=self._timeout) as response:
                    body = response.read(MAX_BODY_BYTES).decode("utf-8", errors="replace")
                    return json.loads(body) if body.strip() else {}
            except urllib.error.HTTPError as exc:
                detail = exc.read(MAX_BODY_BYTES).decode("utf-8", errors="replace").strip()
                last_error = f"HTTP {exc.code} {exc.reason}: {detail[:300]}"
                if exc.code in (401, 403):
                    raise StatuspageError(
                        f"{last_error} - check STATUSPAGE_API_KEY and that the key may manage this page"
                    )
                if exc.code == 404:
                    raise StatuspageError(f"{last_error} - check the page id and component id")
                if exc.code != 429 and exc.code < 500:
                    raise StatuspageError(last_error)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                last_error = f"request failed: {exc}"

            if attempt < STATUSPAGE_MAX_ATTEMPTS:
                backoff = 2 ** attempt
                log(f"  statuspage: {redact(last_error)} - retrying in {backoff}s")
                time.sleep(backoff)

        raise StatuspageError(last_error or "unknown error")

    def list_pages(self) -> list:
        return self._request("GET", "/pages") or []

    def list_components(self, page_id: str) -> list:
        return self._request("GET", f"/pages/{page_id}/components") or []

    def get_component(self, page_id: str, component_id: str) -> dict:
        return self._request("GET", f"/pages/{page_id}/components/{component_id}")

    def set_component_status(self, page_id: str, component_id: str, status: str) -> dict:
        if status not in VALID_COMPONENT_STATUSES:
            raise StatuspageError(f"refusing to send unknown component status {status!r}")
        return self._request(
            "PATCH",
            f"/pages/{page_id}/components/{component_id}",
            {"component": {"status": status}},
        )


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------

def run_monitor(monitor: dict, client: StatuspageClient | None, dry_run: bool) -> dict:
    name = monitor["name"]
    log(f"--- {name} ---")

    url = resolve(monitor["url"], f"monitor \"{name}\" url")
    headers = {
        key: resolve(value, f"monitor \"{name}\" header {key}")
        for key, value in monitor["headers"].items()
    }
    # In a dry run Statuspage is never contacted, so the ids may stay unresolved.
    needs_ids = client is not None
    page_id = resolve(monitor["page_id"], f"monitor \"{name}\" page_id", required=needs_ids)
    component_id = resolve(monitor["component_id"], f"monitor \"{name}\" component_id", required=needs_ids)

    log(f"  url={display(monitor['url'])} method={str(monitor['method']).upper()} "
        f"timeout={monitor['timeout_seconds']:g}s expected_status={monitor['expected_status']}")
    log(f"  thresholds: {monitor['failure_threshold']} consecutive failures -> {monitor['down_status']}, "
        f"{monitor['success_threshold']} consecutive successes -> {monitor['up_status']} "
        f"(retry delay {monitor['retry_delay_seconds']:g}s)")
    log(f"  component reference: page_id={display(monitor['page_id'])} "
        f"component_id={display(monitor['component_id'])}")

    current_status = None
    if client is not None:
        component = client.get_component(page_id, component_id)
        current_status = component.get("status")
        log(f"  current component status: {current_status or 'unknown'}")
    else:
        log("  current component status: unknown (dry run - Statuspage not contacted)")

    currently_up = current_status == monitor["up_status"]
    verdict, results = evaluate(monitor, url, headers, currently_up)

    if verdict == UP:
        desired = monitor["up_status"]
    elif verdict == DOWN:
        desired = monitor["down_status"]
    else:
        desired = monitor["flapping_status"]
        log("  result: FLAPPING - neither threshold reached in this run")

    last = results[-1]
    log(f"  result: {verdict} (last check: {last})")

    action = "none"
    if desired is None:
        action = "skipped (flapping, component left unchanged)"
    elif client is None:
        action = f"would set '{desired}' (dry run)"
    elif desired == current_status:
        action = f"already '{desired}' - no update needed"
    else:
        client.set_component_status(page_id, component_id, desired)
        action = f"updated {current_status or 'unknown'} -> {desired}"
    log(f"  statuspage: {action}")

    return {
        "name": name,
        "url": display(monitor["url"]),
        "verdict": verdict,
        "http_status": last.status,
        "elapsed_ms": last.elapsed_ms,
        "attempts": len(results),
        "previous_status": current_status,
        "desired_status": desired,
        "action": action,
    }


def list_components(client: StatuspageClient) -> int:
    """Print every page and component id so new monitors can be configured."""
    pages = client.list_pages()
    if not pages:
        log("no pages found for this API key")
        return 1

    for page in pages:
        print(f"\npage_id      {page.get('id')}   {page.get('name', '')}")
        components = client.list_components(page.get("id", ""))
        if not components:
            print("  (no components on this page yet)")
            continue
        for component in components:
            kind = "group    " if component.get("group") else "component"
            print(f"  {kind}  {component.get('id')}   {component.get('name', '')}"
                  f"   [{component.get('status', '?')}]")

    print("\nUse a component id in monitors.json:")
    print('  { "name": "My API", "url": "https://...", "component_id": "<component id above>" }')
    return 0


def write_job_summary(rows: list[dict]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    icons = {UP: "UP", DOWN: "DOWN", FLAPPING: "FLAPPING"}
    lines = [
        f"### API monitor - {now()}",
        "",
        "| Monitor | URL | Result | HTTP | Response time | Statuspage |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        http = row["http_status"] if row["http_status"] is not None else "-"
        result = icons.get(row["verdict"], row["verdict"])
        lines.append(
            f"| {row['name']} | `{row['url']}` | **{result}** | {http} | "
            f"{row['elapsed_ms']} ms | {row['action']} |"
        )
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(redact("\n".join(lines)) + "\n\n")
    except OSError as exc:
        log(f"could not write job summary: {exc}")


def env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check HTTP endpoints and update Atlassian Statuspage components.")
    parser.add_argument("--config", default=os.environ.get("MONITOR_CONFIG", "monitors.json"),
                        help="path to the monitor configuration (default: monitors.json)")
    parser.add_argument("--only", default=os.environ.get("MONITOR_ONLY", ""),
                        help="comma separated monitor names to run (default: all enabled monitors)")
    parser.add_argument("--dry-run", action="store_true", default=env_flag("DRY_RUN"),
                        help="run the health checks but never call the Statuspage API")
    parser.add_argument("--validate", action="store_true",
                        help="only validate the configuration file and exit")
    parser.add_argument("--list-components", action="store_true",
                        help="list all pages and component ids for the API key and exit")
    args = parser.parse_args(argv)

    collect_sensitive_values()

    if args.list_components:
        api_key = os.environ.get("STATUSPAGE_API_KEY", "").strip()
        if not api_key:
            log("STATUSPAGE_API_KEY is not set")
            return 1
        try:
            return list_components(StatuspageClient(api_key))
        except StatuspageError as exc:
            log(f"statuspage error: {exc}")
            return 1

    try:
        _, monitors = load_config(args.config)
    except ConfigError as exc:
        log(f"configuration error: {exc}")
        return 1

    if args.validate:
        for monitor in monitors:
            state = "enabled" if monitor["enabled"] else "disabled"
            log(f"ok: {monitor['name']} ({state}) -> {display(monitor['url'])}")
        log(f"configuration valid: {len(monitors)} monitor(s) in {args.config}")
        return 0

    selected = [name.strip() for name in args.only.split(",") if name.strip()]
    if selected:
        unknown = sorted(set(selected) - {m["name"] for m in monitors})
        if unknown:
            log(f"configuration error: unknown monitor name(s): {', '.join(unknown)}")
            return 1
        monitors = [m for m in monitors if m["name"] in selected]
    monitors = [m for m in monitors if m["enabled"]]

    if not monitors:
        log("nothing to do: no enabled monitors selected")
        return 0

    client = None
    if not args.dry_run:
        api_key = os.environ.get("STATUSPAGE_API_KEY", "").strip()
        if not api_key:
            log("configuration error: STATUSPAGE_API_KEY is not set (add it as a GitHub Secret)")
            return 1
        client = StatuspageClient(api_key)

    log(f"starting checks for {len(monitors)} monitor(s) from {args.config}"
        + (" (dry run)" if args.dry_run else ""))

    rows: list[dict] = []
    failures: list[str] = []
    for monitor in monitors:
        try:
            rows.append(run_monitor(monitor, client, args.dry_run))
        except (ConfigError, StatuspageError) as exc:
            log(f"  ERROR for \"{monitor['name']}\": {exc}")
            failures.append(monitor["name"])
            rows.append({
                "name": monitor["name"], "url": display(monitor["url"]), "verdict": "ERROR",
                "http_status": None, "elapsed_ms": 0, "attempts": 0,
                "previous_status": None, "desired_status": None,
                "action": f"error: {redact(str(exc))[:200]}",
            })

    log()
    down = [row["name"] for row in rows if row["verdict"] == DOWN]
    up = [row["name"] for row in rows if row["verdict"] == UP]
    log(f"summary: {len(up)} UP, {len(down)} DOWN, {len(failures)} error(s)")
    for row in rows:
        log(f"  {row['name']}: {row['verdict']} - {row['action']}")
    write_job_summary(rows)

    if failures:
        return 1
    if down and env_flag("FAIL_JOB_ON_DOWN"):
        log("FAIL_JOB_ON_DOWN is enabled and at least one monitor is DOWN - failing the job")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
