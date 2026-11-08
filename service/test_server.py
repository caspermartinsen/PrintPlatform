import hashlib
import fcntl
import http.client
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

from server import Handler, HTTPServer, Store, validate


def archive(entries=None):
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as output:
        for name, content in (entries or {'[Content_Types].xml': '<Types/>', 'Metadata/plate_1.gcode': 'G28'}).items():
            output.writestr(name, content)
    return data.getvalue()


class ServiceTests(unittest.TestCase):
    def test_update_lock_blocks_start_without_changing_queue(self):
        job_id = self.upload()
        with (self.store.root / 'update.lock').open('a') as guard:
            fcntl.flock(guard,fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(ValueError):
                self.store.start(job_id)
        self.assertEqual(self.store.jobs()[0]['state'],'queued')
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(self.directory.name)
        self.http = HTTPServer(('127.0.0.1', 0), Handler)
        self.http.token = 'a' * 40
        self.http.store = self.store
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()
        self.store.lock.close()
        self.directory.cleanup()

    def request(self, method, path, body=None, auth=True, **headers):
        connection = http.client.HTTPConnection(*self.http.server_address, timeout=3)
        if auth:
            headers['Authorization'] = 'Bearer ' + self.http.token
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    def upload(self):
        status, body = self.request('POST', '/v1/jobs', archive(), **{'X-Filename': 'test.gcode.3mf'})
        self.assertEqual(status, 201)
        return body['id']

    def test_auth_upload_and_explicit_start(self):
        with patch('server.subprocess.Popen', side_effect=AssertionError('No printer processes allowed')):
            self.assertEqual(self.request('GET', '/v1/status', auth=False)[0], 401)
            job_id = self.upload()
            self.assertEqual(self.store.jobs()[0]['state'], 'queued')
            path = '/v1/jobs/' + job_id + '/start'
            self.assertEqual(self.request('POST', path, '{}')[0], 400)
            with patch.dict(os.environ, {'PRINT_MODE': 'simulate'}):
                self.assertEqual(self.request('POST', path, json.dumps({'confirm_job_id': job_id, 'printer_ready': True}))[0], 202)
                for attempt in range(100):
                    if self.store.jobs()[0]['state'] == 'simulated':
                        break
                    time.sleep(0.01)
            self.assertEqual(self.store.jobs()[0]['state'], 'simulated')
            self.assertEqual(self.request('POST', path, json.dumps({'confirm_job_id': job_id, 'printer_ready': True}))[0], 409)

    def test_busy_unknown_and_restart(self):
        first = self.upload()
        second = self.upload()
        self.store.state(first, 'submitted')
        with self.assertRaises(ValueError):
            self.store.start(second)
        self.store.state(first, 'starting')
        self.store.lock.close()
        self.store = Store(self.directory.name)
        self.http.store = self.store
        self.assertEqual(self.store.jobs()[0]['state'], 'unknown')
        with self.assertRaises(ValueError):
            self.store.start(second)
        self.assertEqual(self.request('POST', '/v1/jobs/' + first + '/resolve', '{"printer_checked_idle":true}')[0], 200)

    def test_archive_rejection(self):
        for entries in ({'../bad': 'x'}, {'Metadata/plate_1.gcode': 'x'},
                        {'[Content_Types].xml': 'x', 'Metadata/plate_1.gcode': 'x', 'Metadata/plate_2.gcode': 'x'}):
            self.assertEqual(self.request('POST', '/v1/jobs', archive(entries), **{'X-Filename': 'test.gcode.3mf'})[0], 400)
        self.assertEqual(self.store.jobs(), [])

    def test_live_adapter_uses_fake_process_only(self):
        job_id = self.upload()
        with self.store.db() as connection:
            connection.execute('UPDATE jobs SET selection=? WHERE id=?',
                               (json.dumps({'ams_id':0,'slot':3,'filament_id':1}),job_id))
        params = Path(self.directory.name) / 'template.json'
        params.write_text('{"ftp_file":"bunny.gcode.3mf", "ams_mapping":"[0]"}')
        environment = {'PRINT_MODE': 'live', 'BAMBU_REPO': self.directory.name,
                       'BAMBU_PARAMS': str(params), 'BAMBU_DEV_ID': 'fake',
                       'BAMBU_DEV_IP': '127.0.0.1', 'BAMBU_ACCESS_CODE': 'fake', 'BAMBU_CERT': '/fake'}
        with patch.dict(os.environ, environment), patch('server.subprocess.Popen') as launch:
            launch.return_value.__enter__.return_value.wait.return_value = 0
            def capture(*args, **kwargs):
                plan = json.loads((self.store.root / (job_id + '.params.json')).read_text())
                self.assertEqual(json.loads(plan['ams_mapping']), [2])
                self.assertEqual(json.loads(plan['ams_mapping2']), [{'ams_id':0,'slot_id':2}])
                self.assertTrue(plan['task_use_ams'])
                return launch.return_value
            launch.side_effect = capture
            self.store.run(job_id)
            command = launch.call_args.args[0]
            self.assertIn('local_print', command)
            self.assertNotIn('API_TOKEN', launch.call_args.kwargs['env'])
        self.assertEqual(self.store.jobs()[0]['state'], 'submitted')

    def test_driver_failure_never_requeues(self):
        job_id = self.upload()
        params = Path(self.directory.name) / 'template.json'
        params.write_text('{}')
        environment = {'PRINT_MODE': 'live', 'BAMBU_REPO': self.directory.name,
                       'BAMBU_PARAMS': str(params), 'BAMBU_DEV_ID': 'fake',
                       'BAMBU_DEV_IP': '127.0.0.1', 'BAMBU_ACCESS_CODE': 'fake', 'BAMBU_CERT': '/fake'}
        with patch.dict(os.environ, environment), patch('server.subprocess.Popen', side_effect=OSError('failed')) as launch:
            self.store.run(job_id)
            launch.assert_called_once()
        self.assertEqual(self.store.jobs()[0]['state'], 'unknown')
        with self.assertRaises(ValueError):
            self.store.start(job_id)


if __name__ == '__main__':
    unittest.main()
