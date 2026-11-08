import json
import xml.etree.ElementTree as ET
import zipfile


def analyze(path):
    result = {'supported': False, 'reason': 'Filen mangler verificerbare filamentoplysninger.', 'filaments': []}
    try:
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo('Metadata/slice_info.config')
            if info.file_size > 1024 * 1024:
                return result
            data = archive.read(info)
            if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
                return result
            root = ET.fromstring(data)
            plates = root.findall('plate')
            if len(plates) != 1:
                return result
            profiles = {}
            if 'Metadata/project_settings.config' in archive.namelist():
                if archive.getinfo('Metadata/project_settings.config').file_size > 2 * 1024**2:
                    return result
                profiles = json.loads(archive.read('Metadata/project_settings.config'))
                if not isinstance(profiles, dict):
                    return result
            result['printer_model'] = str(profiles.get('printer_model', 'Ukendt'))[:80]
            for filament in plates[0].findall('filament'):
                if max(float(filament.get('used_g', '0')), float(filament.get('used_m', '0'))) <= 0:
                    continue
                filament_id = int(filament.attrib['id'])
                if not 1 <= filament_id <= 16:
                    return result
                names = profiles.get('filament_settings_id', [])
                name = names[filament_id - 1] if isinstance(names, list) and len(names) >= filament_id else ''
                result['filaments'].append({'id': filament_id, 'material': filament.get('type', '')[:40],
                    'color': filament.get('color', '')[:9], 'profile': str(name)[:120]})
            if len(result['filaments']) != 1:
                result['reason'] = 'Kun print med ét anvendt filament understøttes.'
                return result
            if not result['filaments'][0]['material']:
                return result
            if result['filaments'][0]['id'] != 1:
                result['reason'] = 'Slice étfarveprintet med filament 1 som det anvendte filament.'
                return result
            result.update(supported=True, reason=None)
    except (KeyError, ValueError, TypeError, ET.ParseError, zipfile.BadZipFile):
        return result
    return result


def select(analysis, printer, payload):
    if not analysis.get('supported') or payload.get('bed_clear') is not True:
        raise ValueError('Filanalyse og bekræftelse af tom byggeplade kræves.')
    unit_id, slot_id = payload.get('ams_id'), payload.get('slot')
    if type(unit_id) is not int or type(slot_id) is not int or not 0 <= unit_id < 16 or not 1 <= slot_id <= 4:
        raise ValueError('Vælg en AMS-plads.')
    if not printer.get('connected') or printer.get('stale'):
        raise ValueError('Afventer frisk printerstatus.')
    slot = next((slot for slot in printer.get('ams_slots', []) if slot.get('ams_id') == unit_id and slot.get('slot') == slot_id), None)
    if not slot or slot.get('stale') or slot.get('present') is not True or not slot.get('material'):
        raise ValueError('AMS-pladsen er tom eller ukendt.')
    filament = analysis['filaments'][0]
    if slot['material'].upper() != filament['material'].upper():
        raise ValueError('Materialet matcher ikke filen. Slice filen til det valgte materiale.')
    warnings = []
    if filament.get('color', '').lstrip('#').upper()[:6] != str(slot.get('color', '')).upper()[:6]:
        warnings.append('Farven afviger fra filen.')
    if not slot.get('name') or slot['name'].upper() not in filament.get('profile', '').upper():
        warnings.append('Filamentprofilen afviger eller kan ikke verificeres.')
    if warnings and payload.get('accept_differences') is not True:
        raise ValueError('Bekræft forskelle i farve og filamentprofil.')
    return {'ams_id': unit_id, 'slot': slot_id, 'material': slot['material'], 'name': slot.get('name'),
            'color': slot.get('color'), 'filament_id': filament['id'], 'warnings': warnings}


def mapping(selection):
    index = selection['filament_id'] - 1
    flat = [-1] * (index + 1)
    flat[index] = selection['ams_id'] * 4 + selection['slot'] - 1
    return {'task_use_ams': True, 'ams_mapping': json.dumps(flat),
            'ams_mapping2': json.dumps([{'ams_id': selection['ams_id'], 'slot_id': selection['slot'] - 1}])}
