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
install -d -m 755 /opt/print-platform/deploy
install -m 644 "$project_dir/deploy/updater.py" /opt/print-platform/deploy/updater.py
install -d -m 750 -o root -g print-service /etc/print-service
install -d -m 700 -o print-service -g print-service /var/lib/print-service
if [ ! -e /var/lib/print-service/update.lock ]; then
    install -m 600 -o print-service -g print-service /dev/null /var/lib/print-service/update.lock
fi
install -m 644 "$project_dir/service/server.py" /opt/print-platform/service/server.py
install -m 644 "$project_dir/service/telemetry.py" /opt/print-platform/service/telemetry.py
install -m 644 "$project_dir/service/agent.py" /opt/print-platform/service/agent.py
install -m 644 "$project_dir/service/print_plan.py" /opt/print-platform/service/print_plan.py
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
install -m 644 "$project_dir/deploy/print-agent.service" /etc/systemd/system/print-agent.service
install -m 644 "$project_dir/deploy/print-updater.service" /etc/systemd/system/print-updater.service
install -m 644 "$project_dir/deploy/print-updater.timer" /etc/systemd/system/print-updater.timer
systemctl daemon-reload
echo 'Installed, not started. Check configuration, then enable as described in README.'
