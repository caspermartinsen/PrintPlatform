import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import zipfile

from cloud import test_regressions
from cloud.model_files import project_info, prepare_project, sliced_preview
from print_plan import analyze, select, mapping, required_version, live_ready
from server import Store, Handler as PiHandler
from slicer.fixtures import project_file, sliced_file


def printer():
    return {'connected':True,'stale':False,'state':'IDLE','error_code':0,'ams_slots':[
        {'ams_id':0,'slot':index+1,'material':'PLA','name':'PLA Basic','color':color+'FF','present':True,'stale':False}
        for index,color in enumerate(('FF0000','0000FF','00FF00','FFFFFF'))]}


class MultiColorTests(unittest.TestCase):
    setUp = test_regressions.RegressionTests.setUp
    tearDown = test_regressions.RegressionTests.tearDown
    call = test_regressions.RegressionTests.call
    stored = test_regressions.RegressionTests.stored

    def report(self, version=3):
        return {'mode':'simulate','plan_version':version,'printer':printer(),'local_busy':False}

    def upload(self, name='colors.gcode.3mf', data=None):
        status, uploaded = self.call('/api/jobs',sliced_file() if data is None else data,{'X-Filename':name})
        self.assertEqual(status,201)
        return uploaded['id']

    def choices(self):
        return [{'filament_id':1,'ams_id':0,'slot':1},{'filament_id':2,'ams_id':0,'slot':2}]

    def approve(self, job_id):
        return self.call('/api/jobs/'+job_id+'/approve',
                         {'confirm_job_id':job_id,'bed_clear':True,'preview_checked':True,'filaments':self.choices()})

    def test_sliced_multicolor_3mf_and_sparse_filament_ids(self):
        job_id = self.upload(data=sliced_file(ids=(2,4)))
        analysis = json.loads(self.stored(job_id)['analysis'])
        self.assertTrue(analysis['supported'])
        self.assertEqual([item['id'] for item in analysis['filaments']],[2,4])
        self.assertEqual(required_version(analysis),3)
        selection = select(analysis,printer(),{'bed_clear':True,'filaments':[
            {'filament_id':2,'ams_id':0,'slot':1},{'filament_id':4,'ams_id':0,'slot':2}]})
        parameters = mapping(selection)
        self.assertEqual(json.loads(parameters['ams_mapping']),[-1,0,-1,1])
        self.assertEqual(json.loads(parameters['ams_mapping2']),[
            {'ams_id':255,'slot_id':255},{'ams_id':0,'slot_id':0},
            {'ams_id':255,'slot_id':255},{'ams_id':0,'slot_id':1}])

    def test_all_colors_are_required_without_duplicate_or_unknown_ids(self):
        job_id = self.upload()
        analysis = json.loads(self.stored(job_id)['analysis'])
        for choices in (self.choices()[:1],[self.choices()[0]]*2,
                        [self.choices()[0],{'filament_id':9,'ams_id':0,'slot':2}]):
            with self.assertRaises(ValueError):
                select(analysis,printer(),{'bed_clear':True,'filaments':choices})

    def test_changed_empty_or_wrong_material_second_spool_is_rejected(self):
        job_id = self.upload()
        analysis = json.loads(self.stored(job_id)['analysis'])
        for change in ({'stale':True},{'present':False},{'material':'PETG'}):
            current = printer();current['ams_slots'][1].update(change)
            with self.assertRaises(ValueError):
                select(analysis,current,{'bed_clear':True,'filaments':self.choices(),'accept_differences':True})
        current=printer();current['ams_slots'][1]['name']='PLA Matte'
        chosen=select(analysis,current,{'bed_clear':True,'filaments':self.choices(),'accept_differences':True})
        with self.assertRaises(ValueError):
            live_ready(analysis,current,chosen)

    def test_old_agent_cannot_approve_or_collect_multicolor(self):
        job_id = self.upload()
        self.call('/agent/poll',self.report(2))
        self.assertEqual(self.approve(job_id)[0],409)
        self.call('/agent/poll',self.report(3))
        self.assertEqual(self.approve(job_id)[0],200)
        self.assertIsNone(self.call('/agent/poll',self.report(2))[1]['job'])
        self.assertEqual(self.call('/agent/poll',self.report(3))[1]['job']['id'],job_id)

    def test_toolpath_preview_preserves_logical_colors(self):
        job_id = self.upload(data=sliced_file(ids=(2,4)))
        preview = json.loads(self.call('/api/jobs/'+job_id+'/preview')[1])
        self.assertEqual(preview['layers'][0]['filament_ids'],[2,2,4,4])
        self.assertEqual([item['id'] for item in preview['filaments']],[2,4])

    def test_sliced_file_named_plain_3mf_is_detected(self):
        job_id = self.upload(name='colors.3mf')
        self.assertEqual(self.stored(job_id)['state'],'queued')
        self.assertIsNone(self.stored(job_id)['model_info'])

    def test_project_colors_meshes_and_paint_are_preserved(self):
        data = project_file(painted=True)
        info = project_info(data)
        self.assertEqual(info['filament_count'],2)
        self.assertEqual(info['filaments'][1]['color'],'#0000FF')
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'project.3mf'
            options=[{'color':item['color'],'profile_name':'PLA Basic'} for item in info['filaments']]
            prepare_project(data,path,options)
            with zipfile.ZipFile(io.BytesIO(data)) as original, zipfile.ZipFile(path) as prepared:
                self.assertEqual(original.read('3D/3dmodel.model'),prepared.read('3D/3dmodel.model'))
                config=prepared.read('Metadata/model_settings.config')
                self.assertIn(b'value="2"',config)

    def test_project_custom_scripts_are_removed(self):
        data=project_file();output=io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data)) as source,zipfile.ZipFile(output,'w') as target:
            for name in source.namelist():
                content=source.read(name)
                if name=='Metadata/project_settings.config':
                    settings=json.loads(content);settings['post_process']=['DO_NOT_EXECUTE'];content=json.dumps(settings)
                if name=='Metadata/model_settings.config':
                    content=content.replace(b'<object id="1">',b'<object id="1"><metadata key="post_process" value="DO_NOT_EXECUTE"/>')
                target.writestr(name,content)
            target.writestr('Metadata/custom_gcode_per_layer.xml','<custom_gcodes_per_layer><layer top_z="0.6" type="4" extruder="1" color="" extra="DO_NOT_EXECUTE" gcode="DO_NOT_EXECUTE"/></custom_gcodes_per_layer>')
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'safe.3mf'
            prepare_project(output.getvalue(),path,[{'color':color,'profile_name':'PLA Basic'} for color in ('#FF0000','#0000FF')])
            with zipfile.ZipFile(path) as archive:
                self.assertNotIn('Metadata/custom_gcode_per_layer.xml',archive.namelist())
                self.assertNotIn(b'DO_NOT_EXECUTE',archive.read('Metadata/project_settings.config'))
                self.assertNotIn(b'DO_NOT_EXECUTE',archive.read('Metadata/model_settings.config'))

    def test_project_rejects_external_xml_entities_and_multiple_plates(self):
        for name,replacement in (
            ('_rels/.rels',b'<Relationships><Relationship TargetMode="External" Target="https://example.invalid/model"/></Relationships>'),
            ('_rels/.rels',b'<Relationships><Relationship Target="../../etc/passwd"/></Relationships>'),
            ('_rels/.rels',b'<Relationships><Relationship Target="%2e%2e/%2e%2e/etc/passwd"/></Relationships>'),
            ('Metadata/model_settings.config',b'<!DOCTYPE x [<!ENTITY x "bad">]><config/>'),
            ('Metadata/model_settings.config',b'<config><plate/><plate/></config>')):
            output=io.BytesIO()
            with zipfile.ZipFile(io.BytesIO(project_file())) as source,zipfile.ZipFile(output,'w') as target:
                for entry in source.namelist():
                    target.writestr(entry,replacement if entry==name else source.read(entry))
            self.assertEqual(self.call('/api/jobs',output.getvalue(),{'X-Filename':'bad.3mf'})[0],400)

    def test_complete_project_to_multicolor_local_simulation(self):
        with patch.dict('os.environ',{'SLICER_TOKEN':'s'*32}):
            job_id=self.upload('colors.3mf',project_file())
            self.call('/agent/poll',self.report())
            self.call('/slicer/heartbeat',{'slicer_version':2})
            options={'filaments':self.choices(),'layer_height':0.2,'infill':15}
            self.assertEqual(self.call('/api/jobs/'+job_id+'/slice',options)[0],202)
            self.assertIsNone(self.call('/slicer/poll',{})[1]['job'])
            command=self.call('/slicer/poll',{'slicer_version':2})[1]['job']
            self.assertEqual(command['id'],job_id)
            self.assertEqual(self.call('/slicer/jobs/'+job_id+'/result',sliced_file(),{'X-Slice-Token':command['slice_token']})[0],201)
            self.assertEqual(self.approve(job_id)[0],200)
            command=self.call('/agent/poll',self.report())[1]['job']
            payload=self.call('/agent/files/'+job_id)[1]
            store=Store(self.directory.name+'/pi')
            try:
                local=SimpleNamespace(store=store,token='local-token',telemetry=SimpleNamespace(snapshot=printer))
                local_id=self.call('/v1/jobs',payload,{'X-Filename':command['name']},PiHandler,local)[1]['id']
                start={'confirm_job_id':local_id,'printer_ready':True,'execution_mode':'simulate','selection':json.loads(command['selection'])}
                self.assertEqual(self.call('/v1/jobs/'+local_id+'/remote-start',start,handler_type=PiHandler,server=local)[0],202)
                for _ in range(100):
                    if store.jobs()[0]['state']!='starting':break
                    time.sleep(0.01)
                self.assertEqual(store.jobs()[0]['state'],'simulated')
                parameters=json.loads(store.jobs()[0]['plan'])
                self.assertEqual(json.loads(parameters['ams_mapping']),[0,1])
                self.assertEqual(json.loads(parameters['ams_mapping2']),[{'ams_id':0,'slot_id':0},{'ams_id':0,'slot_id':1}])
            finally:
                store.lock.close()

    def test_pi_rechecks_every_selected_spool(self):
        job_id=self.upload();self.call('/agent/poll',self.report());self.assertEqual(self.approve(job_id)[0],200)
        command=self.call('/agent/poll',self.report())[1]['job'];data=self.call('/agent/files/'+job_id)[1]
        store=Store(self.directory.name+'/pi')
        try:
            changed=printer();changed['ams_slots'][1]['color']='00FF00FF'
            local=SimpleNamespace(store=store,token='local-token',telemetry=SimpleNamespace(snapshot=lambda:changed))
            local_id=self.call('/v1/jobs',data,{'X-Filename':command['name']},PiHandler,local)[1]['id']
            payload={'confirm_job_id':local_id,'printer_ready':True,'execution_mode':'simulate','selection':json.loads(command['selection'])}
            self.assertEqual(self.call('/v1/jobs/'+local_id+'/remote-start',payload,handler_type=PiHandler,server=local)[0],400)
            self.assertEqual(store.jobs()[0]['state'],'queued')
        finally:
            store.lock.close()


    def test_four_colors_map_to_all_four_ams_slots(self):
        job_id = self.upload(data=sliced_file(ids=(1,2,3,4),colors=('#FF0000','#0000FF','#00FF00','#FFFFFF')))
        analysis=json.loads(self.stored(job_id)['analysis'])
        choices=[{'filament_id':index,'ams_id':0,'slot':index} for index in range(1,5)]
        plan=mapping(select(analysis,printer(),{'bed_clear':True,'filaments':choices}))
        self.assertEqual(json.loads(plan['ams_mapping']),[0,1,2,3])
        self.assertEqual(json.loads(plan['ams_mapping2']),[{'ams_id':0,'slot_id':index} for index in range(4)])


    def test_layer_tool_changes_survive_without_executable_user_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'layered.3mf'
            prepare_project(project_file(layered=True),path,[{'color':color,'profile_name':'PLA Basic'} for color in ('#FF0000','#0000FF')])
            with zipfile.ZipFile(path) as archive:
                data=archive.read('Metadata/custom_gcode_per_layer.xml')
                self.assertIn(b'type="2"',data)
                self.assertIn(b'extruder="2"',data)
                self.assertIn(b'gcode="tool_change"',data)
                self.assertIn(b'extra=""',data)


class StandardMaterialTests(unittest.TestCase):
    def standard_file(self):
        from slicer.fixtures import standard_project_file
        return standard_project_file()

    def test_standard_materials_become_part_filaments_without_changing_model(self):
        from cloud.model_files import safe_xml
        data=self.standard_file()
        info=project_info(data)
        self.assertEqual([f['color'] for f in info['filaments']],['#151515','#2879E8'])
        filaments=[{'color':f['color'],'profile_name':'PLA Basic'} for f in info['filaments']]
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'prepared.3mf'
            prepare_project(data,path,filaments)
            with zipfile.ZipFile(path) as prepared, zipfile.ZipFile(io.BytesIO(data)) as original:
                self.assertEqual(prepared.read('3D/3dmodel.model'),original.read('3D/3dmodel.model'))
                config=safe_xml(prepared.read('Metadata/model_settings.config'))
                parts=config.findall("object[@id='3']/part")
                self.assertEqual([(p.get('id'),p.find("metadata[@key='extruder']").get('value')) for p in parts],[('1','1'),('2','2')])

    def test_unknown_material_rejected(self):
        data=self.standard_file()
        output=io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(output,'w') as target:
            for name in source.namelist():
                content=source.read(name)
                if name=='3D/3dmodel.model':content=content.replace(b'pid="10"',b'pid="999"')
                target.writestr(name,content)
        with self.assertRaises(ValueError):project_info(output.getvalue())

    def test_standard_projects_arrange_as_assembly_but_native_projects_keep_placement(self):
        from unittest.mock import MagicMock
        from slicer.worker import run_slice
        from cloud.test_slicing import result_file
        options={'source_kind':'3mf','layer_height':0.2,'infill':15,'filaments':[
            {'filament_id':index+1,'profile':'pla-basic','profile_name':'PLA Basic','color':color}
            for index,color in enumerate(('#151515','#2879E8'))]}
        for data,arrange in ((self.standard_file(),'1'),(project_file(),'0')):
            with self.subTest(arrange=arrange), tempfile.TemporaryDirectory() as directory:
                root=Path(directory);profiles=root/'profiles';profiles.mkdir()
                (profiles/'process.json').write_text('{}')
                (profiles/'pla-basic.json').write_text('{}')
                output=root/'output';output.mkdir()
                (output/'result.gcode.3mf').write_bytes(result_file())
                child=MagicMock();child.__enter__.return_value=child;child.wait.return_value=0
                with patch('slicer.worker.subprocess.Popen',return_value=child) as launch:
                    run_slice(data,options,output,profiles,'fake-slicer')
                command=launch.call_args.args[0]
                self.assertEqual(command[command.index('--arrange')+1],arrange)
                with zipfile.ZipFile(output/'model.3mf') as prepared, zipfile.ZipFile(io.BytesIO(data)) as original:
                    self.assertEqual(prepared.read('3D/3dmodel.model'),original.read('3D/3dmodel.model'))
