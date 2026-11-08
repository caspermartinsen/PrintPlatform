"""Exercise real request handlers and durable stores without opening sockets."""
import io
import json
import tempfile
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import quote

from cloud.server import Platform, Handler
from cloud.test_slicing import model, result_file
from service.agent import Agent
from server import Handler as PiHandler, Store, upload_name


class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.platform = Platform(('127.0.0.1', 0), self.directory.name + '/cloud',
                                 'user', 'password', 'agent-token', 'https://test', bind_and_activate=False)
        self.no_driver = patch('server.subprocess.Popen', side_effect=AssertionError('No printer processes'))
        self.no_driver.start()

    def tearDown(self):
        self.no_driver.stop()
        self.platform.server_close()
        self.directory.cleanup()

    def call(self, path, body=None, headers=None, handler_type=Handler, server=None):
        server = server or self.platform
        handler = object.__new__(handler_type)
        handler.server = server
        handler.command = 'GET' if body is None else 'POST'
        handler.path = path
        handler.connection = SimpleNamespace(settimeout=lambda _: None)
        handler.headers = Message()
        token = ('Bearer ' + 's' * 32) if path.startswith('/slicer/') else self.platform.agent_auth if path.startswith('/agent/') else self.platform.browser_auth
        handler.headers['Authorization'] = token if handler_type is Handler else 'Bearer local-token'
        handler.headers['Origin'] = 'https://test'
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        if body is not None:
            handler.headers['Content-Length'] = str(len(data))
        for key, value in (headers or {}).items():
            handler.headers[key] = value
        handler.rfile = io.BytesIO(data)
        result = []
        handler.respond = lambda status, payload, *args, **kwargs: result.append((status, payload))
        handler.reply = handler.respond
        handler.dispatch()
        self.assertEqual(len(result), 1)
        return result[0]

    def job(self, state='dispatched', receipt=None, mode='simulate'):
        job_id = str(uuid.uuid4())
        with self.platform.db() as db:
            db.execute('INSERT INTO jobs(id,name,state,created,execution_mode,receipt) VALUES(?,?,?,?,?,?)',
                       (job_id, 'model.gcode.3mf', state, time.time(), mode, json.dumps(receipt) if receipt else None))
        return job_id

    def stored(self, job_id):
        with self.platform.db() as db:
            return dict(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())

    def test_dispatched_job_cannot_be_closed_before_receipt(self):
        job_id = self.job()
        status, _ = self.call('/api/jobs/' + job_id + '/resolve', {'confirm_job_id': job_id, 'printer_checked_idle': True})
        self.assertEqual(status, 409)
        self.assertEqual(self.stored(job_id)['state'], 'dispatched')

    def test_late_receipt_is_saved_and_requires_new_inspection(self):
        job_id = self.job('resolved', mode='live')
        local_id = str(uuid.uuid4())
        report = {'id': job_id, 'state': 'submitted', 'local_job_id': local_id}
        self.assertEqual(self.call('/agent/result', report)[0], 200)
        row = self.stored(job_id)
        self.assertEqual(row['state'], 'unknown')
        self.assertEqual(json.loads(row['receipt'])['local_job_id'], local_id)
        poll = self.call('/agent/poll', {'mode':'live', 'plan_version':2, 'local_busy':True})[1]
        self.assertEqual(poll['resolutions'], [])
        self.assertIsNone(poll['job'])
        # A lost HTTP acknowledgement is safe to repeat; no print is repeated.
        self.assertEqual(self.call('/agent/result', report)[0], 200)
        self.assertEqual(self.call('/api/jobs/' + job_id + '/resolve', {'confirm_job_id':job_id})[0], 400)
        self.assertEqual(self.call('/api/jobs/' + job_id + '/resolve', {'confirm_job_id':job_id,'printer_checked_idle':True})[0], 200)
        self.assertEqual(self.call('/agent/result', report)[0], 200)
        self.assertEqual(self.stored(job_id)['state'], 'resolved')
        self.assertIn(local_id, self.call('/agent/poll', {'mode':'live','plan_version':2,'local_busy':True})[1]['resolutions'])

    def test_retry_without_local_id_preserves_saved_receipt(self):
        local_id = str(uuid.uuid4())
        job_id = self.job('unknown', {'local_job_id':local_id,'driver_accepted':False})
        self.assertEqual(self.call('/agent/result', {'id':job_id,'state':'unknown'})[0], 200)
        self.assertEqual(json.loads(self.stored(job_id)['receipt'])['local_job_id'], local_id)

    def test_conflicting_local_job_id_is_rejected(self):
        local_id = str(uuid.uuid4())
        job_id = self.job('unknown', {'local_job_id':local_id})
        status, _ = self.call('/agent/result', {'id':job_id,'state':'unknown','local_job_id':str(uuid.uuid4())})
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(self.stored(job_id)['receipt'])['local_job_id'], local_id)

    def test_simulation_receipt_never_becomes_a_live_receipt(self):
        job_id = self.job()
        self.assertEqual(self.call('/agent/result', {'id':job_id,'state':'submitted'})[0], 400)
        self.assertEqual(self.stored(job_id)['state'], 'dispatched')

    def test_unicode_upload_names_work_on_cloud_and_pi(self):
        store = Store(self.directory.name + '/pi')
        try:
            server = SimpleNamespace(store=store, token='local-token')
            for name in ('MODEL.GCODE.3MF', 'Nøglering til børn.gcode.3mf', 'Jason & Jamie 🐼.gcode.3mf'):
                headers = {'X-Filename-Encoded':quote(name, safe='')}
                status, body = self.call('/api/jobs', result_file(), headers)
                self.assertEqual(status, 201)
                self.assertEqual(self.stored(body['id'])['name'], name)
                self.assertEqual(self.call('/v1/jobs', result_file(), headers, PiHandler, server)[0], 201)
                self.assertEqual(store.jobs()[-1]['name'], name)
        finally:
            store.lock.close()

    def test_encoded_stl_name_is_accepted(self):
        status, body = self.call('/api/jobs', model(), {'X-Filename-Encoded':quote('Nøglering.STL')})
        self.assertEqual(status, 201)
        self.assertEqual(body['state'], 'model')

    def test_unsafe_and_malformed_names_are_rejected(self):
        for encoded in ('../bad.gcode.3mf','bad%2Fname.stl','bad%5Cname.stl','bad%00.stl','bad%0A.stl','bad%ZZ.stl','%FF.stl'):
            self.assertEqual(self.call('/api/jobs', model(), {'X-Filename-Encoded':encoded})[0], 400)
        with self.assertRaises(ValueError):
            upload_name({'X-Filename':'not-a-print.txt'}, allow_stl=True)

    def test_agent_commands_use_legacy_compatible_internal_names(self):
        status, upload = self.call('/api/jobs', result_file(), {'X-Filename':'MODEL.GCODE.3MF'})
        self.assertEqual(status, 201)
        with self.platform.db() as db:
            db.execute("UPDATE jobs SET state='approved' WHERE id=?", (upload['id'],))
        command = self.call('/agent/poll', {'mode':'simulate','plan_version':2})[1]['job']
        self.assertEqual(command['name'], upload['id'] + '.gcode.3mf')
        self.assertEqual(self.stored(upload['id'])['name'], 'MODEL.GCODE.3MF')

    def test_parallel_polls_dispatch_one_job_only(self):
        for _ in range(5):
            self.job('approved')
        def poll(_):
            return self.call('/agent/poll', {'mode':'simulate','plan_version':2,'local_busy':False})[1]['job']
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(poll, range(16)))
        self.assertEqual(sum(job is not None for job in results), 1)

    def test_status_remains_available_while_an_upload_waits(self):
        # Uploads serialize their file operations, but do not lock status requests.
        with self.platform.files_guard, ThreadPoolExecutor(max_workers=1) as pool:
            status = pool.submit(self.call, '/api/status').result(timeout=2)
        self.assertEqual(status[0], 200)

    def test_request_limit_returns_retryable_503(self):
        for _ in range(16):
            self.assertTrue(self.platform.request_slots.acquire(blocking=False))
        request = Mock()
        with patch.object(self.platform, 'shutdown_request') as shutdown:
            self.platform.process_request(request, ('127.0.0.1', 1))
        self.assertIn(b'503 Service Unavailable', request.sendall.call_args[0][0])
        shutdown.assert_called_once_with(request)
        for _ in range(16):
            self.platform.request_slots.release()

    def slice_job(self):
        status, uploaded = self.call('/api/jobs', model(), {'X-Filename':'model.stl'})
        self.assertEqual(status, 201)
        job_id = uploaded['id']
        with self.platform.db() as db:
            db.execute("UPDATE jobs SET state='slice_queued',slice_token='attempt-1' WHERE id=?", (job_id,))
        return job_id

    def test_slice_lease_expires_and_rejects_late_result(self):
        job_id = self.slice_job()
        with patch.dict('os.environ', {'SLICER_TOKEN':'s' * 32}):
            job = self.call('/slicer/poll', {})[1]['job']
            self.assertEqual(job['id'], job_id)
            self.assertGreater(self.stored(job_id)['slice_expires'], time.time())
            with self.platform.db() as db:
                db.execute('UPDATE jobs SET slice_expires=? WHERE id=?', (time.time()-1,job_id))
            self.assertEqual(self.call('/api/status')[1]['jobs'][0]['state'], 'slice_failed')
            self.assertEqual(self.call('/slicer/jobs/' + job_id + '/result', result_file(), {'X-Slice-Token':'attempt-1'})[0], 409)
            self.assertEqual(self.stored(job_id)['slice_error'], 'worker_interrupted')
            self.assertIsNone(self.stored(job_id)['slice_token'])

    def test_legacy_orphaned_slice_expires(self):
        job_id = self.slice_job()
        with self.platform.db() as db:
            db.execute("UPDATE jobs SET state='slicing',slice_expires=NULL WHERE id=?", (job_id,))
        self.assertEqual(self.call('/api/status')[1]['jobs'][0]['state'], 'slice_failed')

    def test_parallel_slicer_polls_claim_once(self):
        self.slice_job()
        with patch.dict('os.environ', {'SLICER_TOKEN':'s' * 32}), ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _:self.call('/slicer/poll', {})[1]['job'], range(8)))
        self.assertEqual(sum(job is not None for job in results), 1)

    def prepare_model(self):
        status, uploaded = self.call('/api/jobs', model(), {'X-Filename-Encoded':quote('Nøglering.STL')})
        self.assertEqual(status, 201)
        printer = {'connected':True,'stale':False,'state':'IDLE','error_code':0,
                   'ams_slots':[{'ams_id':0,'slot':1,'name':'PLA Basic','material':'PLA','color':'000000FF','present':True,'stale':False}]}
        self.call('/agent/poll', {'mode':'simulate','plan_version':2,'printer':printer})
        options = {'ams_id':0,'slot':1,'layer_height':0.2,'infill':15}
        self.assertEqual(self.call('/api/jobs/' + uploaded['id'] + '/slice', options)[0], 202)
        job = self.call('/slicer/poll', {})[1]['job']
        self.assertEqual(self.call('/slicer/jobs/' + job['id'] + '/result', result_file(), {'X-Slice-Token':job['slice_token']})[0], 201)
        return job['id'], printer, options

    def test_complete_stl_to_local_simulation_flow(self):
        with patch.dict('os.environ', {'SLICER_TOKEN':'s' * 32}):
            job_id, printer, _ = self.prepare_model()
            approval = {'confirm_job_id':job_id,'ams_id':0,'slot':1,'bed_clear':True,'preview_checked':True,'execution_mode':'simulate'}
            self.assertEqual(self.call('/api/jobs/' + job_id + '/approve', approval)[0], 200)
            command = self.call('/agent/poll', {'mode':'simulate','plan_version':2,'printer':printer})[1]['job']
            data = self.call('/agent/files/' + job_id)[1]
            store = Store(self.directory.name + '/pi')
            try:
                server = SimpleNamespace(store=store, token='local-token', telemetry=SimpleNamespace(snapshot=lambda:printer))
                uploaded = self.call('/v1/jobs', data, {'X-Filename':command['name']}, PiHandler, server)[1]
                local_id = uploaded['id']
                payload = {'confirm_job_id':local_id,'printer_ready':True,'selection':json.loads(command['selection']),'execution_mode':'simulate'}
                self.assertEqual(self.call('/v1/jobs/' + local_id + '/remote-start', payload, handler_type=PiHandler, server=server)[0], 202)
                for _ in range(100):
                    if store.jobs()[0]['state'] != 'starting':
                        break
                    time.sleep(0.01)
                self.assertEqual(store.jobs()[0]['state'], 'simulated')
                self.assertEqual(self.call('/agent/result', {'id':job_id,'local_job_id':local_id,'state':'simulated'})[0], 200)
                self.assertEqual(self.stored(job_id)['state'], 'simulated')
                self.assertTrue(self.stored(job_id)['preview_ready'])
            finally:
                store.lock.close()

    def test_reslicing_and_approval_cannot_both_win(self):
        with patch.dict('os.environ', {'SLICER_TOKEN':'s' * 32}):
            job_id, _, options = self.prepare_model()
            approval = {'confirm_job_id':job_id,'ams_id':0,'slot':1,'bed_clear':True,'preview_checked':True,'execution_mode':'simulate'}
            with ThreadPoolExecutor(max_workers=2) as pool:
                approve = pool.submit(self.call, '/api/jobs/' + job_id + '/approve', approval)
                reslice = pool.submit(self.call, '/api/jobs/' + job_id + '/slice', options)
                codes = approve.result()[0], reslice.result()[0]
            self.assertIn(codes, ((200,409),(409,202)))

    def test_agent_keeps_reporting_status_while_awaiting_driver(self):
        agent = Agent(self.directory.name + '/agent', 'https://test', 'agent-token', 'http://local', 'local-token')
        path = agent.root / (str(uuid.uuid4()) + '.json')
        record = {'id':path.stem, 'local_job_id':str(uuid.uuid4()), 'state':'awaiting'}
        agent.save(path, record)
        status = {'mode':'simulate','jobs':[{'id':record['local_job_id'],'state':'starting'}], 'printer':{'state':'PREPARE'}}
        try:
            with patch.object(agent, 'finish', return_value=False), patch('service.agent.request', return_value=status), patch.object(agent, 'remote', return_value={'job':None, 'resolutions':[]}) as remote:
                agent.tick()
            self.assertEqual(remote.call_count, 1)
            self.assertEqual(remote.call_args[0][0], '/agent/poll')
            self.assertTrue(remote.call_args[0][1]['local_busy'])
            self.assertEqual(remote.call_args[0][1]['printer']['state'], 'PREPARE')
            self.assertEqual(json.loads(path.read_text())['state'], 'awaiting')
        finally:
            agent.lock.close()

    def test_upload_limit_reports_actual_size_without_reading_body(self):
        handler = object.__new__(Handler)
        handler.server = self.platform
        handler.command = 'POST'
        handler.path = '/api/jobs'
        handler.connection = SimpleNamespace(settimeout=lambda _: None)
        handler.headers = Message()
        for key,value in {'Authorization':self.platform.browser_auth,'Origin':'https://test',
                          'X-Filename':'large.3mf','Content-Length':str(101 * 1024**2)}.items():
            handler.headers[key] = value
        handler.rfile = Mock()
        result = []
        handler.respond = lambda status,payload: result.append((status,payload))
        handler.dispatch()
        self.assertEqual(result[0][0],413)
        self.assertIn('101.0 MiB',result[0][1]['error'])
        self.assertIn('100 MiB',result[0][1]['error'])
        handler.rfile.read.assert_not_called()
        self.assertEqual(list(self.platform.root.glob('*.3mf')),[])

    def test_38_kib_multicolor_project_uses_upload_limit(self):
        import zipfile
        from slicer.fixtures import project_file
        output = io.BytesIO(project_file())
        with zipfile.ZipFile(output, 'a') as archive:
            archive.comment = b'x' * (38 * 1024 - len(output.getvalue()))
        data = output.getvalue()
        self.assertEqual(len(data), 38 * 1024)
        status, uploaded = self.call('/api/jobs', data, {'X-Filename':'colors.3mf'})
        self.assertEqual(status, 201)
        self.assertEqual(uploaded['state'], 'model')
        with self.platform.db() as connection:
            info = json.loads(connection.execute('SELECT model_info FROM jobs WHERE id=?', (uploaded['id'],)).fetchone()[0])
        self.assertEqual(len(info['filaments']), 2)
