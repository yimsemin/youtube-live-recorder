#!/usr/bin/env python3
"""Open a dedicated Firefox profile for a one-time YouTube (Google) login.

The recorder reads live cookies straight from this profile with
``yt-dlp --cookies-from-browser firefox:<profile>``. Nothing is exported by
hand; re-run this tool whenever YouTube starts asking to "confirm you're not a
bot" and just sign in again.

The login check does NOT need a live stream: it inspects the profile's YouTube
auth cookies and confirms yt-dlp can use them on an ordinary public video.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import winreg
from pathlib import Path

from common import (
    CONFIG_DIR, ROOT, CREATE_NO_WINDOW,
    atomic_json_write, kst_text, read_json_or_empty, ytdlp_path,
)

RECORDER_CONFIG_PATH = CONFIG_DIR / "recorder.json"
AUTH_STATUS_PATH = CONFIG_DIR / "auth-status.json"
DEFAULT_PROFILE_DIR = ROOT / "auth" / "firefox-profile"
LOGIN_URL = "https://accounts.google.com/ServiceLogin?continue=https%3A%2F%2Fwww.youtube.com%2F"
# Public, always-available video used only to confirm the cookie pipeline works.
PROBE_VIDEO = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
YOUTUBE_LOGIN_COOKIES = {"LOGIN_INFO", "SID", "__Secure-3PSID", "__Secure-1PSID", "SAPISID"}


def find_firefox() -> Path | None:
    for base in (r"C:\Program Files\Mozilla Firefox\firefox.exe",
                 r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe"):
        if Path(base).is_file():
            return Path(base)
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, r"SOFTWARE\Mozilla\Mozilla Firefox") as key:
                version = winreg.QueryValueEx(key, "CurrentVersion")[0]
            with winreg.OpenKey(hive, rf"SOFTWARE\Mozilla\Mozilla Firefox\{version}\Main") as key:
                path = Path(winreg.QueryValueEx(key, "PathToExe")[0])
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def write_status(state: str, detail: str, profile: Path, cookies: list[str]) -> None:
    atomic_json_write(AUTH_STATUS_PATH, {
        "State": state,               # ok | login_required | unknown
        "Detail": detail,
        "ProfileDir": str(profile),
        "YouTubeLoginCookies": cookies,
        "CheckedKST": kst_text(),
    })


def youtube_login_cookies(profile: Path) -> list[str]:
    """Names of YouTube auth cookies found in the profile (Firefox stays openable)."""
    db = profile / "cookies.sqlite"
    if not db.is_file():
        return []
    tmp = Path(tempfile.gettempdir()) / f"ytr_cookies_{os.getpid()}.sqlite"
    try:
        shutil.copy(db, tmp)
        con = sqlite3.connect(f"file:{tmp}?immutable=1", uri=True)
        try:
            rows = con.execute(
                "SELECT name FROM moz_cookies WHERE host LIKE '%youtube.com'"
            ).fetchall()
        finally:
            con.close()
        return sorted({n for (n,) in rows} & YOUTUBE_LOGIN_COOKIES)
    except (OSError, sqlite3.Error):
        return []
    finally:
        tmp.unlink(missing_ok=True)


def ytdlp_can_use_cookies(profile: Path, config: dict) -> tuple[bool, str]:
    """Run yt-dlp on an ordinary public video with the SAME options the recorder uses."""
    args = [str(ytdlp_path()), "--ignore-config", "--quiet", "--no-warnings",
            "--cookies-from-browser", f"firefox:{profile}"]
    if str(config.get("YtDlpJsRuntime", "")).strip():
        args += ["--js-runtimes", str(config["YtDlpJsRuntime"])]
    if str(config.get("YtDlpExtractorArgs", "")).strip():
        args += ["--extractor-args", str(config["YtDlpExtractorArgs"])]
    args += ["--simulate", "--skip-download", "-O", "%(id)s", PROBE_VIDEO]
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=150, creationflags=CREATE_NO_WINDOW, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"yt-dlp 실행 실패: {exc}"
    if result.returncode == 0:
        return True, ""
    return False, (result.stderr or "").strip()[-400:]


def evaluate(profile: Path, config: dict) -> tuple[str, str, list[str]]:
    cookies = youtube_login_cookies(profile)
    ok, detail = ytdlp_can_use_cookies(profile, config)
    if cookies and ok:
        return "ok", "YouTube 로그인 쿠키가 있고 yt-dlp가 정상적으로 사용합니다.", cookies
    if cookies and not ok:
        low = detail.lower()
        if "sign in" in low or "confirm you" in low or "cookies" in low:
            return "login_required", f"쿠키는 있으나 yt-dlp가 거부당했습니다: {detail}", cookies
        return "unknown", f"쿠키는 있으나 확인 영상 처리 실패(네트워크/일시적일 수 있음): {detail}", cookies
    if not cookies and ok:
        return "unknown", "yt-dlp는 동작하지만 로그인 쿠키를 찾지 못했습니다. Firefox에서 YouTube 로그인을 확인하세요.", cookies
    return "login_required", "YouTube 로그인 쿠키가 없습니다. Firefox 창에서 Google 계정으로 로그인하세요.", cookies


def main() -> int:
    config = read_json_or_empty(RECORDER_CONFIG_PATH)
    profile = Path(str(config.get("AuthProfileDir") or DEFAULT_PROFILE_DIR))
    profile.mkdir(parents=True, exist_ok=True)

    firefox = find_firefox()
    if firefox is None:
        print("Firefox를 찾을 수 없습니다. https://www.mozilla.org/firefox/ 설치 후 다시 실행하세요.")
        write_status("unknown", "Firefox 미설치", profile, [])
        return 2

    print("=" * 66)
    print(" YouTube 로그인 전용 Firefox 창을 엽니다.")
    print(f"  프로필: {profile}")
    print(" 1) 열리는 창에서 Google 계정으로 로그인하세요.")
    print(" 2) youtube.com 이 정상으로 보이면 로그인 완료입니다.")
    print(" 3) 이 검정 창으로 돌아와 Enter 를 누르세요. (Firefox 는 열어두어도 됩니다)")
    print("=" * 66)

    subprocess.Popen([str(firefox), "-profile", str(profile), "-no-remote",
                      "-new-instance", LOGIN_URL])
    try:
        input("\n로그인을 마쳤으면 Enter: ")
    except (EOFError, KeyboardInterrupt):
        pass
    time.sleep(1)

    if not config.get("AuthProfileDir"):
        config["AuthProfileDir"] = str(profile)
        try:
            atomic_json_write(RECORDER_CONFIG_PATH, config)
            print(f"recorder.json 의 AuthProfileDir 을 {profile} 로 설정했습니다.")
        except OSError as exc:
            print(f"recorder.json 갱신 실패(수동 설정 필요): {exc}")

    state, detail, cookies = evaluate(profile, config)
    write_status(state, detail, profile, cookies)
    label = {"ok": "정상", "login_required": "로그인 필요", "unknown": "확인 불가"}.get(state, state)
    print(f"\n인증 상태: {label}")
    print(detail)
    if cookies:
        print(f"확인된 로그인 쿠키: {', '.join(cookies)}")
    print("\n(스트리밍이 아직 시작 전이어도 위 상태가 '정상'이면 녹화 준비 완료입니다.)")
    return 0 if state == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
