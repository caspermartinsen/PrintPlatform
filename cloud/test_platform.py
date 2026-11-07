import base64
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
import zipfile

from cloud.server import Platform
from service.agent import Agent, request
from server import Store, HTTPServer, Handler


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.platform = Platform(('127.0.0.1', 0), root / 'cloud', 'user', 'password', 'agent-token', 'http://test')
        self.local = HTTPServer(('127.0.0.1', 0), Handler)
        self.local.token = 'local-token'
        self.local.store = Store(root / 'pi')
        self.threads = []
        for server in (self.platform, self.local):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.threads.append(thread)
        self.cloud_url = 'http://127.0.0.1:' + str(self.platform.server_port)
        self.agent = Agent(root / 'agent', 'https://test', 'agent-token',
                           'http://127.0.0.1:' + str(self.local.server_port), 'local-token')
        self.agent.cloud = self.cloud_url

    def tearDown(self):
        for server in (self.platform, self.local):
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join()
        self.agent.lock.close()
        self.local.store.lock.close()
        self.directory.cleanup()

    def browser(self, path, body=None, origin='http://test'):
        headers = {'Authorization': self.platform.browser_auth, 'Origin': origin,
                   'X-Filename': 'test.gcode.3mf'}
        message = urllib.request.Request(self.cloud_url + path, data=body, headers=headers)
        with urllib.request.urlopen(message) as response:
            return json.load(response)

    def upload(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as archive:
            archive.writestr('[Content_Types].xml', '<Types/>')
            archive.writestr('Metadata/plate_1.gcode', '; simulation')
        return self.browser('/api/jobs', data.getvalue())['id']

    def test_complete_nat_flow_and_ack_retry(self):
        with patch('server.subprocess.Popen', side_effect=AssertionError('No physical printing')):
            job_id = self.upload()
            self.agent.tick()
            self.assertEqual(self.local.store.jobs(), [])
            self.browser('/api/jobs/' + job_id + '/approve', json.dumps({'confirm_job_id': job_id}).encode())
            original = self.agent.remote
            def lost_ack(path, body=None, raw=False):
                if path == '/agent/result':
                    raise OSError('Connection lost')
                return original(path, body, raw)
            with patch.object(self.agent, 'remote', side_effect=lost_ack):
                with self.assertRaises(OSError):
                    self.agent.tick()
            self.assertEqual(len(self.local.store.jobs()), 1)
            self.agent.tick()
            self.assertEqual(len(self.local.store.jobs()), 1)
            status = self.browser('/api/status')
            self.assertTrue(status['agent_online'])
            self.assertEqual(status['jobs'][0]['state'], 'simulated')

    def test_dispatched_command_never_redelivered(self):
        job_id = self.upload()
        self.browser('/api/jobs/' + job_id + '/approve', json.dumps({'confirm_job_id': job_id}).encode())
        report = {'mode': 'simulate', 'printer': {'state': 'unknown'}}
        self.assertEqual(self.agent.remote('/agent/poll', report)['job']['id'], job_id)
        self.assertIsNone(self.agent.remote('/agent/poll', report)['job'])

    def test_auth_origin_and_live_mode_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(self.cloud_url + '/api/status')
        self.assertEqual(error.exception.code, 401)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.browser('/api/jobs', b'x', origin='https://attacker.example')
        self.assertEqual(error.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.agent.remote('/agent/poll', {'mode': 'live'})
        self.assertEqual(error.exception.code, 400)
        with patch.dict('os.environ', {'PRINT_MODE': 'live'}):
            with self.assertRaises(ValueError):
                self.agent.tick()

    def test_restart_with_pending_journal_is_unknown(self):
        job_id = self.upload()
        self.browser('/api/jobs/' + job_id + '/approve', json.dumps({'confirm_job_id': job_id}).encode())
        self.agent.remote('/agent/poll', {'mode':'simulate'})
        self.agent.save(self.agent.root / (job_id + '.json'), {'id':job_id, 'state':'pending'})
        self.agent.tick()
        self.assertEqual(self.browser('/api/status')['jobs'][0]['state'], 'unknown')
        self.assertEqual(self.local.store.jobs(), [])
        self.browser('/api/jobs/' + job_id + '/resolve', json.dumps({'confirm_job_id': job_id}).encode())
        self.assertEqual(self.browser('/api/status')['jobs'][0]['state'], 'resolved')
        self.assertTrue(self.agent.remote('/agent/result', {'id':job_id, 'state':'unknown'})['saved'])
