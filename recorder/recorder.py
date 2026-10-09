#!/usr/bin/env python3
"""Supervise a resilient, receive-only YouTube LIVE recording pipeline on Windows.

Pipeline:  yt-dlp (authenticated HLS receive) --> pipe --> FFmpeg segment muxer --> MKV parts

Only the viewer side is used. No YouTube account, channel, or API setting is changed.
The supervisor keeps the pipeline alive across stream/network drops, pauses safely on
low disk, and records HLS media-sequence boundaries so gaps can be found and back-filled.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))

import argparse
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from common import (
    CONFIG_DIR, LOGS_DIR, ROOT,
    CREATE_NEW_PROCESS_GROUP,
    acquire_lock, atomic_json_write, find_ffmpeg, graceful_stop, kst_text,
    load_json, now_kst, release_lock, setup_rotating_logger,
    validate_youtube_url, ytdlp_path,
    DEFAULT_JS_RUNTIME, js_runtime_arg, ytdlp_cache_args,
)

CONFIG_PATH = CONFIG_DIR / "recorder.json"
STATUS_PATH = CONFIG_DIR / "status.json"
STOP_PATH = CONFIG_DIR / "stop.request"
LOCK_PATH = CONFIG_DIR / "recorder.lock"
CONTINUITY_LOG = LOGS_DIR / "continuity.log"

DEFAULTS = {
    "Url": "",
    "RecordingDirectory": str(ROOT / "recordings"),
    "SegmentHours": 2,
    "AuthProfileDir": "",
    "YtDlpCookiesFile": "",
    "YtDlpFormat": "300/95/best",
    "YtDlpJsRuntime": DEFAULT_JS_RUNTIME,
    "YtDlpExtractorArgs": "youtube:player_client=default,web_safari;player_js_version=actual",
    "MinimumFreeSpaceGB": 50,
    "ResumeMarginGB": 5,
    "ReconnectDelaySeconds": 10,
    "DiskCheckIntervalSeconds": 30,
    "LogMaxMB": 10,
    "LogBackups": 10,
}

SEQUENCE_RE = re.compile(r"/sq/(\d+)/")


def load_config() -> dict:
    config = DEFAULTS.copy()
    if CONFIG_PATH.exists():
        config.update(load_json(CONFIG_PATH))
    return config


def validate_config(config: dict) -> None:
    numeric_rules = {
        "SegmentHours": (0.05, 24),
        "MinimumFreeSpaceGB": (1, 10000),
        "ResumeMarginGB": (0, 1000),
        "ReconnectDelaySeconds": (1, 3600),
        "DiskCheckIntervalSeconds": (5, 3600),
        "LogMaxMB": (1, 1024),
        "LogBackups": (1, 100),
    }
    for name, (low, high) in numeric_rules.items():
        value = config.get(name)
        if not isinstance(value, (int, float)) or not low <= value <= high:
            raise ValueError(f"Invalid {name}: expected {low}..{high}")

    directory = config.get("RecordingDirectory")
    if not isinstance(directory, str) or not directory.strip():
        raise ValueError("RecordingDirectory must be a non-empty absolute path")
    if not Path(directory).is_absolute():
        raise ValueError("RecordingDirectory must be an absolute path")

    for key in ("YtDlpFormat", "YtDlpJsRuntime", "YtDlpExtractorArgs"):
        if not str(config.get(key, "")).strip():
            raise ValueError(f"{key} must be non-empty")

    runtime_path = js_runtime_arg(config["YtDlpJsRuntime"]).partition(":")[2]
    if runtime_path and not Path(runtime_path).is_file():
        raise ValueError(f"JS runtime was not found: {runtime_path} (place node.exe in recorder\\node\\)")

    profile = str(config.get("AuthProfileDir", "")).strip()
    cookies = str(config.get("YtDlpCookiesFile", "")).strip()
    if profile:
        if not Path(profile).is_dir():
            raise ValueError("AuthProfileDir must be an existing directory (run login-youtube.bat)")
    elif cookies:
        if not Path(cookies).is_file():
            raise ValueError("YtDlpCookiesFile must be an existing file")
    else:
        raise ValueError("Set AuthProfileDir (preferred) or YtDlpCookiesFile for YouTube login")


def append_continuity(line: str) -> None:
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with CONTINUITY_LOG.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{kst_text()}\t{line}\n")
    except OSError:
        pass


class Recorder:
    def __init__(self, config: dict, ffmpeg: Path, ytdlp: Path, logger):
        self.config = config
        self.ffmpeg = ffmpeg
        self.ytdlp = ytdlp
        self.logger = logger
        self.recordings_dir = Path(config["RecordingDirectory"])
        self.stop_event = threading.Event()
        self.supervisor_started = kst_text()
        self.state = "starting"
        self.state_message = ""
        self.receiver_process: subprocess.Popen | None = None
        self.ffmpeg_process: subprocess.Popen | None = None
        self.output_pattern = ""
        self.current_attempt_glob = ""
        self._seq_lock = threading.Lock()
        self.first_sequence: int | None = None
        self.last_sequence: int | None = None
        self.prev_attempt_last_sequence: int | None = None

    # -- lifecycle ------------------------------------------------------

    def install_signal_handlers(self) -> None:
        def handle_signal(signum, _frame):
            self.logger.info("Received signal %s; safe stop requested", signum)
            self.stop_event.set()

        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), handle_signal)

    def stop_requested(self) -> bool:
        if STOP_PATH.exists():
            self.state_message = "Safe stop requested"
            self.stop_event.set()
        return self.stop_event.is_set()

    def interruptible_wait(self, seconds: float) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.stop_requested():
                return False
            time.sleep(max(0.0, min(1.0, end - time.monotonic())))
        return True

    # -- disk ---------------------------------------------------------

    def free_gb(self) -> float:
        import shutil
        return shutil.disk_usage(self.recordings_dir).free / (1024 ** 3)

    def wait_for_disk_space(self) -> bool:
        minimum = float(self.config["MinimumFreeSpaceGB"])
        resume_at = minimum + float(self.config["ResumeMarginGB"])
        while not self.stop_requested():
            free = self.free_gb()
            if free >= resume_at:
                return True
            self.state = "paused_low_disk"
            self.state_message = f"Free space {free:.2f} GB; waiting for at least {resume_at:.2f} GB"
            self.write_status()
            self.logger.warning(self.state_message)
            if not self.interruptible_wait(30):
                return False
        return False

    # -- status -----------------------------------------------------

    def newest_recording(self) -> str:
        try:
            newest = max(self.recordings_dir.glob(self.current_attempt_glob or "*.mkv"),
                         key=lambda p: p.stat().st_mtime)
            return str(newest)
        except (ValueError, OSError):
            return ""

    def receiver_pid(self) -> int | None:
        p = self.receiver_process
        return p.pid if p and p.poll() is None else None

    def ffmpeg_pid(self) -> int | None:
        p = self.ffmpeg_process
        return p.pid if p and p.poll() is None else None

    def write_status(self) -> None:
        try:
            free = round(self.free_gb(), 2)
        except OSError:
            free = None
        status = {
            "State": self.state,
            "Message": self.state_message,
            "SupervisorPid": os.getpid(),
            "ReceiverKind": "yt-dlp",
            "ReceiverPid": self.receiver_pid(),
            "FfmpegPid": self.ffmpeg_pid(),
            "SupervisorStartedKST": self.supervisor_started,
            "UpdatedKST": kst_text(),
            "Url": self.config["Url"],
            "RecordingDirectory": str(self.recordings_dir),
            "SegmentHours": self.config["SegmentHours"],
            "MinimumFreeSpaceGB": self.config["MinimumFreeSpaceGB"],
            "FreeSpaceGB": free,
            "OutputPattern": self.output_pattern,
            "NewestRecording": self.newest_recording(),
            "FirstSequence": self.first_sequence,
            "LastSequence": self.last_sequence,
        }
        try:
            atomic_json_write(STATUS_PATH, status)
        except OSError as exc:
            self.logger.warning("Could not update status file; recording continues: %s", exc)

    # -- pipeline ---------------------------------------------------

    def build_receiver_args(self) -> list[str]:
        args = [str(self.ytdlp), "--ignore-config", "--newline",
                "--ffmpeg-location", str(self.ffmpeg), *ytdlp_cache_args()]
        profile = str(self.config.get("AuthProfileDir", "")).strip()
        if profile:
            args += ["--cookies-from-browser", f"firefox:{profile}"]
        elif str(self.config.get("YtDlpCookiesFile", "")).strip():
            args += ["--cookies", str(self.config["YtDlpCookiesFile"])]
        args += [
            "--js-runtimes", js_runtime_arg(self.config["YtDlpJsRuntime"]),
            "--extractor-args", str(self.config["YtDlpExtractorArgs"]),
            "-f", str(self.config["YtDlpFormat"]),
            "--retries", "infinite",
            "--fragment-retries", "infinite",
            "--retry-sleep", "fragment:linear=1:30:2",
            "--socket-timeout", "30",
            "--no-part",
            "--downloader", "ffmpeg",
            "--downloader-args", "ffmpeg:-nostdin",
            "-o", "-",
            self.config["Url"],
        ]
        return args

    def build_ffmpeg_args(self, output_pattern: Path) -> list[str]:
        segment_seconds = max(1, int(float(self.config["SegmentHours"]) * 3600))
        return [
            str(self.ffmpeg), "-hide_banner", "-nostats", "-loglevel", "warning",
            "-fflags", "+genpts+discardcorrupt", "-i", "pipe:0",
            "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy", "-sn", "-dn",
            "-f", "segment", "-segment_time", str(segment_seconds),
            "-segment_format", "matroska", "-reset_timestamps", "1",
            str(output_pattern),
        ]

    def note_sequence(self, value: int) -> None:
        with self._seq_lock:
            if self.first_sequence is None:
                self.first_sequence = value
                prev = self.prev_attempt_last_sequence
                if prev is not None:
                    gap = value - prev - 1
                    if gap > 0:
                        msg = f"GAP {gap} segment(s): previous attempt ended at sq={prev}, new attempt starts at sq={value}"
                        self.logger.warning(msg)
                        append_continuity(msg)
                    else:
                        append_continuity(f"continuous: sq {prev} -> {value}")
                append_continuity(f"attempt start sq={value} file={Path(self.output_pattern).name}")
            self.last_sequence = max(value, self.last_sequence or 0)

    def pipe_to_log(self, pipe, label: str) -> None:
        try:
            for raw in iter(pipe.readline, b""):
                text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if not text:
                    continue
                self.logger.info("[%s] %s", label, text)
                match = SEQUENCE_RE.search(text)
                if match:
                    self.note_sequence(int(match.group(1)))
        except Exception as exc:  # logging must never take down the recorder
            self.logger.warning("[%s] log reader stopped: %s", label, exc)
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    def pipe_stream(self, source, destination) -> None:
        try:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                destination.write(chunk)
                destination.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            self.logger.info("Pipeline copy ended: %s", exc)
        finally:
            for stream in (source, destination):
                try:
                    stream.close()
                except Exception:
                    pass

    def launch_pipeline(self) -> threading.Thread:
        attempt = now_kst().strftime("%Y%m%d_%H%M%S")
        output_pattern = self.recordings_dir / f"YouTube_{attempt}_KST_part_%03d.mkv"
        self.output_pattern = str(output_pattern)
        self.current_attempt_glob = f"YouTube_{attempt}_KST_part_*.mkv"
        with self._seq_lock:
            if self.last_sequence is not None:
                self.prev_attempt_last_sequence = self.last_sequence
            self.first_sequence = None
            self.last_sequence = None
        flags = CREATE_NEW_PROCESS_GROUP

        self.logger.info("Starting yt-dlp; format=%s", self.config["YtDlpFormat"])
        self.receiver_process = subprocess.Popen(
            self.build_receiver_args(), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, creationflags=flags,
        )
        try:
            self.ffmpeg_process = subprocess.Popen(
                self.build_ffmpeg_args(output_pattern), stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, bufsize=0, creationflags=flags,
            )
        except Exception:
            graceful_stop(self.receiver_process, "yt-dlp", self.logger)
            raise

        copy_thread = threading.Thread(
            target=self.pipe_stream,
            args=(self.receiver_process.stdout, self.ffmpeg_process.stdin),
            name="stream-copy", daemon=True,
        )
        copy_thread.start()
        for pipe, label, tname in (
            (self.receiver_process.stderr, "yt-dlp", "yt-dlp-log"),
            (self.ffmpeg_process.stderr, "ffmpeg", "ffmpeg-log"),
        ):
            threading.Thread(target=self.pipe_to_log, args=(pipe, label), name=tname, daemon=True).start()
        return copy_thread

    def finish_pipeline(self, copy_thread: threading.Thread, reason: str) -> None:
        receiver, ffmpeg = self.receiver_process, self.ffmpeg_process
        if receiver and receiver.poll() is None:
            graceful_stop(receiver, "yt-dlp", self.logger)
        copy_thread.join(timeout=15)
        if ffmpeg and ffmpeg.stdin:
            try:
                ffmpeg.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass
        if ffmpeg and ffmpeg.poll() is None:
            self.logger.info("Waiting for FFmpeg to finalize the current MKV (%s)", reason)
            try:
                ffmpeg.wait(timeout=30)
            except subprocess.TimeoutExpired:
                graceful_stop(ffmpeg, "FFmpeg", self.logger)
        self.logger.info(
            "Pipeline ended: reason=%s yt-dlp=%s ffmpeg=%s last_sq=%s",
            reason, receiver.poll() if receiver else None,
            ffmpeg.poll() if ffmpeg else None, self.last_sequence,
        )
        if self.last_sequence is not None:
            append_continuity(f"attempt end   sq={self.last_sequence} reason={reason}")
        self.receiver_process = None
        self.ffmpeg_process = None

    def run_attempt(self) -> str:
        copy_thread = self.launch_pipeline()
        self.state = "connecting"
        self.state_message = "Waiting for / receiving the YouTube LIVE stream"
        self.write_status()
        disk_interval = float(self.config["DiskCheckIntervalSeconds"])
        next_disk_check = time.monotonic()
        reason = "pipeline_exit"
        try:
            while True:
                if self.stop_requested():
                    reason = "safe_stop"
                    break
                now = time.monotonic()
                if now >= next_disk_check:
                    if self.free_gb() <= float(self.config["MinimumFreeSpaceGB"]):
                        self.state = "paused_low_disk"
                        self.state_message = f"Free space reached protection limit"
                        self.logger.warning(self.state_message)
                        reason = "low_disk"
                        break
                    next_disk_check = now + disk_interval
                if self.receiver_process and self.receiver_process.poll() is not None:
                    reason = "receiver_exit"
                    break
                if self.ffmpeg_process and self.ffmpeg_process.poll() is not None:
                    reason = "ffmpeg_exit"
                    break
                if any(self.recordings_dir.glob(self.current_attempt_glob)):
                    self.state = "recording"
                    self.state_message = "Recording with stream copy (no re-encode)"
                self.write_status()
                time.sleep(2)
        finally:
            self.finish_pipeline(copy_thread, reason)
        return reason

    def run(self) -> int:
        self.install_signal_handlers()
        try:
            STOP_PATH.unlink(missing_ok=True)
        except OSError as exc:
            self.logger.warning("Could not clear old stop request: %s", exc)

        self.logger.info("Recorder supervisor started; PID=%s", os.getpid())
        self.recordings_dir.mkdir(parents=True, exist_ok=True)

        while not self.stop_requested():
            if self.free_gb() <= float(self.config["MinimumFreeSpaceGB"]):
                if not self.wait_for_disk_space():
                    break
            try:
                reason = self.run_attempt()
            except Exception:
                self.logger.exception("Could not start or supervise the recording pipeline")
                reason = "launch_error"

            if self.stop_requested():
                break
            if reason == "low_disk":
                if not self.wait_for_disk_space():
                    break
                continue

            self.state = "reconnecting"
            delay = float(self.config["ReconnectDelaySeconds"])
            self.state_message = f"Pipeline ended ({reason}); reconnecting in {delay:g} s"
            self.write_status()
            self.logger.warning(self.state_message)
            if not self.interruptible_wait(delay):
                break

        self.state = "stopped"
        self.state_message = "Recorder stopped safely"
        self.write_status()
        self.logger.info("Recorder supervisor stopped")
        return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="YouTube LIVE recorder supervisor")
    parser.add_argument("url", nargs="?", help="YouTube LIVE URL; saved to config when provided")
    return parser.parse_args()


def main() -> int:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    args = parse_args()

    try:
        config = load_config()
        if args.url:
            config["Url"] = validate_youtube_url(args.url)
            atomic_json_write(CONFIG_PATH, config)
        elif not str(config.get("Url", "")).strip():
            config["Url"] = validate_youtube_url(input("YouTube LIVE URL: "))
            atomic_json_write(CONFIG_PATH, config)
        else:
            config["Url"] = validate_youtube_url(str(config["Url"]))
        validate_config(config)
        Path(config["RecordingDirectory"]).mkdir(parents=True, exist_ok=True)
        ffmpeg = find_ffmpeg()
        ytdlp = ytdlp_path()
        lock_handle = acquire_lock(LOCK_PATH)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    logger = setup_rotating_logger("youtube-recorder", "recorder.log",
                                   config["LogMaxMB"], config["LogBackups"])
    logger.info("Configuration loaded from %s", CONFIG_PATH)
    recorder = Recorder(config, ffmpeg, ytdlp, logger)
    try:
        return recorder.run()
    except KeyboardInterrupt:
        recorder.stop_event.set()
        logger.info("Keyboard interrupt received")
        return 130
    except Exception:
        logger.exception("Fatal recorder error")
        return 1
    finally:
        release_lock(lock_handle)


if __name__ == "__main__":
    raise SystemExit(main())
