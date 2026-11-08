import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
import zipfile

from cloud.model_files import stl_info, sliced_preview, three_mf_info
from slicer.worker import normalize_model


def model():
    vertices=[(0,0,0),(10,0,0),(0,10,0),(0,0,10)]
    faces=[(0,2,1),(0,1,3),(1,2,3),(2,0,3)]
    data=b'solid binary'.ljust(80,b'\0')+struct.pack('<I',len(faces))
    for face in faces:
        data+=struct.pack('<12fH',0,0,0,*(value for index in face for value in vertices[index]),0)
    return data


def result_file():
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w') as archive:
        archive.writestr('[Content_Types].xml','<Types/>')
        archive.writestr('Metadata/project_settings.config',json.dumps({'printer_model':'Bambu Lab P2S','curr_bed_type':'Textured PEI Plate','layer_height':'0.2','sparse_infill_density':'15%','filament_settings_id':['Bambu PLA Basic @BBL P2S']}))
        archive.writestr('Metadata/slice_info.config','<config><plate><metadata key="prediction" value="600"/><metadata key="printer_model_id" value="N7"/><metadata key="nozzle_diameters" value="0.4"/><filament id="1" type="PLA" color="#000000" used_g="1.2"/></plate></config>')
        archive.writestr('Metadata/plate_1.gcode','G90\nM83\nG1 X100 Y100 Z0.2\n; CHANGE_LAYER\nG1 X110 Y100 E1\nG1 X110 Y110 E1\nG1 X100 Y100\n')
    return output.getvalue()


class ModelTests(unittest.TestCase):
    def test_binary_and_ascii_stl(self):
        self.assertEqual(stl_info(model())['dimensions_mm'],[10,10,10])
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'model.stl';normalize_model(model(),path)
            self.assertEqual(stl_info(path.read_bytes())['triangles'],4)

    def test_invalid_and_oversize_models(self):
        for data in (b'not stl',model()[:-1],model().replace(struct.pack('<f',10),struct.pack('<f',300)),model().replace(struct.pack('<f',10),struct.pack('<f',float('nan')))):
            with self.subTest(size=len(data)), self.assertRaises(ValueError):stl_info(data)

    def test_preview_excludes_travel_and_reports_estimates(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'result.3mf';path.write_bytes(result_file())
            preview=sliced_preview(path)
        self.assertEqual(preview['segment_count'],2)
        self.assertEqual(preview['estimated_seconds'],600)
        self.assertEqual(preview['layers'][0]['z'],0.2)
        self.assertEqual(preview['layers'][0]['segments'][0],[100,100,110,100])


def project_file(colors=None):
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w') as archive:
        archive.writestr('[Content_Types].xml','<Types/>')
        archive.writestr('3D/3dmodel.model','<model unit="millimeter"><resources><object id="1"><mesh><vertices><vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/><vertex x="0" y="10" z="10"/></vertices><triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object></resources><build><item objectid="1"/></build></model>')
        archive.writestr('Metadata/project_settings.config',json.dumps({'filament_colour':colors or ['#000000','#FFFFFF']}))
    return output.getvalue()


class ProjectTests(unittest.TestCase):
    def test_project_colors_preserved_and_invalid_archive_rejected(self):
        info=three_mf_info(project_file())
        self.assertEqual(info['format'],'3mf')
        self.assertEqual(info['filaments'],[{'id':1,'color':'#000000'},{'id':2,'color':'#FFFFFF'}])
        for data in (b'bad zip',project_file(['invalid'])):
            with self.assertRaises(ValueError):three_mf_info(data)

    def test_worker_keeps_project_and_loads_each_trusted_filament(self):
        from unittest.mock import patch, MagicMock
        from slicer.worker import run_slice
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);profiles=root/'profiles';profiles.mkdir()
            (profiles/'process.json').write_text('{}')
            (profiles/'pla-basic.json').write_text('{}')
            destination=root/'output';destination.mkdir()
            (destination/'result.gcode.3mf').write_bytes(result_file())
            child=MagicMock();child.__enter__.return_value=child;child.wait.return_value=0
            item={'profile':'pla-basic','color':'#000000'}
            options={**item,'layer_height':0.2,'infill':15,'filaments':[item,{**item,'color':'#FFFFFF'}]}
            with patch('slicer.worker.subprocess.Popen',return_value=child) as launch:
                run_slice(project_file(),options,destination,profiles,'fake-slicer')
            command=launch.call_args.args[0]
            self.assertEqual((destination/'model.3mf').read_bytes(),project_file())
            self.assertEqual(len(command[command.index('--load-filaments')+1].split(';')),2)
            self.assertEqual(json.loads((destination/'filament-1.json').read_text())['filament_colour'],['#FFFFFF'])

    def test_multiple_plates_and_xml_entities_rejected(self):
        for name, body in [('Metadata/model_settings.config','<config><plate/><plate/></config>'),('3D/evil.model','<!DOCTYPE model><model/>')]:
            output=io.BytesIO(project_file())
            with zipfile.ZipFile(output,'a') as archive:archive.writestr(name,body)
            with self.assertRaises(ValueError):three_mf_info(output.getvalue())

    def test_preview_tracks_tool_changes(self):
        output=io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(result_file())) as source, zipfile.ZipFile(output,'w') as target:
            for name in source.namelist():
                data=source.read(name)
                if name=='Metadata/plate_1.gcode':data=data.replace(b'G1 X110 Y110 E1',b'T1\nG1 X110 Y110 E1')
                target.writestr(name,data)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'result.3mf';path.write_bytes(output.getvalue())
            self.assertEqual(sliced_preview(path)['layers'][0]['tools'],[0,1])
