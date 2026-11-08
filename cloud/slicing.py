import hashlib
import json
import re
import time
import uuid

from cloud.model_files import sliced_preview


PROFILES = {'PLA Basic': 'pla-basic', 'PLA Matte': 'pla-matte', 'PLA Translucent': 'pla-translucent'}


def route(handler):
    platform = handler.server
    path = handler.path
    match = re.fullmatch(r'/(api|slicer)/jobs/([a-f0-9-]{36})/(model|slice|preview|sliced-file|result|failed)', path)
    if handler.command == 'POST' and path in ('/slicer/poll','/slicer/heartbeat'):
        json.loads(handler.body())
        with platform.db() as connection:
            connection.execute('INSERT OR REPLACE INTO slicer_health VALUES (1,?)', (time.time(),))
            if path == '/slicer/heartbeat':
                handler.respond(200,{'ok':True})
                return True
            job = connection.execute("SELECT * FROM jobs WHERE state='slice_queued' ORDER BY created LIMIT 1").fetchone()
            if job:
                connection.execute("UPDATE jobs SET state='slicing' WHERE id=?", (job['id'],))
        handler.respond(200, {'job': dict(job) if job else None})
        return True
    if not match:
        return False
    audience, job_id, action = match.groups()
    with platform.db() as connection:
        row = connection.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            handler.respond(404, {'error': 'Jobbet findes ikke.'})
            return True
        job = dict(row)
    if handler.command == 'GET' and action == 'model' and job.get('model_info'):
        handler.respond(200, (platform.root / (job_id + ('.3mf' if json.loads(job['model_info']).get('format') == '3mf' else '.stl'))).read_bytes(), 'application/octet-stream')
    elif handler.command == 'GET' and audience == 'api' and action == 'sliced-file' and job['sha256'] and job['state'] not in ('model','slice_queued','slicing','slice_failed'):
        handler.respond(200, (platform.root / (job_id + '.gcode.3mf')).read_bytes(), 'application/octet-stream')
    elif handler.command == 'GET' and audience == 'api' and action == 'preview' and job.get('preview_ready'):
        handler.respond(200, (platform.root / (job_id + '.preview.json')).read_bytes(), 'application/json; charset=utf-8')
    elif handler.command == 'POST' and audience == 'api' and action == 'slice':
        if job['state'] not in ('model','slice_failed','queued','slicing','slice_queued') or not job.get('model_info'):
            handler.respond(409, {'error': 'Jobbet kan ikke slices i sin nuværende tilstand.'})
            return True
        payload = json.loads(handler.body())
        layer = payload.get('layer_height')
        infill = payload.get('infill')
        if type(layer) not in (int,float) or layer not in (0.12,0.2,0.28) or type(infill) is not int or infill not in (10,15,20,40,100):
            raise ValueError('Ugyldige slice-indstillinger.')
        with platform.db() as connection:
            status = connection.execute('SELECT * FROM telemetry WHERE id=1').fetchone()
        report = json.loads(status['body']) if status else {}
        printer = report.get('printer', {})
        sources = json.loads(job['model_info']).get('filaments', [{'id':1}])
        choices = payload.get('filaments', [payload])
        if not isinstance(choices,list) or len(choices) != len(sources) or any(not isinstance(c,dict) for c in choices):
            raise ValueError('Vælg AMS-filament for hver projektfarve.')
        filaments = []
        for choice in choices:
            slot = next((item for item in printer.get('ams_slots', []) if item.get('ams_id') == choice.get('ams_id') and item.get('slot') == choice.get('slot')), None)
            if not status or time.time() - status['seen'] > 45 or not printer.get('connected') or printer.get('stale') or not slot or slot.get('stale') or slot.get('present') is not True:
                raise ValueError('Vælg en frisk, fyldt AMS-plads fra en online Pi.')
            if slot.get('name') not in PROFILES or slot.get('material') != 'PLA':
                raise ValueError('Slicing understøtter PLA Basic, PLA Matte og PLA Translucent på P2S.')
            color = slot.get('color')
            if not isinstance(color,str) or not re.fullmatch('[0-9A-Fa-f]{8}',color):
                raise ValueError('Farven er ukendt.')
            filaments.append({'profile':PROFILES[slot['name']], 'profile_name':slot['name'], 'color':'#'+color[:6], 'ams_id':slot['ams_id'], 'slot':slot['slot']})
        options = {**filaments[0], 'layer_height':layer, 'infill':infill, 'filaments':filaments}
        with platform.db() as connection:
            connection.execute("UPDATE jobs SET state='slice_queued',slice_options=?,slice_token=?,analysis=NULL,selection=NULL,sha256=NULL,preview_ready=0,slice_error=NULL WHERE id=?", (json.dumps(options), str(uuid.uuid4()), job_id))
        handler.respond(202, {'id':job_id,'state':'slice_queued'})
    elif handler.command == 'POST' and audience == 'slicer' and action in ('result','failed'):
        if handler.headers.get('X-Slice-Token') != job.get('slice_token') or job['state'] != 'slicing':
            handler.respond(409, {'error': 'Dette slice-forsøg er udløbet.'})
            return True
        if action == 'failed':
            payload = json.loads(handler.body())
            allowed = ('timeout','slicer_failed','invalid_result','worker_interrupted')
            reason = payload.get('error') if payload.get('error') in allowed else 'slicer_failed'
            with platform.db() as connection:
                connection.execute("UPDATE jobs SET state='slice_failed',slice_error=? WHERE id=?", (reason,job_id))
            handler.respond(200, {'saved':True})
            return True
        from server import validate
        from print_plan import analyze
        data = handler.body(100 * 1024**2)
        if sum(item.stat().st_size for item in platform.root.iterdir()) + len(data) > 2 * 1024**3:
            handler.respond(507,{'error':'Lagerkvoten er nået.'})
            return True
        temporary = platform.root / (job_id + '.slice.part')
        try:
            temporary.write_bytes(data)
            validate(temporary)
            analysis = analyze(temporary)
            options = json.loads(job['slice_options'])
            expected = {index+1: filament for index,filament in enumerate(options.get('filaments',[options]))}
            matches = all(actual['id'] in expected
                          and expected[actual['id']]['profile_name'] in actual['profile']
                          and expected[actual['id']]['color'].upper() == actual['color'].upper()
                          for actual in analysis['filaments'])
            if not analysis['supported'] or analysis.get('printer_model') != 'Bambu Lab P2S' or not matches:
                raise ValueError('Slicerresultatet matcher ikke den valgte profil.')
            preview = sliced_preview(temporary)
            if not preview['segment_count'] or preview['printer_model_id']!='N7' or preview['nozzle_diameters']!='0.4' or preview['bed_type']!='Textured PEI Plate' or preview['layer_height']!=options['layer_height'] or preview['infill']!=str(options['infill'])+'%':
                raise ValueError('Slicerresultatet mangler visbare printbaner.')
            (platform.root / (job_id + '.preview.json')).write_text(json.dumps(preview))
            temporary.replace(platform.root / (job_id + '.gcode.3mf'))
            with platform.db() as connection:
                connection.execute("UPDATE jobs SET state='queued',analysis=?,sha256=?,preview_ready=1 WHERE id=?", (json.dumps(analysis), hashlib.sha256(data).hexdigest(),job_id))
        finally:
            temporary.unlink(missing_ok=True)
        handler.respond(201, {'id':job_id,'state':'queued'})
    else:
        handler.respond(404, {'error':'Not found'})
    return True
