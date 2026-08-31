#!/usr/bin/env python3
"""One-shot recorder watchdog for Healthchecks.io.

This process only reads recorder state. It never starts, stops, or restarts the
recorder. Windows Task Scheduler launches it once per minute.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from urllib import error, request

from common import (
    CONFIG_DIR, LOGS_DIR,
    acquire_lock, atomic_json_write, kst_text, load_json, named_process_alive,
    now_kst, parse_kst, process_alive, read_json_or_empty, release_lock,
    setup_rotating_logger,
)

CONFIG_PATH = CONFIG_DIR / "healthcheck.json"
RECORDER_STATUS_PATH = CONFIG_DIR / "status.json"
AUTH_STATUS_PATH = CONFIG_DIR / "auth-status.json"
STATE_PATH = CONFIG_DIR / "healthcheck-state.json"
LOCK_PATH = CONFIG_DIR / "healthcheck.lock"


def find_active_recording(status: dict) -> tuple[Path | None, int | None]:
    candidates: list[Path] = []
    pattern_text = status.get("OutputPattern")
    if isinstance(pattern_text, str) and pattern_text:
        pattern = Path(pattern_text)
        try:
            candidates.extend(pattern.parent.glob(pattern.name.replace("%03d", "*")))
        except OSError:
            pass
    newest_text = status.get("NewestRecording")
    if isinstance(newest_text, str) and newest_text:
        newest = Path(newest_text)
        if newest not in candidates:
            candidates.append(newest)

    existing: list[tuple[int, Path]] = []
    for candidate in candidates:
        try:
            existing.append((candidate.stat().st_mtime_ns, candidate))
        except OSError:
            continue
    if not existing:
        return None, None
    active = max(existing, key=lambda item: item[0])[1]
    try:
        return active, active.stat().st_size
    except OSError:
        return None, None


def ping_healthchecks(ping_url: str, failed: bool, body: str, config: dict) -> tuple[bool, str]:
    endpoint = ping_url.rstrip("/") + ("/fail" if failed else "")
    retries = max(1, int(config.get("HttpRetries", 3)))
    timeout = max(3, int(config.get("HttpTimeoutSeconds", 10)))
    last_result = "not attempted"
    for attempt in range(1, retries + 1):
        try:
            req = request.Request(
                endpoint, data=body.encode("utf-8"),
                headers={"Content-Type": "text/plain; charset=utf-8",
                         "User-Agent": "YouTubeRecorder-Watchdog/1.0"},
                method="POST",
            )
            with request.urlopen(req, timeout=timeout) as response:
                code = int(response.status)
            if 200 <= code < 300:
                return True, f"HTTP {code}"
            last_result = f"HTTP {code}"
        except error.HTTPError as exc:
            last_result = f"HTTP {exc.code}"
        except error.URLError as exc:
            last_result = f"network error ({type(exc.reason).__name__})"
        except (OSError, TimeoutError) as exc:
            last_result = f"network error ({type(exc).__name__})"
        if attempt < retries:
            time.sleep(attempt)
    return False, last_result


def evaluate(config: dict, previous: dict, simulated_failure: bool) -> tuple[dict, list[str]]:
    current_time = now_kst()
    issues: list[str] = []
    facts: dict = {"CheckedKST": kst_text(current_time), "RecorderState": "unknown",
                   "FreeSpaceGB": None, "ActiveFile": None, "ActiveFileBytes": None}

    status = read_json_or_empty(RECORDER_STATUS_PATH)
    if not status:
        issues.append("recorder status file is missing or invalid")

    recorder_state = str(status.get("State", "unknown")).lower()
    facts["RecorderState"] = recorder_state

    updated = parse_kst(status.get("UpdatedKST"))
    max_age = int(config.get("StatusMaxAgeSeconds", 120))
    if updated is None:
        issues.append("recorder status timestamp is invalid")
    else:
        age = (current_time - updated).total_seconds()
        facts["StatusAgeSeconds"] = round(age)
        if age > max_age:
            issues.append(f"recorder status is older than {max_age} seconds")

    if not process_alive(status.get("SupervisorPid")):
        issues.append("recorder supervisor process is not running")

    nonrecording_since = parse_kst(previous.get("NonRecordingSinceKST"))
    if recorder_state == "recording":
        nonrecording_since = None
        if not process_alive(status.get("ReceiverPid")):
            issues.append("yt-dlp receiver process is not running")
        if not process_alive(status.get("FfmpegPid")):
            issues.append("FFmpeg process is not running")
    elif recorder_state in {"connecting", "reconnecting"}:
        if nonrecording_since is None:
            nonrecording_since = current_time
        grace = int(config.get("ReconnectGraceSeconds", 180))
        elapsed = (current_time - nonrecording_since).total_seconds()
        facts["ReconnectSeconds"] = round(elapsed)
        if elapsed >= grace:
            issues.append(f"recorder has been reconnecting for {round(elapsed)} seconds")
            auth = read_json_or_empty(AUTH_STATUS_PATH)
            if str(auth.get("State")) == "login_required":
                issues.append("YouTube login is required (run login-youtube.bat)")
    else:
        issues.append(f"recorder state is {recorder_state}")

    directory_text = status.get("RecordingDirectory")
    directory = Path(directory_text) if isinstance(directory_text, str) and directory_text else None
    if directory is None:
        issues.append("recording directory is not configured")
    else:
        try:
            if not directory.is_dir():
                raise OSError("not a directory")
            free_gb = shutil.disk_usage(directory).free / (1024 ** 3)
            facts["FreeSpaceGB"] = round(free_gb, 1)
            minimum_free = float(config.get("AlertMinimumFreeSpaceGB", 60))
            if free_gb < minimum_free:
                issues.append(f"recording storage free space is below {minimum_free:g} GB")
        except OSError:
            issues.append("recording directory is inaccessible")

    storage_name = str(config.get("StorageProcessName", "")).strip()
    storage_process = named_process_alive(storage_name)
    facts["StorageProcess"] = (
        "not-configured" if not storage_name
        else "running" if storage_process is True
        else "not-running" if storage_process is False
        else "unknown"
    )
    if storage_process is False:
        issues.append(f"storage process is not running: {storage_name}")

    active_path, active_size = find_active_recording(status)
    if active_path is not None:
        facts["ActiveFile"] = active_path.name
        facts["ActiveFileBytes"] = active_size

    last_active = previous.get("ActiveFilePath")
    last_size = previous.get("ActiveFileBytes")
    last_growth = parse_kst(previous.get("LastGrowthKST"))
    current_active = str(active_path) if active_path is not None else None

    if recorder_state == "recording":
        if active_path is None or active_size is None:
            issues.append("active recording file cannot be found")
        else:
            grew = isinstance(last_size, int) and active_size > last_size
            if current_active != last_active or grew or last_growth is None:
                last_growth = current_time
            stall_limit = int(config.get("FileStallSeconds", 180))
            stall_seconds = (current_time - last_growth).total_seconds()
            facts["FileStallSeconds"] = round(stall_seconds)
            if stall_seconds >= stall_limit:
                issues.append(f"active recording file has not grown for {round(stall_seconds)} seconds")
    else:
        last_growth = current_time

    if simulated_failure:
        issues.append("simulated failure (dry-run only)")

    next_state = {
        "LastRunKST": kst_text(current_time),
        "NonRecordingSinceKST": kst_text(nonrecording_since) if nonrecording_since else None,
        "ActiveFilePath": current_active,
        "ActiveFileBytes": active_size,
        "LastGrowthKST": kst_text(last_growth) if last_growth else None,
        "LastHealth": "FAIL" if issues else "OK",
        "LastIssues": issues,
        "Facts": facts,
    }
    return next_state, issues


def diagnostic_body(state: dict, issues: list[str]) -> str:
    facts = state["Facts"]
    lines = [
        "YouTube Recorder health report",
        f"KST: {facts.get('CheckedKST')}",
        f"Result: {'FAIL' if issues else 'OK'}",
        f"Recorder state: {facts.get('RecorderState')}",
        f"Status age: {facts.get('StatusAgeSeconds', 'n/a')} sec",
        f"Storage process: {facts.get('StorageProcess')}",
        f"Free space: {facts.get('FreeSpaceGB', 'n/a')} GB",
        f"Active file: {facts.get('ActiveFile') or 'n/a'}",
        f"Active bytes: {facts.get('ActiveFileBytes', 'n/a')}",
    ]
    if issues:
        lines.append("Issues:")
        lines.extend(f"- {issue}" for issue in issues)
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="YouTube recorder health check")
    parser.add_argument("--dry-run", action="store_true", help="evaluate without pinging or updating state")
    parser.add_argument("--simulate-failure", action="store_true", help="add a test failure; requires --dry-run")
    args = parser.parse_args()
    if args.simulate_failure and not args.dry_run:
        parser.error("--simulate-failure requires --dry-run")

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        lock = acquire_lock(LOCK_PATH)
    except RuntimeError:
        return 0

    try:
        try:
            config = load_json(CONFIG_PATH)
        except (OSError, ValueError, json.JSONDecodeError):
            print("Healthcheck configuration is missing or invalid.", file=sys.stderr)
            return 2

        ping_url = str(config.get("PingUrl", "")).strip()
        if not ping_url.startswith("https://hc-ping.com/"):
            print("Healthchecks ping URL is missing or invalid.", file=sys.stderr)
            return 2

        logger = setup_rotating_logger("youtube-recorder-healthcheck", "healthcheck.log",
                                       config.get("LogMaxMB", 5), config.get("LogBackups", 5))
        previous = read_json_or_empty(STATE_PATH)
        state, issues = evaluate(config, previous, args.simulate_failure)
        body = diagnostic_body(state, issues)
        if args.dry_run:
            print(body, end="")
            return 1 if issues else 0

        ping_ok, ping_result = ping_healthchecks(ping_url, bool(issues), body, config)
        state["LastPingKST"] = kst_text()
        state["LastPingResult"] = ping_result
        state["LastPingSucceeded"] = ping_ok
        try:
            atomic_json_write(STATE_PATH, state)
        except OSError as exc:
            logger.warning("Could not write watchdog state: %s", exc)

        if previous.get("LastHealth") != state["LastHealth"]:
            logger.error("Health changed to FAIL: %s", "; ".join(issues)) if issues \
                else logger.info("Health changed to OK")
        if not ping_ok:
            logger.warning("Healthchecks ping failed after retries: %s", ping_result)
            return 2
        return 1 if issues else 0
    finally:
        release_lock(lock)


if __name__ == "__main__":
    raise SystemExit(main())
