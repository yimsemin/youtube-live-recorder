#!/usr/bin/env python3
"""Local-only control panel for the YouTube recorder.

Serves a single self-contained HTML page and a small JSON API on
http://127.0.0.1:8787/ . No Node, no build step. Bound to loopback only.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))

import json
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from common import (
    CONFIG_DIR, LOGS_DIR, RECORDER_DIR, ROOT, KST, CREATE_NO_WINDOW,
    acquire_lock, atomic_json_write, find_python, kst_text, process_alive,
    read_json_or_empty, release_lock, setup_rotating_logger, validate_youtube_url,
)

CONFIG_PATH = CONFIG_DIR / "recorder.json"
STATUS_PATH = CONFIG_DIR / "status.json"
FAILOVER_STATUS_PATH = CONFIG_DIR / "failover-status.json"
BACKFILL_STATUS_PATH = CONFIG_DIR / "backfill-status.json"
AUTH_STATUS_PATH = CONFIG_DIR / "auth-status.json"
DASHBOARD_PID_PATH = CONFIG_DIR / "dashboard-api.pid"
LOCK_PATH = CONFIG_DIR / "dashboard.lock"
HTML_PATH = Path(__file__).with_name("dashboard.html")

HOST, PORT = "127.0.0.1", 8787
ALLOWED_ORIGINS = {f"http://{h}:{PORT}" for h in ("127.0.0.1", "localhost", "[::1]")}

LOGGER = setup_rotating_logger("youtube-dashboard", "dashboard.log", 5, 5)


# -- helpers -----------------------------------------------------------

def disk_info(path: Path) -> dict:
    try:
        usage = shutil.disk_usage(path)
        return {
            "totalGB": round(usage.total / 1024 ** 3, 1),
            "freeGB": round(usage.free / 1024 ** 3, 1),
            "usedPercent": round(usage.used / usage.total * 100, 1) if usage.total else 0,
        }
    except OSError as exc:
        return {"error": str(exc), "totalGB": 0, "freeGB": 0, "usedPercent": 0}


def tail_text(path: Path, lines: int = 25) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    except OSError:
        return []


def list_recordings(directory: Path, active: str) -> list[dict]:
    try:
        paths = sorted(directory.glob("*.mkv"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    active_nc = os.path.normcase(active) if active else ""
    rows = []
    for path in paths[:40]:
        try:
            stat = path.stat()
        except OSError:
            continue
        rows.append({
            "name": path.name,
            "sizeGB": round(stat.st_size / 1024 ** 3, 2),
            "modifiedKST": datetime.fromtimestamp(stat.st_mtime, KST).strftime("%Y-%m-%d %H:%M:%S"),
            "active": os.path.normcase(str(path)) == active_nc,
        })
    return rows


def launch_script(script_name: str, *extra: str) -> int:
    process = subprocess.Popen(
        [str(find_python()), str(RECORDER_DIR / script_name), *extra],
        cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, close_fds=True,
        creationflags=CREATE_NO_WINDOW | 0x00000200 | 0x00000008,  # NEW_GROUP | DETACHED
    )
    return process.pid


def ensure_guardian() -> None:
    if not process_alive(read_json_or_empty(FAILOVER_STATUS_PATH).get("GuardianPid")):
        (CONFIG_DIR / "failover.disabled").unlink(missing_ok=True)
        (CONFIG_DIR / "failover.stop.request").unlink(missing_ok=True)
        try:
            launch_script("failover_guardian.py")
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Could not start failover guardian: %s", exc)


def build_status() -> dict:
    config = read_json_or_empty(CONFIG_PATH)
    recorder = read_json_or_empty(STATUS_PATH)
    guardian = read_json_or_empty(FAILOVER_STATUS_PATH)
    backfill = read_json_or_empty(BACKFILL_STATUS_PATH)
    auth = read_json_or_empty(AUTH_STATUS_PATH)

    recorder_alive = process_alive(recorder.get("SupervisorPid"))
    directory = Path(config.get("RecordingDirectory") or ROOT / "recordings")
    active = str(recorder.get("NewestRecording") or "")

    if recorder.get("State") == "recording" and not recorder_alive:
        recorder["State"] = "unexpected_stop"
        recorder["Message"] = "상태 파일은 녹화 중이지만 감독 프로세스가 없습니다. [녹화 시작]으로 복구하세요."
    if backfill.get("State") in {"downloading", "muxing"} and not process_alive(backfill.get("Pid")):
        backfill["State"] = "interrupted"

    processes = [
        {"label": "녹화 감독", "pid": recorder.get("SupervisorPid"),
         "alive": recorder_alive, "note": recorder.get("State", "-")},
        {"label": "수신 (yt-dlp)", "pid": recorder.get("ReceiverPid"),
         "alive": process_alive(recorder.get("ReceiverPid")), "note": ""},
        {"label": "FFmpeg", "pid": recorder.get("FfmpegPid"),
         "alive": process_alive(recorder.get("FfmpegPid")), "note": ""},
        {"label": "장애 감시기", "pid": guardian.get("GuardianPid"),
         "alive": process_alive(guardian.get("GuardianPid")), "note": guardian.get("State", "-")},
        {"label": "대시보드", "pid": os.getpid(), "alive": True, "note": ""},
    ]
    if guardian.get("FallbackActive"):
        processes.append({"label": "예비 녹화", "pid": guardian.get("FallbackReceiverPid"),
                          "alive": process_alive(guardian.get("FallbackReceiverPid")),
                          "note": "로컬 예비 녹화 중"})

    return {
        "ok": True,
        "serverTimeKST": kst_text(),
        "config": config,
        "recorder": recorder,
        "recorderAlive": recorder_alive,
        "guardian": guardian,
        "backfill": backfill,
        "backfillAlive": process_alive(backfill.get("Pid")),
        "auth": auth,
        "processes": processes,
        "recordings": list_recordings(directory, active),
        "storage": disk_info(directory),
        "localDisk": disk_info(ROOT),
        "logs": {
            "recorder": tail_text(LOGS_DIR / "recorder.log"),
            "continuity": tail_text(LOGS_DIR / "continuity.log", 12),
        },
    }


# -- settings / actions ----------------------------------------------

def save_settings(body: dict) -> dict:
    config = read_json_or_empty(CONFIG_PATH)
    if "Url" in body:
        config["Url"] = validate_youtube_url(str(body["Url"]))
    if "RecordingDirectory" in body:
        path = Path(str(body["RecordingDirectory"]).strip())
        if not path.is_absolute():
            raise ValueError("저장 위치는 절대 경로여야 합니다.")
        path.mkdir(parents=True, exist_ok=True)
        config["RecordingDirectory"] = str(path)
    if "SegmentHours" in body:
        hours = float(body["SegmentHours"])
        if not 0.25 <= hours <= 12:
            raise ValueError("분할 주기는 0.25~12시간이어야 합니다.")
        config["SegmentHours"] = hours
    if "MinimumFreeSpaceGB" in body:
        minimum = float(body["MinimumFreeSpaceGB"])
        if not 10 <= minimum <= 2000:
            raise ValueError("최소 여유공간은 10~2000GB이어야 합니다.")
        config["MinimumFreeSpaceGB"] = minimum
    if "YtDlpFormat" in body:
        fmt = str(body["YtDlpFormat"]).strip()
        if not fmt or len(fmt) > 100:
            raise ValueError("포맷 값을 확인하세요.")
        config["YtDlpFormat"] = fmt
    atomic_json_write(CONFIG_PATH, config)
    return {"ok": True, "message": "설정을 저장했습니다. 녹화기 재시작 후 적용됩니다."}


def restart_recorder() -> dict:
    import time
    status = read_json_or_empty(STATUS_PATH)
    pid = status.get("SupervisorPid")
    if process_alive(pid):
        (CONFIG_DIR / "stop.request").write_text("stop\n", encoding="ascii")
        deadline = time.monotonic() + 50
        while time.monotonic() < deadline and process_alive(pid):
            time.sleep(1)
        if process_alive(pid):
            raise RuntimeError("안전 종료가 50초 안에 끝나지 않아 재시작하지 않았습니다.")
    ensure_guardian()
    return {"ok": True, "message": "녹화기를 안전하게 재시작했습니다.", "pid": launch_script("recorder.py")}


# -- HTTP -----------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "YouTubeRecorderDashboard/2.0"

    def log_message(self, fmt, *args):
        LOGGER.info("%s - %s", self.client_address[0], fmt % args)

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, data: dict) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _require_local(self) -> None:
        origin = self.headers.get("Origin")
        if origin is not None and origin not in ALLOWED_ORIGINS:
            raise PermissionError("로컬 대시보드 요청만 허용됩니다.")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                self._send(200, HTML_PATH.read_bytes(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, b"dashboard.html missing", "text/plain; charset=utf-8")
        elif self.path == "/health":
            self._json(200, {"ok": True})
        elif self.path == "/api/status":
            self._json(200, build_status())
        else:
            self._json(404, {"ok": False, "message": "not found"})

    do_HEAD = do_GET

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if not 0 <= length <= 65536:
            raise ValueError("요청 본문이 너무 큽니다.")
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw.decode("utf-8")) if raw else {}

    def do_POST(self):
        try:
            self._require_local()
            body = self._body()
            route = self.path
            if route == "/api/settings":
                response = save_settings(body)
            elif route == "/api/recorder/start":
                if process_alive(read_json_or_empty(STATUS_PATH).get("SupervisorPid")):
                    response = {"ok": True, "message": "이미 실행 중입니다."}
                else:
                    ensure_guardian()
                    response = {"ok": True, "message": "녹화기를 시작했습니다.", "pid": launch_script("recorder.py")}
            elif route == "/api/recorder/stop":
                (CONFIG_DIR / "stop.request").write_text("stop\n", encoding="ascii")
                response = {"ok": True, "message": "안전 종료를 요청했습니다. 현재 MKV 마감을 기다립니다."}
            elif route == "/api/recorder/restart":
                response = restart_recorder()
            elif route == "/api/system/stop-all":
                launch_script("manage.py", "stop-all")
                response = {"ok": True, "message": "전체 종료를 시작했습니다. 잠시 후 상태를 확인하세요."}
            elif route == "/api/auth/login":
                response = {"ok": True, "message": "로그인 창을 엽니다.", "pid": launch_script("auth_login.py")}
            elif route == "/api/backfill/start":
                if process_alive(read_json_or_empty(BACKFILL_STATUS_PATH).get("Pid")):
                    response = {"ok": True, "message": "과거구간 회수가 이미 실행 중입니다."}
                else:
                    response = {"ok": True, "message": "과거구간 회수를 시작했습니다.", "pid": launch_script("backfill.py")}
            elif route == "/api/backfill/stop":
                (CONFIG_DIR / "backfill-stop.request").write_text("stop\n", encoding="ascii")
                response = {"ok": True, "message": "과거구간 회수 중지를 요청했습니다. 임시 파일은 보존됩니다."}
            elif route == "/api/open-folder":
                path = Path(read_json_or_empty(CONFIG_PATH).get("RecordingDirectory", ROOT / "recordings"))
                if not path.is_absolute() or not path.exists():
                    raise ValueError("저장 폴더를 찾을 수 없습니다.")
                os.startfile(str(path))  # noqa: S606
                response = {"ok": True, "message": "저장 폴더를 열었습니다."}
            else:
                self._json(404, {"ok": False, "message": "not found"})
                return
            self._json(200, response)
        except PermissionError as exc:
            self._json(403, {"ok": False, "message": str(exc)})
        except (ValueError, OSError, RuntimeError, json.JSONDecodeError) as exc:
            LOGGER.exception("Dashboard request failed: %s", self.path)
            self._json(400, {"ok": False, "message": str(exc)})


def main() -> int:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        lock = acquire_lock(LOCK_PATH)
    except RuntimeError:
        print("Dashboard API is already running")
        return 0
    DASHBOARD_PID_PATH.write_text(str(os.getpid()), encoding="ascii")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    LOGGER.info("Dashboard listening on http://%s:%s", HOST, PORT)
    print(f"Dashboard: http://{HOST}:{PORT}/")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        DASHBOARD_PID_PATH.unlink(missing_ok=True)
        release_lock(lock)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
