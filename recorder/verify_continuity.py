#!/usr/bin/env python3
"""Read-only continuity check for a folder of ``YouTube_*.mkv`` recordings.

Groups parts by reconnect attempt, measures each file's real media duration
with FFmpeg, and reports the gap (if any) between one attempt's end and the
next attempt's start. Nothing is moved or written.

    python verify_continuity.py [DIRECTORY]

DIRECTORY defaults to RecordingDirectory from config/recorder.json.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))

import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from common import CONFIG_DIR, KST, CREATE_NO_WINDOW, find_ffmpeg, read_json_or_empty

NAME_RE = re.compile(r"^YouTube_(\d{8}_\d{6})_KST_part_(\d{3})\.mkv$")
DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)")


def probe(ffmpeg: Path, path: Path) -> dict:
    out = subprocess.run([str(ffmpeg), "-hide_banner", "-i", str(path)],
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace", creationflags=CREATE_NO_WINDOW, check=False).stderr
    seconds = 0.0
    match = DURATION_RE.search(out)
    if match:
        h, m, s = match.groups()
        seconds = int(h) * 3600 + int(m) * 60 + float(s)
    return {"seconds": seconds, "video": "Video:" in out, "audio": "Audio:" in out}


def attempt_start(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y%m%d_%H%M%S").replace(tzinfo=KST)


def hms(seconds: float) -> str:
    return str(timedelta(seconds=round(seconds)))


def main() -> int:
    if len(sys.argv) > 1:
        directory = Path(sys.argv[1])
    else:
        directory = Path(read_json_or_empty(CONFIG_DIR / "recorder.json").get("RecordingDirectory", ""))
    if not directory.is_dir():
        print(f"디렉터리를 찾을 수 없습니다: {directory}", file=sys.stderr)
        return 2

    ffmpeg = find_ffmpeg()
    attempts: dict[str, list[Path]] = {}
    for path in sorted(directory.glob("YouTube_*.mkv")):
        match = NAME_RE.match(path.name)
        if match:
            attempts.setdefault(match.group(1), []).append(path)

    if not attempts:
        print(f"{directory} 에 표준 이름의 MKV가 없습니다.")
        return 1

    print(f"== 연속성 점검: {directory} ==\n")
    summary = []
    prev_end: datetime | None = None
    prev_label = ""
    total = 0.0
    for stamp in sorted(attempts):
        start = attempt_start(stamp)
        parts = sorted(attempts[stamp])
        span = 0.0
        print(f"[{start:%Y-%m-%d %H:%M:%S} KST]  ({len(parts)} parts)")
        for path in parts:
            info = probe(ffmpeg, path)
            span += info["seconds"]
            flags = "".join(("" if info["video"] else " 영상없음", "" if info["audio"] else " 음성없음"))
            print(f"   {path.name}  {hms(info['seconds'])}{flags}")
        end = start + timedelta(seconds=span)
        total += span
        print(f"   -> 합계 {hms(span)},  구간 끝 {end:%Y-%m-%d %H:%M:%S}")
        if prev_end is not None:
            gap = (start - prev_end).total_seconds()
            verdict = "OK(연속)" if abs(gap) <= 3 else f"공백 {hms(gap)}" if gap > 0 else f"겹침 {hms(-gap)}"
            print(f"   이전 구간과의 경계: {verdict}")
            summary.append((prev_label, stamp, gap))
        print()
        prev_end, prev_label = end, stamp

    print("== 요약 ==")
    print(f"총 녹화 길이 합계: {hms(total)}")
    gaps = [(a, b, g) for a, b, g in summary if g > 3]
    if gaps:
        print("공백 구간:")
        for a, b, g in gaps:
            print(f"  {a} -> {b} : {hms(g)} 누락 (backfill / recover_hls_sequence 대상)")
    else:
        print("경계 공백 없음.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
