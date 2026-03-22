# FISHTANK Skimmer v5.1

**Multi-stream HLS capture tool for fishtank.live**

Captures live video streams concurrently via ffmpeg, with automatic hourly rotation, safe archival, session management, and self-healing health monitoring.

---

## Table of Contents

- [Requirements](#requirements)
- [Quick Start](#quick-start)
- [Menu Reference](#menu-reference)
- [Architecture](#architecture)
- [File Safety](#file-safety)
- [Health Monitor](#health-monitor)
- [Troubleshooting](#troubleshooting)

---

## Requirements

| Dependency | Notes |
|---|---|
| **Python 3.8+** | Windows only (uses `msvcrt` for non-blocking input) |
| **ffmpeg** | Must be in system `PATH` |
| **VLC** | Optional — for live playback (auto-detected or set in `PATH`) |
| **Browser cookies** | Exported from fishtank.live in Netscape format |

### Cookie Export

Use a browser extension like **"Get cookies.txt LOCALLY"** to export cookies while logged in to fishtank.live. Save as `fishtank.cookies.txt` in the same directory as the script.

---

## Quick Start

```bash
# 1. Place your exported cookie file alongside the script
cp ~/Downloads/fishtank.cookies.txt ./fishtank.cookies.txt

# 2. Run
python capture_streams.py
```

On startup, the script runs a verbose 5-step initialization:

1. **Cookie file** — checks existence, size, and modification date
2. **JWT tokens** — extracts and validates tokens, shows expiry times
3. **API config** — fetches stream list, host assignments, and online status
4. **Session probe** — verifies a token works against a live stream via ffmpeg
5. **Stale capture recovery** — checks `currently-capturing/` for leftover files, probes with ffprobe, remuxes if corrupt, archives or routes to `needs-repair/`

---

## Menu Reference

| Key | Action | Description |
|-----|--------|-------------|
| `1` | Start Archiver (All) | Captures all online streams concurrently |
| `2` | Start Selective | Pick specific cameras by number |
| `3` | Stop Archiver | Stops captures, archives all files |
| `4` | Video File Stats | Live dashboard with filename, size, growth; `R` toggles 5s auto-refresh |
| `5` | Launch VLC | Opens a live stream in VLC for real-time viewing |
| `6` | Toggle Bitrate | Switches between MAX and MIN quality |
| `7` | System Cleanup | Kills all ffmpeg processes with verbose output (process count, file archival, orphan scan) |
| `8` | Refresh Config | Re-fetches API config with verbose diff (new/removed streams, host changes) |
| `9` | System Diagnostic | Full diagnostic: cookies, tokens, API, ffmpeg, session probe, archive status |
| `0` | Exit | Archives files, kills processes, exits |

---

## Architecture

```
capture_streams.py
├── Archiver class
│   ├── start()              → Launch capture loop + health monitor
│   ├── _graceful_stop()      → 3-stage ffmpeg shutdown (q → terminate → kill)
│   ├── stop()               → Graceful stop all, archive, cleanup
│   ├── _loop()              → Main capture thread (15s cycle)
│   │   ├── Rotation check   → Hourly archive rotation
│   │   ├── Config refresh   → Re-fetch API every 30 min
│   │   └── Per-stream       → Start/restart/stall-detect ffmpeg
│   ├── _health_loop()       → Health monitor thread (30s cycle)
│   │   └── _health_check()  → Diagnose dead captures, re-auth, re-fetch
│   ├── _rotate()            → Move files to archive, sweep orphans
│   └── _archive_current_files() → Safe archival on stop/exit
├── verbose_startup()        → 4-step initialization with detailed output
├── main_menu()              → Interactive CLI loop
├── show_stats()             → Video file stats with auto-refresh
└── launch_vlc()             → VLC integration
```

### Threads

| Thread | Purpose | Cycle |
|--------|---------|-------|
| `_loop` | Capture management, stall detection, rotation | 15s |
| `_health_loop` | Self-healing diagnostics (session, connectivity) | 30s |

Both are daemon threads — they exit when the main process exits.

---

## File Safety

**Captured files are never deleted.** All file handling uses `shutil.move()` to relocate captures into timestamped archive directories.

### Archive Naming

```
archives/
├── archive_03192026-140000/
│   ├── dabc-1_140000.mp4
│   └── dabc-2_140000.mp4
├── archive_03192026-150000/
└── ...
```

Format: `archive_MMDDYYYY-HHMMSS`

### When Files Are Archived

| Event | Archives? |
|-------|-----------|
| Hourly rotation | Yes — all tracked files + orphan sweep |
| Stop archiver (menu 3) | Yes — all tracked files |
| Force cleanup (menu 7) | Yes — via `stop()` |
| Exit (menu 0) | Yes — via `force_kill_all()` → `stop()` |
| Script crash / Ctrl+C | Yes — `atexit` handler calls `force_kill_all()` |

### Only Deleted File

`probe_test.mp4` — a 3-second temporary file used for session verification. This is not a capture file.

---

## Health Monitor

The health monitor runs as a separate thread alongside the capture loop (started automatically with the archiver). Every 30 seconds it checks:

1. **Are all captures dead?** → Likely a session or connectivity issue
   - Checks token expiry, attempts re-authentication
   - Re-fetches API config in case hosts changed
   - Active captures continue uninterrupted during diagnosis
2. **Are some captures dead?** → Individual stream issues
   - Logs the status; the main loop handles per-stream restarts

The health monitor never kills or restarts processes directly — it fixes the underlying cause (stale token, changed hosts) and lets the main loop handle restarts naturally.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| "No tokens found" | Cookie file missing or empty | Re-export cookies from browser |
| "All tokens failed" | Tokens expired or session revoked | Re-export cookies while logged in |
| "API request failed" | Network issue or API down | Check internet; retry with option 8 |
| Captures start then die | Host changed, token expiring | Health monitor should auto-recover; check option 9 |
| Files left in working dir | Unclean shutdown before v4.1 | Use option 7 to clean up; rotation will also sweep them |

---

## Directory Structure

```
ft.claude/
├── fishtank-capture.py        # Main script
├── fishtank.cookies.txt      # Browser cookies (user-provided)
├── debug.log                 # Runtime log (reset on each launch)
├── README.md
├── CHANGELOG.md
├── currently-capturing/      # Active capture files (moved to archives on rotation/stop)
│   └── *.mkv
├── archives/                 # Archived captures
│   ├── archive_MMDDYYYY-HHMMSS/
│   │   └── *.mkv
│   └── needs-repair/         # Unfixable files (run fix_captures.py)
└── ...
```

---

See [CHANGELOG.md](CHANGELOG.md) for version history.
