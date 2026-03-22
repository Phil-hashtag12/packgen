"""
cleanup.py — Background file cleanup for PackGen.

Runs as a daemon thread.  Every CLEANUP_INTERVAL seconds it:
  1. Deletes upload/output directories older than FILE_TTL seconds
  2. Closes and removes fitz docs for expired jobs from the doc cache
  3. Logs disk usage stats

Safe to call from multiple threads — uses a simple lock for the
job-doc cache (imported from store).
"""

import os, shutil, time, threading, logging
from pathlib import Path
from store import close_job_docs, _doc_cache

log = logging.getLogger(__name__)

UPLOAD_DIR       = Path(os.environ.get("UPLOAD_DIR",  "uploads"))
OUTPUT_DIR       = Path(os.environ.get("OUTPUT_DIR",  "outputs"))
SESSION_DIR      = Path(os.environ.get("SESSION_DIR", "sessions"))
FILE_TTL         = int(os.environ.get("FILE_TTL_SECONDS",    str(8 * 3600)))   # 8 h
SESSION_TTL      = int(os.environ.get("SESSION_TTL_SECONDS", str(30 * 86400))) # 30 days
CLEANUP_INTERVAL = int(os.environ.get("CLEANUP_INTERVAL_SECONDS", "600"))      # 10 min

_lock     = threading.Lock()
_started  = False


def _dir_age(path: Path) -> float:
    """Return age of directory in seconds based on mtime."""
    try:
        return time.time() - path.stat().st_mtime
    except Exception:
        return 0


def _disk_usage(path: Path) -> int:
    """Return total size of a directory tree in bytes."""
    total = 0
    try:
        for f in path.rglob("*"):
            try:
                total += f.stat().st_size
            except Exception:
                pass
    except Exception:
        pass
    return total


def cleanup_once():
    """Run a single cleanup pass. Safe to call manually."""
    with _lock:
        now   = time.time()
        freed = 0

        # ── Upload directories ────────────────────────────────────────────────
        for jid_dir in list(UPLOAD_DIR.iterdir()) if UPLOAD_DIR.exists() else []:
            if not jid_dir.is_dir():
                continue
            age = _dir_age(jid_dir)
            if age > FILE_TTL:
                size = _disk_usage(jid_dir)
                try:
                    shutil.rmtree(jid_dir)
                    freed += size
                    log.debug("Deleted upload dir %s (age %.0fh)", jid_dir.name, age/3600)
                except Exception as e:
                    log.warning("Could not delete %s: %s", jid_dir, e)

        # ── Output directories ────────────────────────────────────────────────
        for jid_dir in list(OUTPUT_DIR.iterdir()) if OUTPUT_DIR.exists() else []:
            if not jid_dir.is_dir():
                continue
            age = _dir_age(jid_dir)
            if age > FILE_TTL:
                size = _disk_usage(jid_dir)
                try:
                    shutil.rmtree(jid_dir)
                    freed += size
                    log.debug("Deleted output dir %s (age %.0fh)", jid_dir.name, age/3600)
                except Exception as e:
                    log.warning("Could not delete %s: %s", jid_dir, e)

        # ── Expired fitz doc caches ───────────────────────────────────────────
        expired_jids = []
        for jid, cache in list(_doc_cache.items()):
            # If the job's upload dir is gone, the docs are useless
            if not (UPLOAD_DIR / jid).exists():
                expired_jids.append(jid)
        for jid in expired_jids:
            close_job_docs(jid)
            log.debug("Closed docs for expired job %s", jid)

        # ── Session files ─────────────────────────────────────────────────────
        deleted_sessions = 0
        for sf in list(SESSION_DIR.glob("*.json")) if SESSION_DIR.exists() else []:
            age = time.time() - sf.stat().st_mtime
            if age > SESSION_TTL:
                try:
                    sf.unlink()
                    deleted_sessions += 1
                except Exception as e:
                    log.warning("Could not delete session %s: %s", sf, e)

        # ── Log stats ─────────────────────────────────────────────────────────
        if freed or deleted_sessions or expired_jids:
            log.info(
                "Cleanup: freed %.1f MB from files, %d sessions deleted, "
                "%d doc caches closed",
                freed / 1024 / 1024, deleted_sessions, len(expired_jids),
            )


def _cleanup_loop():
    while True:
        try:
            cleanup_once()
        except Exception as e:
            log.error("Cleanup loop error: %s", e)
        time.sleep(CLEANUP_INTERVAL)


def start_cleanup_thread():
    """Start the background cleanup daemon thread (call once at app startup)."""
    global _started
    if _started:
        return
    _started = True
    t = threading.Thread(target=_cleanup_loop, daemon=True, name="cleanup")
    t.start()
    log.info("Cleanup thread started (interval=%ds, file_ttl=%dh)",
             CLEANUP_INTERVAL, FILE_TTL // 3600)
