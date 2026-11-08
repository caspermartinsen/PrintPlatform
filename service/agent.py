import hashlib
import fcntl
import json
import os
from pathlib import Path
import time
import urllib.request
import urllib.error
import uuid


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
                return
            return self.work()

    def work(self):
        for path in self.root.glob('*.json'):
            record = json.loads(path.read_text())
            if record['state'] == 'pending':
                record['state'] = 'unknown'
                self.save(path, record)
            self.remote('/agent/result', record)
            path.unlink()
        status = request(self.local, '/v1/status', self.api_token)
        if status.get('mode') != 'simulate':
            raise ValueError('Agent refuses physical printing in this version')
        job = self.remote('/agent/poll', {'mode': 'simulate', 'plan_version': 1, 'printer': status['printer']})['job']
        if job is None:
            return
        job_id = str(uuid.UUID(job['id']))
        path = self.root / (job_id + '.json')
        record = {'id': job_id, 'state': 'pending'}
        self.save(path, record)
        try:
            data = self.remote('/agent/files/' + job_id, raw=True)
            if hashlib.sha256(data).hexdigest() != job['sha256']:
                raise ValueError('Checksum mismatch')
            uploaded = request(self.local, '/v1/jobs', self.api_token, data, extra={'X-Filename': job['name']})
            local_id = uploaded['id']
            fresh = request(self.local, '/v1/status', self.api_token)
            if fresh['mode'] != 'simulate':
                raise ValueError('Mode changed')
            request(self.local, '/v1/jobs/' + local_id + '/simulate', self.api_token,
                    {'confirm_job_id': local_id, 'printer_ready': True, 'selection': json.loads(job['selection'])})
            for attempt in range(30):
                local_jobs = request(self.local, '/v1/jobs', self.api_token)['jobs']
                result = next(item for item in local_jobs if item['id'] == local_id)
                if result['state'] != 'starting':
                    record['state'] = 'simulated' if result['state'] == 'simulated' else 'unknown'
                    break
                time.sleep(0.1)
            else:
                record['state'] = 'unknown'
        except Exception:
            record['state'] = 'unknown'
        self.save(path, record)
        self.remote('/agent/result', record)
        path.unlink()


def main():
    os.umask(0o077)
    agent = Agent(os.environ.get('AGENT_DATA_DIR', '/var/lib/print-agent'), os.environ['PLATFORM_URL'],
                  os.environ['AGENT_TOKEN'], 'http://127.0.0.1:' + os.environ.get('PORT', '8787'), os.environ['API_TOKEN'],
                  Path(os.environ.get('DATA_DIR', '/var/lib/print-service')) / 'update.lock')
    connected = False
    while True:
        try:
            agent.tick()
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
