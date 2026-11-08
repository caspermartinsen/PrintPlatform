import math
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


def sliced_preview(path):
    layers = {}
    position = {'X': 0.0, 'Y': 0.0, 'Z': 0.0, 'E': 0.0}
    absolute_xyz, absolute_e = True, True
    started = False
    count, omitted = 0, 0
    with zipfile.ZipFile(path) as archive:
        with archive.open('Metadata/plate_1.gcode') as source:
            for raw in source:
                line = raw.decode('ascii', errors='ignore').strip()
                if line.startswith('; CHANGE_LAYER') or line.startswith(';LAYER_CHANGE'):
                    started = True
                command = line.split(';', 1)[0].strip()
                if not command:
                    continue
                operation = command.split()[0]
                if operation == 'G90': absolute_xyz = True
                if operation == 'G91': absolute_xyz = False
                if operation == 'M82': absolute_e = True
                if operation == 'M83': absolute_e = False
                values = {key: float(value) for key, value in re.findall(r'([XYZE])\s*(-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)', command)}
                if not all(math.isfinite(value) for value in values.values()):
                    continue
                if operation == 'G92':
                    position.update(values)
                    continue
                if operation not in ('G0', 'G1', 'G2', 'G3'):
                    continue
                previous = position.copy()
                for key, value in values.items():
                    absolute = absolute_e if key == 'E' else absolute_xyz
                    position[key] = value if absolute else position[key] + value
                if not started or position['E'] <= previous['E'] or not any(key in values for key in ('X', 'Y')):
                    continue
                if not all(0 <= position[key] <= 256 and 0 <= previous[key] <= 256 for key in ('X', 'Y', 'Z')):
                    continue
                height = round(position['Z'], 3)
                segments = layers.setdefault(height, [])
                if operation in ('G2', 'G3') or count >= 200000:
                    omitted += 1
                    continue
                segments.append([round(previous['X'], 3), round(previous['Y'], 3), round(position['X'], 3), round(position['Y'], 3)])
                count += 1
        root = ET.fromstring(archive.read('Metadata/slice_info.config'))
        settings=json.loads(archive.read('Metadata/project_settings.config'))
        metadata = {item.get('key'): item.get('value') for item in root.findall('.//plate/metadata')}
        used = sum(float(item.get('used_g', '0')) for item in root.findall('.//plate/filament'))
        seconds=float(metadata.get('prediction',0))
        if not math.isfinite(used) or not math.isfinite(seconds) or used<0 or seconds<0:
            raise ValueError('Ugyldige slicerestimater.')
    return {'layers': [{'z': height, 'segments': segments} for height, segments in sorted(layers.items())],
            'estimated_seconds': seconds, 'filament_grams': round(used, 2),
            'printer_model_id':metadata.get('printer_model_id'),'nozzle_diameters':metadata.get('nozzle_diameters'),'bed_type':settings.get('curr_bed_type'),
            'layer_height':float(settings.get('layer_height',0)),'infill':settings.get('sparse_infill_density'),
            'omitted_segments': omitted, 'segment_count': count}
