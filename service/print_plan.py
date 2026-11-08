import json
import math
import xml.etree.ElementTree as ET
import zipfile


PLAN_VERSION = 3
MAX_FILAMENTS = 16


def analyze(path):
    result = {'supported': False, 'reason': 'Filen mangler verificerbare filamentoplysninger.', 'filaments': []}
    try:
        with zipfile.ZipFile(path) as archive:
            if archive.getinfo('Metadata/slice_info.config').file_size > 1024**2:
                return result
            data = archive.read('Metadata/slice_info.config')
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
            names = profiles.get('filament_settings_id', [])
            if not isinstance(names, list) or len(names) > MAX_FILAMENTS:
                return result
            seen = set()
            for filament in plates[0].findall('filament'):
                grams, meters = float(filament.get('used_g', '0')), float(filament.get('used_m', '0'))
                if not all(math.isfinite(value) and value >= 0 for value in (grams, meters)):
                    return result
                if max(grams, meters) == 0:
                    continue
                filament_id = int(filament.attrib['id'])
                material = filament.get('type', '')[:40]
                if not 1 <= filament_id <= MAX_FILAMENTS or filament_id in seen or not material:
                    return result
                seen.add(filament_id)
                name = names[filament_id - 1] if len(names) >= filament_id else ''
                result['filaments'].append({'id': filament_id, 'material': material,
                    'color': filament.get('color', '')[:9], 'profile': str(name)[:120]})
            if not seen:
                return result
            result['filaments'].sort(key=lambda item: item['id'])
            result['filament_count'] = max(len(names), max(seen))
            result.update(supported=True, reason=None)
    except (KeyError, ValueError, TypeError, ET.ParseError, zipfile.BadZipFile):
        return result
    return result


def required_version(analysis):
    filaments = analysis.get('filaments', [])
    return 3 if len(filaments) > 1 or filaments and filaments[0]['id'] != 1 else 1


def selection_items(selection):
    return selection.get('filaments', [selection])


def select(analysis, printer, payload):
    if not analysis.get('supported') or payload.get('bed_clear') is not True:
        raise ValueError('Filanalyse og bekræftelse af tom byggeplade kræves.')
    if not printer.get('connected') or printer.get('stale'):
        raise ValueError('Afventer frisk printerstatus.')
    filaments = analysis['filaments']
    choices = payload.get('filaments')
    if choices is None and len(filaments) == 1:
        choices = [{**payload, 'filament_id': filaments[0]['id']}]
    if not isinstance(choices, list) or len(choices) != len(filaments) or not all(isinstance(item, dict) for item in choices):
        raise ValueError('Vælg én AMS-plads for hvert anvendt filament.')
    by_id = {}
    for choice in choices:
        index = choice.get('filament_id')
        if type(index) is not int or index in by_id:
            raise ValueError('Filamentvalgene indeholder et ugyldigt eller gentaget filament.')
        by_id[index] = choice
    if set(by_id) != {item['id'] for item in filaments}:
        raise ValueError('Alle anvendte filamenter skal have et AMS-valg.')
    selected, warnings = [], []
    for filament in filaments:
        choice = by_id[filament['id']]
        unit_id, slot_id = choice.get('ams_id'), choice.get('slot')
        if type(unit_id) is not int or type(slot_id) is not int or not 0 <= unit_id < 16 or not 1 <= slot_id <= 4:
            raise ValueError('Vælg en AMS-plads for filament ' + str(filament['id']) + '.')
        slot = next((item for item in printer.get('ams_slots', []) if item.get('ams_id') == unit_id and item.get('slot') == slot_id), None)
        if not slot or slot.get('stale') or slot.get('present') is not True or not slot.get('material'):
            raise ValueError('AMS-pladsen for filament ' + str(filament['id']) + ' er tom eller ukendt.')
        if slot['material'].upper() != filament['material'].upper():
            raise ValueError('Materialet matcher ikke filament ' + str(filament['id']) + '. Slice filen til det valgte materiale.')
        differences = []
        if filament.get('color', '').lstrip('#').upper()[:6] != str(slot.get('color', '')).upper()[:6]:
            differences.append('Farven afviger fra filen.')
        if not slot.get('name') or slot['name'].upper() not in filament.get('profile', '').upper():
            differences.append('Filamentprofilen afviger eller kan ikke verificeres.')
        warnings.extend('Filament ' + str(filament['id']) + ': ' + warning for warning in differences)
        selected.append({'ams_id': unit_id, 'slot': slot_id, 'material': slot['material'], 'name': slot.get('name'),
                         'color': slot.get('color'), 'filament_id': filament['id'], 'warnings': differences})
    if warnings and payload.get('accept_differences') is not True:
        raise ValueError('Bekræft de viste forskelle i farve og filamentprofil.')
    count = analysis.get('filament_count', max(item['id'] for item in filaments))
    if len(selected) == 1:
        return {**selected[0], 'filament_count': count}
    return {'filaments': selected, 'filament_count': count, 'warnings': warnings}


def mapping(selection):
    items = selection_items(selection)
    count = selection.get('filament_count', max(item['filament_id'] for item in items))
    if type(count) is not int or not 1 <= count <= MAX_FILAMENTS:
        raise ValueError('Ugyldigt filamentantal.')
    flat = [-1] * count
    detailed = [{'ams_id': 255, 'slot_id': 255} for _ in range(count)]
    seen = set()
    for item in items:
        index = item['filament_id']
        if type(index) is not int or not 1 <= index <= count or index in seen:
            raise ValueError('Ugyldigt filamentindeks.')
        seen.add(index)
        unit, slot = item['ams_id'], item['slot']
        if type(unit) is not int or type(slot) is not int or not 0 <= unit < 16 or not 1 <= slot <= 4:
            raise ValueError('Ugyldigt AMS-valg.')
        flat[index - 1] = unit * 4 + slot - 1
        detailed[index - 1] = {'ams_id': unit, 'slot_id': slot - 1}
    return {'task_use_ams': True, 'ams_mapping': json.dumps(flat), 'ams_mapping2': json.dumps(detailed)}


def printer_available(printer):
    if not printer.get('connected') or printer.get('stale') is not False:
        return False
    error = printer.get('error_code')
    if type(error) not in (int, float) or error != 0:
        return False
    if printer.get('state') in ('IDLE', 'FINISH'):
        return True
    remaining = printer.get('remaining_minutes')
    return printer.get('state') == 'FAILED' and type(remaining) in (int, float) and remaining == 0


def live_ready(analysis, printer, selection):
    if analysis.get('printer_model') != 'Bambu Lab P2S':
        raise ValueError('Live-print kræver en fil slicet til Bambu Lab P2S.')
    if not printer_available(printer):
        raise ValueError('Printeren skal være klar uden fejl. Kontrollér printerens skærm.')
    for item in selection_items(selection):
        if any('profil' in warning.lower() for warning in item.get('warnings', [])):
            raise ValueError('Profilkonflikt for filament ' + str(item.get('filament_id', '?')) + '. Vælg en matchende AMS-plads eller slice med spolens profil.')
