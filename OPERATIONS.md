# YouTube LIVE 수신 녹화기 — 운영 안내

Windows에서 공개 YouTube LIVE를 일반 시청자처럼 수신해 MKV로 저장합니다.
YouTube 채널·송출 설정·API는 전혀 건드리지 않습니다. 수신 방식은 **yt-dlp** 하나입니다.

설치 위치는 자유입니다. 이 문서에서는 저장소를 받은 폴더를 `<ROOT>` 로 적습니다
(예: `%USERPROFILE%\Documents\YouTubeRecorder`). 경로에 한글·공백이 있어도 됩니다.

---

## 0. 요구 사항 / 준비물

이 저장소에는 코드와 예제 설정만 있습니다. 아래 실행 파일은 직접 내려받아 배치합니다
(모두 `.gitignore` 대상이라 커밋되지 않습니다).

| 준비물 | 위치 | 출처 |
|---|---|---|
| `yt-dlp.exe` | `<ROOT>\recorder\yt-dlp.exe` | <https://github.com/yt-dlp/yt-dlp/releases> |
| Python + FFmpeg | `<ROOT>\recorder\python\` (임베디드 Python) **또는** `<ROOT>\recorder\streamlink-*-x86_64\` | 아래 설명 |
| Node.js | `C:\Program Files\nodejs\node.exe` | <https://nodejs.org> |
| Firefox | 시스템 기본 설치 | <https://www.mozilla.org/firefox/> |

**Python + FFmpeg 배치 방법 (둘 중 하나):**

- `recorder\python\` 에 임베디드 Python(`python.exe`, `pythonw.exe`)을 두고, `ffmpeg.exe` 를 PATH 또는 같은 폴더에 둡니다.
- 또는 **Streamlink Windows 포터블 배포본**(<https://github.com/streamlink/windows-builds/releases>)의
  `streamlink-*-x86_64` 폴더를 통째로 `recorder\` 아래에 둡니다. 이 프로젝트는 Streamlink 자체를 쓰지 않고,
  그 배포본에 **함께 들어있는 `Python\` 과 `ffmpeg\` 만 재사용**합니다. `_resolve-python.bat` 이 자동으로 찾습니다.

- Node.js 는 yt-dlp 의 JavaScript 챌린지 해결에 필요합니다. 경로가 다르면
  `config\recorder.json` 의 `YtDlpJsRuntime` 을 맞춰 줍니다.

시각 표기는 **KST(UTC+9)** 로 고정입니다.

---

## 1. 처음 한 번 준비

1. `config` 예제를 복사해 개인 설정을 만듭니다.

   ```powershell
   Copy-Item .\config\recorder.example.json    .\config\recorder.json
   Copy-Item .\config\failover.example.json    .\config\failover.json
   Copy-Item .\config\healthcheck.example.json .\config\healthcheck.json
   ```

2. `config\recorder.json` 에 값을 채웁니다.

   | 키 | 설명 |
   |---|---|
   | `Url` | YouTube LIVE 주소. 비우면 첫 실행 때 물어봅니다 |
   | `RecordingDirectory` | 최종 저장 폴더(절대 경로). **방송 1개 = 폴더 1개** |
   | `AuthProfileDir` | 로그인 전용 Firefox 프로필 폴더. `login-youtube.bat` 이 자동으로 채웁니다 |
   | `YtDlpCookiesFile` | (선택) 내보낸 쿠키 파일. `AuthProfileDir` 이 우선입니다 |
   | `SegmentHours` | 파일 분할 주기(기본 2시간) |
   | `MinimumFreeSpaceGB` | 이 값 이하로 떨어지면 현재 파일을 마감하고 대기 |

3. 실행 파일을 배치합니다 — 자세한 내용은 위 **0절** 참고
   (`recorder\yt-dlp.exe`, `recorder\python\` 또는 `recorder\streamlink-*-x86_64\`, Node.js).

4. `config\healthcheck.json` — 원격 알림을 쓸 때만 `PingUrl` 에 Healthchecks.io 주소 입력.

5. `login-youtube.bat` 를 한 번 실행해 YouTube(Google) 로그인. (아래 3절)

---

## 2. 매일 쓰는 방법 — `control-panel.bat`

`control-panel.bat` 더블클릭. 번호 메뉴:

| 번호 | 동작 | 설명 |
|---|---|---|
| 1 | 상태 보기 | 모든 프로세스(감독/yt-dlp/FFmpeg/감시기/대시보드) 생존 여부, 로그인 상태, 예약 작업 |
| 2 | 녹화 시작 | 감독 + 장애 감시기 + 예약 감시 작업을 함께 켭니다 |
| 3 | 녹화 안전 종료 | 현재 MKV를 정상 마감하고 종료 |
| 4 | 전체 종료 | 녹화기·감시기·대시보드를 모두 안전 정리 |
| 5 | 대시보드 열기 | 브라우저 제어판 |
| 6 | YouTube 로그인 | 로그인 창 |

명령줄에서 URL을 바로 줄 수도 있습니다:

```bat
start-recorder.bat "https://www.youtube.com/live/XXXXXXXX"
```

---

## 3. YouTube 로그인 (봇 확인 대응)

장시간 녹화 중 YouTube가 `Sign in to confirm you're not a bot` 으로 막을 때가 있습니다.

1. `login-youtube.bat` 실행 → 전용 Firefox 창이 열립니다.
2. Google 계정으로 로그인하고 `youtube.com` 이 정상으로 보이면 **그 창을 닫습니다**.
3. 자동으로 로그인 상태를 확인하고 `config\auth-status.json` 에 결과를 씁니다.

- 쿠키는 이 전용 프로필에 남고, 녹화기가 `yt-dlp --cookies-from-browser` 로 **매번 최신 쿠키를 직접 읽습니다**. 손으로 내보낼 필요가 없습니다.
- 다시 막히면 `login-youtube.bat` 을 다시 실행해 로그인만 하면 됩니다.
- 대시보드와 `상태 보기` 에 로그인 상태가 표시됩니다.

---

## 4. 웹 대시보드

`start-dashboard.bat` → `http://127.0.0.1:8787/` 자동 오픈. 이 PC의 loopback에서만 열립니다.

- 큰 상태 배지, 동작 버튼(시작/안전 종료/재시작/전체 종료/로그인/폴더 열기)
- 시스템 프로세스 표, YouTube 로그인 상태
- 설정(URL·저장 위치·분할 시간·최소 여유공간·포맷) — 저장 후 재시작하면 적용
- 최근 파일, 연속성 로그, 녹화 로그

`stop-dashboard.bat` 은 대시보드만 종료하고 녹화기는 건드리지 않습니다.

Node/빌드가 필요 없습니다. Python 서버 하나가 HTML을 직접 제공합니다.

---

## 5. 프로세스가 안 꺼질 때

1. `control-panel.bat` → `1. 상태 보기` 로 무엇이 살아있는지 확인.
   - 표 맨 아래 "추적되지 않는 yt-dlp/FFmpeg" 항목이 있으면 그게 정리 대상입니다.
2. `control-panel.bat` → `4. 전체 종료` (내부적으로 `python recorder\manage.py stop-all`).
   - 감독에 안전 종료 요청 → 최대 45초 대기 → 감시기 정지 → 대시보드 정지 →
     남은 앱 소유 yt-dlp/FFmpeg 강제 정리 → 요약 출력.
3. 예약 감시 작업까지 끄려면: `python recorder\manage.py stop-all --include-tasks`

작업 관리자에서 창을 강제로 닫지 마세요. 마지막 MKV의 끝 인덱스가 없어질 수 있습니다.

---

## 6. 파일과 연속성 정책

- 최종 파일명: `YouTube_YYYYMMDD_HHMMSS_KST_part_NNN.mkv` (연결 시도 시각 + 분할 번호)
- **최종 폴더 루트의 MKV는 이름순으로 이어붙이면 전체 방송이 되어야 합니다.** 다른 접두사를 만들지 않습니다.
- 영상·음성은 `-c copy` 로 저장(재인코딩 없음).
- 재접속할 때마다 HLS 시퀀스 경계를 `logs\continuity.log` 에 기록하고, 공백이 생기면 경고합니다.
- 복구·변환 중간 파일은 `staging` 에서만 다루고, 검증(A/V·길이·경계·SHA-256)이 끝난 최종본만 루트에 둡니다.
- 대체된 중복 조각은 삭제하지 말고 루트 아래 `_중복백업\날짜_사유\` 로 옮깁니다.
- 최종 녹화 파일은 자동 삭제하지 않습니다.

### 점검·복구 도구

| 도구 | 용도 |
|---|---|
| `python recorder\verify_continuity.py [폴더]` | 폴더의 `YouTube_*.mkv` 연속성(실측 길이·경계 공백) 점검 |
| `start-backfill.bat` | `BackfillCutoverKST` 까지의 DVR 과거 구간 회수 |
| `python recorder\recover_hls_sequence.py --url ... --start-sequence N --end-sequence M --output OUT.ts` | 특정 HLS 시퀀스 구간만 회수 |
| `python recorder\reconcile_staging.py STAGING_DIR` | staging 자료를 분류하고 배치안 제시(읽기 전용) |

---

## 7. 저장소 장애 시 예비 녹화

`failover_guardian.py` 는 정상 메인 녹화기를 점검 목적으로 중단·재시작하지 않습니다.

- 주 저장소(`RecordingDirectory`)가 **실제로 사라지면** `failover.json` 의 `FallbackDirectory`(로컬)에 예비 녹화 시작.
- 주 저장소가 돌아오고 **메인 활성 파일이 실제로 커지는 것이 확인될 때까지** 예비 녹화를 유지.
- 예비 파일은 A/V 스캔 + SHA-256 검증 후 `*.validated.json` 과 함께 대기 목록에 둡니다. 경계가 확정되기 전에는 루트에 넣지 않습니다.
- 메인 감독이 죽어 있으면(저장소는 정상) 감시기가 `recorder.py` 를 다시 띄웁니다(`RestartCooldownSeconds` 간격).

`install-failover-task.ps1` 이 1분 간격 확인 예약 작업을 설치합니다. `-NoStart` 를 주면 비활성 상태로 설치합니다.

---

## 8. 원격 상태 알림 (Healthchecks.io)

`install-healthcheck-task.ps1` 이 1분 간격 점검 작업을 설치합니다. 이 작업은 녹화기를 제어하지 않습니다.

점검 항목: 감독/yt-dlp/FFmpeg 생존, 상태 파일 갱신, 활성 파일 크기 증가, 저장소 여유 공간,
(설정 시) 저장소 클라이언트 프로세스, 재접속이 길고 로그인 필요 상태이면 별도 경고.

- `status-healthcheck.bat` — 최근 실행·마지막 ping 결과
- `healthcheck-now.bat` — 즉시 1회 점검 + ping
- `test-healthcheck.bat` — 외부 ping 없이 로컬 점검만

Ping URL은 로그에 기록하지 않습니다. `config\healthcheck.json` 은 비공개로 유지하세요.

---

## 9. 새 방송으로 바꿀 때

1. `control-panel.bat` → `3. 안전 종료` (또는 이미 정지 상태면 생략).
2. `config\recorder.json` 의 `Url` 을 새 주소로, `RecordingDirectory` 를 **새 폴더**로 변경.
   (이전 방송 자료와 절대 섞지 않습니다. 폴더 1개 = 방송 1개.)
3. `login-youtube.bat` 으로 로그인 유효성 확인(짧게).
4. `control-panel.bat` → `2. 녹화 시작`.
5. `1. 상태 보기` 또는 대시보드에서 감독·yt-dlp·FFmpeg가 모두 살아있고
   최근 파일 크기가 계속 커지는지 확인. 짧은 샘플로 영상·음성 확인.
6. `status-healthcheck.bat` 가 OK로 바뀌는지 확인.

---

## 10. 폴더 구조

```
<ROOT>\
  recorder\        Python 코드, dashboard.html, yt-dlp.exe, python\ 또는 streamlink-* 배포본
  config\          *.json 설정(비공개), *.example.json(공개), 런타임 상태/락/요청 파일
  auth\            로그인 전용 Firefox 프로필(비공개)
  logs\            recorder.log, failover.log, healthcheck.log, dashboard.log, continuity.log
  recordings\      failover-spool\ 등 로컬 스풀(최종본은 RecordingDirectory 에 별도)
  staging\         복구·변환 중간 파일
  *.bat            control-panel / start-* / stop-* / status-* / login-youtube
```
