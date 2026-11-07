#!/bin/sh
set -eu
if [ "$(id -u)" -ne 0 ]; then
    echo 'Run with sudo sh deploy/install.sh'
    exit 1
fi
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
test -x /usr/bin/python3
id print-service >/dev/null 2>&1 || useradd --system --home-dir /var/lib/print-service --shell /usr/sbin/nologin print-service
install -d -m 755 /opt/print-platform/service
install -d -m 700 /etc/print-service
install -d -m 700 -o print-service -g print-service /var/lib/print-service
install -m 644 "$project_dir/service/server.py" /opt/print-platform/service/server.py
if [ ! -e /etc/print-service/environment ]; then
    install -m 600 "$project_dir/deploy/environment.example" /etc/print-service/environment
    /usr/bin/python3 - <<'PY'
from pathlib import Path
import secrets
path = Path('/etc/print-service/environment')
path.write_text(path.read_text().replace('REPLACE_WITH_RANDOM_TOKEN_MINIMUM_32_CHARACTERS', secrets.token_urlsafe(48)))
PY
fi
install -m 644 "$project_dir/deploy/print-service.service" /etc/systemd/system/print-service.service
systemctl daemon-reload
echo 'Installed, not started. Check configuration, then enable as described in README.'
