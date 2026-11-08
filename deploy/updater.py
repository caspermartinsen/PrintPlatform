import fcntl
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tarfile
import time
import urllib.request


FILES = ('server.py', 'telemetry.py', 'agent.py', 'print_plan.py')


def candidate():
    repository = 'https://github.com/caspermartinsen/PrintPlatform.git'
    output = subprocess.check_output(['git', 'ls-remote', repository, 'refs/heads/main'],
                                     timeout=30, env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'}, stderr=subprocess.DEVNULL).decode()
    revision = output.split()[0]
    if not re.fullmatch('[0-9a-f]{40}', revision):
        raise ValueError('Invalid revision')
    return revision


def download(revision):
    with urllib.request.urlopen('https://codeload.github.com/caspermartinsen/PrintPlatform/tar.gz/' + revision, timeout=45) as response:
        data = response.read(32 * 1024**2 + 1)
    if len(data) > 32 * 1024**2:
        raise ValueError('Archive too large')
    files = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
        for member in archive:
            parts = member.name.split('/')
            if len(parts) == 3 and parts[1] == 'service' and parts[2] in FILES:
                if not member.isfile() or member.size > 1024**2 or parts[2] in files:
                    raise ValueError('Invalid source member')
                content = archive.extractfile(member).read()
                compile(content, parts[2], 'exec')
                files[parts[2]] = content
    if set(files) != set(FILES):
        raise ValueError('Incomplete update')
    return files


class Updater:
    def __init__(self, root='/var/lib/print-updater', service='/opt/print-platform/service', data='/var/lib/print-service'):
        self.root = Path(root)
        self.service = Path(service)
        self.data = Path(data)
        self.root.mkdir(parents=True, exist_ok=True)

    def systemctl(self, *args):
        return subprocess.run(['systemctl', *args], check=True, timeout=40, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def active(self, unit):
        return subprocess.run(['systemctl', 'is-active', '--quiet', unit], timeout=10).returncode == 0

    def health(self):
        pid = subprocess.check_output(['systemctl', 'show', 'print-service', '-p', 'MainPID', '--value'], timeout=10).decode().strip()
        settings = dict(item.split('=', 1) for item in Path('/proc/' + pid + '/environ').read_bytes().decode().split('\0') if '=' in item)
        request = urllib.request.Request('http://127.0.0.1:' + settings.get('PORT', '8787') + '/v1/status',
                                         headers={'Authorization': 'Bearer ' + settings['API_TOKEN']})
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.load(response)

    def mark(self, revision, status):
        temporary = self.root / 'version.tmp'
        temporary.write_text(json.dumps({'revision': revision, 'status': status, 'time': time.time()}))
        temporary.replace(self.root / 'version.json')

    def apply(self, revision, files):
        with (self.root / 'updater.lock').open('a') as singleton:
            fcntl.flock(singleton, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if all((self.service / name).read_bytes() == content for name, content in files.items()):
                self.mark(revision, 'current')
                return 'unchanged'
            if not self.active('print-service'):
                return 'deferred: service is stopped'
            with (self.data / 'update.lock').open('a') as guard:
                try:
                    fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return 'deferred: print command active'
                agent_was_active = self.active('print-agent')
                self.systemctl('stop', 'print-agent')
                stopped = False
                backup = self.root / 'previous-service'
                try:
                    status = self.health()
                    if any(job['state'] in ('starting', 'submitted', 'unknown') for job in status['jobs']):
                        return 'deferred: unresolved print job'
                    if status.get('printer', {}).get('state') in ('RUNNING', 'PREPARE', 'PAUSE', 'SLICING'):
                        return 'deferred: printer busy'
                    if status.get('mode') == 'live' and (not status.get('printer', {}).get('connected') or status.get('printer', {}).get('stale')):
                        return 'deferred: live printer status unavailable'
                    journal = Path('/var/lib/print-agent')
                    if list(journal.glob('*.json')):
                        return 'deferred: agent has unacknowledged jobs'
                    if backup.exists():
                        shutil.rmtree(backup)
                    shutil.copytree(self.service, backup, ignore=shutil.ignore_patterns('__pycache__'))
                    self.systemctl('stop', 'print-service')
                    stopped = True
                    database = self.data / 'jobs.sqlite'
                    with sqlite3.connect(database) as source, sqlite3.connect(self.root / 'previous.sqlite') as target:
                        source.backup(target)
                    try:
                        for name, content in files.items():
                            path = self.service / name
                            temporary = path.with_suffix('.new')
                            temporary.write_bytes(content)
                            temporary.chmod(0o644)
                            temporary.replace(path)
                        self.systemctl('start', 'print-service')
                        for attempt in range(15):
                            try:
                                self.health()
                                break
                            except Exception:
                                time.sleep(1)
                        else:
                            raise RuntimeError('API healthcheck failed')
                    except Exception:
                        self.systemctl('stop', 'print-service')
                        for name in FILES:
                            shutil.copy2(backup / name, self.service / name)
                        for suffix in ('-journal', '-wal', '-shm'):
                            Path(str(database) + suffix).unlink(missing_ok=True)
                        shutil.copy2(self.root / 'previous.sqlite', database)
                        self.systemctl('start', 'print-service')
                        self.mark(revision, 'rolled_back')
                        raise
                    self.mark(revision, 'current')
                    return 'updated: ' + revision[:12]
                finally:
                    if stopped and not self.active('print-service'):
                        self.systemctl('start', 'print-service')
                    fcntl.flock(guard, fcntl.LOCK_UN)
                    if agent_was_active:
                        self.systemctl('start', 'print-agent')


def main():
    os.umask(0o077)
    updater = Updater()
    revision = candidate()
    marker = updater.root / 'version.json'
    if marker.exists() and json.loads(marker.read_text()).get('revision') == revision:
        return
    print(updater.apply(revision, download(revision)), flush=True)


if __name__ == '__main__':
    main()
