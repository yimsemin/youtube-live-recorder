# YouTube LIVE Recorder

[English](README.md) · 한국어

Windows에서 공개 YouTube LIVE를 **일반 시청자처럼 수신**해 MKV 조각으로 연속 저장하는 도구입니다.
장애 감시·로컬 예비 녹화·DVR 구간 회수·연속성 점검·로컬 대시보드를 포함합니다.
YouTube 채널·송출 설정·API는 변경하지 않으며, 수신은 `yt-dlp` 하나만 사용합니다.

이 저장소에는 실제 스트림 URL·저장 위치·Healthchecks.io ping URL·실행 상태·로그·녹화 파일이
포함되지 않습니다. 개인 설정은 `config/*.example.json` 을 복사해 만들며 `.gitignore` 로 제외됩니다.

## 주요 기능

- **수신 전용 파이프라인**: `yt-dlp`(인증 HLS 수신) → 파이프 → FFmpeg segment muxer → MKV 조각. 재인코딩 없음(`-c copy`).
- **자동 재접속**: 스트림·네트워크 끊김에도 파이프라인을 살려 두고, 디스크 여유가 부족하면 안전하게 현재 파일을 마감하고 대기.
- **연속성 기록·점검**: 재접속마다 HLS media-sequence 경계를 기록하고 공백을 경고. `verify_continuity.py` 로 실측 길이 기반 점검.
- **로컬 예비 녹화**: 주 저장 드라이브가 사라지면 로컬 스풀에 예비 캡처를 시작하고, 드라이브 복귀 후 메인이 다시 커지는 것을 확인하면 검증(A/V + SHA-256) 후 정리. 정상 메인 녹화기는 절대 중단하지 않음.
- **DVR 백필**: 방송 시작 이후 놓친 과거 구간을 DVR에서 회수.
- **로컬 대시보드**: `127.0.0.1` loopback 전용, Node/빌드 없이 Python 서버가 단일 HTML 제공.
- **원격 알림(선택)**: Healthchecks.io 로 1분 간격 상태 ping.

## 요구 사항

- Windows 10 / 11
- **Node.js** (`C:\Program Files\nodejs\node.exe`) — yt-dlp 의 JS 챌린지 해결용
- 다음을 직접 내려받아 배치 (모두 `.gitignore` 대상):
  - `recorder\yt-dlp.exe` — <https://github.com/yt-dlp/yt-dlp/releases>
  - Python + FFmpeg — `recorder\python\` (임베디드 Python) **또는** Streamlink Windows 포터블 배포본
    `recorder\streamlink-*-x86_64\` (그 안의 `Python\` 과 `ffmpeg\` 를 재사용).
    Streamlink 포터블: <https://github.com/streamlink/windows-builds/releases>
  - Firefox — 로그인 전용 프로필 생성용

> 이 프로젝트는 시각 표기가 **KST(UTC+9)** 로 고정되어 있습니다.

## 빠른 시작

```powershell
Copy-Item .\config\recorder.example.json    .\config\recorder.json
Copy-Item .\config\failover.example.json    .\config\failover.json
Copy-Item .\config\healthcheck.example.json .\config\healthcheck.json
# recorder.json 의 Url, RecordingDirectory 입력 (자세한 키는 OPERATIONS.md)
.\login-youtube.bat          # YouTube(Google) 로그인 1회
.\control-panel.bat          # 상태 / 시작 / 종료 / 대시보드 / 로그인
```

설치·운영 방법 전체는 [OPERATIONS.md](OPERATIONS.md) 참고.

## 구성

- `recorder/` — Python 녹화 감독기(`recorder.py`), 장애 감시기(`failover_guardian.py`),
  DVR 백필(`backfill.py`), HLS 시퀀스 복구(`recover_hls_sequence.py`),
  연속성 점검(`verify_continuity.py`), staging 정리(`reconcile_staging.py`),
  로그인 도우미(`auth_login.py`), 프로세스 관리(`manage.py`),
  대시보드 서버(`dashboard_server.py` + `dashboard.html`), 공용 모듈(`common.py`)
- 루트 `*.bat` / `*.ps1` — Windows 실행·상태·종료 스크립트. `control-panel.bat` 이 진입점
- `config/*.example.json` — 설정 템플릿

## 공개 전 점검

커밋 전에 실제 URL·저장 경로·기관명·ping 토큰이 추적 파일에 없는지 확인합니다.

```bash
git grep -nI -E "hc-ping|youtube\.com/live/|googlevideo"
```

`config/recorder.json`, `config/healthcheck.json`, `config/failover.json`, `auth/`, `logs/`,
`recordings/`, `staging/` 는 커밋하지 않습니다.

## 라이선스

[MIT](LICENSE)
