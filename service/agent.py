import hashlib
import fcntl
import json
import os
from pathlib import Path
import time
import urllib.request
import urllib.error
import uuid
from print_plan import printer_available


def request(base, path, token, body=None, raw=False, extra=None):
    headers = {'Authorization': 'Bearer ' + token}
    headers.update(extra or {})
    if isinstance(body, dict):
        body = json.dumps(body).encode()
        headers['Content-Type'] = 'application/json'
    message = urllib.request.Request(base + path, data=body, headers=headers)
    with urllib.request.urlopen(message, timeout=20) as response:
        data = response.read(100 * 1024**2 + 1)
        if len(data) > 100 * 1024**2:
            raise ValueError('Response too large')
        return data if raw else json.loads(data)


class Agent:
    def __init__(self, root, cloud, agent_token, local, api_token, update_guard=None):
        if not cloud.startswith('https://'):
            raise ValueError('Platform URL must use HTTPS')
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = (self.root / 'agent.lock').open('a')
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.cloud = cloud.rstrip('/')
        self.agent_token = agent_token
        self.local = local.rstrip('/')
        self.api_token = api_token
        self.update_guard = update_guard

    def remote(self, path, body=None, raw=False):
        return request(self.cloud, path, self.agent_token, body, raw)

    def save(self, path, record):
        temporary = path.with_suffix('.tmp')
        with temporary.open('w') as target:
            json.dump(record, target)
            target.flush()
            os.fsync(target.fileno())
        temporary.replace(path)
        descriptor = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def tick(self):
        if self.update_guard is None:
            return self.work()
        with Path(self.update_guard).open('a') as guard:
            try:
                fcntl.flock(guard, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            return self.work()

    def work(self):
        for path in self.root.glob('*.json'):
            record = json.loads(path.read_text())
            if record['state'] == 'awaiting':
                if not self.finish(record):
                    return
                self.save(path, record)
            if record['state'] == 'pending':
                record['state'] = 'unknown'
                self.save(path, record)
            self.remote('/agent/result', record)
            path.unlink()
        status = request(self.local, '/v1/status', self.api_token)
        if status.get('mode') not in ('simulate','live'):
            raise ValueError('Unknown local mode')
        response = self.remote('/agent/poll', {'mode': status['mode'], 'remote_live_enabled':status.get('remote_live_enabled',False),
            'local_busy':any(item['state'] in ('starting','submitted','unknown') for item in status['jobs']), 'plan_version': 2, 'printer': status['printer']})
        for local_id in response.get('resolutions',[]):
            local_job = next((item for item in status['jobs'] if item['id']==local_id),None)
            printer = status['printer']
            if local_job and local_job['state'] in ('submitted','unknown') and printer_available(printer):
                request(self.local,'/v1/jobs/'+local_id+'/resolve',self.api_token,{'printer_checked_idle':True})
        job = response['job']
        if job is None:
            return
        job_id = str(uuid.UUID(job['id']))
        path = self.root / (job_id + '.json')
        record = {'id': job_id, 'state': 'pending'}
        self.save(path, record)
        try:
            execution_mode = job.get('execution_mode') or 'simulate'
            if execution_mode not in ('simulate','live') or execution_mode == 'live' and not status.get('remote_live_enabled'):
                raise ValueError('Physical printing disabled')
            data = self.remote('/agent/files/' + job_id, raw=True)
            if hashlib.sha256(data).hexdigest() != job['sha256']:
                raise ValueError('Checksum mismatch')
            uploaded = request(self.local, '/v1/jobs', self.api_token, data, extra={'X-Filename': job['name']})
            local_id = uploaded['id']
            fresh = request(self.local, '/v1/status', self.api_token)
            if execution_mode == 'live' and not fresh.get('remote_live_enabled'):
                raise ValueError('Physical printing disabled')
            record.update(state='awaiting',local_job_id=local_id)
            self.save(path,record)
            request(self.local, '/v1/jobs/' + local_id + '/remote-start', self.api_token,
                    {'confirm_job_id': local_id, 'printer_ready': True, 'selection': json.loads(job['selection']),
                     'execution_mode':execution_mode, 'confirm_physical':'START PRINT' if execution_mode=='live' else None})
            for attempt in range(30):
                if self.finish(record):
                    break
                time.sleep(0.1)
            else:
                return
        except Exception as error:
            record['state'] = 'unknown'
            record['diagnostics'] = {'error_type':type(error).__name__}
            if isinstance(error,urllib.error.HTTPError):
                record['diagnostics']['http_status'] = error.code
        self.save(path, record)
        self.remote('/agent/result', record)
        path.unlink()

    def finish(self, record):
        jobs = request(self.local, '/v1/jobs', self.api_token)['jobs']
        job = next((item for item in jobs if item['id']==record['local_job_id']),None)
        if job and job['state']=='starting':
            return False
        record['state'] = job['state'] if job and job['state'] in ('simulated','submitted') else 'unknown'
        if job and job.get('diagnostics'):
            record['diagnostics'] = json.loads(job['diagnostics'])
        return True


def main():
    os.umask(0o077)
    agent = Agent(os.environ.get('AGENT_DATA_DIR', '/var/lib/print-agent'), os.environ['PLATFORM_URL'],
                  os.environ['AGENT_TOKEN'], 'http://127.0.0.1:' + os.environ.get('PORT', '8787'), os.environ['API_TOKEN'],
                  Path(os.environ.get('DATA_DIR', '/var/lib/print-service')) / 'update.lock')
    connected = False
    while True:
        try:
            if agent.tick() is not False:
                if not connected:
                    print('Platform connected; status delivered', flush=True)
                connected = True
        except Exception as error:
            connected = False
            detail = 'HTTP ' + str(error.code) if isinstance(error, urllib.error.HTTPError) else type(error).__name__
            print('Platform connection unavailable or job requires review: ' + detail, flush=True)
        time.sleep(5)


if __name__ == '__main__':
    main()
