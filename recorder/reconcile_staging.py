#!/usr/bin/env python3
"""Inventory recovery/staging media and propose where each file belongs.

Read-only by default: prints an inventory (real A/V + duration + any time range
parsed from the filename) and a proposed disposition against the canonical
recording root. Use the printed plan to move files by hand, or pass
``--backup-dupes`` to move only exact-size duplicates of an existing root file
into ``_중복백업\\<date>_reconcile``.

    python reconcile_staging.py STAGING_DIR [STAGING_DIR ...] [--root ROOT] [--backup-dupes]
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))

import argparse
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from common import CONFIG_DIR, KST, CREATE_NO_WINDOW, find_ffmpeg, kst_text, read_json_or_empty

MEDIA_SUFFIXES = {".mkv", ".ts", ".mp4", ".m4a", ".m4v", ".webm"}
DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)")
SEQ_RE = re.compile(r"seq[_-]?(\d+)[_-](\d+)", re.IGNORECASE)
RANGE_RE = re.compile(r"(\d{8}[_-]\d{6}).*?(?:to|_)(\d{8}[_-]\d{6})")
ROOT_NAME_RE = re.compile(r"^YouTube_(\d{8}_\d{6})_KST_part_\d{3}\.mkv$")


def probe(ffmpeg: Path, path: Path) -> dict:
    out = subprocess.run([str(ffmpeg), "-hide_banner", "-i", str(path)],
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace", creationflags=CREATE_NO_WINDOW, check=False).stderr
    seconds = 0.0
    m = DURATION_RE.search(out)
    if m:
        h, mi, s = m.groups()
        seconds = int(h) * 3600 + int(mi) * 60 + float(s)
    return {"seconds": seconds, "video": "Video:" in out, "audio": "Audio:" in out}


def parse_hint(name: str) -> str:
    seq = SEQ_RE.search(name)
    if seq:
        return f"HLS seq {seq.group(1)}..{seq.group(2)}"
    rng = RANGE_RE.search(name)
    if rng:
        return f"range {rng.group(1)} .. {rng.group(2)}"
    return ""


def root_sizes(root: Path) -> dict[int, str]:
    sizes: dict[int, str] = {}
    for path in root.glob("YouTube_*.mkv"):
        if ROOT_NAME_RE.match(path.name):
            try:
                sizes[path.stat().st_size] = path.name
            except OSError:
                continue
    return sizes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("staging", nargs="+", type=Path)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--backup-dupes", action="store_true",
                        help="move exact-size duplicates of a root file into _중복백업")
    args = parser.parse_args()

    root = args.root or Path(read_json_or_empty(CONFIG_DIR / "recorder.json").get("RecordingDirectory", ""))
    if not root.is_dir():
        print(f"녹화 루트를 찾을 수 없습니다: {root}", file=sys.stderr)
        return 2

    ffmpeg = find_ffmpeg()
    known = root_sizes(root)
    backup_dir = root / "_중복백업" / f"{datetime.now(KST):%Y%m%d}_reconcile"
    moved = 0

    print(f"== staging 정리 분석 ==\n루트: {root}\n루트 표준 파일 {len(known)}개\n")
    for base in args.staging:
        if not base.is_dir():
            print(f"(건너뜀, 폴더 아님) {base}")
            continue
        print(f"--- {base} ---")
        for path in sorted(base.rglob("*")):
            if path.suffix.lower() not in MEDIA_SUFFIXES or not path.is_file():
                continue
            size = path.stat().st_size
            info = probe(ffmpeg, path)
            hint = parse_hint(path.name)
            av = ("V" if info["video"] else "-") + ("A" if info["audio"] else "-")
            dur = f"{info['seconds'] / 3600:.2f}h"
            rel = path.relative_to(base)
            if size in known:
                disp = f"중복(루트 {known[size]}와 크기 동일)"
                if args.backup_dupes:
                    backup_dir.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(path), str(backup_dir / path.name))
                    disp += " -> _중복백업 이동"
                    moved += 1
            elif not info["video"] or not info["audio"]:
                disp = "보류(영상/음성 불완전 — 병합 필요)"
            elif info["seconds"] < 5:
                disp = "보류(너무 짧음)"
            else:
                disp = "검토(정본 후보 — 경계 확인 필요)"
            print(f"  {rel}  [{av} {dur}] {hint}  => {disp}")
        print()

    if args.backup_dupes:
        print(f"_중복백업으로 이동한 파일: {moved}개 ({backup_dir})")
    else:
        print("이동 없음(분석만). 실제 배치는 verify_continuity.py 결과와 대조해 수동으로 진행하세요.")
    print(kst_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
