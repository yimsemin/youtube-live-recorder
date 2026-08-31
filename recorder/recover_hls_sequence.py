#!/usr/bin/env python3
"""Download an exact, inclusive YouTube HLS sequence range in order.

This is intentionally separate from the live recorder.  It never signals or
modifies the recorder processes and writes only to the requested output path.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import http.cookiejar
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent.parent


def find_tools() -> tuple[Path, Path]:
    ytdlp = ROOT / "recorder" / "yt-dlp.exe"
    ffmpeg_candidates = sorted((ROOT / "recorder").glob("streamlink-*-x86_64/ffmpeg/ffmpeg.exe"), reverse=True)
    if not ytdlp.is_file() or not ffmpeg_candidates:
        raise FileNotFoundError("yt-dlp or FFmpeg was not found")
    return ytdlp, ffmpeg_candidates[0]


def load_cookie_header(path: Path) -> str:
    jar = http.cookiejar.MozillaCookieJar(str(path))
    jar.load(ignore_discard=True, ignore_expires=True)
    return "; ".join(f"{cookie.name}={cookie.value}" for cookie in jar)


def fetch(url: str, cookie_header: str, timeout: int = 30) -> bytes:
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.5 Safari/605.1.15",
        "Origin": "https://www.youtube.com",
        "Referer": "https://www.youtube.com/",
        "Cookie": cookie_header,
    }
    request = Request(url, headers=headers)
    with urlopen(request, timeout=timeout) as response:
        return response.read()


def manifest_template(video_url: str, cookies_file: Path, js_runtime: str, extractor_args: str, format_id: str) -> tuple[str, int, str, str]:
    ytdlp, ffmpeg = find_tools()
    result = subprocess.run(
        [
            str(ytdlp), "--ffmpeg-location", str(ffmpeg),
            "--cookies", str(cookies_file), "--js-runtimes", js_runtime,
            "--extractor-args", extractor_args, "-f", format_id,
            "--get-url", video_url,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    manifest_url = result.stdout.strip()
    if not manifest_url:
        raise RuntimeError("yt-dlp returned no media manifest URL (check login/cookies)")
    cookie_header = load_cookie_header(cookies_file)
    lines = fetch(manifest_url, cookie_header).decode("utf-8").splitlines()
    program_time = next((line.split(":", 1)[1] for line in lines
                         if line.startswith("#EXT-X-PROGRAM-DATE-TIME:")), "")
    segment_url = next((line for line in lines if line and not line.startswith("#")), "")
    match = re.search(r"/sq/(\d+)/", segment_url)
    if not match:
        raise RuntimeError("The HLS media playlist has no numbered segment URL")
    return segment_url, int(match.group(1)), program_time, cookie_header


def sequence_url(template: str, sequence: int) -> str:
    return re.sub(r"/sq/\d+/", f"/sq/{sequence}/", template, count=1)


def download_one(template: str, sequence: int, retries: int, cookie_header: str) -> bytes:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            data = fetch(sequence_url(template, sequence), cookie_header, timeout=45)
            if len(data) < 188 or data[0] != 0x47:
                raise RuntimeError(f"sequence {sequence} is not a valid MPEG-TS segment")
            return data
        except Exception as exc:  # network failures are retried with backoff
            last_error = exc
            time.sleep(min(10, 1 + attempt * 2))
    raise RuntimeError(f"sequence {sequence} failed after {retries} attempts: {last_error}")


def write_progress(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--cookies-file", type=Path, required=True)
    parser.add_argument("--js-runtime", default=r"node:C:\Program Files\nodejs\node.exe")
    parser.add_argument("--extractor-args", default="youtube:player_client=default,web_safari;player_js_version=actual")
    parser.add_argument("--format-id", default="300")
    parser.add_argument("--start-sequence", type=int, required=True)
    parser.add_argument("--end-sequence", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--retries", type=int, default=6)
    args = parser.parse_args()

    if args.start_sequence < 0 or args.end_sequence < args.start_sequence:
        parser.error("invalid sequence range")
    if args.output.exists():
        parser.error(f"output already exists; refusing to overwrite: {args.output}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    partial = args.output.with_suffix(args.output.suffix + ".part")
    if partial.exists():
        # A previous run left a partial file. Retire it (do not silently resume
        # from an unknown offset) so this run starts clean.
        retired = partial.with_suffix(partial.suffix + f".failed-{int(time.time())}")
        partial.rename(retired)
        print(f"이전 미완성 파일을 {retired.name} 로 옮기고 새로 시작합니다.", file=sys.stderr)
    progress = args.output.with_suffix(args.output.suffix + ".progress.json")
    template, anchor_sequence, anchor_time, cookie_header = manifest_template(
        args.url, args.cookies_file, args.js_runtime, args.extractor_args, args.format_id
    )
    total = args.end_sequence - args.start_sequence + 1
    completed = 0
    started = datetime.now().astimezone().isoformat()

    with partial.open("xb") as destination, ThreadPoolExecutor(max_workers=args.workers) as pool:
        for batch_start in range(args.start_sequence, args.end_sequence + 1, args.batch_size):
            batch_end = min(args.end_sequence, batch_start + args.batch_size - 1)
            futures = {
                pool.submit(download_one, template, sequence, args.retries, cookie_header): sequence
                for sequence in range(batch_start, batch_end + 1)
            }
            segments: dict[int, bytes] = {}
            for future in as_completed(futures):
                sequence = futures[future]
                segments[sequence] = future.result()
            for sequence in range(batch_start, batch_end + 1):
                destination.write(segments[sequence])
                completed += 1
            destination.flush()
            write_progress(
                progress,
                {
                    "State": "downloading",
                    "Started": started,
                    "Updated": datetime.now().astimezone().isoformat(),
                    "AnchorSequence": anchor_sequence,
                    "AnchorProgramDateTime": anchor_time,
                    "StartSequence": args.start_sequence,
                    "EndSequence": args.end_sequence,
                    "CompletedSegments": completed,
                    "TotalSegments": total,
                    "ProgressPercent": round(completed / total * 100, 2),
                    "PartialBytes": destination.tell(),
                },
            )

    partial.replace(args.output)
    write_progress(
        progress,
        {
            "State": "completed",
            "Started": started,
            "Updated": datetime.now().astimezone().isoformat(),
            "AnchorSequence": anchor_sequence,
            "AnchorProgramDateTime": anchor_time,
            "StartSequence": args.start_sequence,
            "EndSequence": args.end_sequence,
            "CompletedSegments": total,
            "TotalSegments": total,
            "ProgressPercent": 100.0,
            "Output": str(args.output),
            "OutputBytes": args.output.stat().st_size,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
