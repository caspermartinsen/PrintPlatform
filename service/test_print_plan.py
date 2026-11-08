import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from print_plan import analyze, select, mapping, live_ready, printer_available


class PrintPlanTests(unittest.TestCase):
    def test_live_requires_idle_matching_profile_and_p2s(self):
        analysis={'printer_model':'Bambu Lab P2S'}
        printer={'connected':True,'stale':False,'state':'IDLE','error_code':0}
        live_ready(analysis,printer,{'warnings':[]})
        for state in ('RUNNING','PAUSE','FAILED','unknown'):
            with self.assertRaises(ValueError): live_ready(analysis,{**printer,'state':state},{'warnings':[]})
        with self.assertRaises(ValueError): live_ready({'printer_model':'Other'},printer,{'warnings':[]})
        with self.assertRaises(ValueError): live_ready(analysis,printer,{'warnings':['Filamentprofilen afviger.']})

    def test_cancelled_print_requires_fresh_status_no_error_and_zero_remaining(self):
        printer={'connected':True,'stale':False,'state':'FAILED','error_code':0,'remaining_minutes':0}
        self.assertTrue(printer_available(printer))
        live_ready({'printer_model':'Bambu Lab P2S'},printer,{'warnings':[]})
        for change in ({'error_code':1},{'error_code':None},{'error_code':False},
                       {'remaining_minutes':1},{'remaining_minutes':None},{'remaining_minutes':False},
                       {'connected':False},{'stale':True},{'stale':None},
                       {'state':'RUNNING'},{'state':'PAUSE'},{'state':'PREPARE'},{'state':'SLICING'},{'state':'unknown'}):
            with self.subTest(change=change):
                self.assertFalse(printer_available({**printer,**change}))
                with self.assertRaises(ValueError):
                    live_ready({'printer_model':'Bambu Lab P2S'},{**printer,**change},{'warnings':[]})
        with self.assertRaises(ValueError):
            select(self.analysis,{**printer,'ams_slots':[self.slot]},{**self.payload,'bed_clear':False})
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'model.3mf'
        self.analysis = {'supported':True, 'filaments':[{'id':1,'material':'PLA','profile':'Bambu PLA Matte @BBL P2S','color':'#FFFFFF'}]}
        self.slot = {'ams_id':0,'slot':2,'present':True,'stale':False,'material':'PLA','name':'PLA Basic','color':'000000FF'}
        self.printer = {'connected':True,'stale':False,'ams_slots':[self.slot]}
        self.payload = {'ams_id':0,'slot':2,'bed_clear':True,'accept_differences':True}

    def tearDown(self):
        self.directory.cleanup()

    def file(self, filaments):
        with zipfile.ZipFile(self.path, 'w') as archive:
            archive.writestr('Metadata/project_settings.config', json.dumps({'filament_settings_id':['PLA Matte','PETG','PLA Basic','PLA Matte']}))
            archive.writestr('Metadata/slice_info.config', '<config><plate>' + filaments + '</plate></config>')
        return analyze(self.path)

    def test_used_filaments_not_saved_profiles(self):
        analysis = self.file('<filament id="1" type="PLA" used_g="10" color="#FFFFFF"/><filament id="2" type="PETG" used_g="0"/>')
        self.assertTrue(analysis['supported'])
        self.assertEqual(len(analysis['filaments']), 1)
        self.assertFalse(self.file('<filament id="1" type="PLA" used_g="1"/><filament id="2" type="PETG" used_g="2"/>')['supported'])
        self.assertFalse(self.file('')['supported'])
        self.assertFalse(self.file('<filament id="3" type="PLA" used_g="2"/>')['supported'])

    def test_warning_acceptance_and_mapping(self):
        selection = select(self.analysis, self.printer, self.payload)
        self.assertEqual(len(selection['warnings']), 2)
        self.assertEqual(json.loads(mapping(selection)['ams_mapping']), [1])
        self.assertEqual(json.loads(mapping(selection)['ams_mapping2']), [{'ams_id':0,'slot_id':1}])
        with self.assertRaises(ValueError):
            select(self.analysis, self.printer, {**self.payload, 'accept_differences':False})

    def test_empty_stale_wrong_material_and_bed(self):
        for change in ({'present':False}, {'stale':True}, {'material':'PETG'}, {'material':None}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                select(self.analysis, {**self.printer,'ams_slots':[{**self.slot,**change}]}, self.payload)
        for change in ({'bed_clear':False}, {'slot':True}, {'slot':5}, {'ams_id':-1}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                select(self.analysis, self.printer, {**self.payload, **change})
        with self.assertRaises(ValueError):
            select(self.analysis, {**self.printer,'stale':True}, self.payload)

    def test_xml_entities_refused(self):
        with zipfile.ZipFile(self.path,'w') as archive:
            archive.writestr('Metadata/slice_info.config', '<!DOCTYPE config [<!ENTITY name "PLA">]><config/>')
        self.assertFalse(analyze(self.path)['supported'])
