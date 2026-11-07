import argparse
import getpass
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess


parser = argparse.ArgumentParser(description='Configure printer telemetry without enabling physical print')
parser.add_argument('--serial', required=True)
parser.add_argument('--ip', required=True)
parser.add_argument('--ca', required=True)
args = parser.parse_args()
if os.geteuid() != 0:
    raise SystemExit('Run with sudo')
environment = Path('/etc/print-service/environment')
lines = environment.read_text().splitlines()
settings = dict(line.split('=', 1) for line in lines if line and not line.startswith('#'))
if settings.get('PRINT_MODE') != 'simulate':
    raise SystemExit('This setup requires PRINT_MODE=simulate')
context = ssl.create_default_context(cafile=args.ca)
with socket.create_connection((args.ip, 8883), timeout=5) as raw:
    with context.wrap_socket(raw, server_hostname=args.serial):
        pass
code = getpass.getpass('Printer access code (stored locally, not displayed): ')
if not code.isalnum() or not 4 <= len(code) <= 64:
    raise SystemExit('Invalid access code format')
destination = Path('/var/lib/print-service/config/cert/printer.cer')
destination.parent.mkdir(parents=True, exist_ok=True)
shutil.copyfile(args.ca, destination)
destination.chmod(0o644)
updates = {'BAMBU_DEV_ID': args.serial, 'BAMBU_DEV_IP': args.ip,
           'BAMBU_ACCESS_CODE': code, 'BAMBU_TELEMETRY_CA': str(destination),
           'TELEMETRY_ENABLED': '1'}
output = []
for line in lines:
    key = line.split('=', 1)[0]
    if key in updates:
        output.append(key + '=' + updates.pop(key))
    else:
        output.append(line)
output.extend(key + '=' + value for key, value in updates.items())
temporary = environment.with_suffix('.tmp')
descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(descriptor, 'w') as target:
    target.write('\n'.join(output) + '\n')
    target.flush()
    os.fsync(target.fileno())
temporary.replace(environment)
subprocess.run(['systemctl', 'restart', 'print-service'], check=True)
print('Telemetry enabled. Physical printing remains simulated.')
