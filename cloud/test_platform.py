import base64
import fcntl
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
from telemetry import Telemetry


class PlatformTests(unittest.TestCase):
    def test_agent_defers_poll_during_update(self):
        self.agent.update_guard = self.local.store.root / 'update.lock'
        with self.agent.update_guard.open('a') as guard:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(self.agent, 'work') as work:
                self.assertFalse(self.agent.tick())
                work.assert_not_called()
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.platform = Platform(('127.0.0.1', 0), root / 'cloud', 'user', 'password', 'agent-token', 'http://test')
        self.local = HTTPServer(('127.0.0.1', 0), Handler)
        self.local.token = 'local-token'
        self.local.store = Store(root / 'pi')
        self.local.telemetry = Telemetry()
        self.local.telemetry.connected = True
        self.local.telemetry.ingest(b'{"print":{"gcode_state":"IDLE","ams":{"tray_exist_bits":"1","ams":[{"id":"0","tray":[{"id":"0","tray_type":"PLA","tray_sub_brands":"PLA Basic","tray_color":"000000FF"}]}]}}}')
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
            archive.writestr('Metadata/slice_info.config', '<config><plate><filament id="1" type="PLA" color="#000000" used_g="1"/></plate></config>')
            archive.writestr('Metadata/project_settings.config', '{"filament_settings_id":["PLA Basic"]}')
        return self.browser('/api/jobs', data.getvalue())['id']

    def approve(self, job_id):
        self.agent.remote('/agent/poll', {'mode':'simulate', 'printer':self.local.telemetry.snapshot()})
        return self.browser('/api/jobs/' + job_id + '/approve', json.dumps({'confirm_job_id': job_id,
            'ams_id':0,'slot':1,'bed_clear':True,'accept_differences':True}).encode())

    def test_complete_nat_flow_and_ack_retry(self):
        with patch('server.subprocess.Popen', side_effect=AssertionError('No physical printing')):
            job_id = self.upload()
            self.agent.tick()
            self.assertEqual(self.local.store.jobs(), [])
            self.approve(job_id)
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
            self.assertEqual(json.loads(self.local.store.jobs()[0]['plan'])['ams_mapping'], '[0]')

    def test_dispatched_command_never_redelivered(self):
        job_id = self.upload()
        self.approve(job_id)
        report = {'mode': 'simulate', 'plan_version':1, 'printer': {'state': 'unknown'}}
        self.assertEqual(self.agent.remote('/agent/poll', report)['job']['id'], job_id)
        self.assertIsNone(self.agent.remote('/agent/poll', report)['job'])

    def test_double_approval_and_ams_changed_after_approval(self):
        job_id = self.upload()
        self.approve(job_id)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.browser('/api/jobs/' + job_id + '/approve', json.dumps({'confirm_job_id':job_id}).encode())
        self.assertEqual(error.exception.code, 409)
        self.local.telemetry.ingest(b'{"print":{"ams":{"ams":[{"id":"0","tray":[{"id":"0","tray_color":"FFFFFFFF"}]}]}}}')
        with patch('server.subprocess.Popen', side_effect=AssertionError('No physical print')):
            self.agent.tick()
        self.assertEqual(self.browser('/api/status')['jobs'][0]['state'], 'unknown')

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
        self.approve(job_id)
        self.agent.remote('/agent/poll', {'mode':'simulate', 'plan_version':1})
        self.agent.save(self.agent.root / (job_id + '.json'), {'id':job_id, 'state':'pending'})
        self.agent.tick()
        self.assertEqual(self.browser('/api/status')['jobs'][0]['state'], 'unknown')
        self.assertEqual(self.local.store.jobs(), [])
        self.browser('/api/jobs/' + job_id + '/resolve', json.dumps({'confirm_job_id': job_id}).encode())
        self.assertEqual(self.browser('/api/status')['jobs'][0]['state'], 'resolved')
        self.assertTrue(self.agent.remote('/agent/result', {'id':job_id, 'state':'unknown'})['saved'])
