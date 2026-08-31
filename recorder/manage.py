#!/usr/bin/env python3
"""Status and safe shutdown for every process this recorder starts.

    python manage.py status        show all recorder processes + scheduled tasks
    python manage.py stop-all      safely stop the recorder, guardian, dashboard
    python manage.py stop-all --include-tasks   also disable the watchdog tasks

"safe" means: ask the supervisor to finalize the current MKV first, then the
guardian, then the dashboard, and only force-kill leftovers that belong to us.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from common import (
    CONFIG_DIR, ROOT, CREATE_NO_WINDOW,
    kst_text, kill_process_tree, process_alive, read_json_or_empty,
)

STATUS_PATH = CONFIG_DIR / "status.json"
FAILOVER_STATUS_PATH = CONFIG_DIR / "failover-status.json"
BACKFILL_STATUS_PATH = CONFIG_DIR / "backfill-status.json"
AUTH_STATUS_PATH = CONFIG_DIR / "auth-status.json"
DASHBOARD_PID_PATH = CONFIG_DIR / "dashboard-api.pid"

STOP_REQUEST = CONFIG_DIR / "stop.request"
FAILOVER_DISABLED = CONFIG_DIR / "failover.disabled"
FAILOVER_STOP = CONFIG_DIR / "failover.stop.request"
BACKFILL_STOP = CONFIG_DIR / "backfill-stop.request"

TASK_NAMES = ("YouTubeRecorder Failover Guardian", "YouTubeRecorder Healthcheck")


def _pid_from_file(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def owned_media_processes() -> list[dict]:
    """yt-dlp/ffmpeg processes whose command line points back into this install."""
    root = str(ROOT).lower()
    query = (
        "Get-CimInstance Win32_Process -Filter "
        "\"Name='yt-dlp.exe' OR Name='ffmpeg.exe'\" | "
        "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", query],
            capture_output=True, text=True, timeout=20,
            creationflags=CREATE_NO_WINDOW, check=False,
        )
        data = json.loads(result.stdout or "null")
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    if data is None:
        return []
    rows = data if isinstance(data, list) else [data]
    owned = []
    for row in rows:
        cmd = (row.get("CommandLine") or "").lower()
        if root in cmd:
            owned.append({"pid": row.get("ProcessId"), "name": row.get("Name")})
    return owned


def scheduled_task_states() -> dict[str, str]:
    states: dict[str, str] = {}
    for name in TASK_NAMES:
        try:
            result = subprocess.run(
                ["schtasks.exe", "/Query", "/TN", name, "/FO", "LIST"],
                capture_output=True, text=True, timeout=15,
                creationflags=CREATE_NO_WINDOW, check=False,
            )
            state = "not-installed"
            for line in result.stdout.splitlines():
                low = line.lower()
                if low.startswith("status:") or low.startswith("상태:"):
                    state = line.split(":", 1)[1].strip()
            states[name] = state
        except (OSError, subprocess.SubprocessError):
            states[name] = "unknown"
    return states


def cmd_status() -> int:
    recorder = read_json_or_empty(STATUS_PATH)
    guardian = read_json_or_empty(FAILOVER_STATUS_PATH)
    backfill = read_json_or_empty(BACKFILL_STATUS_PATH)
    auth = read_json_or_empty(AUTH_STATUS_PATH)
    dash_pid = _pid_from_file(DASHBOARD_PID_PATH)

    def line(label: str, pid, extra: str = "") -> str:
        alive = "실행중" if process_alive(pid) else "정지 "
        pid_text = str(pid) if pid else "-"
        return f"  {label:<16} {alive}  PID {pid_text:<8} {extra}"

    print(f"== YouTube 녹화 시스템 상태 ({kst_text()}) ==")
    print(f"  설치 위치        {ROOT}")
    print()
    print("[ 핵심 프로세스 ]")
    print(line("녹화 감독", recorder.get("SupervisorPid"),
               f"상태={recorder.get('State', '?')}  갱신={recorder.get('UpdatedKST', '?')}"))
    print(line("  수신 yt-dlp", recorder.get("ReceiverPid"),
               f"seq {recorder.get('FirstSequence')}..{recorder.get('LastSequence')}"))
    print(line("  FFmpeg", recorder.get("FfmpegPid"),
               Path(recorder.get("NewestRecording") or "-").name))
    print(line("장애 감시기", guardian.get("GuardianPid"),
               f"상태={guardian.get('State', '?')}  예비녹화={guardian.get('FallbackActive')}"))
    print(line("  예비 yt-dlp", guardian.get("FallbackReceiverPid")))
    print(line("  예비 FFmpeg", guardian.get("FallbackFfmpegPid")))
    print(line("대시보드", dash_pid))
    if backfill:
        print(line("과거구간 회수", backfill.get("Pid"), f"상태={backfill.get('State', '?')}"))
    print()
    print("[ YouTube 로그인 ]")
    print(f"  상태 {auth.get('State', '미확인')}  ({auth.get('CheckedKST', '-')})  {auth.get('Detail', '')}")
    print()
    print("[ 예약 작업(자동 감시) ]")
    for name, state in scheduled_task_states().items():
        print(f"  {name:<34} {state}")
    print()
    orphans = owned_media_processes()
    tracked = {recorder.get("ReceiverPid"), recorder.get("FfmpegPid"),
               guardian.get("FallbackReceiverPid"), guardian.get("FallbackFfmpegPid")}
    loose = [p for p in orphans if p["pid"] not in tracked]
    if loose:
        print("[ 주의: 추적되지 않는 yt-dlp/FFmpeg (정리 대상) ]")
        for p in loose:
            print(f"  {p['name']}  PID {p['pid']}")
    else:
        print("추적되지 않는 yt-dlp/FFmpeg 프로세스는 없습니다.")
    return 0


def _wait_gone(pid, seconds: float) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not process_alive(pid):
            return True
        time.sleep(1)
    return not process_alive(pid)


def cmd_stop_all(include_tasks: bool) -> int:
    done: list[str] = []
    recorder = read_json_or_empty(STATUS_PATH)
    guardian = read_json_or_empty(FAILOVER_STATUS_PATH)
    backfill = read_json_or_empty(BACKFILL_STATUS_PATH)

    # 1) recorder supervisor: ask it to finalize the current MKV.
    sup = recorder.get("SupervisorPid")
    if process_alive(sup):
        STOP_REQUEST.write_text("stop\n", encoding="ascii")
        print("녹화 감독에 안전 종료를 요청했습니다. 현재 MKV 마감 대기(최대 45초)...")
        done.append("녹화 감독 정지" if _wait_gone(sup, 45) else "녹화 감독 종료 대기 시간 초과")
    else:
        done.append("녹화 감독 이미 정지")

    # 2) failover guardian.
    gpid = guardian.get("GuardianPid")
    if process_alive(gpid):
        FAILOVER_DISABLED.write_text("disabled\n", encoding="ascii")
        FAILOVER_STOP.write_text("stop\n", encoding="ascii")
        done.append("장애 감시기 정지" if _wait_gone(gpid, 20) else "장애 감시기 종료 대기 초과")
    else:
        done.append("장애 감시기 이미 정지")

    # 3) backfill, if running.
    if process_alive(backfill.get("Pid")):
        BACKFILL_STOP.write_text("stop\n", encoding="ascii")
        done.append("과거구간 회수 중지 요청(임시 파일 보존)")

    # 4) dashboard.
    dash_pid = _pid_from_file(DASHBOARD_PID_PATH)
    if process_alive(dash_pid):
        kill_process_tree(int(dash_pid))
        DASHBOARD_PID_PATH.unlink(missing_ok=True)
        done.append("대시보드 정지")

    # 5) leftover app-owned yt-dlp / ffmpeg.
    time.sleep(2)
    leftovers = owned_media_processes()
    for p in leftovers:
        if p["pid"]:
            kill_process_tree(int(p["pid"]))
    if leftovers:
        done.append(f"남은 yt-dlp/FFmpeg {len(leftovers)}개 정리")

    # 6) optional: watchdog scheduled tasks.
    if include_tasks:
        for name in TASK_NAMES:
            subprocess.run(["schtasks.exe", "/Change", "/TN", name, "/DISABLE"],
                           capture_output=True, creationflags=CREATE_NO_WINDOW, check=False)
        done.append("예약 감시 작업 비활성화")

    print("\n== 종료 요약 ==")
    for item in done:
        print(f"  - {item}")
    print("\n다시 시작하려면 start-recorder.bat 을 실행하세요.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="recorder process manager")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="show all recorder processes")
    stop = sub.add_parser("stop-all", help="safely stop recorder, guardian, dashboard")
    stop.add_argument("--include-tasks", action="store_true",
                      help="also disable the Task Scheduler watchdogs")
    args = parser.parse_args()
    if args.command == "status":
        return cmd_status()
    if args.command == "stop-all":
        return cmd_stop_all(args.include_tasks)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
