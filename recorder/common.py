#!/usr/bin/env python3
"""Shared helpers for the YouTube LIVE recorder tools.

Every tool in this folder runs on Windows against the same layout:

    <ROOT>/recorder/*.py        this code
    <ROOT>/config/*.json        settings + runtime status (git-ignored)
    <ROOT>/logs/*.log           rotating logs (git-ignored)
    <ROOT>/recorder/yt-dlp.exe  bundled receiver (git-ignored)
    <ROOT>/recorder/streamlink-*-x86_64/  portable Python + FFmpeg (git-ignored)

Only the receive side is used: no YouTube account, channel, or API is changed.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

# The portable Python runs on a Korean (cp949) console by default; force UTF-8 so
# Korean status text and em-dashes never raise UnicodeEncodeError.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError, OSError):
        pass

# --- layout ---------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
RECORDER_DIR = ROOT / "recorder"
CONFIG_DIR = ROOT / "config"
LOGS_DIR = ROOT / "logs"

KST = timezone(timedelta(hours=9), name="KST")

# Windows process creation flags (subprocess re-exports these but importing them
# from one place keeps the call sites short and consistent).
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008

_KST_FORMAT = "%Y-%m-%d %H:%M:%S KST"


# --- time ----------------------------------------------------------------

def now_kst() -> datetime:
    return datetime.now(KST)


def kst_text(value: datetime | None = None) -> str:
    return (value or now_kst()).astimezone(KST).strftime(_KST_FORMAT)


def parse_kst(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value.strip().removesuffix(" KST"),
                                 "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
    except (TypeError, ValueError):
        return None


# --- JSON --------------------------------------------------------------

def load_json(path: Path) -> dict:
    """Read a JSON object, tolerating a UTF-8 BOM. Raises on malformed input."""
    with path.open("r", encoding="utf-8-sig") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"JSON root is not an object: {path.name}")
    return value


def read_json_or_empty(path: Path) -> dict:
    """Read a JSON object, returning {} for any missing/invalid file."""
    try:
        return load_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def atomic_json_write(path: Path, data: dict) -> None:
    """Write JSON to a temp file then os.replace() it into place.

    A concurrent reader (dashboard, watchdog) can briefly hold the destination
    open on Windows; retry long enough for those short reads to finish instead
    of surfacing a sharing violation as a fault.
    """
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(20):
            try:
                os.replace(temp_path, path)
                return
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        temp_path.unlink(missing_ok=True)


# --- processes ---------------------------------------------------------

def process_alive(pid: object) -> bool:
    """True if pid names a live process. Accepts int/str/None."""
    try:
        numeric = int(pid)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if numeric <= 0:
        return False
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, numeric)
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


def named_process_alive(image_name: str) -> bool | None:
    """True/False if the given IMAGENAME is running, or None if the check failed."""
    image_name = image_name.strip()
    if not image_name:
        return None
    try:
        result = subprocess.run(
            ["tasklist.exe", "/FI", f"IMAGENAME eq {image_name}", "/NH"],
            capture_output=True, text=True, timeout=10,
            creationflags=CREATE_NO_WINDOW, check=False,
        )
        if result.returncode != 0:
            return None
        return image_name.lower() in result.stdout.lower()
    except (OSError, subprocess.SubprocessError):
        return None


def kill_process_tree(pid: int) -> None:
    subprocess.run(
        ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW, check=False,
    )


def graceful_stop(process: subprocess.Popen | None, label: str,
                  logger: logging.Logger, ctrl_break_timeout: float = 12,
                  kill_timeout: float = 8) -> None:
    """Ask a child process group to stop, escalating to taskkill /T /F."""
    if not process or process.poll() is not None:
        return
    logger.info("Requesting graceful stop of %s (PID %s)", label, process.pid)
    try:
        import signal
        process.send_signal(signal.CTRL_BREAK_EVENT)
        process.wait(timeout=ctrl_break_timeout)
        return
    except (OSError, ValueError, subprocess.TimeoutExpired):
        logger.warning("Force-stopping %s process tree (PID %s)", label, process.pid)
        kill_process_tree(process.pid)
        try:
            process.wait(timeout=kill_timeout)
        except subprocess.TimeoutExpired:
            logger.error("%s did not exit after taskkill", label)


# --- runtime discovery ----------------------------------------------------

def _bundles() -> list[Path]:
    return sorted(RECORDER_DIR.glob("streamlink-*-x86_64"), reverse=True)


def find_ffmpeg() -> Path:
    """Locate ffmpeg.exe: recorder/ffmpeg/ then any portable bundle."""
    direct = RECORDER_DIR / "ffmpeg" / "ffmpeg.exe"
    if direct.is_file():
        return direct
    for bundle in _bundles():
        candidate = bundle / "ffmpeg" / "ffmpeg.exe"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("ffmpeg.exe was not found (recorder/ffmpeg/ or a portable bundle)")


def find_python() -> Path:
    """Locate the portable python.exe used to launch sibling scripts."""
    direct = RECORDER_DIR / "python" / "python.exe"
    if direct.is_file():
        return direct
    for bundle in _bundles():
        candidate = bundle / "Python" / "python.exe"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("portable python.exe was not found")


def find_pythonw() -> Path:
    for base in [RECORDER_DIR / "python"] + [b / "Python" for b in _bundles()]:
        candidate = base / "pythonw.exe"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("portable pythonw.exe was not found")


def ytdlp_path() -> Path:
    path = RECORDER_DIR / "yt-dlp.exe"
    if not path.is_file():
        raise FileNotFoundError(f"yt-dlp.exe was not found: {path}")
    return path


# --- logging -----------------------------------------------------------

def setup_rotating_logger(name: str, filename: str, max_mb: float = 10,
                          backups: int = 10) -> logging.Logger:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()
    handler = RotatingFileHandler(
        LOGS_DIR / filename, maxBytes=int(max_mb * 1024 * 1024),
        backupCount=int(backups), encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


# --- single-instance lock ----------------------------------------------

def acquire_lock(path: Path):
    """Take an exclusive 1-byte lock on `path`. Raises RuntimeError if held."""
    import msvcrt
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        raise RuntimeError(f"another instance holds {path.name}")
    return handle


def release_lock(handle) -> None:
    import msvcrt
    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    finally:
        handle.close()


# --- YouTube URL validation ------------------------------------------

def validate_youtube_url(url: str) -> str:
    """Return the URL unchanged if it is a plausible YouTube HTTP(S) URL."""
    from urllib.parse import urlparse

    url = (url or "").strip()
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    allowed = (
        host in {"youtu.be", "youtube.com", "www.youtube.com", "m.youtube.com",
                 "youtube-nocookie.com", "manifest.googlevideo.com"}
        or host.endswith(".youtube.com")
        or host.endswith(".youtube-nocookie.com")
    )
    if parsed.scheme not in {"http", "https"} or not allowed:
        raise ValueError("유효한 YouTube HTTP(S) 주소를 입력하세요.")
    return url
