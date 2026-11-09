#!/usr/bin/env bash
# READ-ONLY NetPoa host compatibility probe for ClipCash.
#
#   bash scripts/netpoa/check_host.sh [smtp_host [smtp_port]]
#
# Run in cPanel > Terminal (or SSH) as the cPanel user. It changes nothing: no
# files are written, no package is installed, no cron entry is created and no
# database is contacted. The optional SMTP check opens a TCP connection, reads
# the server greeting and closes it; it never logs in or sends mail.
# Copy the output into docs/deployment/netpoa_compatibility.md, section 2.
set -u

section() { printf '\n== %s\n' "$1"; }

section "Operating system"
uname -srm 2>/dev/null
[ -r /etc/os-release ] && grep -E '^(PRETTY_NAME|VERSION_ID)=' /etc/os-release
if command -v ldd >/dev/null 2>&1; then
    echo "glibc: $(ldd --version 2>/dev/null | head -n 1)"
fi

section "Python interpreters (ClipCash is tested on 3.13; Django 5.2 needs 3.10+)"
found=0
for candidate in /opt/alt/python3*/bin/python3.* /usr/bin/python3.* /usr/local/bin/python3.*; do
    case "$candidate" in *-config|*m) continue ;; esac
    if [ -x "$candidate" ]; then
        echo "$candidate -> $("$candidate" -c 'import sys; print(sys.version.split()[0])' 2>/dev/null)"
        found=1
    fi
done
[ "$found" -eq 1 ] || echo "No interpreter found in the usual places; check cPanel > Setup Python App."

section "Passenger / Python app selector"
command -v passenger-config >/dev/null 2>&1 && passenger-config --version 2>/dev/null
command -v cloudlinux-selector >/dev/null 2>&1 && echo "cloudlinux-selector present" \
    || echo "cloudlinux-selector not on PATH (normal on some hosts; use the cPanel page)"

section "PostgreSQL client tools (needed for backups and the role scripts)"
for tool in psql pg_dump pg_restore; do
    if command -v "$tool" >/dev/null 2>&1; then
        echo "$tool: $("$tool" --version 2>/dev/null)"
    else
        echo "$tool: NOT FOUND"
    fi
done

section "Cron and locking"
command -v crontab >/dev/null 2>&1 && echo "crontab command present" || echo "crontab command NOT FOUND (use cPanel > Cron Jobs)"
command -v flock >/dev/null 2>&1 && echo "flock present" || echo "flock NOT FOUND (runs may overlap; still safe)"
command -v curl >/dev/null 2>&1 && echo "curl present" || echo "curl NOT FOUND"

section "Process limits for this shell (CloudLinux LVE limits are on cPanel > Resource Usage)"
echo "max user processes: $(ulimit -u 2>/dev/null)"
echo "virtual memory (KB): $(ulimit -v 2>/dev/null)"
echo "open files: $(ulimit -n 2>/dev/null)"
echo "CPUs visible: $(nproc 2>/dev/null)"

section "Disk usage of the home directory"
du -sh "$HOME" 2>/dev/null | cut -f1
quota -s 2>/dev/null | tail -n +3 || true

section "Outbound HTTPS (port 443) to the services ClipCash needs"
for host in pypi.org video.bunnycdn.com; do
    if command -v curl >/dev/null 2>&1; then
        code="$(curl -sS -o /dev/null -m 10 -w '%{http_code}' "https://$host/" 2>/dev/null)"
        echo "https://$host/ -> HTTP ${code:-no answer}"
    fi
done

if [ $# -ge 1 ]; then
    smtp_host="$1"; smtp_port="${2:-587}"
    section "SMTP reachability ($smtp_host:$smtp_port, greeting only, no login, no mail)"
    py=""
    for candidate in python3 /usr/bin/python3; do command -v "$candidate" >/dev/null 2>&1 && py="$candidate" && break; done
    if [ -n "$py" ]; then
        "$py" - "$smtp_host" "$smtp_port" <<'PY'
import socket, ssl, sys
host, port = sys.argv[1], int(sys.argv[2])
try:
    raw = socket.create_connection((host, port), timeout=10)
    sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host) if port == 465 else raw
    greeting = sock.recv(200).decode("ascii", "replace").strip().splitlines()[0]
    print("connected; greeting:", greeting[:80])
    sock.close()
except Exception as exc:
    print("NOT reachable:", type(exc).__name__, str(exc)[:120])
PY
    else
        echo "python3 not found for the SMTP probe"
    fi
fi

printf '\nDone. Nothing was changed.\n'
