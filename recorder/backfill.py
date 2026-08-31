#!/usr/bin/env python3
"""Recover the elapsed DVR portion of a YouTube LIVE, then split-copy it to MKV."""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))

import json
import logging
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

from common import (
    CONFIG_DIR, LOGS_DIR, ROOT, KST,
    acquire_lock, atomic_json_write, find_ffmpeg, kst_text as now_kst_text,
    parse_kst as _parse_kst, release_lock, setup_rotating_logger, ytdlp_path,
)

CONFIG_PATH = CONFIG_DIR / "recorder.json"
STATUS_PATH = CONFIG_DIR / "backfill-status.json"
STOP_PATH = CONFIG_DIR / "backfill-stop.request"
LOCK_PATH = CONFIG_DIR / "backfill.lock"
STAGING_DIR = ROOT / "recordings" / "backfill-staging"


def parse_kst(value: str) -> datetime:
    result = _parse_kst(value)
    if result is None:
        raise ValueError(f"Invalid KST timestamp: {value!r} (expected 'YYYY-MM-DD HH:MM:SS')")
    return result


class Backfill:
    def __init__(self, config: dict, ffmpeg: Path, ytdlp: Path, logger: logging.Logger):
        self.config = config
        self.ffmpeg = ffmpeg
        self.ytdlp = ytdlp
        self.logger = logger
        self.process: subprocess.Popen | None = None
        self.state = "starting"
        self.message = "Reading LIVE metadata"
        self.started = now_kst_text()
        self.start_kst: datetime | None = None
        self.cutover_kst = parse_kst(config["BackfillCutoverKST"])
        self.target_seconds = 0
        self.target_fragment = 0
        self.current_fragment = 0
        self.fragment_count = 0
        self.title = ""
        self.output_files: list[str] = []
        self.staging_base = ""

    def write_status(self) -> None:
        progress = 0.0
        if self.target_fragment:
            progress = min(100.0, self.current_fragment / self.target_fragment * 100.0)
        atomic_json_write(
            STATUS_PATH,
            {
                "State": self.state,
                "Message": self.message,
                "Pid": os.getpid(),
                "YtDlpPid": self.process.pid if self.process and self.process.poll() is None else None,
                "StartedKST": self.started,
                "UpdatedKST": now_kst_text(),
                "Title": self.title,
                "LiveStartKST": self.start_kst.strftime("%Y-%m-%d %H:%M:%S KST") if self.start_kst else None,
                "CutoverKST": self.cutover_kst.strftime("%Y-%m-%d %H:%M:%S KST"),
                "TargetSeconds": self.target_seconds,
                "CurrentFragment": self.current_fragment,
                "TargetFragment": self.target_fragment,
                "FragmentCountAtStart": self.fragment_count,
                "ProgressPercent": round(progress, 1),
                "StagingDirectory": str(STAGING_DIR),
                "OutputFiles": self.output_files,
            },
        )

    def read_metadata(self) -> None:
        command = [
            str(self.ytdlp), "--ignore-config", "--no-warnings", "--dump-single-json",
            "--skip-download", self.config["Url"],
        ]
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=True, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        metadata = json.loads(result.stdout)
        if metadata.get("live_status") != "is_live":
            raise RuntimeError("The configured URL is not currently live")
        timestamp = metadata.get("release_timestamp") or metadata.get("timestamp")
        if not timestamp:
            raise RuntimeError("YouTube did not expose a DVR start timestamp")
        self.start_kst = datetime.fromtimestamp(float(timestamp), tz=timezone.utc).astimezone(KST)
        self.title = str(metadata.get("title") or "")
        self.target_seconds = int((self.cutover_kst - self.start_kst).total_seconds())
        if self.target_seconds < 60:
            raise RuntimeError("Backfill cutover must be at least 60 seconds after the LIVE start")
        # YouTube's DVR DASH fragments are approximately two seconds each.
        self.target_fragment = math.ceil(self.target_seconds / 2.0) + 2
        start_name = self.start_kst.strftime("%Y%m%d_%H%M%S")
        cutover_name = self.cutover_kst.strftime("%Y%m%d_%H%M%S")
        self.staging_base = f"backfill_{start_name}_to_{cutover_name}"
        self.logger.info(
            "Backfill range: %s -> %s (%s seconds, target fragment %s)",
            self.start_kst, self.cutover_kst, self.target_seconds, self.target_fragment,
        )

    def ytdl_state_path(self) -> Path | None:
        candidates = sorted(STAGING_DIR.glob(f"{self.staging_base}.f298.*.ytdl"))
        return candidates[0] if candidates else None

    def format_state_path(self, format_id: str) -> Path | None:
        candidates = sorted(STAGING_DIR.glob(f"{self.staging_base}.f{format_id}.*.ytdl"))
        return candidates[0] if candidates else None

    def base_media_part(self, format_id: str) -> Path | None:
        candidates = [
            path for path in STAGING_DIR.glob(f"{self.staging_base}.f{format_id}.*.part")
            if "-Frag" not in path.name and path.is_file()
        ]
        return max(candidates, key=lambda path: path.stat().st_size) if candidates else None

    def format_resume_state(self, format_id: str) -> tuple[int, int]:
        state_path = self.format_state_path(format_id)
        if not state_path:
            return 0, 0
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            downloader = data.get("downloader", {})
            current = int(downloader.get("current_fragment", {}).get("index") or 0)
            count = int(downloader.get("fragment_count") or 0)
            return current, count
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return 0, 0

    def contiguous_fragment(self, format_id: str) -> tuple[int, int]:
        current, count = self.format_resume_state(format_id)
        media = self.base_media_part(format_id)
        if not media:
            return current, count
        prefix = media.name + "-Frag"
        completed: set[int] = set()
        for path in STAGING_DIR.glob(prefix + "*"):
            suffix = path.name.removeprefix(prefix)
            if suffix.isdigit() and path.stat().st_size > 0:
                completed.add(int(suffix))
        while current + 1 in completed:
            current += 1
        return current, count

    def read_fragment_state(self) -> None:
        video_current, video_count = self.contiguous_fragment("298")
        audio_current, audio_count = self.contiguous_fragment("140")
        if video_current and audio_current:
            self.current_fragment = min(video_current, audio_current)
        else:
            self.current_fragment = max(video_current, audio_current)
        self.fragment_count = max(video_count, audio_count)

    def consolidate_fragments(self, format_id: str, limit: int | None = None) -> int:
        current, count = self.format_resume_state(format_id)
        contiguous, _ = self.contiguous_fragment(format_id)
        if limit is not None:
            contiguous = min(contiguous, limit)
        media = self.base_media_part(format_id)
        state_path = self.format_state_path(format_id)
        if not media or not state_path or contiguous <= current:
            return current
        self.logger.info("Consolidating format %s fragments %s..%s", format_id, current + 1, contiguous)
        with media.open("ab") as output:
            for index in range(current + 1, contiguous + 1):
                fragment = Path(str(media) + f"-Frag{index}")
                with fragment.open("rb") as source:
                    while chunk := source.read(1024 * 1024):
                        output.write(chunk)
                fragment.unlink()
            output.flush()
            os.fsync(output.fileno())
        atomic_json_write(
            state_path,
            {"downloader": {"current_fragment": {"index": contiguous}, "fragment_count": count}},
        )
        return contiguous

    def stop_ytdlp(self) -> None:
        if not self.process or self.process.poll() is not None:
            return
        self.logger.info("Stopping yt-dlp at video fragment %s", self.current_fragment)
        try:
            self.process.send_signal(signal.CTRL_BREAK_EVENT)
            self.process.wait(timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            subprocess.run(
                ["taskkill.exe", "/PID", str(self.process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass

    def download(self) -> None:
        # Resume from every already-completed concurrent fragment without re-downloading it.
        self.consolidate_fragments("298")
        self.consolidate_fragments("140")
        output_template = str(STAGING_DIR / f"{self.staging_base}.%(ext)s")
        command = [
            str(self.ytdlp), "--ignore-config", "--quiet", "--no-warnings",
            "--live-from-start", "--concurrent-fragments", "16",
            "--ffmpeg-location", str(self.ffmpeg.parent),
            "-f", "298+140", "--merge-output-format", "mkv", "--remux-video", "mkv",
            "-o", output_template, self.config["Url"],
        ]
        self.state = "downloading"
        self.message = "Recovering the elapsed LIVE from the DVR start"
        self.write_status()
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
        )
        try:
            while True:
                if STOP_PATH.exists():
                    self.state = "stopped"
                    self.message = "Backfill stopped; staging files were kept for resume"
                    self.write_status()
                    raise InterruptedError(self.message)
                self.read_fragment_state()
                self.write_status()
                if self.current_fragment >= self.target_fragment:
                    break
                if self.process.poll() is not None:
                    self.read_fragment_state()
                    if self.current_fragment >= self.target_fragment:
                        break
                    raise RuntimeError(f"yt-dlp exited before the cutover (code {self.process.returncode})")
                time.sleep(5)
        finally:
            self.stop_ytdlp()
        self.process = None
        self.consolidate_fragments("298", self.target_fragment)
        self.consolidate_fragments("140", self.target_fragment)
        self.current_fragment = self.target_fragment
        self.write_status()

    def media_part(self, format_id: str) -> Path:
        candidates = [
            path for path in STAGING_DIR.glob(f"{self.staging_base}.f{format_id}.*")
            if path.suffix.lower() not in {".ytdl"} and path.is_file()
        ]
        if not candidates:
            raise FileNotFoundError(f"Staging media for format {format_id} was not found")
        return max(candidates, key=lambda path: path.stat().st_size)

    def mux_and_split(self) -> None:
        if not self.start_kst:
            raise RuntimeError("LIVE start time is unknown")
        video = self.media_part("298")
        audio = self.media_part("140")
        recording_dir = Path(self.config["RecordingDirectory"])
        # The recording root is a canonical playback set. Backfills must use
        # the same YouTube prefix; superseded overlaps belong in _중복백업.
        output_pattern = recording_dir / f"YouTube_{self.start_kst.strftime('%Y%m%d_%H%M%S')}_KST_part_%03d.mkv"
        if any(recording_dir.glob(output_pattern.name.replace("%03d", "*"))):
            raise FileExistsError("Backfill output already exists; refusing to overwrite it")
        self.state = "muxing"
        self.message = "Combining video/audio and splitting into two-hour MKV files"
        self.write_status()
        command = [
            str(self.ffmpeg), "-hide_banner", "-nostats", "-loglevel", "error", "-n",
            "-fflags", "+genpts+discardcorrupt", "-i", str(video), "-i", str(audio),
            "-t", str(self.target_seconds), "-map", "0:v:0", "-map", "1:a:0",
            "-c", "copy", "-sn", "-dn", "-f", "segment", "-segment_time", "7200",
            "-segment_format", "matroska", "-reset_timestamps", "1", str(output_pattern),
        ]
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode:
            self.logger.error("FFmpeg backfill mux failed: %s", result.stderr[-4000:])
            raise RuntimeError("FFmpeg could not finalize the backfill")
        files = sorted(recording_dir.glob(output_pattern.name.replace("%03d", "*")))
        if not files:
            raise RuntimeError("Backfill produced no MKV files")
        for path in files:
            verify = subprocess.run(
                [str(self.ffmpeg), "-hide_banner", "-v", "error", "-i", str(path),
                 "-t", "10", "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "NUL"],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if verify.returncode:
                raise RuntimeError(f"A/V verification failed for {path.name}")
        self.output_files = [str(path) for path in files]
        # Only our verified temporary download files are removed.
        for path in STAGING_DIR.glob(f"{self.staging_base}.*"):
            if path.is_file():
                path.unlink()

    def run(self) -> int:
        try:
            STOP_PATH.unlink(missing_ok=True)
            self.read_metadata()
            self.write_status()
            self.download()
            self.mux_and_split()
            self.state = "completed"
            self.message = "Elapsed LIVE recovery completed and verified"
            self.current_fragment = self.target_fragment
            self.write_status()
            self.logger.info("Backfill completed: %s", self.output_files)
            return 0
        except InterruptedError:
            return 130
        except Exception as exc:
            self.state = "error"
            self.message = str(exc)
            self.write_status()
            self.logger.exception("Backfill failed")
            return 1


def main() -> int:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    logger = setup_rotating_logger("youtube-backfill", "backfill.log", 10, 5)
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
        for key in ("Url", "RecordingDirectory", "BackfillCutoverKST"):
            if not config.get(key):
                raise ValueError(f"Missing configuration value: {key}")
        parse_kst(config["BackfillCutoverKST"])  # fail early on a bad timestamp
        YT_DLP = ytdlp_path()
        ffmpeg = find_ffmpeg()
        lock_handle = acquire_lock(LOCK_PATH)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    try:
        return Backfill(config, ffmpeg, YT_DLP, logger).run()
    finally:
        release_lock(lock_handle)


if __name__ == "__main__":
    raise SystemExit(main())
