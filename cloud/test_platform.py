import base64
import fcntl
import io
import signal
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
        self.local.telemetry.ingest(b'{"print":{"gcode_state":"IDLE","print_error":0,"ams":{"tray_exist_bits":"1","ams":[{"id":"0","tray":[{"id":"0","tray_type":"PLA","tray_sub_brands":"PLA Basic","tray_color":"000000FF"}]}]}}}')
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

    def browser(self, path, body=None, origin='http://test', filename='test.gcode.3mf'):
        headers = {'Authorization': self.platform.browser_auth, 'Origin': origin,
                   'X-Filename': filename}
        message = urllib.request.Request(self.cloud_url + path, data=body, headers=headers)
        with urllib.request.urlopen(message) as response:
            return json.load(response)

    def upload(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as archive:
            archive.writestr('[Content_Types].xml', '<Types/>')
            archive.writestr('Metadata/plate_1.gcode', '; simulation')
            archive.writestr('Metadata/slice_info.config', '<config><plate><filament id="1" type="PLA" color="#000000" used_g="1"/></plate></config>')
            archive.writestr('Metadata/project_settings.config', '{"filament_settings_id":["PLA Basic"],"printer_model":"Bambu Lab P2S"}')
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
            self.agent.remote('/agent/poll', {'mode': 'invalid'})
        self.assertEqual(error.exception.code, 400)
        with patch.dict('os.environ', {'PRINT_MODE': 'live'}):
            self.agent.tick()

    def test_live_acknowledgement_retry_without_second_driver_call(self):
        self.local.telemetry.ingest(b'{"print":{"gcode_state":"FAILED","mc_remaining_time":0}}')
        job_id = self.upload()
        template = Path(self.directory.name) / 'params.json'
        template.write_text('{}')
        environment = {'PRINT_MODE':'live','ALLOW_REMOTE_LIVE':'1','ALLOW_LIVE_PRINT':'1',
            'BAMBU_REPO':self.directory.name,'BAMBU_PARAMS':str(template),'BAMBU_DEV_ID':'fake',
            'BAMBU_DEV_IP':'127.0.0.1','BAMBU_ACCESS_CODE':'fake','BAMBU_CERT':'/fake'}
        with patch.dict('os.environ',environment), patch('server.subprocess.Popen') as launch:
            launch.return_value.__enter__.return_value.wait.return_value=-signal.SIGPIPE
            launch.return_value.__enter__.return_value.stdout=io.StringIO('{"_kind":"action_dispatched","rc":0}\n{"_kind":"finished","status":6,"code":0}\n')
            self.agent.tick()
            payload={'confirm_job_id':job_id,'ams_id':0,'slot':1,'bed_clear':True,
                'accept_differences':True,'execution_mode':'live','confirm_physical':'START PRINT'}
            self.browser('/api/jobs/'+job_id+'/approve',json.dumps(payload).encode())
            original=self.agent.remote
            def lost_ack(path,body=None,raw=False):
                if path=='/agent/result': raise OSError('Lost acknowledgement')
                return original(path,body,raw)
            with patch.object(self.agent,'remote',side_effect=lost_ack), self.assertRaises(OSError):
                self.agent.tick()
            self.agent.tick()
            launch.assert_called_once()
            job=self.browser('/api/status')['jobs'][0]
            self.assertEqual(job['state'],'submitted')
            self.assertTrue(json.loads(job['receipt'])['driver_accepted'])
            self.assertEqual(json.loads(job['receipt'])['diagnostics']['shutdown_warning'],'SIGPIPE_AFTER_ACCEPT')
            self.assertEqual(len(self.local.store.jobs()),1)
            self.browser('/api/jobs/'+job_id+'/resolve',json.dumps({'confirm_job_id':job_id,'printer_checked_idle':True}).encode())
            self.agent.tick()
            self.assertEqual(self.local.store.jobs()[0]['state'],'resolved')

    def test_live_approval_disabled_by_default_and_simulation_stays_simulation(self):
        job_id=self.upload()
        self.agent.tick()
        payload={'confirm_job_id':job_id,'ams_id':0,'slot':1,'bed_clear':True,
            'accept_differences':True,'execution_mode':'live','confirm_physical':'START PRINT'}
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.browser('/api/jobs/'+job_id+'/approve',json.dumps(payload).encode())
        self.assertEqual(error.exception.code,409)
        self.approve(job_id)
        with patch.dict('os.environ',{'PRINT_MODE':'live','ALLOW_REMOTE_LIVE':'1'}), patch('server.subprocess.Popen',side_effect=AssertionError('Simulation must never run driver')) as launch:
            self.agent.tick()
            launch.assert_not_called()
        self.assertEqual(self.browser('/api/status')['jobs'][0]['state'],'simulated')

    def test_agent_restart_follows_local_status_without_starting_again(self):
        job_id=self.upload()
        self.approve(job_id)
        self.agent.remote('/agent/poll',{'mode':'simulate','plan_version':2})
        local_id='00000000-0000-0000-0000-000000000001'
        with self.local.store.db() as connection:
            connection.execute('INSERT INTO jobs (id,name,sha256,state,created) VALUES (?,?,?,?,?)',(local_id,'test.gcode.3mf','fake','starting',0))
        path=self.agent.root/(job_id+'.json')
        self.agent.save(path,{'id':job_id,'local_job_id':local_id,'state':'awaiting'})
        with patch('server.subprocess.Popen',side_effect=AssertionError('Never repeat a start')) as launch:
            self.agent.tick()
            self.assertTrue(path.exists())
            self.local.store.state(local_id,'simulated')
            self.agent.tick()
            launch.assert_not_called()
        self.assertFalse(path.exists())
        self.assertEqual(self.browser('/api/status')['jobs'][0]['state'],'simulated')

    def test_local_live_gate_rejects_missing_activation_or_confirmation(self):
        payload={'execution_mode':'live','selection':{'ams_id':0,'slot':1},
            'printer_ready':True,'confirm_job_id':'00000000-0000-0000-0000-000000000001'}
        for enabled,confirmation in (('0','START PRINT'),('1','')):
            with patch.dict('os.environ',{'PRINT_MODE':'live','ALLOW_REMOTE_LIVE':enabled}), patch('server.subprocess.Popen') as launch:
                with self.assertRaises(urllib.error.HTTPError) as error:
                    request(self.agent.local,'/v1/jobs/'+payload['confirm_job_id']+'/remote-start',
                            'local-token',{**payload,'confirm_physical':confirmation})
                self.assertEqual(error.exception.code,409)
                launch.assert_not_called()

    def model_upload(self):
        from cloud.test_slicing import model
        return self.browser('/api/jobs',model(),filename='cube.stl')['id']

    def test_stl_slice_preview_never_approves_print(self):
        from cloud.test_slicing import result_file
        from slicer.worker import Worker
        token='slicer-test-token-at-least-32-characters'
        with patch.dict('os.environ',{'SLICER_TOKEN':token,'SLICER_ALLOW_INTERNAL_HTTP':'1'}):
            job_id=self.model_upload()
            self.agent.tick()
            options={'ams_id':0,'slot':1,'layer_height':0.2,'infill':15}
            self.browser('/api/jobs/'+job_id+'/slice',json.dumps(options).encode())
            worker=Worker(self.cloud_url,token,self.directory.name,'/fake')
            def fake_slice(data,options,destination,profiles,binary):
                path=destination/'result.gcode.3mf';path.write_bytes(result_file());return path
            with patch('slicer.worker.run_slice',side_effect=fake_slice):
                worker.tick()
            status=self.browser('/api/status')
            job=status['jobs'][0]
            self.assertEqual(job['state'],'queued')
            self.assertTrue(job['analysis']['supported'])
            self.assertTrue(job['preview_ready'])
            self.assertNotIn('slice_token',job)
            preview=self.browser('/api/jobs/'+job_id+'/preview')
            self.assertEqual(preview['filament_grams'],1.2)
            self.assertEqual(preview['segment_count'],2)
            self.agent.tick()
            self.assertEqual(self.local.store.jobs(),[])
            payload={'confirm_job_id':job_id,'ams_id':0,'slot':1,'bed_clear':True,'accept_differences':True}
            with self.assertRaises(urllib.error.HTTPError):
                self.browser('/api/jobs/'+job_id+'/approve',json.dumps(payload).encode())
            self.browser('/api/jobs/'+job_id+'/approve',json.dumps({**payload,'preview_checked':True}).encode())
            with patch('server.subprocess.Popen',side_effect=AssertionError('Only simulation')):
                self.agent.tick()
            self.assertEqual(self.browser('/api/status')['jobs'][0]['state'],'simulated')

    def test_stale_slice_result_cannot_replace_new_attempt(self):
        from cloud.test_slicing import result_file
        from slicer.worker import Worker
        token='slicer-test-token-at-least-32-characters'
        with patch.dict('os.environ',{'SLICER_TOKEN':token,'SLICER_ALLOW_INTERNAL_HTTP':'1'}):
            job_id=self.model_upload();self.agent.tick()
            options=json.dumps({'ams_id':0,'slot':1,'layer_height':0.2,'infill':15}).encode()
            self.browser('/api/jobs/'+job_id+'/slice',options)
            worker=Worker(self.cloud_url,token,self.directory.name,'/fake')
            old=worker.request('/slicer/poll',{})['job']
            self.browser('/api/jobs/'+job_id+'/slice',options)
            with self.assertRaises(urllib.error.HTTPError) as error:
                worker.request('/slicer/jobs/'+job_id+'/result',result_file(),headers={'X-Slice-Token':old['slice_token']})
            self.assertEqual(error.exception.code,409)
            self.assertEqual(self.browser('/api/status')['jobs'][0]['state'],'slice_queued')

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

    def test_multicolor_project_upload_and_slice_options(self):
        from cloud.test_slicing import project_file
        data=project_file()
        job_id=self.browser('/api/jobs',data,filename='colors.3mf')['id']
        job=self.browser('/api/status')['jobs'][0]
        self.assertEqual(job['state'],'model')
        self.assertEqual(len(job['model_info']['filaments']),2)
        self.agent.tick()
        options={'filaments':[{'ams_id':0,'slot':1},{'ams_id':0,'slot':1}],'layer_height':0.2,'infill':15}
        self.browser('/api/jobs/'+job_id+'/slice',json.dumps(options).encode())
        job=self.browser('/api/status')['jobs'][0]
        self.assertEqual(len(job['slice_options']['filaments']),2)
        self.assertEqual(job['state'],'slice_queued')
        from cloud.test_slicing import result_file
        from slicer.worker import Worker
        import io
        import zipfile
        token='slicer-test-token-at-least-32-characters'
        with patch.dict('os.environ',{'SLICER_TOKEN':token,'SLICER_ALLOW_INTERNAL_HTTP':'1'}):
            worker=Worker(self.cloud_url,token,self.directory.name,'/fake')
            def fake_slice(data,options,destination,profiles,binary):
                output=io.BytesIO()
                with zipfile.ZipFile(io.BytesIO(result_file())) as original, zipfile.ZipFile(output,'w') as archive:
                    for name in original.namelist():
                        body=original.read(name)
                        if name=='Metadata/slice_info.config':
                            body=body.replace(b'</plate>',b'<filament id="2" type="PLA" color="#000000" used_g="1"/></plate>')
                        if name=='Metadata/project_settings.config':
                            config=json.loads(body);config['filament_settings_id']*=2;body=json.dumps(config).encode()
                        archive.writestr(name,body)
                path=destination/'result.gcode.3mf';path.write_bytes(output.getvalue());return path
            with patch('slicer.worker.run_slice',side_effect=fake_slice):worker.tick()
        job=self.browser('/api/status')['jobs'][0]
        self.assertEqual(job['state'],'queued')
        self.assertEqual(len(job['analysis']['filaments']),2)
        payload={'confirm_job_id':job_id,'filaments':[{'filament_id':i,'ams_id':0,'slot':1} for i in (1,2)],'bed_clear':True,'accept_differences':True,'preview_checked':True}
        self.browser('/api/jobs/'+job_id+'/approve',json.dumps(payload).encode())
        self.agent.tick()
        self.assertEqual(self.browser('/api/status')['jobs'][0]['state'],'simulated')
        from print_plan import mapping
        selection=self.browser('/api/status')['jobs'][0]['selection']
        self.assertEqual(json.loads(mapping(selection)['ams_mapping']),[0,0])
