from __future__ import annotations

import ctypes
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DISABLED_PATH = ROOT / "config" / "failover.disabled"
STATUS_PATH = ROOT / "config" / "failover-status.json"
GUARDIAN_PATH = ROOT / "recorder" / "failover_guardian.py"


def process_alive(value: object) -> bool:
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


def find_pythonw() -> Path:
    direct = ROOT / "recorder" / "python" / "pythonw.exe"
    if direct.is_file():
        return direct
    for bundle in sorted((ROOT / "recorder").glob("streamlink-*-x86_64"), reverse=True):
        candidate = bundle / "Python" / "pythonw.exe"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("Portable pythonw.exe was not found")


def main() -> int:
    if DISABLED_PATH.exists():
        return 0

    if STATUS_PATH.is_file():
        try:
            status = json.loads(STATUS_PATH.read_text(encoding="utf-8-sig"))
            if process_alive(status.get("GuardianPid")):
                return 0
        except (OSError, ValueError, TypeError):
            pass

    subprocess.Popen(
        [str(find_pythonw()), str(GUARDIAN_PATH)],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
        close_fds=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
