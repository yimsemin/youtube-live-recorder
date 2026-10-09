# YouTube LIVE Recorder

English · [한국어](README.ko.md)

A Windows tool that records a public YouTube LIVE stream **as an ordinary viewer**
and stores it as continuous MKV parts. It adds failure monitoring, a local
fallback capture, DVR catch-up, continuity checks, and a local dashboard.

It does not modify your YouTube account, channel, streaming settings, or the API.
Reception uses `yt-dlp` only.

> **Note:** This tool accesses YouTube by automated means and saves stream content,
> which may conflict with the [YouTube Terms of Service](https://www.youtube.com/t/terms).
> Long continuous recording may trigger access limits or account restrictions, and
> YouTube's policies can change at any time. Use it at your own risk, ideally with a
> dedicated Google account, and respect the broadcaster's rights.

This repository contains **no** real stream URL, storage location, Healthchecks.io
ping URL, runtime state, logs, or recordings. Your own settings are made by
copying `config/*.example.json` and are excluded via `.gitignore`.

## Features

- **Receive-only pipeline** — `yt-dlp` (authenticated HLS receive) → pipe → FFmpeg
  segment muxer → MKV parts. No re-encoding (`-c copy`).
- **Auto-reconnect** — keeps the pipeline alive across stream/network drops;
  pauses safely by finalizing the current file when free disk runs low.
- **Continuity tracking** — records HLS media-sequence boundaries on every
  reconnect and warns about gaps; `verify_continuity.py` checks against measured
  media duration.
- **Local fallback capture** — if the primary recording drive disappears, a local
  spool capture starts; after the drive returns and the primary file is growing
  again, the fallback is verified (A/V scan + SHA-256) and parked for
  reconciliation. A healthy primary recorder is never stopped.
- **DVR backfill** — recovers the elapsed DVR portion missed before the recorder
  started.
- **Local dashboard** — loopback-only (`127.0.0.1`); a single Python server
  serves one self-contained HTML page, no Node or build step.
- **Remote notification (optional)** — one-minute status pings to Healthchecks.io.

## Requirements

- Windows 10 / 11
- Download and place these yourself (all are `.gitignore`d, so deleting this folder removes them):
  - `recorder\node\node.exe` — portable Node.js (zip) from <https://nodejs.org/en/download>; used by yt-dlp
    to solve its JS challenge
  - `recorder\yt-dlp.exe` — <https://github.com/yt-dlp/yt-dlp/releases>
  - Python + FFmpeg — either an embedded Python in `recorder\python\`, **or** the
    Streamlink Windows portable build in `recorder\streamlink-*-x86_64\` (its
    bundled `Python\` and `ffmpeg\` are reused). Portable build:
    <https://github.com/streamlink/windows-builds/releases>
  - Firefox — for the dedicated login profile

> Timestamps in this project are fixed to **KST (UTC+9)**.

## Quick start

```powershell
Copy-Item .\config\recorder.example.json    .\config\recorder.json
Copy-Item .\config\failover.example.json    .\config\failover.json
Copy-Item .\config\healthcheck.example.json .\config\healthcheck.json
# Fill in Url and RecordingDirectory in recorder.json (see OPERATIONS.md for all keys)
.\login-youtube.bat          # one-time YouTube (Google) login
.\control-panel.bat          # status / start / stop / dashboard / login
```

Full setup and operations guide (Korean): [OPERATIONS.md](OPERATIONS.md).

## Layout

- `recorder/` — Python supervisor (`recorder.py`), failover guardian
  (`failover_guardian.py`), DVR backfill (`backfill.py`), HLS sequence recovery
  (`recover_hls_sequence.py`), continuity check (`verify_continuity.py`), staging
  reconciliation (`reconcile_staging.py`), login helper (`auth_login.py`),
  process management (`manage.py`), dashboard server (`dashboard_server.py` +
  `dashboard.html`), shared module (`common.py`)
- Root `*.bat` / `*.ps1` — Windows run/status/stop scripts; `control-panel.bat`
  is the entry point
- `config/*.example.json` — configuration templates

## Before committing

Confirm no real URL, storage path, organization name, or ping token is in a
tracked file:

```bash
git grep -nI -E "hc-ping|youtube\.com/live/|googlevideo"
```

`config/recorder.json`, `config/healthcheck.json`, `config/failover.json`,
`auth/`, `logs/`, `recordings/`, and `staging/` are never committed.

## License

[MIT](LICENSE)
