import base64
import contextlib
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
sys.path.insert(1, str(Path(__file__).resolve().parents[1]))
from server import validate as validate_archive, safe_diagnostics
from print_plan import analyze, select, live_ready
from cloud.model_files import stl_info, three_mf_info
from cloud import slicing


class Platform(HTTPServer):
    def __init__(self, address, root, username, password, agent_token, public_url):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.browser_auth = 'Basic ' + base64.b64encode((username + ':' + password).encode()).decode()
        self.agent_auth = 'Bearer ' + agent_token
        self.public_url = public_url.rstrip('/')
        with self.db() as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, name TEXT, sha256 TEXT, state TEXT, created REAL)')
            connection.execute('CREATE TABLE IF NOT EXISTS telemetry (id INTEGER PRIMARY KEY, body TEXT, seen REAL)')
            columns = {row[1] for row in connection.execute('PRAGMA table_info(jobs)')}
            for column in ('analysis', 'selection', 'execution_mode', 'receipt','model_info','slice_options','slice_token','slice_error'):
                if column not in columns:
                    connection.execute('ALTER TABLE jobs ADD COLUMN ' + column + ' TEXT')
            if 'preview_ready' not in columns:
                connection.execute('ALTER TABLE jobs ADD COLUMN preview_ready INTEGER DEFAULT 0')
            connection.execute('CREATE TABLE IF NOT EXISTS slicer_health (id INTEGER PRIMARY KEY, seen REAL)')
        super().__init__(address, Handler)

    @contextlib.contextmanager
    def db(self):
        connection = sqlite3.connect(self.root / 'platform.sqlite')
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA synchronous=FULL')
        try:
            with connection:
                yield connection
        finally:
            connection.close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def respond(self, status, body, content_type='application/json', challenge=False):
        data = json.dumps(body).encode() if content_type == 'application/json' else body
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.send_header('Connection', 'close')
        if challenge:
            self.send_header('WWW-Authenticate', 'Basic realm="PrintPlatform", charset="UTF-8"')
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def body(self, maximum=8192):
        lengths = self.headers.get_all('Content-Length', [])
        if len(lengths) != 1 or self.headers.get('Transfer-Encoding'):
            raise ValueError('Content-Length required')
        size = int(lengths[0])
        if not 0 < size <= maximum:
            raise ValueError('Body too large')
        body = self.rfile.read(size)
        if len(body) != size:
            raise ValueError('Incomplete body')
        return body

    def dispatch(self):
        self.connection.settimeout(20)
        if self.command == 'GET' and self.path == '/healthz':
            self.respond(200, {'ok': True})
            return
        slicer = self.path.startswith('/slicer/')
        agent = self.path.startswith('/agent/')
        expected = ('Bearer ' + os.environ.get('SLICER_TOKEN','')) if slicer else self.server.agent_auth if agent else self.server.browser_auth
        if slicer and len(os.environ.get('SLICER_TOKEN','')) < 32:
            self.respond(503, {'error':'Sliceren er ikke konfigureret.'})
            return
        if not hmac.compare_digest(self.headers.get('Authorization', ''), expected):
            self.respond(401, {'error': 'Login required'}, challenge=not agent)
            return
        if self.command == 'POST' and not agent and not slicer and self.headers.get('Origin') != self.server.public_url:
            self.respond(403, {'error': 'Origin rejected'})
            return
        try:
            self.route(agent)
        except (ValueError, TypeError, KeyError) as error:
            self.respond(400, {'error': str(error) if isinstance(error,ValueError) else 'Invalid request'})
        except (OSError, sqlite3.Error):
            self.respond(503, {'error': 'Storage unavailable'})

    def route(self, agent):
        platform = self.server
        if slicing.route(self):
            return
        if self.command == 'GET' and not agent and self.path in ('/', '/app.js', '/style.css','/preview.js'):
            name, mime = {'/': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'application/javascript'), '/style.css': ('style.css', 'text/css'),'/preview.js':('built/preview.js','application/javascript')}[self.path]
            self.respond(200, (Path(__file__).parents[1] / 'frontend' / name).read_bytes(), mime)
        elif self.command == 'GET' and self.path == '/api/status':
            with platform.db() as connection:
                jobs = [dict(row) for row in connection.execute('SELECT * FROM jobs ORDER BY created DESC LIMIT 1000')]
                telemetry = connection.execute('SELECT * FROM telemetry WHERE id=1').fetchone()
                slicer_health = connection.execute('SELECT seen FROM slicer_health WHERE id=1').fetchone()
            seen = telemetry['seen'] if telemetry else None
            for job in jobs:
                job['analysis'] = json.loads(job['analysis']) if job['analysis'] else analyze(platform.root / (job['id'] + '.gcode.3mf')) if job['sha256'] else {'supported':False,'reason':'Afventer slicing.','filaments':[]}
                job['selection'] = json.loads(job['selection']) if job['selection'] else None
                for field in ('model_info','slice_options'):
                    job[field] = json.loads(job[field]) if job[field] else None
                job.pop('slice_token',None)
            self.respond(200, {'jobs': jobs, 'agent_online': seen is not None and time.time() - seen < 45,
                               'slicer_online':slicer_health is not None and time.time()-slicer_health['seen'] < 45,
                               'live_enabled': os.environ.get('ALLOW_LIVE_PRINT','0') == '1' and telemetry is not None and time.time()-seen < 45 and json.loads(telemetry['body']).get('remote_live_enabled') is True,
                               'last_seen': seen, 'pi': json.loads(telemetry['body']) if telemetry else None})
        elif self.command == 'POST' and self.path == '/api/jobs':
            name = self.headers.get('X-Filename', '')
            if not re.fullmatch(r'[A-Za-z0-9_. -]{1,120}\.(?:3mf|stl)', name, re.IGNORECASE):
                raise ValueError('Filename')
            with platform.db() as connection:
                count = connection.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
            if count >= 1000 or sum(item.stat().st_size for item in platform.root.iterdir()) > 2 * 1024**3:
                self.respond(507, {'error': 'Storage quota reached'})
                return
            job_id = str(uuid.uuid4())
            is_3mf = name.lower().endswith('.3mf') and not name.lower().endswith('.gcode.3mf')
            is_model = name.lower().endswith('.stl') or is_3mf
            path = platform.root / (job_id + ('.3mf' if is_3mf else '.stl' if is_model else '.gcode.3mf'))
            try:
                data = self.body((50 if is_model else 100) * 1024**2)
                with path.open('xb') as target:
                    target.write(data)
                    target.flush()
                    os.fsync(target.fileno())
                model_info = three_mf_info(data) if is_3mf else stl_info(data) if is_model else None
                if not is_model:
                    validate_archive(path)
                digest = hashlib.sha256(data).hexdigest()
                if model_info:
                    model_info['sha256']=digest
                with platform.db() as connection:
                    connection.execute('INSERT INTO jobs (id,name,sha256,state,created,analysis,model_info) VALUES (?,?,?,?,?,?,?)', (job_id, name, None if is_model else digest, 'model' if is_model else 'queued', time.time(), None if is_model else json.dumps(analyze(path)), json.dumps(model_info) if is_model else None))
            except Exception:
                path.unlink(missing_ok=True)
                raise
            self.respond(201, {'id': job_id,'state':'model' if is_model else 'queued'})
        elif self.command == 'POST' and re.fullmatch(r'/api/jobs/[a-f0-9-]{36}/(approve|resolve)', self.path):
            job_id = self.path.split('/')[3]
            payload = json.loads(self.body())
            if payload.get('confirm_job_id') != job_id:
                raise ValueError('Confirmation required')
            with platform.db() as connection:
                if self.path.endswith('/approve'):
                    job = connection.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
                    if not job or job['state'] != 'queued':
                        self.respond(409, {'error': 'Jobbet er allerede godkendt eller afsluttet.'})
                        return
                    telemetry = connection.execute('SELECT * FROM telemetry WHERE id=1').fetchone()
                    if not telemetry or time.time() - telemetry['seen'] > 45:
                        self.respond(409, {'error': 'Pi er offline.'})
                        return
                    analysis = analyze(platform.root / (job_id + '.gcode.3mf'))
                    if job['model_info'] and payload.get('preview_checked') is not True:
                        raise ValueError('Bekræft at du har kontrolleret det slicede preview.')
                    execution_mode = payload.get('execution_mode','simulate')
                    report = json.loads(telemetry['body'])
                    try:
                        if execution_mode not in ('simulate','live'):
                            raise ValueError('Ukendt printtilstand.')
                        selection = select(analysis, json.loads(telemetry['body']).get('printer', {}), payload)
                        if execution_mode == 'live':
                            if os.environ.get('ALLOW_LIVE_PRINT','0') != '1' or report.get('remote_live_enabled') is not True or report.get('plan_version') != 2 or payload.get('confirm_physical') != 'START PRINT':
                                raise ValueError('Fysisk printstart er ikke aktiveret eller bekræftet.')
                            live_ready(analysis, report.get('printer',{}), selection)
                    except ValueError as error:
                        self.respond(409, {'error': str(error)})
                        return
                    changed = connection.execute("UPDATE jobs SET state='approved',selection=?,analysis=?,execution_mode=? WHERE id=? AND state='queued'", (json.dumps(selection), json.dumps(analysis), execution_mode, job_id)).rowcount
                else:
                    target = connection.execute('SELECT state FROM jobs WHERE id=?',(job_id,)).fetchone()
                    if target and target['state']=='submitted' and payload.get('printer_checked_idle') is not True:
                        raise ValueError('Printer inspection required')
                    changed = connection.execute("UPDATE jobs SET state='resolved' WHERE id=? AND state IN ('unknown','dispatched','submitted')", (job_id,)).rowcount
            self.respond(200 if changed else 409, {'saved': bool(changed)})
        elif agent and self.command == 'POST' and self.path == '/agent/poll':
            report = json.loads(self.body(262144))
            if not isinstance(report, dict) or report.get('mode') not in ('simulate','live'):
                raise ValueError('Invalid mode')
            with platform.db() as connection:
                connection.execute('INSERT OR REPLACE INTO telemetry VALUES (1,?,?)', (json.dumps(report), time.time()))
                active = connection.execute("SELECT 1 FROM jobs WHERE state IN ('dispatched','submitted','unknown')").fetchone()
                job = None if active or report.get('local_busy') or report.get('plan_version') not in (1,2) else connection.execute("SELECT * FROM jobs WHERE state='approved' AND (COALESCE(execution_mode,'simulate')='simulate' OR ?=1) ORDER BY created LIMIT 1", (int(os.environ.get('ALLOW_LIVE_PRINT','0') == '1' and report.get('remote_live_enabled') is True and report.get('plan_version') == 2),)).fetchone()
                if job:
                    connection.execute("UPDATE jobs SET state='dispatched' WHERE id=?", (job['id'],))
                resolutions = [json.loads(row['receipt']).get('local_job_id') for row in connection.execute("SELECT receipt FROM jobs WHERE state='resolved' AND receipt IS NOT NULL")]
            command = dict(job) if job else None
            if command and command.get('model_info'):
                command['name'] = Path(command['name']).stem + '.gcode.3mf'
            self.respond(200, {'job': command,'resolutions':[item for item in resolutions if item]})
        elif agent and self.command == 'GET' and re.fullmatch(r'/agent/files/[a-f0-9-]{36}', self.path):
            job_id = self.path.split('/')[-1]
            with platform.db() as connection:
                job = connection.execute("SELECT 1 FROM jobs WHERE id=? AND state='dispatched'", (job_id,)).fetchone()
            if not job:
                self.respond(404, {'error': 'Job not dispatched'})
                return
            self.respond(200, (platform.root / (job_id + '.gcode.3mf')).read_bytes(), 'application/octet-stream')
        elif agent and self.command == 'POST' and self.path == '/agent/result':
            report = json.loads(self.body())
            if report['state'] not in ('simulated', 'submitted', 'unknown'):
                raise ValueError('Invalid state')
            with platform.db() as connection:
                target = connection.execute('SELECT execution_mode FROM jobs WHERE id=?',(report['id'],)).fetchone()
                if target and (report['state']=='submitted' and target['execution_mode']!='live' or report['state']=='simulated' and target['execution_mode']=='live'):
                    raise ValueError('Result does not match approved execution mode')
                receipt = {'local_job_id':report.get('local_job_id'), 'driver_accepted':report['state']=='submitted',
                           'diagnostics':safe_diagnostics(report.get('diagnostics',{}))}
                changed = connection.execute("UPDATE jobs SET state=?,receipt=? WHERE id=? AND state IN ('dispatched', ?)", (report['state'], json.dumps(receipt), report['id'], report['state'])).rowcount
                if not changed:
                    changed = connection.execute("SELECT 1 FROM jobs WHERE id=? AND state='resolved'", (report['id'],)).fetchone() is not None
            self.respond(200 if changed else 409, {'saved': bool(changed)})
        else:
            self.respond(404, {'error': 'Not found'})


def main():
    os.umask(0o077)
    required = {key: os.environ[key] for key in ('WEB_USER', 'WEB_PASSWORD', 'AGENT_TOKEN', 'PUBLIC_URL')}
    if len(required['WEB_PASSWORD']) < 16 or len(required['AGENT_TOKEN']) < 32:
        raise SystemExit('Use a password of at least 16 characters and agent token of at least 32')
    Platform(('0.0.0.0', int(os.environ.get('PORT', '3000'))), os.environ.get('DATA_DIR', '/data'),
             required['WEB_USER'], required['WEB_PASSWORD'], required['AGENT_TOKEN'], required['PUBLIC_URL']).serve_forever()


if __name__ == '__main__':
    main()
