"""
Phusion Passenger entry point (cPanel "Setup Python App", e.g. NetPoa).

cPanel loads this file from the application root and serves the WSGI callable
named ``application``. It only makes the project importable and hands over to
config.wsgi, the same entry point Gunicorn uses on Render, so both hosts run
identical settings and middleware. See docs/deployment/netpoa_runbook.md.

Nothing here is host-specific: no paths, domains or credentials. Configuration
comes from the environment or from the private .env file in this directory
(read by config.settings), exactly as on every other host.
"""

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

# Passenger may start the interpreter from another working directory.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# setdefault: an explicitly configured settings module still wins.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

from config.wsgi import application  # noqa: E402  (must follow the path set-up above)

__all__ = ["application"]
