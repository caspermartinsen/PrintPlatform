import math
import io
import posixpath
from pathlib import PurePosixPath
from urllib.parse import unquote
import json
import re
import struct
import xml.etree.ElementTree as ET
import zipfile


def stl_info(data):
    if not 84 <= len(data) <= 50 * 1024**2:
        raise ValueError('STL skal være højst 50 MiB og indeholde en 3D-model.')
    count = struct.unpack_from('<I', data, 80)[0]
    if 84 + count * 50 == len(data):
        if not 0 < count <= 1000000:
            raise ValueError('For mange trekanter i STL.')
        vertices = (triangle[offset:offset + 3] for triangle in struct.iter_unpack('<12fH', data[84:]) for offset in (3, 6, 9))
    else:
        try:
            text = data.decode('ascii')
        except UnicodeDecodeError as error:
            raise ValueError('Ugyldig STL.') from error
        matches = re.findall(r'\bvertex\s+([^\s]+)\s+([^\s]+)\s+([^\s]+)', text)
        if not text.lstrip().startswith('solid') or not text.rstrip().endswith('endsolid') and 'endsolid ' not in text:
            raise ValueError('Ugyldig ASCII STL.')
        if not matches or len(matches) % 3 or len(matches) > 3000000:
            raise ValueError('Ugyldige STL-trekanter.')
        count = len(matches) // 3
        vertices = (tuple(float(value) for value in vertex) for vertex in matches)
    minimum, maximum = [math.inf] * 3, [-math.inf] * 3
    for vertex in vertices:
        for axis, value in enumerate(vertex):
            if not math.isfinite(value) or abs(value) > 1000000:
                raise ValueError('Ugyldige modelkoordinater.')
            minimum[axis] = min(minimum[axis], value)
            maximum[axis] = max(maximum[axis], value)
    dimensions = [round(maximum[axis] - minimum[axis], 3) for axis in range(3)]
    if any(value <= 0 or value > 256 for value in dimensions):
        raise ValueError('Modellen skal have volumen og passe på P2S-pladen (256 × 256 × 256 mm).')
    return {'triangles': count, 'dimensions_mm': dimensions}


def project_archive(data):
    if not 0 < len(data) <= 100 * 1024**2:
        raise ValueError('3MF skal være højst 100 MiB.')
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        entries = archive.infolist()
        if len(entries) > 4096 or sum(item.file_size for item in entries) > 512 * 1024**2:
            raise ValueError('3MF-arkivet er for stort.')
        names = set()
        for item in entries:
            name = item.filename
            if (name in names or '\\' in name or PurePosixPath(name).is_absolute()
                    or '..' in PurePosixPath(name).parts or item.flag_bits & 1
                    or (item.external_attr >> 16) & 0o170000 == 0o120000):
                raise ValueError('Ugyldigt 3MF-arkiv.')
            names.add(name)
            with archive.open(item) as source:
                while source.read(65536):
                    pass
        if '[Content_Types].xml' not in names or '3D/3dmodel.model' not in names:
            raise ValueError('Filen mangler en 3MF-model.')
        return archive
    except (zipfile.BadZipFile, RuntimeError, EOFError, NotImplementedError) as error:
        raise ValueError('Ugyldigt 3MF-arkiv.') from error


def safe_xml(data):
    text = data.decode('utf-8-sig')
    if '\x00' in text or '<!DOCTYPE' in text.upper() or '<!ENTITY' in text.upper():
        raise ValueError('XML-entiteter understøttes ikke.')
    try:
        return ET.fromstring(text)
    except ET.ParseError as error:
        raise ValueError('Ugyldige 3MF-modeldata.') from error


def standard_project(data):
    """Translate core 3MF object materials into Bambu part/extruder metadata."""
    with project_archive(data) as archive:
        names = set(archive.namelist())
        if 'Metadata/project_settings.config' in names or 'Metadata/model_settings.config' in names:
            return data
        ns = {'m':'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'}
        if archive.getinfo('3D/3dmodel.model').file_size > 64 * 1024**2:
            raise ValueError('En 3MF-modeldel er for stor.')
        root = safe_xml(archive.read('3D/3dmodel.model'))
        resources = root.find('m:resources',ns)
        build = root.find('m:build',ns)
        if resources is None or build is None:
            raise ValueError('3MF mangler modelressourcer eller byggeplade.')
        objects = {item.get('id'):item for item in resources.findall('m:object',ns)}
        if len(objects) != len(resources.findall('m:object',ns)):
            raise ValueError('Gentagne 3MF-objekt-id’er.')
        palettes = {item.get('id'):list(item) for item in resources.findall('m:basematerials',ns)}
        colors, assignments = [], {}
        for object_id, item in objects.items():
            mesh = item.find('m:mesh',ns)
            if mesh is None:
                continue
            pid, index = item.get('pid'), item.get('pindex','0')
            properties = set()
            for triangle in mesh.findall('m:triangles/m:triangle',ns):
                triangle_pid = triangle.get('pid',pid)
                indices = [triangle.get('p1',index)]
                indices += [triangle.get(key,indices[0]) for key in ('p2','p3')]
                properties.update((triangle_pid,value) for value in indices)
            if len(properties) > 1:
                raise ValueError('Standard-3MF med flere materialer på samme mesh skal opdeles i farvede dele.')
            if properties:
                pid, index = next(iter(properties))
            color = '#7282DD'
            if pid is not None:
                try:
                    number = int(index)
                    if number < 0: raise ValueError('Ugyldigt materialeindeks.')
                    color = palettes[pid][number].get('displaycolor','')
                except (KeyError,IndexError,TypeError) as error:
                    raise ValueError('Standard-3MF refererer til et ukendt materiale.') from error
                if not re.fullmatch(r'#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?',color):
                    raise ValueError('Ugyldig standard-3MF-materialefarve.')
            color = color[:7].upper()
            if color not in colors: colors.append(color)
            assignments[object_id] = colors.index(color)+1
        if not assignments or len(colors)>16:
            raise ValueError('Standard-3MF skal have 1–16 filamentfarver.')
        config = ET.Element('config')
        def leaves(object_id, trail=()):
            if len(trail) > 32 or object_id in trail or object_id not in objects:
                raise ValueError('Ugyldig eller cirkulær 3MF-samling.')
            if object_id in assignments:
                return [object_id]
            result = []
            for child in objects[object_id].findall('m:components/m:component',ns):
                result.extend(leaves(child.get('objectid'),trail+(object_id,)))
            return result
        for object_id, item in objects.items():
            entry = ET.SubElement(config,'object',id=object_id)
            ET.SubElement(entry,'metadata',key='name',value=item.get('name','Model'))
            for leaf in leaves(object_id):
                part = ET.SubElement(entry,'part',id=leaf,subtype='normal_part')
                ET.SubElement(part,'metadata',key='extruder',value=str(assignments[leaf]))
            if object_id in assignments:
                ET.SubElement(entry,'metadata',key='extruder',value=str(assignments[object_id]))
        plate = ET.SubElement(config,'plate')
        ET.SubElement(plate,'metadata',key='plater_id',value='1')
        for index,item in enumerate(build.findall('m:item',ns)):
            leaves(item.get('objectid'))
            instance = ET.SubElement(plate,'model_instance')
            for key,value in (('object_id',item.get('objectid')),('instance_id',str(index)),('identify_id',str(index+1))):
                ET.SubElement(instance,'metadata',key=key,value=value)
        settings = {'filament_colour':colors,'filament_type':['PLA']*len(colors),
                    'filament_settings_id':['']*len(colors)}
        output = io.BytesIO()
        with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as target:
            for entry in archive.infolist():target.writestr(entry,archive.read(entry))
            target.writestr('Metadata/project_settings.config',json.dumps(settings))
            target.writestr('Metadata/model_settings.config',ET.tostring(config,encoding='utf-8',xml_declaration=True))
        return output.getvalue()


def project_info(data):
    data = standard_project(data)
    with project_archive(data) as archive:
        names = set(archive.namelist())
        if any(re.fullmatch(r'Metadata/plate_\d+\.gcode', name) for name in names):
            raise ValueError('Upload færdigslicede filer som .gcode.3mf.')
        for name in ('Metadata/project_settings.config','Metadata/model_settings.config'):
            if name not in names or archive.getinfo(name).file_size > 2 * 1024**2:
                raise ValueError('Gem projektet som 3MF i Bambu Studio med farver og én plade.')
        settings = json.loads(archive.read('Metadata/project_settings.config'))
        config = safe_xml(archive.read('Metadata/model_settings.config'))
        plates = config.findall('plate')
        if len(plates) != 1:
            raise ValueError('3MF-projektet skal indeholde præcis én plade.')
        if not isinstance(settings,dict):
            raise ValueError('Ugyldige 3MF-indstillinger.')
        colors, materials = settings.get('filament_colour'), settings.get('filament_type')
        profiles = settings.get('filament_settings_id', [])
        if (not isinstance(colors,list) or not 1 <= len(colors) <= 16
                or not isinstance(materials,list) or len(materials) != len(colors)
                or not isinstance(profiles,list)):
            raise ValueError('3MF-projektet mangler filament- og farveoplysninger.')
        filaments = []
        for index, color in enumerate(colors):
            if not isinstance(color,str) or not re.fullmatch(r'#[A-Fa-f0-9]{6}(?:[A-Fa-f0-9]{2})?',color):
                raise ValueError('3MF-projektet har en ugyldig farve.')
            if not isinstance(materials[index],str) or not materials[index]:
                raise ValueError('3MF-projektet mangler materialetype.')
            filaments.append({'id':index+1,'material':materials[index][:40],'color':color[:7].upper(),
                              'profile':str(profiles[index])[:120] if index < len(profiles) else ''})
        vertices = 0
        for name in names:
            if name.endswith('.rels'):
                if archive.getinfo(name).file_size > 1024**2:
                    raise ValueError('3MF-relationen er for stor.')
                relations = safe_xml(archive.read(name))
                for relation in relations:
                    target = unquote(relation.get('Target','')).split('#',1)[0]
                    if relation.get('TargetMode','').lower() == 'external' or ':' in target or '\\' in target or target.startswith('//'):
                        raise ValueError('Eksterne 3MF-referencer understøttes ikke.')
                    base = '' if name == '_rels/.rels' else posixpath.dirname(posixpath.dirname(name))
                    resolved = posixpath.normpath(target.lstrip('/') if target.startswith('/') else posixpath.join(base,target))
                    if not target or resolved == '..' or resolved.startswith('../') or resolved not in names:
                        raise ValueError('3MF-relationen skal pege på en fil inde i arkivet.')
            if not name.lower().endswith('.model'):
                continue
            if archive.getinfo(name).file_size > 64 * 1024**2:
                raise ValueError('En 3MF-modeldel er for stor.')
            xml = archive.read(name)
            text = xml.decode('utf-8-sig')
            if '\x00' in text or '<!DOCTYPE' in text.upper() or '<!ENTITY' in text.upper():
                raise ValueError('XML-entiteter understøttes ikke.')
            try:
                for event, element in ET.iterparse(io.BytesIO(xml), events=('start','end')):
                    tag = element.tag.rsplit('}',1)[-1]
                    if event == 'start' and tag == 'model' and element.get('unit','millimeter') != 'millimeter':
                        raise ValueError('3MF-projektet skal være i millimeter.')
                    if event == 'end':
                        if tag == 'vertex':
                            values = [float(element.get(axis,'nan')) for axis in ('x','y','z')]
                            if not all(math.isfinite(value) and abs(value) <= 1000000 for value in values):
                                raise ValueError('Ugyldige 3MF-koordinater.')
                            vertices += 1
                            if vertices > 3000000:
                                raise ValueError('For mange modelpunkter.')
                        for key,value in element.attrib.items():
                            if key.rsplit('}',1)[-1] == 'path':
                                value = unquote(value)
                                resolved = posixpath.normpath(value.lstrip('/') if value.startswith('/') else posixpath.join(posixpath.dirname(name),value))
                                if '\\' in value or ':' in value or resolved.startswith('../') or resolved not in names:
                                    raise ValueError('Modelreferencen skal pege inde i 3MF-filen.')
                        element.clear()
            except ET.ParseError as error:
                raise ValueError('Ugyldig 3MF-model.') from error
        if not vertices:
            raise ValueError('3MF-projektet indeholder ingen model.')
    return {'kind':'3mf','dimensions_mm':None,'filaments':filaments,'filament_count':len(filaments)}


def safe_layer_changes(data, filaments):
    if len(data) > 1024**2:
        raise ValueError('For mange lagbaserede skift.')
    source = safe_xml(data)
    if source.tag != 'custom_gcodes_per_layer':
        raise ValueError('Ugyldige lagbaserede farveskift.')
    plates = source.findall('plate')
    if len(plates) > 1:
        raise ValueError('Lagbaserede skift skal høre til én plade.')
    entries = plates[0] if plates else source
    if plates:
        info = entries.find('plate_info')
        if info is not None and info.get('id') != '1':
            raise ValueError('Farveskiftet skal høre til den første plade.')
    root = ET.Element('custom_gcodes_per_layer')
    plate = ET.SubElement(root,'plate')
    ET.SubElement(plate,'plate_info',id='1')
    count = 0
    for layer in entries.findall('layer'):
        is_tool_change = layer.get('type') == '2' or layer.get('type') is None and layer.get('gcode') == 'tool_change'
        if not is_tool_change:
            continue
        z, index = float(layer.get('top_z','nan')), int(layer.get('extruder','0'))
        if not math.isfinite(z) or not 0 < z <= 256 or not 1 <= index <= len(filaments):
            raise ValueError('Ugyldigt lag eller filament til farveskift.')
        ET.SubElement(plate,'layer',top_z=str(z),type='2',extruder=str(index),
                      color=filaments[index-1]['color'],extra='',gcode='tool_change')
        count += 1
    if not count:
        return None
    ET.SubElement(plate,'mode',value='MultiAsSingle')
    return ET.tostring(root,encoding='utf-8',xml_declaration=True)


def prepare_project(data, path, filaments):
    """Preserve meshes, assembly and paint; replace imported executable settings."""
    data = standard_project(data)
    info = project_info(data)
    if len(filaments) != info['filament_count']:
        raise ValueError('Alle projektets filamenter skal have en profil.')
    settings = {'filament_colour':[item['color'] for item in filaments],
                'filament_type':['PLA'] * len(filaments),
                'filament_settings_id':[item['profile_name'] for item in filaments],
                'filament_diameter':['1.75'] * len(filaments)}
    with project_archive(data) as source, zipfile.ZipFile(path,'w',compression=zipfile.ZIP_DEFLATED) as target:
        for entry in source.infolist():
            name = entry.filename
            if name == 'Metadata/custom_gcode_per_layer.xml':
                changes = safe_layer_changes(source.read(entry),filaments)
                if changes is not None:
                    target.writestr(name,changes)
                continue
            if name == 'Metadata/project_settings.config' or name.endswith('.gcode') or name in (
                    'Metadata/layer_config_ranges.xml',
                    'Metadata/layer_heights_profile.txt','Metadata/filament_sequence.json'):
                continue
            content = source.read(entry)
            if name == 'Metadata/model_settings.config':
                config = safe_xml(content)
                allowed = {'name','extruder','matrix','plater_id','plate_name','object_id','instance_id','identify_id'}
                for parent in config.iter():
                    for child in list(parent):
                        if child.tag == 'metadata' and child.get('key') not in allowed:
                            parent.remove(child)
                content = ET.tostring(config,encoding='utf-8',xml_declaration=True)
            target.writestr(name,content)
        target.writestr('Metadata/project_settings.config',json.dumps(settings))


def sliced_preview(path):
    layers, filament_layers = {}, {}
    position = {'X':0.0,'Y':0.0,'Z':0.0,'E':0.0}
    absolute_xyz, absolute_e = True, True
    started = False
    count, omitted, tool = 0, 0, 0
    with zipfile.ZipFile(path) as archive:
        root = safe_xml(archive.read('Metadata/slice_info.config'))
        settings = json.loads(archive.read('Metadata/project_settings.config'))
        names = settings.get('filament_settings_id',[])
        filaments = [{'id':int(item.get('id')),'color':item.get('color','#5363CF'),'material':item.get('type',''),
                      'profile':names[int(item.get('id'))-1] if len(names) >= int(item.get('id')) else ''}
                     for item in root.findall('.//plate/filament')
                     if max(float(item.get('used_g','0')),float(item.get('used_m','0'))) > 0]
        if filaments:
            tool = filaments[0]['id'] - 1
        with archive.open('Metadata/plate_1.gcode') as source:
            for raw in source:
                line = raw.decode('ascii',errors='ignore').strip()
                if line.startswith('; CHANGE_LAYER') or line.startswith(';LAYER_CHANGE'):
                    started = True
                command = line.split(';',1)[0].strip()
                if not command:
                    continue
                operation = command.split()[0]
                tool_change = re.fullmatch(r'T(\d+)',operation)
                if tool_change and 0 <= int(tool_change[1]) < 16:
                    tool = int(tool_change[1])
                if operation == 'G90': absolute_xyz = True
                if operation == 'G91': absolute_xyz = False
                if operation == 'M82': absolute_e = True
                if operation == 'M83': absolute_e = False
                values = {key:float(value) for key,value in re.findall(r'([XYZE])\s*(-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)',command)}
                if not all(math.isfinite(value) for value in values.values()):
                    continue
                if operation == 'G92':
                    position.update(values)
                    continue
                if operation not in ('G0','G1','G2','G3'):
                    continue
                previous = position.copy()
                for key,value in values.items():
                    absolute = absolute_e if key == 'E' else absolute_xyz
                    position[key] = value if absolute else position[key] + value
                if not started or position['E'] <= previous['E'] or not any(key in values for key in ('X','Y')):
                    continue
                if not all(0 <= position[key] <= 256 and 0 <= previous[key] <= 256 for key in ('X','Y','Z')):
                    continue
                height = round(position['Z'],3)
                segments = layers.setdefault(height,[])
                ids = filament_layers.setdefault(height,[])
                if operation in ('G2','G3') or count >= 200000:
                    omitted += 1
                    continue
                segments.append([round(previous['X'],3),round(previous['Y'],3),round(position['X'],3),round(position['Y'],3)])
                ids.append(tool+1)
                count += 1
        metadata = {item.get('key'):item.get('value') for item in root.findall('.//plate/metadata')}
        used = sum(float(item.get('used_g','0')) for item in root.findall('.//plate/filament'))
        seconds = float(metadata.get('prediction',0))
        if not math.isfinite(used) or not math.isfinite(seconds) or used < 0 or seconds < 0:
            raise ValueError('Ugyldige slicerestimater.')
    return {'layers':[{'z':height,'segments':segments,'filament_ids':filament_layers[height]} for height,segments in sorted(layers.items())],
            'filaments':filaments,'estimated_seconds':seconds,'filament_grams':round(used,2),
            'printer_model_id':metadata.get('printer_model_id'),'nozzle_diameters':metadata.get('nozzle_diameters'),
            'bed_type':settings.get('curr_bed_type'),'layer_height':float(settings.get('layer_height',0)),
            'infill':settings.get('sparse_infill_density'),'omitted_segments':omitted,'segment_count':count}


def is_sliced_3mf(data):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return 'Metadata/plate_1.gcode' in archive.namelist()
    except zipfile.BadZipFile:
        return False
