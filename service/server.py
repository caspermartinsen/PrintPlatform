import contextlib
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import sqlite3
import subprocess
import threading
import time
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from telemetry import Telemetry
from print_plan import analyze, select, mapping, live_ready


ACTIVE = ('starting', 'submitted', 'unknown')
MAX_UPLOAD = 100 * 1024 * 1024


def validate(path):
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > 4096 or sum(entry.file_size for entry in entries) > 512 * 1024 * 1024:
                raise ValueError('Archive too large')
            names = set()
            for entry in entries:
                name = entry.filename
                if (name in names or '\\' in name or PurePosixPath(name).is_absolute()
                        or '..' in PurePosixPath(name).parts or entry.flag_bits & 1
                        or (entry.external_attr >> 16) & 0o170000 == 0o120000):
                    raise ValueError('Unsafe archive member')
                names.add(name)
            if '[Content_Types].xml' not in names or 'Metadata/plate_1.gcode' not in names:
                raise ValueError('Requires a sliced single-plate 3MF with plate_1.gcode')
            if any(re.fullmatch(r'Metadata/plate_(?!1\.)\d+\.gcode', name) for name in names):
                raise ValueError('Multiple plates are not supported')
            for entry in entries:
                with archive.open(entry) as source:
                    while source.read(65536):
                        pass
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError) as error:
        raise ValueError('Invalid archive') from error


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.guard = threading.RLock()
        self.lock = (self.root / 'service.lock').open('a')
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with self.db() as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, name TEXT, sha256 TEXT, state TEXT, created REAL)')
            connection.execute("UPDATE jobs SET state='unknown' WHERE state='starting'")
            columns = {row[1] for row in connection.execute('PRAGMA table_info(jobs)')}
            for column in ('selection', 'plan', 'execution_mode'):
                if column not in columns:
                    connection.execute('ALTER TABLE jobs ADD COLUMN ' + column + ' TEXT')
        for temporary in self.root.glob('*.params.json'):
            temporary.unlink()
        for temporary in self.root.glob('*.part'):
            temporary.unlink()

    @contextlib.contextmanager
    def db(self):
        with self.guard:
            connection = sqlite3.connect(self.root / 'jobs.sqlite')
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA synchronous=FULL')
            try:
                with connection:
                    yield connection
            finally:
                connection.close()

    def jobs(self):
        with self.db() as connection:
            return [dict(row) for row in connection.execute('SELECT * FROM jobs ORDER BY created LIMIT 1000')]

    def state(self, job_id, state):
        with self.db() as connection:
            connection.execute('UPDATE jobs SET state=? WHERE id=?', (state, job_id))

    def start(self, job_id, selection=None, execution_mode=None):
        update_lock = (self.root / 'update.lock').open('a')
        try:
            fcntl.flock(update_lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            self.reserve(job_id, selection, execution_mode)
        except (OSError, ValueError):
            update_lock.close()
            raise ValueError('Service is updating or job cannot be started')
        threading.Thread(target=self.run_locked, args=(job_id, update_lock), daemon=True).start()

    def run_locked(self, job_id, update_lock):
        try:
            self.run(job_id)
        finally:
            update_lock.close()

    def reserve(self, job_id, selection, execution_mode):
        with self.db() as connection:
            job = connection.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not job or job['state'] != 'queued':
                raise ValueError('Job is not queued; never retry an ambiguous start')
            if connection.execute("SELECT 1 FROM jobs WHERE state IN ('starting','submitted','unknown')").fetchone():
                raise ValueError('Printer reserved; reconcile the active job first')
            connection.execute("UPDATE jobs SET state='starting',selection=?,execution_mode=? WHERE id=?", (json.dumps(selection) if selection else None, execution_mode or os.environ.get('PRINT_MODE','simulate'), job_id))

    def run(self, job_id):
        state = 'unknown'
        try:
            path = self.root / (job_id + '.gcode.3mf')
            with self.db() as connection:
                job = connection.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
                expected = job['sha256']
            with path.open('rb') as source:
                digest = hashlib.sha256()
                for chunk in iter(lambda: source.read(65536), b''):
                    digest.update(chunk)
                if digest.hexdigest() != expected:
                    raise ValueError('File changed')
            selection = json.loads(job['selection']) if job['selection'] else None
            plan = mapping(selection) if selection else {}
            with self.db() as connection:
                connection.execute('UPDATE jobs SET plan=? WHERE id=?', (json.dumps(plan), job_id))
            if (job['execution_mode'] or os.environ.get('PRINT_MODE', 'simulate')) == 'simulate':
                state = 'simulated'
            else:
                repo = Path(os.environ['BAMBU_REPO']).resolve()
                params = json.loads(Path(os.environ['BAMBU_PARAMS']).read_text())
                if not isinstance(params, dict):
                    raise ValueError('Invalid params')
                for field in ('config_filename', 'dst_file', 'ftp_file_md5'):
                    params.pop(field, None)
                params.update(filename=str(path), dev_id=os.environ['BAMBU_DEV_ID'],
                              dev_ip=os.environ['BAMBU_DEV_IP'], password=os.environ['BAMBU_ACCESS_CODE'],
                              ftp_file=path.name, plate_index=1, task_name=job_id, project_name=job_id)
                params.update(plan)
                params_path = self.root / (job_id + '.params.json')
                params_path.write_text(json.dumps(params))
                command = [str(repo / 'tools/plugin_runner.sh'), '--abi', '02.05.02',
                           '--plugin-path', str(repo / 'build/libbambu_networking.so'),
                           '--action', 'local_print', '--params-json', str(params_path),
                           '--gcode-3mf', str(path), '--dev-id', os.environ['BAMBU_DEV_ID'],
                           '--dev-ip', os.environ['BAMBU_DEV_IP'],
                           '--access-code', os.environ['BAMBU_ACCESS_CODE'],
                           '--cert-file', os.environ['BAMBU_CERT'], '--cert-lan-only', '0',
                           '--connect-settle-ms', '15000', '--timeout', '180', '--no-watchdog-defeat']
                environment = {key: value for key, value in os.environ.items()
                               if key not in ('API_TOKEN', 'BAMBU_ACCESS_CODE')}
                with subprocess.Popen(command, cwd=repo, env=environment, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, start_new_session=True) as process:
                    try:
                        result = process.wait(timeout=240)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                        result = -1
                state = 'submitted' if result == 0 else 'unknown'
        except Exception:
            state = 'unknown'
        finally:
            (self.root / (job_id + '.params.json')).unlink(missing_ok=True)
            self.state(job_id, state)


class Handler(BaseHTTPRequestHandler):
    server_version = 'PrintService'

    def log_message(self, *_args):
        pass

    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def dispatch(self):
        self.connection.settimeout(15)
        if not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + self.server.token):
            self.reply(401, {'error': 'Unauthorized'})
            return
        try:
            self.route()
        except (ValueError, TypeError, KeyError, TimeoutError, OSError):
            self.reply(400, {'error': 'Invalid request, file, or storage operation'})

    def body(self, limit):
        lengths = self.headers.get_all('Content-Length', [])
        if self.headers.get('Transfer-Encoding') or len(lengths) != 1:
            raise ValueError('Content-Length required')
        length = int(lengths[0])
        if not 0 < length <= limit:
            raise ValueError('Invalid length')
        return length

    def route(self):
        store = self.server.store
        if self.command == 'GET' and self.path == '/v1/status':
            self.reply(200, {'mode': os.environ.get('PRINT_MODE', 'simulate'),
                             'remote_live_enabled': os.environ.get('ALLOW_REMOTE_LIVE','0') == '1' and os.environ.get('PRINT_MODE') == 'live',
                             'printer': self.server.telemetry.snapshot() if hasattr(self.server, 'telemetry') else {'state': 'unknown'},
                             'jobs': store.jobs(), 'automatic_start': False})
        elif self.command == 'GET' and self.path == '/v1/jobs':
            self.reply(200, {'jobs': store.jobs()})
        elif self.command == 'POST' and self.path == '/v1/jobs':
            name = self.headers.get('X-Filename', '')
            if not re.fullmatch(r'[A-Za-z0-9_. -]{1,120}\.gcode\.3mf', name):
                raise ValueError('Invalid filename')
            length = self.body(MAX_UPLOAD)
            if len(store.jobs()) >= 1000 or sum(item.stat().st_size for item in store.root.iterdir()) + length > 2 * 1024**3:
                self.reply(507, {'error': 'Storage quota reached'})
                return
            job_id = str(uuid.uuid4())
            temporary = store.root / (job_id + '.part')
            destination = store.root / (job_id + '.gcode.3mf')
            try:
                digest = hashlib.sha256()
                with temporary.open('xb') as target:
                    remaining = length
                    while remaining:
                        chunk = self.rfile.read(min(65536, remaining))
                        if not chunk:
                            raise ValueError('Incomplete upload')
                        target.write(chunk)
                        digest.update(chunk)
                        remaining -= len(chunk)
                    target.flush()
                    os.fsync(target.fileno())
                validate(temporary)
                temporary.rename(destination)
                directory_fd = os.open(store.root, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
                with store.db() as connection:
                    connection.execute('INSERT INTO jobs (id,name,sha256,state,created) VALUES (?,?,?,?,?)',
                                       (job_id, name, digest.hexdigest(), 'queued', time.time()))
            finally:
                temporary.unlink(missing_ok=True)
            self.reply(201, {'id': job_id, 'state': 'queued', 'sha256': digest.hexdigest()})
        elif self.command == 'POST' and re.fullmatch(r'/v1/jobs/[0-9a-f-]{36}/(start|simulate|remote-start|resolve)', self.path):
            payload = json.loads(self.rfile.read(self.body(4096)))
            job_id, action = self.path.split('/')[3:]
            if action in ('start', 'simulate', 'remote-start'):
                execution_mode = None
                if action == 'remote-start':
                    execution_mode = payload.get('execution_mode')
                    if execution_mode not in ('simulate','live') or not payload.get('selection'):
                        raise ValueError('Explicit mode and selection required')
                    if execution_mode == 'live' and (os.environ.get('ALLOW_REMOTE_LIVE','0') != '1' or os.environ.get('PRINT_MODE') != 'live' or payload.get('confirm_physical') != 'START PRINT'):
                        self.reply(409, {'error':'Physical printing is disabled or not confirmed'})
                        return
                if action == 'simulate' and os.environ.get('PRINT_MODE', 'simulate') != 'simulate':
                    self.reply(409, {'error': 'Simulation endpoint refuses live mode'})
                    return
                selection = None
                if payload.get('selection') is not None:
                    choice = payload['selection']
                    current = self.server.telemetry.snapshot()
                    selection = select(analyze(store.root / (job_id + '.gcode.3mf')), current,
                                       {**choice, 'bed_clear': payload.get('printer_ready'), 'accept_differences': True})
                    if any(selection.get(key) != choice.get(key) for key in ('material', 'name', 'color', 'filament_id')):
                        raise ValueError('AMS contents changed after approval')
                    if execution_mode == 'live':
                        live_ready(analyze(store.root / (job_id + '.gcode.3mf')), current, selection)
                if payload.get('confirm_job_id') != job_id or payload.get('printer_ready') is not True:
                    raise ValueError('Explicit confirmation required')
                try:
                    store.start(job_id, selection, execution_mode)
                except ValueError as error:
                    self.reply(409, {'error': str(error)})
                    return
                self.reply(202, {'id': job_id, 'state': 'starting'})
            else:
                if payload != {'printer_checked_idle': True}:
                    raise ValueError('Check printer first')
                with store.db() as connection:
                    changed = connection.execute("UPDATE jobs SET state='resolved' WHERE id=? AND state IN ('submitted','unknown')", (job_id,)).rowcount
                self.reply(200 if changed else 409, {'resolved': bool(changed)})
        else:
            self.reply(404, {'error': 'Not found'})


def main():
    os.umask(0o077)
    token = os.environ.get('API_TOKEN', '')
    if len(token) < 32:
        raise SystemExit('API_TOKEN must contain at least 32 characters')
    if os.environ.get('PRINT_MODE', 'simulate') not in ('simulate', 'live'):
        raise SystemExit('Invalid PRINT_MODE')
    store = Store(os.environ.get('DATA_DIR', '/var/lib/print-service'))
    server = HTTPServer(('127.0.0.1', int(os.environ.get('PORT', '8787'))), Handler)
    server.token = token
    server.store = store
    server.telemetry = Telemetry()
    server.telemetry.start()
    server.serve_forever()


if __name__ == '__main__':
    main()
