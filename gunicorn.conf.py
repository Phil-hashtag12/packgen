# gunicorn.conf.py — Production server configuration for PackGen
import os
import multiprocessing

# ── Binding ───────────────────────────────────────────────────────────────────
bind        = f"0.0.0.0:{os.environ.get('PORT', '5050')}"
backlog     = 512

# ── Workers ───────────────────────────────────────────────────────────────────
# PackGen is I/O-heavy (PDF processing, Anthropic API calls).
# Use 2-4 workers + threads per worker for good concurrency.
# NOTE: Because fitz doc objects live in the process-level _doc_cache in
# store.py, all requests for a given job must hit the same worker.
# We use sticky routing via the jid — but since we now use Redis for job
# state, a cache-miss just re-opens from disk, which is fine.
workers     = int(os.environ.get("GUNICORN_WORKERS",
                  max(2, min(4, multiprocessing.cpu_count()))))
worker_class = "gthread"
threads     = int(os.environ.get("GUNICORN_THREADS", "4"))

# ── Timeouts ──────────────────────────────────────────────────────────────────
timeout          = 300   # 5 min — long-running extract/classify jobs
graceful_timeout = 30
keepalive        = 5

# ── Requests ─────────────────────────────────────────────────────────────────
# Recycle workers to prevent memory leaks from PyMuPDF
max_requests         = 200
max_requests_jitter  = 40

# ── Logging ───────────────────────────────────────────────────────────────────
accesslog  = "-"     # stdout
errorlog   = "-"     # stderr
loglevel   = os.environ.get("LOG_LEVEL", "info")
access_log_format = '%(h)s "%(r)s" %(s)s %(b)sB %(D)sμs'

# ── Process naming ────────────────────────────────────────────────────────────
proc_name = "packgen"

# ── Security ──────────────────────────────────────────────────────────────────
limit_request_line   = 4096
limit_request_fields = 100

# ── Hooks ─────────────────────────────────────────────────────────────────────
def on_starting(server):
    server.log.info("PackGen starting — %d workers × %d threads", workers, threads)

def worker_exit(server, worker):
    # Close any open fitz docs in this worker's process cache
    try:
        from store import _doc_cache, close_job_docs
        for jid in list(_doc_cache.keys()):
            close_job_docs(jid)
    except Exception:
        pass
