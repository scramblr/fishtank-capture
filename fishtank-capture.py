import subprocess
import os
import time
import sys
import threading
import msvcrt
import atexit
import signal
import json
import shutil
import urllib.parse
import base64
from collections import deque
from datetime import datetime, timedelta

# Version Definition
VERSION = "5.1"

# Dynamic Path Resolution
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ARCHIVE_BASE_DIR = os.path.join(SCRIPT_DIR, "archives")
CAPTURE_DIR = os.path.join(SCRIPT_DIR, "currently-capturing")
NEEDS_REPAIR_DIR = os.path.join(ARCHIVE_BASE_DIR, "needs-repair")
COOKIES_PATH = os.path.join(SCRIPT_DIR, "fishtank.cookies.txt")
DEBUG_LOG_PATH = os.path.join(SCRIPT_DIR, "debug.log")

REFERER = "https://www.fishtank.live/"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
FFMPEG_HEADERS = f"Referer: {REFERER}\r\nUser-Agent: {USER_AGENT}\r\n"
API_URL = "https://api.fishtank.live/v1/live-streams"

BITRATE_MAX = "maxbps"
BITRATE_MIN = "minbps"

GRACEFUL_TIMEOUT = 5
TERMINATE_TIMEOUT = 3
RETRY_GIVE_UP = 120         # Seconds of failed retries before marking OFFLINE


def find_vlc():
    default_path = r"C:\Program Files\VideoLAN\VLC\vlc.exe"
    if os.path.exists(default_path):
        return default_path
    found = shutil.which("vlc")
    return found if found else default_path


VLC_PATH = find_vlc()


def decode_jwt_exp(token):
    """Decode JWT payload and return expiry timestamp, or 0 on failure."""
    try:
        payload = token.split('.')[1]
        # Fix base64 padding
        payload += '=' * (4 - len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data.get('exp', 0)
    except Exception:
        return 0


class Archiver:
    def __init__(self):
        self.active_names = []
        self.processes = {}
        self.process_metadata = {}
        self.running = False
        self.next_rotation = None
        self.thread = None
        self.logs = deque(maxlen=20)
        self.last_check = None
        self.retry_interval = 15
        self.bitrate = BITRATE_MAX
        self.stream_ids = []
        self.stream_names = {}       # id -> friendly name
        self.stream_status = {}      # id -> "online"/"offline"
        self.stream_hosts = {}       # id -> per-stream load balancer host
        self.default_host = None
        self.token = None            # Best working JWT token
        self.token_exp = 0           # Expiry of current token
        self.token_locked = False
        self.config_fetched = False
        self.offline_names = set()          # Streams that failed retries for 120s
        self.retry_tracker = {}             # id -> {"first_fail": datetime, "attempts": int}

    def log(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        formatted_msg = f"[{ts}] {msg}"
        self.logs.append(formatted_msg)
        try:
            with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(formatted_msg + "\n")
        except Exception:
            pass

    def extract_tokens(self):
        """Extract all JWT tokens from the cookies file, sorted by expiry (longest-lived first)."""
        found_tokens = []
        try:
            with open(COOKIES_PATH, 'r') as f:
                for line in f:
                    if "sb-wcsaaupukpdmqdjcgaoo-auth-token" in line:
                        parts = line.strip().split('\t')
                        if len(parts) >= 7:
                            decoded = urllib.parse.unquote(parts[6])
                            token_list = json.loads(decoded)
                            if isinstance(token_list, list):
                                found_tokens.extend(token_list)
                    elif "\ttkn\t" in line:
                        parts = line.strip().split('\t')
                        if len(parts) >= 7:
                            found_tokens.append(parts[6])
        except FileNotFoundError:
            self.log("ERROR: Cookies file not found. Export from browser.")
            return []
        except Exception as e:
            self.log(f"ERROR: Failed to parse cookies: {e}")
            return []

        # Deduplicate and filter, sort by expiry (longest-lived first)
        unique = list(set(t for t in found_tokens if len(t) > 50))
        now_ts = time.time()
        # Filter out expired tokens and sort by expiry descending
        valid = [(t, decode_jwt_exp(t)) for t in unique]
        valid = [(t, exp) for t, exp in valid if exp > now_ts]
        valid.sort(key=lambda x: x[1], reverse=True)

        if not valid:
            # Fall back to all tokens if none appear valid (exp check might not matter for streams)
            return unique

        return [t for t, _ in valid]

    def ensure_authenticated(self):
        """Ensure we have a working token. Returns True if ready."""
        if self.token_locked and self.token:
            # Check if token might be expired
            if self.token_exp > 0 and time.time() > self.token_exp:
                self.log("SESSION: Token expired, re-probing...")
                self.token_locked = False
            else:
                return True

        tokens = self.extract_tokens()
        if not tokens:
            self.log("ERROR: No tokens found. Re-export cookies from browser.")
            return False

        # Pick an online stream for probing (offline streams will 404)
        online = self.get_online_streams()
        if online:
            test_stream = online[0]
        elif self.stream_ids:
            test_stream = self.stream_ids[0]
        else:
            test_stream = "dmrm-5"
        test_host = self.get_host(test_stream)

        for token in tokens:
            url = f"https://{test_host}/hls/live+{test_stream}/index.m3u8?jwt={token}&video=maxbps"
            out = os.path.join(SCRIPT_DIR, f"probe_test.mp4")
            cmd = [
                'ffmpeg', '-hide_banner', '-loglevel', 'error',
                '-headers', FFMPEG_HEADERS,
                '-i', url, '-t', '3', '-c', 'copy', '-y', out
            ]
            try:
                subprocess.run(cmd, capture_output=True, timeout=15)
                if os.path.exists(out) and os.path.getsize(out) > 5000:
                    exp = decode_jwt_exp(token)
                    self.token = token
                    self.token_exp = exp
                    self.token_locked = True
                    exp_str = datetime.fromtimestamp(exp).strftime("%Y-%m-%d") if exp else "unknown"
                    self.log(f"SESSION: Token verified (expires {exp_str}).")
                    try:
                        os.remove(out)
                    except Exception:
                        pass
                    return True
            except Exception:
                pass
            try:
                if os.path.exists(out):
                    os.remove(out)
            except Exception:
                pass

        self.log("ERROR: All tokens failed. Re-export cookies from browser.")
        return False

    def get_host(self, stream_id):
        """Get the load balancer host for a specific stream."""
        if stream_id in self.stream_hosts:
            return self.stream_hosts[stream_id]
        if self.default_host:
            return self.default_host
        return "streams-e.fishtank.live"

    def get_url(self, name):
        host = self.get_host(name)
        jwt = self.token if self.token else ""
        return f"https://{host}/hls/live+{name}/index.m3u8?jwt={jwt}&video={self.bitrate}"

    def fetch_latest_config(self):
        """Fetch stream list, status, and per-stream load balancer hosts from API."""
        try:
            cmd = ["curl.exe", "-s", "-b", COOKIES_PATH, API_URL]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if res.returncode != 0:
                self.log("WARN: API request failed.")
                return False

            data = json.loads(res.stdout)

            # Parse stream list
            streams = data.get('liveStreams', [])
            if streams:
                self.stream_ids = sorted([s['id'] for s in streams])
                self.stream_names = {s['id']: s.get('name', s['id']) for s in streams}

            # Parse per-stream load balancer hosts
            lb = data.get('loadBalancer', {})
            if isinstance(lb, dict):
                self.stream_hosts = dict(lb)
                hosts = set(lb.values())
                if hosts:
                    self.default_host = next(iter(hosts))

            # Parse stream status
            status = data.get('liveStreamStatus', {})
            if isinstance(status, dict):
                self.stream_status = dict(status)

            self.config_fetched = True
            online_count = sum(1 for v in self.stream_status.values() if v == "online")
            self.log(f"CONFIG: {len(self.stream_ids)} streams, {online_count} online, host={self.default_host}")
            return True

        except Exception as e:
            self.log(f"WARN: API fetch failed: {e}")
            return False

    def start(self, names):
        if not self.ensure_authenticated():
            self.log("ERROR: Cannot start without valid session.")
            return
        self.active_names = list(names)
        self.offline_names -= set(names)
        self.retry_tracker = {k: v for k, v in self.retry_tracker.items() if k not in names}
        self.running = True
        self.next_rotation = datetime.now() + timedelta(hours=1)
        self.log(f"ARCHIVER: Capture started for {len(names)} feeds.")
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()
        self._start_health_monitor()

    def _archive_current_files(self):
        """Move all captured files to a timestamped archive subdirectory."""
        if not self.process_metadata:
            return
        ts = datetime.now().strftime("archive_%m%d%Y-%H%M%S")
        archive_dir = os.path.join(ARCHIVE_BASE_DIR, ts)
        os.makedirs(archive_dir, exist_ok=True)
        moved = 0
        for name, meta in self.process_metadata.items():
            src = meta.get("file", "")
            if src and os.path.exists(src):
                try:
                    shutil.move(src, os.path.join(archive_dir, os.path.basename(src)))
                    moved += 1
                except Exception:
                    pass
        if moved:
            self.log(f"ARCHIVE: Moved {moved} files to {ts}/")

    def _graceful_stop(self, process, name="unknown"):
        """Gracefully stop an ffmpeg process. Returns True if graceful, False if forced."""
        # Already exited?
        if process.poll() is not None:
            return True

        # Stage 1: Send 'q' to stdin for clean finalization
        try:
            process.stdin.write(b"q\n")
            process.stdin.flush()
            process.stdin.close()
            process.wait(timeout=GRACEFUL_TIMEOUT)
            self.log(f"STOP: {name} exited gracefully.")
            return True
        except (BrokenPipeError, OSError, ValueError):
            self.log(f"STOP: {name} stdin broken, forcing terminate.")
        except subprocess.TimeoutExpired:
            self.log(f"STOP: {name} did not exit in {GRACEFUL_TIMEOUT}s, terminating.")

        # Stage 2: terminate()
        try:
            process.terminate()
            process.wait(timeout=TERMINATE_TIMEOUT)
            self.log(f"STOP: {name} terminated.")
            return False
        except subprocess.TimeoutExpired:
            self.log(f"STOP: {name} did not terminate in {TERMINATE_TIMEOUT}s, killing.")

        # Stage 3: kill()
        try:
            process.kill()
            process.wait(timeout=TERMINATE_TIMEOUT)
        except Exception:
            pass
        self.log(f"STOP: {name} killed.")
        return False

    def stop(self, skip_graceful=False):
        self.running = False
        self.log("ARCHIVER: Stopping all.")
        total = len(self.processes)
        for i, (name, p) in enumerate(self.processes.items(), 1):
            print(f"  [{i}/{total}] Stopping {name}...", end=" ", flush=True)
            if skip_graceful:
                # Skip graceful 'q' phase — go straight to terminate/kill
                if p.poll() is None:
                    try:
                        p.terminate()
                        p.wait(timeout=TERMINATE_TIMEOUT)
                        print("terminated.")
                        self.log(f"STOP: {name} terminated (skip-graceful).")
                    except subprocess.TimeoutExpired:
                        try:
                            p.kill()
                            p.wait(timeout=TERMINATE_TIMEOUT)
                        except Exception:
                            pass
                        print("killed.")
                        self.log(f"STOP: {name} killed (skip-graceful).")
                else:
                    print("already exited.")
            else:
                result = self._graceful_stop(p, name)
                print("done." if result else "forced.")
        print(f"  Archiving {total} capture file(s)...", end=" ", flush=True)
        self._archive_current_files()
        print("done.")
        self.processes = {}
        self.process_metadata = {}
        self.offline_names.clear()
        self.retry_tracker.clear()

    def force_kill_all(self):
        self.stop()
        if os.name == 'nt':
            subprocess.run(
                ["taskkill", "/F", "/IM", "ffmpeg.exe", "/T"],
                stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL
            )
        else:
            subprocess.run(
                ["pkill", "-9", "ffmpeg"],
                stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL
            )

    def _loop(self):
        while self.running:
            now = datetime.now()
            if self.next_rotation and now >= self.next_rotation:
                self._rotate()
                self.next_rotation = now + timedelta(hours=1)

            # Re-fetch config every 30 minutes to track host changes
            if self.last_check and (now - self.last_check) > timedelta(minutes=30):
                self.fetch_latest_config()

            for name in list(self.active_names):
                p = self.processes.get(name)
                meta = self.process_metadata.get(name)

                restart = False
                if p is not None:
                    ret = p.poll()
                    if ret is not None:
                        self.log(f"EXITED: {name} (Code {ret})")
                        restart = True
                        # Track process death for retry timeout
                        if name not in self.retry_tracker:
                            self.retry_tracker[name] = {"first_fail": now, "attempts": 0}
                    elif meta:
                        try:
                            curr = os.path.getsize(meta["file"])
                            if curr <= meta["last_size"]:
                                meta["stalled_count"] += 1
                                if meta["stalled_count"] > 4:
                                    self.log(f"STALL: {name} killing process.")
                                    self._graceful_stop(p, name)
                                    restart = True
                            else:
                                meta["stalled_count"] = 0
                                meta["last_size"] = curr
                                self.retry_tracker.pop(name, None)  # Alive and growing
                        except OSError:
                            pass  # File not created yet

                if p is None or restart:
                    if name in self.processes:
                        del self.processes[name]
                    ts = datetime.now().strftime("%H%M%S")
                    out_path = os.path.join(CAPTURE_DIR, f"{name}_{ts}.mkv")
                    url = self.get_url(name)

                    cmd = [
                        'ffmpeg', '-hide_banner', '-loglevel', 'error',
                        '-headers', FFMPEG_HEADERS,
                        '-i', url,
                        '-c', 'copy', '-y', out_path
                    ]

                    try:
                        self.processes[name] = subprocess.Popen(
                            cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                        )
                        self.process_metadata[name] = {
                            "file": out_path, "last_size": 0, "stalled_count": 0
                        }
                        self.log(f"LIVE: {name} -> {os.path.basename(out_path)}")
                    except Exception as e:
                        self.log(f"FAIL: {name} ({e})")
                        # Track retry failures — give up after RETRY_GIVE_UP seconds
                        if name not in self.retry_tracker:
                            self.retry_tracker[name] = {"first_fail": now, "attempts": 1}
                        else:
                            self.retry_tracker[name]["attempts"] += 1

                # Check if any active stream has been failing too long
                if name in self.retry_tracker:
                    elapsed = (now - self.retry_tracker[name]["first_fail"]).total_seconds()
                    if elapsed >= RETRY_GIVE_UP:
                        attempts = self.retry_tracker[name]["attempts"]
                        self.log(f"OFFLINE: {name} — failed {attempts} attempts over {int(elapsed)}s, giving up.")
                        self.active_names.remove(name)
                        self.offline_names.add(name)
                        self.retry_tracker.pop(name, None)
                        if name in self.processes:
                            del self.processes[name]
                        if name in self.process_metadata:
                            del self.process_metadata[name]

            self.last_check = now
            time.sleep(self.retry_interval)

    def _rotate(self):
        self.log("ROTATION: Moving segments to archive.")
        # Terminate all ffmpeg processes and wait for them
        for name, p in self.processes.items():
            self._graceful_stop(p, name)

        ts = datetime.now().strftime("archive_%m%d%Y-%H%M%S")
        archive_dir = os.path.join(ARCHIVE_BASE_DIR, ts)
        os.makedirs(archive_dir, exist_ok=True)

        # Move known capture files from metadata
        moved = 0
        for name, meta in self.process_metadata.items():
            src = meta.get("file", "")
            if src and os.path.exists(src):
                try:
                    shutil.move(src, os.path.join(archive_dir, os.path.basename(src)))
                    moved += 1
                except Exception:
                    pass

        # Also sweep any orphaned segment files
        try:
            for f_name in os.listdir(CAPTURE_DIR):
                if f_name.endswith((".mkv", ".mp4", ".webm")) and "_" in f_name:
                    src = os.path.join(CAPTURE_DIR, f_name)
                    if os.path.isfile(src) and f_name != "probe_test.mp4":
                        try:
                            shutil.move(src, os.path.join(archive_dir, f_name))
                            moved += 1
                        except Exception:
                            pass
        except Exception:
            pass

        self.log(f"ROTATION: Archived {moved} files to {ts}/")
        self.processes = {}
        self.process_metadata = {}

    def _start_health_monitor(self):
        """Start a background thread that periodically checks capture health."""
        if hasattr(self, '_health_thread') and self._health_thread and self._health_thread.is_alive():
            return
        self._health_thread = threading.Thread(target=self._health_loop, daemon=True)
        self._health_thread.start()
        self.log("HEALTH: Monitor thread started.")

    def _health_loop(self):
        """Background health check loop — diagnoses and recovers from failures."""
        while self.running:
            time.sleep(30)  # Check every 30 seconds
            if not self.running:
                break
            try:
                self._health_check()
            except Exception as e:
                self.log(f"HEALTH: Monitor error: {e}")

    def _health_check(self):
        """Run diagnostics on active captures and self-correct issues."""
        if not self.active_names:
            return

        # Count how many processes are actually alive (read-only snapshot)
        alive = 0
        dead = []
        for name in list(self.active_names):
            p = self.processes.get(name)
            if p is None or p.poll() is not None:
                dead.append(name)
            else:
                alive += 1

        # Only intervene if ALL processes are dead — partial failures are
        # handled by the main loop's per-stream restart logic
        if alive == 0 and dead:
            self.log(f"HEALTH: All {len(dead)} captures are dead. Diagnosing...")

            # Check if token is expired
            if self.token_exp > 0 and time.time() > self.token_exp:
                self.log("HEALTH: Token expired. Attempting re-authentication...")
                self.token_locked = False
                if self.ensure_authenticated():
                    self.log("HEALTH: Re-authenticated successfully. Main loop will restart captures.")
                else:
                    self.log("HEALTH: Re-authentication failed. Cookies may need re-export.")
                    return
            else:
                # Token not expired — try re-probing to verify it still works
                self.log("HEALTH: Token not expired. Verifying session validity...")
                self.token_locked = False
                if self.ensure_authenticated():
                    self.log("HEALTH: Session still valid. Connection may have dropped — main loop will restart.")
                else:
                    self.log("HEALTH: Session invalid despite unexpired token. Cookies stale.")
                    return

            # Re-fetch config in case hosts changed (main loop also does this
            # every 30 min, but if all captures are dead we need it now)
            self.fetch_latest_config()

        elif len(dead) > 0 and alive > 0:
            # Some dead, some alive — individual stream issues, main loop handles restarts
            self.log(f"HEALTH: {alive} alive, {len(dead)} dead ({', '.join(dead[:3])}{'...' if len(dead) > 3 else ''}). Main loop will restart.")

    def get_online_streams(self):
        """Return list of stream IDs that are currently online."""
        if self.stream_status:
            return [sid for sid in self.stream_ids if self.stream_status.get(sid) == "online"]
        return self.stream_ids


archiver = Archiver()


def cleanup_on_exit():
    archiver.force_kill_all()


atexit.register(cleanup_on_exit)
signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))
signal.signal(signal.SIGINT, lambda s, f: sys.exit(0))


def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')


def get_input_with_timeout(prompt, timeout=None):
    sys.stdout.write(prompt)
    sys.stdout.flush()
    input_str = ""
    start_time = time.time()
    while True:
        if msvcrt.kbhit():
            char = msvcrt.getch()
            if char in (b'\r', b'\n'):
                sys.stdout.write('\n')
                return input_str
            elif char == b'\x08':
                if input_str:
                    input_str = input_str[:-1]
                    sys.stdout.write('\b \b')
                    sys.stdout.flush()
            elif char == b'\x03':
                raise KeyboardInterrupt
            else:
                try:
                    s = char.decode('ascii')
                    input_str += s
                    sys.stdout.write(s)
                    sys.stdout.flush()
                except Exception:
                    pass
        if timeout is not None and (time.time() - start_time) > timeout:
            return None
        time.sleep(0.05)


def print_stream_list(streams):
    """Print numbered list of streams with names and status."""
    for i, sid in enumerate(streams):
        name = archiver.stream_names.get(sid, "")
        status = archiver.stream_status.get(sid, "unknown")
        status_icon = "🟢" if status == "online" else "⚫"
        label = f"{sid} ({name})" if name and name != sid else sid
        print(f"  {i+1:>2}. {status_icon} {label}")


def recover_stale_captures():
    """Check currently-capturing/ for stale files from previous sessions and recover them."""
    media_extensions = (".mkv", ".mp4", ".webm")
    try:
        stale_files = [f for f in os.listdir(CAPTURE_DIR)
                       if f.endswith(media_extensions) and os.path.isfile(os.path.join(CAPTURE_DIR, f))]
    except Exception:
        return 0, 0

    if not stale_files:
        return 0, 0

    print(f"       Recovering {len(stale_files)} file(s)...")
    recovered = 0
    unfixable = 0

    for fname in stale_files:
        filepath = os.path.join(CAPTURE_DIR, fname)
        print(f"       Checking: {fname}")

        # Probe with ffprobe
        healthy = False
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", filepath],
                capture_output=True, text=True, timeout=15
            )
            duration_str = result.stdout.strip()
            if duration_str and float(duration_str) > 0:
                healthy = True
        except Exception:
            pass

        if healthy:
            # File is good — archive it using mtime
            try:
                mtime = os.path.getmtime(filepath)
                ts = datetime.fromtimestamp(mtime).strftime("archive_%m%d%Y-%H%M%S")
                archive_dir = os.path.join(ARCHIVE_BASE_DIR, ts)
                os.makedirs(archive_dir, exist_ok=True)
                shutil.move(filepath, os.path.join(archive_dir, fname))
                print(f"       Archived: {fname} → {ts}/")
                recovered += 1
            except (PermissionError, OSError) as e:
                print(f"       WARNING: Could not move {fname}: {e}")
            continue

        # Attempt remux fix
        fixed_path = filepath + ".fixed.mkv"
        fix_success = False
        try:
            result = subprocess.run(
                ["ffmpeg", "-v", "error", "-i", filepath, "-c", "copy", "-y", fixed_path],
                capture_output=True, text=True, timeout=60
            )
            if os.path.exists(fixed_path) and os.path.getsize(fixed_path) > 1000:
                fix_success = True
        except Exception:
            pass

        if fix_success:
            try:
                os.remove(filepath)
                # Rename fixed file to original name
                fixed_final = os.path.join(CAPTURE_DIR, fname)
                shutil.move(fixed_path, fixed_final)
                # Archive the repaired file
                mtime = os.path.getmtime(fixed_final)
                ts = datetime.fromtimestamp(mtime).strftime("archive_%m%d%Y-%H%M%S")
                archive_dir = os.path.join(ARCHIVE_BASE_DIR, ts)
                os.makedirs(archive_dir, exist_ok=True)
                shutil.move(fixed_final, os.path.join(archive_dir, fname))
                print(f"       Repaired and archived: {fname} → {ts}/")
                recovered += 1
            except (PermissionError, OSError) as e:
                print(f"       WARNING: Could not move repaired {fname}: {e}")
        else:
            # Unfixable — move to needs-repair
            try:
                if os.path.exists(fixed_path):
                    os.remove(fixed_path)
            except Exception:
                pass
            try:
                shutil.move(filepath, os.path.join(NEEDS_REPAIR_DIR, fname))
                print(f"       UNFIXABLE: {fname} → archives/needs-repair/")
                unfixable += 1
            except (PermissionError, OSError) as e:
                print(f"       WARNING: Could not move {fname} to needs-repair: {e}")

    if unfixable > 0:
        print(f"\n       WARNING: {unfixable} file(s) could not be repaired.")
        print("       Run fix_captures.py for advanced recovery.")

    return recovered, unfixable


def verbose_startup():
    """Run verbose startup checks before entering the main menu."""
    print(f"=== FISHTANK Skimmer v{VERSION} — Startup ===\n")

    # Step 1: Cookie file
    print("[1/5] Checking cookie file...")
    if os.path.exists(COOKIES_PATH):
        size = os.path.getsize(COOKIES_PATH)
        mtime = datetime.fromtimestamp(os.path.getmtime(COOKIES_PATH)).strftime("%Y-%m-%d %H:%M:%S")
        print(f"       Found: {COOKIES_PATH}")
        print(f"       Size: {size} bytes | Last modified: {mtime}")
    else:
        print(f"       WARNING: Cookie file not found at {COOKIES_PATH}")
        print("       Export cookies from fishtank.live using a browser extension.")

    # Step 2: Token extraction
    print("\n[2/5] Extracting JWT tokens...")
    tokens = archiver.extract_tokens()
    if tokens:
        print(f"       Found {len(tokens)} token(s).")
        for i, t in enumerate(tokens[:3]):
            exp = decode_jwt_exp(t)
            if exp:
                exp_str = datetime.fromtimestamp(exp).strftime("%Y-%m-%d %H:%M")
                remaining = exp - time.time()
                if remaining > 0:
                    hours = int(remaining // 3600)
                    mins = int((remaining % 3600) // 60)
                    print(f"       Token {i+1}: expires {exp_str} ({hours}h {mins}m remaining)")
                else:
                    print(f"       Token {i+1}: EXPIRED ({exp_str})")
            else:
                print(f"       Token {i+1}: expiry unknown")
    else:
        print("       WARNING: No tokens found. Re-export cookies from browser.")

    # Step 3: API config
    print("\n[3/5] Fetching stream configuration from API...")
    if archiver.fetch_latest_config():
        online = archiver.get_online_streams()
        print(f"       Streams: {len(archiver.stream_ids)} total, {len(online)} online")
        print(f"       Host: {archiver.default_host or 'unknown'}")
        if online:
            names = [f"{sid} ({archiver.stream_names.get(sid, '')})" for sid in online[:5]]
            print(f"       Online: {', '.join(names)}{'...' if len(online) > 5 else ''}")
    else:
        print("       WARNING: API fetch failed. Will retry on menu actions.")

    # Step 4: Session probe
    print("\n[4/5] Verifying session with live probe...")
    if archiver.ensure_authenticated():
        exp_str = ""
        if archiver.token_exp:
            exp_str = f" (expires {datetime.fromtimestamp(archiver.token_exp).strftime('%Y-%m-%d')})"
        print(f"       Session VERIFIED{exp_str}")
    else:
        print("       WARNING: Session verification FAILED. Cookies may be stale.")

    # Step 5: Stale capture recovery
    print()
    media_extensions = (".mkv", ".mp4", ".webm")
    try:
        stale_count = len([f for f in os.listdir(CAPTURE_DIR)
                           if f.endswith(media_extensions) and os.path.isfile(os.path.join(CAPTURE_DIR, f))])
    except Exception:
        stale_count = 0

    if stale_count > 0:
        print(f"[5/5] Found {stale_count} stale capture file(s) from previous session.")
        print(f"       [S]kip cleanup (run fix_captures.py to clean and fix all video files)")
        print(f"       [Enter] Run cleanup now\n")
        skip_choice = input("       Choice: ").strip().lower()
        if skip_choice == 's':
            print("       Skipped. Run fix_captures.py for file maintenance.")
        else:
            recover_stale_captures()
    else:
        print("[5/5] No stale captures found.")

    print(f"\n{'='*50}")
    print("Startup complete. Entering main menu...\n")
    time.sleep(1.5)


def main_menu():
    verbose_startup()

    while True:
        clear_screen()
        print(f"=== FISHTANK Skimmer v{VERSION} ===")
        print(f"DIR:  {SCRIPT_DIR}")

        if archiver.token_locked:
            exp_str = ""
            if archiver.token_exp:
                exp_str = f", expires {datetime.fromtimestamp(archiver.token_exp).strftime('%Y-%m-%d')}"
            token_str = f"VERIFIED{exp_str}"
        else:
            token_str = "UNVERIFIED"
        host_str = archiver.default_host or "unknown"
        online_count = len(archiver.get_online_streams())
        print(f"SESS: {token_str} | Host: {host_str} | Online: {online_count}/{len(archiver.stream_ids)}")

        if archiver.running:
            print(f"CAPTURING: {len(archiver.active_names)} streams | "
                  f"Next rotation: {archiver.next_rotation.strftime('%H:%M:%S') if archiver.next_rotation else 'N/A'}")

        print("\nACTIVITY LOG:")
        if not archiver.logs:
            print("  (Ready to capture...)")
        for entry in list(archiver.logs)[-12:]:
            print(f"  {entry}")

        print("\n" + "-" * 40)
        print("1. START ARCHIVER (All Online Streams)")
        print("2. START SELECTIVE CAMERAS")
        if archiver.running:
            print("3. STOP ARCHIVER")
        else:
            print("3. (Archiver not running)")
        print("4. VIEW VIDEO FILE STATS")
        print("5. LAUNCH LIVE VLC WINDOW")
        print("6. TOGGLE BITRATE (MIN/MAX) [Current: " +
              ("MAX" if archiver.bitrate == BITRATE_MAX else "MIN") + "]")
        print("7. FORCE SYSTEM CLEANUP (kill all ffmpeg)")
        print("8. REFRESH CONFIG FROM API")
        print("9. RUN SYSTEM DIAGNOSTIC")
        print("0. EXIT (stops all captures)")

        choice = get_input_with_timeout("\nCommand: ", None)
        if choice is None:
            continue

        choice = choice.strip().lower()

        if choice == '1':
            online = archiver.get_online_streams()
            if online:
                archiver.start(online)
            else:
                archiver.log("WARN: No online streams found.")
        elif choice == '2':
            print("\nAvailable Cameras:")
            print_stream_list(archiver.stream_ids)
            idx_in = input("\nEnter numbers (comma-separated): ")
            try:
                selected = [
                    archiver.stream_ids[int(i.strip()) - 1]
                    for i in idx_in.split(',') if i.strip()
                ]
                if selected:
                    archiver.start(selected)
            except Exception:
                print("Invalid selection.")
                time.sleep(1)
        elif choice == '3':
            if archiver.running:
                n = len(archiver.processes)
                graceful_secs = n * GRACEFUL_TIMEOUT
                fast_secs = n * TERMINATE_TIMEOUT
                print(f"\n  NOTICE: Wrapping up {n} capture file(s), please wait while this completes.")
                print(f"         Graceful stop: up to {graceful_secs // 60}m {graceful_secs % 60}s  |  Fast stop: ~{fast_secs}s")
                print(f"\n  [S] Skip graceful shutdown (faster, may lose last few seconds of video)")
                print(f"  [Enter] Graceful shutdown (preserves all video data)\n")
                stop_choice = input("  Choice: ").strip().lower()
                skip = stop_choice == 's'
                if skip:
                    print(f"\n  Skipping graceful shutdown — terminating {n} process(es)...\n")
                else:
                    print(f"\n  Graceful shutdown — this may take up to {graceful_secs // 60}m {graceful_secs % 60}s...\n")
                archiver.stop(skip_graceful=skip)
                input("\nArchiver stopped. Press Enter...")
            else:
                print("\nArchiver is not running.")
                time.sleep(1)
        elif choice == '4':
            show_stats()
        elif choice == '5':
            launch_vlc()
        elif choice == '6':
            archiver.bitrate = BITRATE_MIN if archiver.bitrate == BITRATE_MAX else BITRATE_MAX
            archiver.log(f"BITRATE: Switched to {'MAX' if archiver.bitrate == BITRATE_MAX else 'MIN'}")
            if archiver.running:
                archiver.stop()
                time.sleep(1)
                archiver.start(archiver.active_names)
        elif choice == '7':
            print("\n--- SYSTEM CLEANUP ---")
            # Show active captures before killing
            active_count = len([p for p in archiver.processes.values() if p and p.poll() is None])
            print(f"Active capture processes: {active_count}")
            if archiver.process_metadata:
                for name, meta in archiver.process_metadata.items():
                    f = meta.get("file", "")
                    sz = ""
                    if f and os.path.exists(f):
                        sz = f" ({os.path.getsize(f) / (1024*1024):.1f} MB)"
                    print(f"  Archiving: {os.path.basename(f)}{sz}")
            print("Stopping archiver and archiving captured files...")
            archiver.force_kill_all()
            print("Killing any remaining ffmpeg processes system-wide...")
            # Count orphan mp4s still in working dir
            orphans = [f for f in os.listdir(CAPTURE_DIR)
                       if f.endswith((".mkv", ".mp4", ".webm"))
                       and "_" in f and f != "probe_test.mp4" and os.path.isfile(os.path.join(CAPTURE_DIR, f))]
            if orphans:
                print(f"Orphaned capture files found: {len(orphans)}")
                for f in orphans:
                    print(f"  {f}")
            else:
                print("No orphaned capture files in currently-capturing/.")
            print("Cleanup complete.")
            input("\nPress Enter to return...")
        elif choice == '8':
            print("\n--- REFRESH CONFIG FROM API ---")
            print(f"Endpoint: {API_URL}")
            print("Fetching...")
            old_ids = set(archiver.stream_ids)
            old_host = archiver.default_host
            success = archiver.fetch_latest_config()
            if success:
                new_ids = set(archiver.stream_ids)
                online = archiver.get_online_streams()
                print(f"Streams:  {len(archiver.stream_ids)} total, {len(online)} online")
                print(f"Host:     {archiver.default_host or 'unknown'}")
                added = new_ids - old_ids
                removed = old_ids - new_ids
                if added:
                    print(f"NEW streams: {', '.join(sorted(added))}")
                if removed:
                    print(f"REMOVED streams: {', '.join(sorted(removed))}")
                if old_host != archiver.default_host:
                    print(f"Host changed: {old_host} -> {archiver.default_host}")
                if not added and not removed and old_host == archiver.default_host:
                    print("No changes detected.")
            else:
                print("FAILED: Could not reach API. Check connectivity.")
            input("\nPress Enter to return...")
        elif choice == '9':
            print("\n--- SYSTEM DIAGNOSTIC ---")
            issues = []

            # Check cookie file
            print("\n[Cookie File]")
            if os.path.exists(COOKIES_PATH):
                size = os.path.getsize(COOKIES_PATH)
                mtime = datetime.fromtimestamp(os.path.getmtime(COOKIES_PATH)).strftime("%Y-%m-%d %H:%M:%S")
                print(f"  Status: FOUND ({size} bytes, modified {mtime})")
            else:
                print("  Status: MISSING")
                issues.append("Cookie file not found")

            # Check tokens
            print("\n[JWT Tokens]")
            tokens = archiver.extract_tokens()
            if tokens:
                print(f"  Found: {len(tokens)} token(s)")
                for i, t in enumerate(tokens[:3]):
                    exp = decode_jwt_exp(t)
                    if exp:
                        remaining = exp - time.time()
                        exp_str = datetime.fromtimestamp(exp).strftime("%Y-%m-%d %H:%M")
                        if remaining > 0:
                            hours = int(remaining // 3600)
                            print(f"  Token {i+1}: valid, expires {exp_str} ({hours}h left)")
                        else:
                            print(f"  Token {i+1}: EXPIRED ({exp_str})")
                            issues.append(f"Token {i+1} expired")
                    else:
                        print(f"  Token {i+1}: expiry unknown")
            else:
                print("  Found: NONE")
                issues.append("No tokens found in cookie file")

            # Check API connectivity
            print("\n[API Connectivity]")
            if archiver.fetch_latest_config():
                online = archiver.get_online_streams()
                print(f"  API: OK ({len(archiver.stream_ids)} streams, {len(online)} online)")
                print(f"  Host: {archiver.default_host or 'unknown'}")
            else:
                print("  API: FAILED")
                issues.append("API unreachable")

            # Check ffmpeg
            print("\n[ffmpeg]")
            ffmpeg_path = shutil.which("ffmpeg")
            if ffmpeg_path:
                print(f"  Path: {ffmpeg_path}")
            else:
                print("  Status: NOT FOUND in PATH")
                issues.append("ffmpeg not in PATH")

            # Session probe
            print("\n[Session Probe]")
            if archiver.running:
                # Don't disrupt active captures — report current state only
                if archiver.token_locked and archiver.token:
                    exp_str = ""
                    if archiver.token_exp:
                        remaining = archiver.token_exp - time.time()
                        exp_str = f" (expires {datetime.fromtimestamp(archiver.token_exp).strftime('%Y-%m-%d')}"
                        if remaining > 0:
                            exp_str += f", {int(remaining // 3600)}h left)"
                        else:
                            exp_str += ", EXPIRED)"
                            issues.append("Token expired while archiver is running")
                        print(f"  Result: CACHED TOKEN{exp_str}")
                    else:
                        print(f"  Result: CACHED TOKEN (expiry unknown)")
                    print("  Note: Skipping live probe while archiver is running")
                else:
                    print("  Result: NO VALID TOKEN (archiver may be failing)")
                    issues.append("No valid token while archiver is running")
            else:
                archiver.token_locked = False  # Safe to force re-probe when not running
                if archiver.ensure_authenticated():
                    exp_str = ""
                    if archiver.token_exp:
                        exp_str = f" (expires {datetime.fromtimestamp(archiver.token_exp).strftime('%Y-%m-%d')})"
                    print(f"  Result: PASSED{exp_str}")
                else:
                    print("  Result: FAILED")
                    issues.append("Session probe failed — cookies may be stale")

            # Check archive directory
            print("\n[Archives]")
            if os.path.exists(ARCHIVE_BASE_DIR):
                archive_count = len([d for d in os.listdir(ARCHIVE_BASE_DIR)
                                     if os.path.isdir(os.path.join(ARCHIVE_BASE_DIR, d))])
                print(f"  Directory: {ARCHIVE_BASE_DIR}")
                print(f"  Archives: {archive_count} rotation(s)")
            else:
                print(f"  Directory: not yet created")

            # Summary
            print(f"\n{'='*40}")
            if issues:
                print(f"DIAGNOSTIC: {len(issues)} issue(s) found:")
                for issue in issues:
                    print(f"  - {issue}")
            else:
                print("DIAGNOSTIC: ALL CHECKS PASSED")
            input("\nPress Enter to return...")
        elif choice == '0':
            if archiver.running:
                n = len(archiver.processes)
                graceful_secs = n * GRACEFUL_TIMEOUT
                print(f"\n  Stopping {n} capture(s) and archiving files.")
                print(f"  This may take up to {graceful_secs // 60}m {graceful_secs % 60}s.\n")
                print(f"  [S] Skip cleanup (run fix_captures.py later to clean and fix all video files)")
                print(f"  [Enter] Run cleanup now\n")
                exit_choice = input("  Choice: ").strip().lower()
                if exit_choice == 's':
                    print("  Killing processes without cleanup...")
                    archiver.running = False
                    for name, p in archiver.processes.items():
                        if p.poll() is None:
                            try:
                                p.kill()
                            except Exception:
                                pass
                    archiver.processes = {}
                    archiver.process_metadata = {}
                else:
                    archiver.force_kill_all()
            print("All captures stopped. Exiting.")
            sys.exit()


def show_stats():
    auto_refresh = False
    prev_sizes = {}     # Track sizes between refreshes for growth detection
    while True:
        clear_screen()
        print("=== Video File Stats ===")
        print(f"{'#':<4} {'Stream':<18} | {'Name':<15} | {'Status':<10} | {'Size (MB)':<10} | {'Growth':<8} | {'Filename'}")
        print("-" * 114)

        # Build indexed stream list for interactive controls
        indexed_streams = list(archiver.stream_ids)

        for idx, name in enumerate(indexed_streams, 1):
            proc = archiver.processes.get(name)
            friendly = archiver.stream_names.get(name, "")[:15]
            api_status = archiver.stream_status.get(name, "")

            if name in archiver.offline_names:
                status = "OFFLINE"
            elif name in archiver.active_names:
                status = "ACTIVE" if proc and proc.poll() is None else "RETRYING"
            elif api_status == "online":
                status = "ONLINE"
            else:
                status = "OFFLINE"

            # Use cached size from process_metadata (updated by _loop every 15s)
            meta = archiver.process_metadata.get(name)
            current_size = meta["last_size"] if meta else 0
            filename = os.path.basename(meta["file"]) if meta and meta.get("file") else "--"

            prev = prev_sizes.get(name, 0)
            growth = "GROWING" if current_size > prev and status == "ACTIVE" else "--"
            prev_sizes[name] = current_size

            print(f"{idx:<4} {name:<18} | {friendly:<15} | {status:<10} | {current_size/(1024*1024):>10.2f} | {growth:<8} | {filename}")

        print(f"\n  Stream controls:  START <#>  |  STOP <#>  |  RETRY <#>")
        refresh_label = "ON" if auto_refresh else "OFF"
        prompt = f"  Auto-refresh: {refresh_label} | R=toggle | Enter=return | Command: "
        timeout = 5 if auto_refresh else None
        choice = get_input_with_timeout(prompt, timeout)

        if choice is None:
            # Timeout while auto-refreshing — just redraw
            continue
        choice = choice.strip().lower()
        if choice == 'r':
            auto_refresh = not auto_refresh
        elif choice == '':
            break
        else:
            # Parse stream control commands: START/STOP/RETRY <number>
            parts = choice.split()
            if len(parts) == 2 and parts[0] in ('start', 'stop', 'retry'):
                cmd_action = parts[0]
                try:
                    stream_num = int(parts[1])
                    if 1 <= stream_num <= len(indexed_streams):
                        stream_name = indexed_streams[stream_num - 1]
                        if cmd_action in ('start', 'retry'):
                            if stream_name in archiver.active_names:
                                print(f"  {stream_name} is already active.")
                            else:
                                archiver.offline_names.discard(stream_name)
                                archiver.retry_tracker.pop(stream_name, None)
                                if stream_name not in archiver.active_names:
                                    archiver.active_names.append(stream_name)
                                print(f"  {stream_name} — queued for capture.")
                        elif cmd_action == 'stop':
                            if stream_name not in archiver.active_names:
                                print(f"  {stream_name} is not active.")
                            else:
                                proc = archiver.processes.get(stream_name)
                                if proc:
                                    archiver._graceful_stop(proc, stream_name)
                                    del archiver.processes[stream_name]
                                archiver.active_names.remove(stream_name)
                                archiver.offline_names.add(stream_name)
                                archiver.process_metadata.pop(stream_name, None)
                                archiver.retry_tracker.pop(stream_name, None)
                                print(f"  {stream_name} — stopped.")
                    else:
                        print(f"  Invalid stream number. Use 1-{len(indexed_streams)}.")
                except ValueError:
                    print(f"  Invalid number: {parts[1]}")
                time.sleep(1.5)


def launch_vlc():
    if not archiver.ensure_authenticated():
        print("No valid session. Cannot launch VLC.")
        input("Press Enter...")
        return

    print("\nLaunch VLC for:")
    online = archiver.get_online_streams()
    print_stream_list(online if online else archiver.stream_ids)
    streams = online if online else archiver.stream_ids

    idx = input("\nNumber: ")
    try:
        name = streams[int(idx) - 1]
        url = archiver.get_url(name)
        friendly = archiver.stream_names.get(name, name)
        subprocess.Popen(
            [VLC_PATH, url, f"--meta-title=Fishtank: {friendly}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        archiver.log(f"VLC: Launched {name} ({friendly})")
    except Exception:
        print("Invalid selection.")
        time.sleep(1)


if __name__ == "__main__":
    try:
        with open(DEBUG_LOG_PATH, 'w') as f:
            pass
    except Exception:
        pass
    os.makedirs(ARCHIVE_BASE_DIR, exist_ok=True)
    os.makedirs(CAPTURE_DIR, exist_ok=True)
    os.makedirs(NEEDS_REPAIR_DIR, exist_ok=True)
    main_menu()
