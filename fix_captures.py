"""
fix_captures.py — Standalone maintenance tool for FISHTANK Skimmer capture files.

Scans currently-capturing/ and archives/ for media files, probes them with ffprobe,
attempts remux repair on corrupt files, and routes unfixable files to archives/needs-repair/.

Usage:
    python fix_captures.py              # Interactive menu
    python fix_captures.py --scan       # Scan and report only (no changes)
    python fix_captures.py --fix        # Scan, repair, and archive automatically
"""

import subprocess
import os
import sys
import shutil
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CAPTURE_DIR = os.path.join(SCRIPT_DIR, "currently-capturing")
ARCHIVE_BASE_DIR = os.path.join(SCRIPT_DIR, "archives")
NEEDS_REPAIR_DIR = os.path.join(ARCHIVE_BASE_DIR, "needs-repair")

MEDIA_EXTENSIONS = (".mkv", ".mp4", ".webm")


def find_media_files(directory):
    """Find all media files in a directory (non-recursive)."""
    files = []
    try:
        for f in os.listdir(directory):
            filepath = os.path.join(directory, f)
            if f.endswith(MEDIA_EXTENSIONS) and os.path.isfile(filepath):
                files.append(filepath)
    except FileNotFoundError:
        pass
    return sorted(files)


def find_all_media_files():
    """Find media files in currently-capturing/ and archives/needs-repair/."""
    files = []
    files.extend(find_media_files(CAPTURE_DIR))
    files.extend(find_media_files(NEEDS_REPAIR_DIR))
    return files


def probe_file(filepath):
    """Probe a media file with ffprobe. Returns (healthy, duration, error_msg)."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", filepath],
            capture_output=True, text=True, timeout=30
        )
        duration_str = result.stdout.strip()
        stderr = result.stderr.strip()
        if duration_str:
            try:
                duration = float(duration_str)
                if duration > 0:
                    return True, duration, None
                return False, 0, "Duration is 0"
            except ValueError:
                return False, 0, f"Invalid duration: {duration_str}"
        return False, 0, stderr or "No duration returned"
    except subprocess.TimeoutExpired:
        return False, 0, "ffprobe timed out"
    except FileNotFoundError:
        return False, 0, "ffprobe not found — is ffmpeg installed and in PATH?"
    except Exception as e:
        return False, 0, str(e)


def format_duration(seconds):
    """Format seconds as HH:MM:SS."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def format_size(bytes_val):
    """Format bytes as human-readable size."""
    if bytes_val >= 1024 * 1024 * 1024:
        return f"{bytes_val / (1024**3):.2f} GB"
    if bytes_val >= 1024 * 1024:
        return f"{bytes_val / (1024**2):.1f} MB"
    if bytes_val >= 1024:
        return f"{bytes_val / 1024:.0f} KB"
    return f"{bytes_val} B"


def scan_files(files):
    """Probe all files and return categorized results."""
    healthy = []
    corrupt = []
    errors = []

    for i, filepath in enumerate(files, 1):
        fname = os.path.basename(filepath)
        rel_dir = os.path.basename(os.path.dirname(filepath))
        label = f"{rel_dir}/{fname}"
        size = os.path.getsize(filepath)

        print(f"  [{i}/{len(files)}] {label} ({format_size(size)})...", end=" ", flush=True)

        ok, duration, err = probe_file(filepath)
        if ok:
            print(f"OK ({format_duration(duration)})")
            healthy.append({"path": filepath, "name": fname, "size": size, "duration": duration})
        else:
            print(f"CORRUPT — {err}")
            corrupt.append({"path": filepath, "name": fname, "size": size, "error": err})

    return healthy, corrupt


def attempt_remux(filepath):
    """Attempt to repair a file via ffmpeg remux. Returns (success, fixed_path)."""
    fixed_path = filepath + ".fixed.mkv"
    try:
        result = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", filepath, "-c", "copy", "-y", fixed_path],
            capture_output=True, text=True, timeout=120
        )
        if os.path.exists(fixed_path) and os.path.getsize(fixed_path) > 1000:
            # Verify the fix
            ok, duration, _ = probe_file(fixed_path)
            if ok:
                return True, fixed_path, duration
        # Fix failed
        if os.path.exists(fixed_path):
            os.remove(fixed_path)
        return False, None, 0
    except Exception:
        if os.path.exists(fixed_path):
            try:
                os.remove(fixed_path)
            except Exception:
                pass
        return False, None, 0


def archive_file(filepath, fname):
    """Move a file to archives/ using its mtime for the folder name."""
    mtime = os.path.getmtime(filepath)
    ts = datetime.fromtimestamp(mtime).strftime("archive_%m%d%Y-%H%M%S")
    archive_dir = os.path.join(ARCHIVE_BASE_DIR, ts)
    os.makedirs(archive_dir, exist_ok=True)
    dest = os.path.join(archive_dir, fname)
    # Avoid overwriting
    if os.path.exists(dest):
        base, ext = os.path.splitext(fname)
        dest = os.path.join(archive_dir, f"{base}_{int(mtime)}{ext}")
    shutil.move(filepath, dest)
    return os.path.basename(archive_dir)


def move_to_needs_repair(filepath, fname):
    """Move a file to archives/needs-repair/."""
    os.makedirs(NEEDS_REPAIR_DIR, exist_ok=True)
    dest = os.path.join(NEEDS_REPAIR_DIR, fname)
    if os.path.exists(dest):
        base, ext = os.path.splitext(fname)
        dest = os.path.join(NEEDS_REPAIR_DIR, f"{base}_{int(os.path.getmtime(filepath))}{ext}")
    shutil.move(filepath, dest)


def cmd_scan():
    """Scan and report — no changes made."""
    print("\n=== Scanning for media files ===\n")

    files = find_all_media_files()
    if not files:
        print("  No media files found in currently-capturing/ or archives/needs-repair/.")
        return

    print(f"  Found {len(files)} file(s) to check.\n")
    healthy, corrupt = scan_files(files)

    print(f"\n{'='*50}")
    print(f"  Healthy: {len(healthy)}")
    print(f"  Corrupt: {len(corrupt)}")
    if healthy:
        total_dur = sum(f["duration"] for f in healthy)
        total_size = sum(f["size"] for f in healthy)
        print(f"  Total duration: {format_duration(total_dur)}")
        print(f"  Total size: {format_size(total_size)}")
    if corrupt:
        print(f"\n  Corrupt files:")
        for f in corrupt:
            print(f"    {f['name']} ({format_size(f['size'])}) — {f['error']}")
    print()


def cmd_fix():
    """Scan, repair, and archive all files."""
    print("\n=== Scanning and repairing media files ===\n")

    files = find_all_media_files()
    if not files:
        print("  No media files found in currently-capturing/ or archives/needs-repair/.")
        return

    print(f"  Found {len(files)} file(s) to check.\n")
    healthy, corrupt = scan_files(files)

    # Archive healthy files that are in currently-capturing/ or needs-repair/
    archived = 0
    for f in healthy:
        filepath = f["path"]
        parent = os.path.basename(os.path.dirname(filepath))
        if parent in ("currently-capturing", "needs-repair"):
            try:
                folder = archive_file(filepath, f["name"])
                print(f"  Archived: {f['name']} → {folder}/")
                archived += 1
            except (PermissionError, OSError) as e:
                print(f"  WARNING: Could not archive {f['name']}: {e}")

    # Attempt repair on corrupt files
    repaired = 0
    unfixable = 0
    if corrupt:
        print(f"\n  Attempting repair on {len(corrupt)} corrupt file(s)...\n")
        for i, f in enumerate(corrupt, 1):
            print(f"  [{i}/{len(corrupt)}] Remuxing {f['name']}...", end=" ", flush=True)
            ok, fixed_path, duration = attempt_remux(f["path"])
            if ok:
                # Replace original with fixed version, then archive
                try:
                    os.remove(f["path"])
                    fname = f["name"]
                    final_path = os.path.join(os.path.dirname(f["path"]), fname)
                    shutil.move(fixed_path, final_path)
                    folder = archive_file(final_path, fname)
                    print(f"REPAIRED ({format_duration(duration)}) → {folder}/")
                    repaired += 1
                except (PermissionError, OSError) as e:
                    print(f"WARNING: {e}")
            else:
                # Move to needs-repair if not already there
                parent = os.path.basename(os.path.dirname(f["path"]))
                if parent != "needs-repair":
                    try:
                        move_to_needs_repair(f["path"], f["name"])
                        print(f"UNFIXABLE → archives/needs-repair/")
                    except (PermissionError, OSError) as e:
                        print(f"UNFIXABLE (could not move: {e})")
                else:
                    print("UNFIXABLE (already in needs-repair/)")
                unfixable += 1

    print(f"\n{'='*50}")
    print(f"  Archived: {archived}  |  Repaired: {repaired}  |  Unfixable: {unfixable}")
    print()


def cmd_clean_needs_repair():
    """List and optionally delete files in needs-repair/."""
    print("\n=== archives/needs-repair/ ===\n")

    files = find_media_files(NEEDS_REPAIR_DIR)
    if not files:
        print("  No files in needs-repair/.")
        return

    total_size = 0
    for i, filepath in enumerate(files, 1):
        fname = os.path.basename(filepath)
        size = os.path.getsize(filepath)
        total_size += size
        mtime = datetime.fromtimestamp(os.path.getmtime(filepath)).strftime("%Y-%m-%d %H:%M")
        print(f"  {i}. {fname}  ({format_size(size)}, {mtime})")

    print(f"\n  Total: {len(files)} file(s), {format_size(total_size)}")
    print(f"\n  [D] Delete all unfixable files")
    print(f"  [R] Re-attempt repair on all")
    print(f"  [Enter] Return to menu\n")

    choice = input("  Choice: ").strip().lower()
    if choice == 'd':
        confirm = input(f"  Delete {len(files)} file(s)? This cannot be undone. [y/N]: ").strip().lower()
        if confirm == 'y':
            for filepath in files:
                try:
                    os.remove(filepath)
                    print(f"  Deleted: {os.path.basename(filepath)}")
                except Exception as e:
                    print(f"  WARNING: {e}")
            print("  Done.")
        else:
            print("  Cancelled.")
    elif choice == 'r':
        print()
        for i, filepath in enumerate(files, 1):
            fname = os.path.basename(filepath)
            print(f"  [{i}/{len(files)}] Remuxing {fname}...", end=" ", flush=True)
            ok, fixed_path, duration = attempt_remux(filepath)
            if ok:
                try:
                    os.remove(filepath)
                    final_path = os.path.join(NEEDS_REPAIR_DIR, fname)
                    shutil.move(fixed_path, final_path)
                    folder = archive_file(final_path, fname)
                    print(f"REPAIRED ({format_duration(duration)}) → {folder}/")
                except (PermissionError, OSError) as e:
                    print(f"WARNING: {e}")
            else:
                print("Still unfixable.")


def interactive_menu():
    """Interactive menu for file maintenance."""
    while True:
        print(f"\n=== FISHTANK Capture File Maintenance ===")
        print(f"  Capture dir:  {CAPTURE_DIR}")
        print(f"  Archive dir:  {ARCHIVE_BASE_DIR}")
        print(f"  Needs repair: {NEEDS_REPAIR_DIR}\n")
        print(f"  1. Scan all files (report only, no changes)")
        print(f"  2. Scan, repair, and archive all files")
        print(f"  3. Manage needs-repair/ folder")
        print(f"  0. Exit\n")

        choice = input("  Choice: ").strip()

        if choice == '1':
            cmd_scan()
            input("  Press Enter to continue...")
        elif choice == '2':
            cmd_fix()
            input("  Press Enter to continue...")
        elif choice == '3':
            cmd_clean_needs_repair()
        elif choice == '0':
            print("  Bye.")
            break
        else:
            print("  Invalid choice.")


if __name__ == "__main__":
    os.makedirs(CAPTURE_DIR, exist_ok=True)
    os.makedirs(NEEDS_REPAIR_DIR, exist_ok=True)

    if len(sys.argv) > 1:
        arg = sys.argv[1].lower()
        if arg == "--scan":
            cmd_scan()
        elif arg == "--fix":
            cmd_fix()
        else:
            print(f"Unknown option: {arg}")
            print("Usage: python fix_captures.py [--scan | --fix]")
            sys.exit(1)
    else:
        interactive_menu()
