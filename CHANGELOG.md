# Changelog

All notable changes to FISHTANK Skimmer are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [5.1] - 2026-03-21

### Changed

- **Output format switched from MP4 to MKV** — Matroska container writes headers incrementally, producing playable files even after abrupt process termination. All captures now output `.mkv` files compatible with DaVinci Resolve, Adobe Premiere, and VLC.
- **Graceful ffmpeg shutdown** — ffmpeg processes are now stopped via stdin `q` command (clean finalization with proper duration/keyframe metadata), with `terminate()` and `kill()` fallbacks. Applies to rotation, stop, exit, cleanup, stall detection, and Ctrl+C paths.
- **File scans updated** — orphan sweeps, stats view, and cleanup now recognize `.mkv`, `.mp4`, and `.webm` files for backwards compatibility with existing archives.
- **UAT test** outputs `.mkv` files.
- **Capture file lifecycle** — active captures now write to `currently-capturing/` directory instead of the script root. On rotation, stop, exit, or crash recovery, files are moved to `archives/archive_MMDDYYYY-HHMMSS/`.
- **Startup recovery** — on launch, stale files in `currently-capturing/` are probed with ffprobe, remuxed if corrupt, and archived. Unfixable files are routed to `archives/needs-repair/` with guidance to run `fix_captures.py`.

### Added

- `_graceful_stop()` method — three-stage process shutdown (stdin `q` → terminate → kill) with error handling for broken pipes and already-exited processes.
- `GRACEFUL_TIMEOUT` and `TERMINATE_TIMEOUT` constants.
- `currently-capturing/` directory for active capture isolation.
- `archives/needs-repair/` directory for unfixable capture files.
- `recover_stale_captures()` function — startup self-healing with ffprobe validation and ffmpeg remux repair.
- `CAPTURE_DIR` and `NEEDS_REPAIR_DIR` constants.
- Startup step [5/5] for stale capture recovery.

---

## [4.1] - 2026-03-19

### Added

- **Health monitor thread** — background thread (30s cycle) that auto-diagnoses and recovers from connection drops, expired/stale sessions, and host changes without interrupting active captures
- **Filename column** in Video File Stats view showing the active capture file per stream
- **Verbose startup sequence** — 4-step initialization (cookie file, JWT tokens, API config, session probe) with detailed status output
- **Verbose System Cleanup** (option 7) — shows active process count, files being archived, and orphan scan results
- **Verbose Refresh Config** (option 8) — shows API endpoint, stream count changes, host changes, new/removed streams
- **Verbose System Diagnostic** (option 9) — full step-by-step check of cookies, tokens, API connectivity, ffmpeg availability, session probe, and archive directory status
- **Auto-refresh toggle** inside Video File Stats view (`R` key, 5-second interval)
- **`_archive_current_files()`** method for safe archival on stop/exit paths

### Changed

- Main menu input uses `timeout=None` — responds instantly to keypresses (was 5s delay when auto-refresh was enabled)
- Auto-refresh feature moved from main menu to Video File Stats view where it's contextually useful
- Archive directories now use `archive_MMDDYYYY-HHMMSS` naming format (was `YYYYMMDD_HHMMSS`)
- Renamed "VIEW LIVE GROWTH MONITOR" → "VIEW VIDEO FILE STATS"
- `stop()` now waits for process termination (3s timeout) and archives all captured files before clearing state
- All exit paths (stop, cleanup, exit, Ctrl+C) now archive files instead of leaving them loose
- Orphan file sweep in rotation excludes `probe_test.mp4`

### Removed

- `R. TOGGLE AUTO-REFRESH (5s)` menu entry from main menu
- `self.auto_refresh` instance state from Archiver class (refresh is now local to stats view)

### Fixed

- **Main menu input lag** — the 5-second polling timeout when auto-refresh was enabled caused the menu to feel unresponsive and swallow in-progress keystrokes
- **Unarchived files on stop/exit** — captured files were left in the working directory when the archiver was stopped or the script exited; they are now always moved to `archives/`

---

## [4.0] - 2026-03-18

### Added

- Initial public release of FISHTANK Skimmer
- Multi-stream concurrent HLS capture via ffmpeg
- JWT authentication from exported browser cookies (Netscape format)
- Automatic hourly segment rotation with archival to `archives/` subdirectories
- Per-stream load balancer host support via API
- Stall detection — kills and restarts ffmpeg processes that stop growing after 4 consecutive checks
- VLC live playback integration with auto-detection
- Bitrate toggle (MIN/MAX)
- Selective camera capture (pick specific streams by number)
- Stream status display (online/offline) from API
- Activity log with 20-entry rolling buffer
- Debug logging to `debug.log`
- API config auto-refresh every 30 minutes during capture
- Orphaned file sweep during rotation (catches files from crashed processes)
- `atexit` and signal handlers for clean shutdown
