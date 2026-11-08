"""Runs only in the isolated slicer image; never connects to a printer."""
import os
from pathlib import Path
import tempfile
import unittest

from cloud.model_files import sliced_preview
from slicer.fixtures import project_file, standard_project_file
from slicer.worker import run_slice


@unittest.skipUnless(os.environ.get('RUN_SLICER_SMOKE') == '1','Requires the slicer container')
class SlicerSmokeTests(unittest.TestCase):
    def check_project(self,layered=False,standard=False):
        options={'source_kind':'3mf','layer_height':0.2,'infill':15,'filaments':[
            {'filament_id':1,'profile':'pla-basic','profile_name':'PLA Basic','color':'#FF0000'},
            {'filament_id':2,'profile':'pla-basic','profile_name':'PLA Basic','color':'#0000FF'}]}
        with tempfile.TemporaryDirectory() as directory:
            result=run_slice(standard_project_file() if standard else project_file(layered=layered),options,Path(directory),Path('/profiles'),'/usr/local/bin/bambu-headless')
            preview=sliced_preview(result)
            self.assertGreater(preview['segment_count'],0)
            self.assertEqual({item['id'] for item in preview['filaments']},{1,2})
            self.assertEqual({item['color'].upper() for item in preview['filaments']},{'#FF0000','#0000FF'})
            seen={index for layer in preview['layers'] for index in layer['filament_ids']}
            self.assertEqual(seen,{1,2})


    def test_real_two_color_project_slicing(self):
        self.check_project()

    def test_real_layer_based_ams_color_change(self):
        self.check_project(layered=True)

    def test_real_standard_3mf_assembly_at_origin(self):
        self.check_project(standard=True)
