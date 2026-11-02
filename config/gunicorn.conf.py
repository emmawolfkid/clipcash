"""
Gunicorn settings for Render (see docs/deployment/render.md).

    gunicorn --config config/gunicorn.conf.py config.wsgi:application

Every value can be overridden through the environment; nothing here is secret.
"""

import os

# Render routes traffic to the port it provides in $PORT (default 10000).
bind = f"0.0.0.0:{os.environ.get('PORT', '10000')}"

# Synchronous workers: the views and the ledger rely on short database
# transactions, not async I/O. Two workers fit a 512 MB instance; raise
# WEB_CONCURRENCY only after checking memory on a larger instance.
workers = int(os.environ.get("WEB_CONCURRENCY", "2"))
worker_class = "sync"
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "60"))
graceful_timeout = 30
keepalive = 5

# Recycle workers periodically to bound memory growth.
max_requests = 1000
max_requests_jitter = 100

# Access logs stay OFF: account activation, email confirmation and password
# reset links carry single-use tokens in the URL path, and request lines would
# copy them into Render's log storage. Errors still go to stderr.
accesslog = None
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")
