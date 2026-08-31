#!/usr/bin/env python3
"""Record to a local spool only while the primary recording drive is offline.

This guardian never stops or restarts a healthy primary recorder. When the primary
storage disappears it starts a local fallback capture; after the drive returns it
keeps the fallback running until the primary active file is growing again, then
finalizes the fallback (A/V scan + SHA-256) and parks it for continuity
reconciliation. Unverified video is never mixed into the final recording root.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))

import hashlib
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from common import (
    CONFIG_DIR, LOGS_DIR, ROOT,
    CREATE_NEW_PROCESS_GROUP,
    acquire_lock, atomic_json_write, find_ffmpeg, graceful_stop, kst_text,
    load_json, now_kst, process_alive, read_json_or_empty, release_lock,
    setup_rotating_logger, ytdlp_path,
)

CONFIG_PATH = CONFIG_DIR / "failover.json"
RECORDER_CONFIG_PATH = CONFIG_DIR / "recorder.json"
RECORDER_STATUS_PATH = CONFIG_DIR / "status.json"
STATUS_PATH = CONFIG_DIR / "failover-status.json"
LOCK_PATH = CONFIG_DIR / "failover.lock"
STOP_PATH = CONFIG_DIR / "failover.stop.request"

DEFAULTS = {
    "FallbackDirectory": str(ROOT / "recordings" / "failover-spool"),
    "FailureConfirmSeconds": 1,
    "PrimaryStableSeconds": 15,
    "MainGrowthConfirmSeconds": 8,
    "CheckIntervalSeconds": 1,
    "FallbackSegmentMinutes": 30,
    "FallbackMinimumFreeSpaceGB": 50,
    "RestartPrimaryWhenMissing": True,
    "RestartCooldownSeconds": 60,
    "LogMaxMB": 10,
    "LogBackups": 10,
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def storage_available(path: Path) -> tuple[bool, float | None]:
    try:
        if not path.is_dir():
            return False, None
        free = shutil.disk_usage(path).free / (1024 ** 3)
        next(path.iterdir(), None)  # force the network drive to answer a real request
        return True, free
    except OSError:
        return False, None


class FallbackPipeline:
    """yt-dlp -> pipe -> FFmpeg segmenter, writing only into the local spool."""

    def __init__(self, config: dict, recorder_config: dict, logger):
        self.config = config
        self.recorder_config = recorder_config
        self.logger = logger
        self.ffmpeg = find_ffmpeg()
        self.ytdlp = ytdlp_path()
        self.spool = Path(config["FallbackDirectory"])
        self.attempt = ""
        self.output_pattern = ""
        self.receiver_process: subprocess.Popen | None = None
        self.ffmpeg_process: subprocess.Popen | None = None
        self.copy_thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return bool(
            self.receiver_process and self.receiver_process.poll() is None
            and self.ffmpeg_process and self.ffmpeg_process.poll() is None
        )

    def files(self) -> list[Path]:
        if not self.attempt:
            return []
        return sorted(self.spool.glob(f"YouTube_{self.attempt}_KST_part_*.mkv"))

    def _receiver_args(self) -> list[str]:
        rc = self.recorder_config
        args = [str(self.ytdlp), "--ignore-config", "--newline",
                "--ffmpeg-location", str(self.ffmpeg)]
        profile = str(rc.get("AuthProfileDir", "")).strip()
        if profile:
            args += ["--cookies-from-browser", f"firefox:{profile}"]
        elif str(rc.get("YtDlpCookiesFile", "")).strip():
            args += ["--cookies", str(rc["YtDlpCookiesFile"])]
        args += [
            "--js-runtimes", str(rc.get("YtDlpJsRuntime", "")),
            "--extractor-args", str(rc.get("YtDlpExtractorArgs", "")),
            "-f", str(rc.get("YtDlpFormat", "300")),
            "--retries", "infinite", "--fragment-retries", "infinite",
            "--retry-sleep", "fragment:linear=1:30:2", "--socket-timeout", "30",
            "--no-part", "--downloader", "ffmpeg",
            "--downloader-args", "ffmpeg:-nostdin", "-o", "-", str(rc["Url"]),
        ]
        return args

    def _log_pipe(self, pipe, label: str) -> None:
        try:
            for raw in iter(pipe.readline, b""):
                text = raw.decode("utf-8", errors="replace").rstrip()
                if text:
                    self.logger.info("[%s] %s", label, text)
        except Exception as exc:
            self.logger.warning("%s log reader ended: %s", label, exc)

    def _copy_pipe(self, source, destination) -> None:
        try:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                destination.write(chunk)
                destination.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            self.logger.info("Fallback pipeline copy ended: %s", exc)
        finally:
            try:
                destination.close()
            except Exception:
                pass

    def start(self) -> None:
        self.spool.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(self.spool).free / (1024 ** 3)
        if free <= float(self.config["FallbackMinimumFreeSpaceGB"]):
            raise RuntimeError(f"Local fallback has only {free:.2f} GB free")
        self.attempt = now_kst().strftime("%Y%m%d_%H%M%S")
        pattern = self.spool / f"YouTube_{self.attempt}_KST_part_%03d.mkv"
        self.output_pattern = str(pattern)
        seconds = max(60, int(float(self.config["FallbackSegmentMinutes"]) * 60))
        ff_args = [
            str(self.ffmpeg), "-hide_banner", "-nostats", "-loglevel", "warning",
            "-fflags", "+genpts+discardcorrupt", "-i", "pipe:0", "-map", "0:v:0?",
            "-map", "0:a:0?", "-c", "copy", "-sn", "-dn", "-f", "segment",
            "-segment_time", str(seconds), "-segment_format", "matroska",
            "-reset_timestamps", "1", str(pattern),
        ]
        self.receiver_process = subprocess.Popen(
            self._receiver_args(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, bufsize=0, creationflags=CREATE_NEW_PROCESS_GROUP,
        )
        try:
            self.ffmpeg_process = subprocess.Popen(
                ff_args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, bufsize=0, creationflags=CREATE_NEW_PROCESS_GROUP,
            )
        except Exception:
            graceful_stop(self.receiver_process, "fallback-yt-dlp", self.logger)
            raise
        self.copy_thread = threading.Thread(
            target=self._copy_pipe,
            args=(self.receiver_process.stdout, self.ffmpeg_process.stdin), daemon=True,
        )
        self.copy_thread.start()
        threading.Thread(target=self._log_pipe, args=(self.receiver_process.stderr, "fallback-yt-dlp"), daemon=True).start()
        threading.Thread(target=self._log_pipe, args=(self.ffmpeg_process.stderr, "fallback-ffmpeg"), daemon=True).start()
        self.logger.warning("Local fallback started: %s", pattern)

    def stop(self) -> list[Path]:
        graceful_stop(self.receiver_process, "fallback-yt-dlp", self.logger)
        if self.copy_thread:
            self.copy_thread.join(timeout=15)
        if self.ffmpeg_process and self.ffmpeg_process.stdin:
            try:
                self.ffmpeg_process.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass
        graceful_stop(self.ffmpeg_process, "fallback-ffmpeg", self.logger)
        paths = self.files()
        self.receiver_process = None
        self.ffmpeg_process = None
        self.logger.info("Local fallback finalized with %d file(s)", len(paths))
        return paths


class Guardian:
    def __init__(self, config: dict, recorder_config: dict, logger):
        self.config, self.recorder_config, self.logger = config, recorder_config, logger
        self.primary = Path(recorder_config["RecordingDirectory"])
        self.fallback = FallbackPipeline(config, recorder_config, logger)
        self.stop_event = threading.Event()
        self.unavailable_since: float | None = None
        self.available_since: float | None = None
        self.growth_since: float | None = None
        self.last_active = ""
        self.last_size: int | None = None
        self.last_restart = 0.0
        self.state, self.message = "starting", ""
        self.pending: list[str] = []

    def install_signals(self) -> None:
        def stop(_signum, _frame):
            self.stop_event.set()
        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), stop)

    def recorder_status(self) -> dict:
        return read_json_or_empty(RECORDER_STATUS_PATH)

    def write_status(self, primary_ok: bool, free: float | None) -> None:
        r = self.fallback.receiver_process
        f = self.fallback.ffmpeg_process
        payload = {
            "State": self.state, "Message": self.message, "UpdatedKST": kst_text(),
            "GuardianPid": os.getpid(), "PrimaryDirectory": str(self.primary),
            "PrimaryAvailable": primary_ok,
            "PrimaryFreeSpaceGB": round(free, 2) if free is not None else None,
            "FallbackDirectory": str(self.fallback.spool),
            "FallbackActive": self.fallback.running,
            "FallbackReceiverPid": r.pid if r and r.poll() is None else None,
            "FallbackFfmpegPid": f.pid if f and f.poll() is None else None,
            "FallbackOutputPattern": self.fallback.output_pattern,
            "PendingContinuityFiles": self.pending,
        }
        try:
            atomic_json_write(STATUS_PATH, payload)
        except OSError as exc:
            self.logger.warning("Could not write failover status: %s", exc)

    def main_growing(self, now: float) -> bool:
        status = self.recorder_status()
        receiver_pid = status.get("ReceiverPid")
        if str(status.get("State", "")).lower() != "recording" or not (
            process_alive(status.get("SupervisorPid"))
            and process_alive(receiver_pid)
            and process_alive(status.get("FfmpegPid"))
        ):
            self.growth_since = None
            return False
        text = status.get("NewestRecording")
        try:
            active = Path(text)
            if active.parent != self.primary:
                raise OSError("not primary")
            size = active.stat().st_size
        except (TypeError, OSError):
            self.growth_since = None
            return False
        if text != self.last_active or self.last_size is None or size > self.last_size:
            if self.growth_since is None:
                self.growth_since = now
            self.last_active, self.last_size = text, size
        return self.growth_since is not None and now - self.growth_since >= float(self.config["MainGrowthConfirmSeconds"])

    def maybe_restart_primary(self, now: float) -> None:
        if not self.config.get("RestartPrimaryWhenMissing", True):
            return
        if process_alive(self.recorder_status().get("SupervisorPid")):
            return
        if now - self.last_restart < float(self.config["RestartCooldownSeconds"]):
            return
        self.last_restart = now
        from common import find_python
        try:
            python = find_python()
        except FileNotFoundError:
            self.logger.error("Cannot restart primary: portable python.exe not found")
            return
        subprocess.Popen(
            [str(python), str(ROOT / "recorder" / "recorder.py")],
            cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=0x08000000 | 0x00000200 | 0x00000008,  # NO_WINDOW | NEW_GROUP | DETACHED
        )
        self.logger.warning("Primary supervisor missing; recorder.py relaunched")

    def validate(self, path: Path) -> tuple[bool, str]:
        result = subprocess.run(
            [str(self.fallback.ffmpeg), "-hide_banner", "-nostats", "-v", "error",
             "-i", str(path), "-map", "0:v:0", "-map", "0:a:0", "-c", "copy",
             "-f", "null", "NUL"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=0x08000000, check=False,
        )
        return (True, sha256(path)) if result.returncode == 0 else (False, result.stderr[-1000:])

    def mark_pending(self, files: list[Path]) -> None:
        rows, pending = [], []
        for path in files:
            ok, detail = self.validate(path)
            pending.append(str(path))
            if ok:
                rows.append({"File": str(path), "Bytes": path.stat().st_size,
                             "SHA256": detail, "ValidatedKST": kst_text()})
            else:
                self.logger.error("Fallback validation failed for %s: %s", path, detail)
        if rows:
            manifest = self.fallback.spool / f"failover_{self.fallback.attempt}.validated.json"
            atomic_json_write(manifest, {"State": "awaiting_continuity_reconciliation", "Files": rows})
            pending.append(str(manifest))
        self.pending = pending

    def run(self) -> int:
        self.install_signals()
        STOP_PATH.unlink(missing_ok=True)
        self.logger.info("Failover guardian started; PID=%s", os.getpid())
        interval = float(self.config["CheckIntervalSeconds"])
        while not self.stop_event.is_set() and not STOP_PATH.exists():
            now = time.monotonic()
            ok, free = storage_available(self.primary)
            if not ok:
                self.available_since = None
                if self.unavailable_since is None:
                    self.unavailable_since = now
                    self.logger.error("Primary unavailable: %s", self.primary)
                if not self.fallback.running and now - self.unavailable_since >= float(self.config["FailureConfirmSeconds"]):
                    try:
                        self.fallback.start()
                    except Exception as exc:
                        self.logger.exception("Could not start local fallback: %s", exc)
                self.state = "recording_local" if self.fallback.running else "primary_unavailable"
                self.message = ("Primary storage unavailable; local fallback recording"
                                if self.fallback.running else
                                "Primary storage unavailable; starting local fallback")
            else:
                self.unavailable_since = None
                if self.available_since is None:
                    self.available_since = now
                self.maybe_restart_primary(now)
                if self.fallback.running:
                    stable = now - self.available_since >= float(self.config["PrimaryStableSeconds"])
                    if stable and self.main_growing(now):
                        self.state, self.message = "finalizing_local", "Primary stable; finalizing local fallback"
                        self.write_status(ok, free)
                        self.mark_pending(self.fallback.stop())
                    else:
                        self.state, self.message = "overlap_handoff", "Primary restored; local continues until primary growth is confirmed"
                else:
                    self.growth_since = None
                    self.state, self.message = "standby", "Primary storage available; primary recording authoritative"
            self.write_status(ok, free)
            self.stop_event.wait(interval)
        if self.fallback.running:
            self.mark_pending(self.fallback.stop())
        ok, free = storage_available(self.primary)
        self.state, self.message = "stopped", "Failover guardian stopped"
        self.write_status(ok, free)
        return 0


def main() -> int:
    config = DEFAULTS.copy()
    if CONFIG_PATH.exists():
        config.update(load_json(CONFIG_PATH))
    recorder_config = load_json(RECORDER_CONFIG_PATH)
    fallback = Path(str(config["FallbackDirectory"]))
    if not fallback.is_absolute() or fallback == Path(str(recorder_config["RecordingDirectory"])):
        raise ValueError("FallbackDirectory must be absolute and different from RecordingDirectory")
    fallback.mkdir(parents=True, exist_ok=True)
    try:
        lock = acquire_lock(LOCK_PATH)
    except RuntimeError:
        return 0
    logger = setup_rotating_logger("youtube-recorder-failover", "failover.log",
                                   config["LogMaxMB"], config["LogBackups"])
    try:
        return Guardian(config, recorder_config, logger).run()
    except Exception:
        logger.exception("Fatal failover guardian error")
        return 1
    finally:
        release_lock(lock)


if __name__ == "__main__":
    raise SystemExit(main())
