import hashlib
import json
import re
import time
import uuid

from cloud.model_files import sliced_preview


PROFILES = {'PLA Basic': 'pla-basic', 'PLA Matte': 'pla-matte', 'PLA Translucent': 'pla-translucent'}
SLICE_LEASE_SECONDS = 420


def expire(platform):
    with platform.db() as connection:
        connection.execute("UPDATE jobs SET state='slice_failed',slice_error='worker_interrupted',slice_token=NULL,slice_expires=NULL WHERE state='slicing' AND (slice_expires IS NULL OR slice_expires < ?)", (time.time(),))


def route(handler):
    platform = handler.server
    path = handler.path
    match = re.fullmatch(r'/(api|slicer)/jobs/([a-f0-9-]{36})/(model|slice|preview|sliced-file|result|failed)', path)
    if handler.command == 'POST' and path in ('/slicer/poll','/slicer/heartbeat'):
        payload = json.loads(handler.body())
        version = payload.get('slicer_version',1)
        if type(version) is not int or version not in (1,2):
            raise ValueError('Ukendt slicerversion.')
        expire(platform)
        with platform.db() as connection:
            connection.execute('INSERT OR REPLACE INTO slicer_health(id,seen,version) VALUES (1,?,?)', (time.time(),version))
            if path == '/slicer/heartbeat':
                handler.respond(200,{'ok':True})
                return True
            candidates = connection.execute("SELECT * FROM jobs WHERE state='slice_queued' ORDER BY created").fetchall()
            job = next((item for item in candidates if version >= 2 or json.loads(item['model_info']).get('kind') != '3mf'),None)
            if job:
                connection.execute("UPDATE jobs SET state='slicing',slice_expires=? WHERE id=?", (time.time() + SLICE_LEASE_SECONDS, job['id']))
        handler.respond(200, {'job': dict(job) if job else None})
        return True
    if not match:
        return False
    audience, job_id, action = match.groups()
    if not getattr(handler, '_slice_locked', False):
        with platform.job_guard(job_id):
            handler._slice_locked = True
            return route(handler)
    if audience == 'slicer' and action in ('result', 'failed'):
        expire(platform)
    if handler.command == 'POST' and action == 'result' and not getattr(handler, '_result_locked', False):
        with platform.files_guard:
            handler._result_locked = True
            return route(handler)
    with platform.db() as connection:
        row = connection.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            handler.respond(404, {'error': 'Jobbet findes ikke.'})
            return True
        job = dict(row)
    if handler.command == 'GET' and action == 'model' and job.get('model_info'):
        handler.respond(200, (platform.root / (job_id + ('.3mf' if json.loads(job['model_info']).get('kind') == '3mf' else '.stl'))).read_bytes(), 'application/octet-stream')
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
        info = json.loads(job['model_info'])
        if info.get('kind') == '3mf':
            with platform.db() as connection:
                health = connection.execute('SELECT seen,version FROM slicer_health WHERE id=1').fetchone()
            if not health or time.time()-health['seen'] > 45 or health['version'] < 2:
                raise ValueError('Opdatér og tilslut slicer-containeren for at klargøre 3MF-projekter.')
        required = info.get('filaments',[{'id':1,'material':'PLA'}])
        choices = payload.get('filaments')
        if choices is None and len(required) == 1:
            choices = [{**payload,'filament_id':required[0]['id']}]
        if (not isinstance(choices,list) or len(choices) != len(required)
                or not all(isinstance(item,dict) and type(item.get('filament_id')) is int for item in choices)
                or {item['filament_id'] for item in choices} != {item['id'] for item in required}):
            raise ValueError('Vælg en AMS-plads for hver projektfarve.')
        if not status or time.time() - status['seen'] > 45 or not printer.get('connected') or printer.get('stale'):
            raise ValueError('Afventer frisk printerstatus.')
        prepared = []
        for filament in required:
            choice = next(item for item in choices if item['filament_id'] == filament['id'])
            if type(choice.get('ams_id')) is not int or type(choice.get('slot')) is not int:
                raise ValueError('Ugyldigt AMS-valg.')
            slot = next((item for item in printer.get('ams_slots',[]) if item.get('ams_id') == choice['ams_id'] and item.get('slot') == choice['slot']),None)
            if not slot or slot.get('stale') or slot.get('present') is not True:
                raise ValueError('Vælg friske, fyldte AMS-pladser.')
            if slot.get('name') not in PROFILES or slot.get('material') != 'PLA' or filament['material'] != 'PLA':
                raise ValueError('Klargøring understøtter PLA Basic, PLA Matte og PLA Translucent på P2S.')
            color = slot.get('color')
            if not isinstance(color,str) or not re.fullmatch('[0-9A-Fa-f]{8}',color):
                raise ValueError('Filamentfarven er ukendt.')
            prepared.append({'filament_id':filament['id'],'profile':PROFILES[slot['name']],'profile_name':slot['name'],
                             'color':'#' + color[:6],'ams_id':slot['ams_id'],'slot':slot['slot']})
        options = {'layer_height':layer,'infill':infill,'source_kind':info.get('kind','stl'),'filaments':prepared}
        if len(prepared) == 1:
            options.update(prepared[0])
        with platform.db() as connection:
            connection.execute("UPDATE jobs SET state='slice_queued',slice_options=?,slice_token=?,analysis=NULL,selection=NULL,sha256=NULL,preview_ready=0,slice_error=NULL,slice_expires=NULL WHERE id=?", (json.dumps(options), str(uuid.uuid4()), job_id))
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
                connection.execute("UPDATE jobs SET state='slice_failed',slice_error=?,slice_expires=NULL WHERE id=? AND slice_token=? AND state='slicing'", (reason,job_id,job['slice_token']))
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
            expected = {item['filament_id']:item for item in options.get('filaments',[{**options,'filament_id':1}])}
            if not analysis['supported'] or analysis.get('printer_model') != 'Bambu Lab P2S':
                raise ValueError('Slicerresultatet mangler filamentoplysninger.')
            for filament in analysis['filaments']:
                configured = expected.get(filament['id'])
                if not configured or configured['profile_name'] not in filament['profile'] or filament['color'].upper() != configured['color'].upper():
                    raise ValueError('Slicerresultatet matcher ikke projektets farvevalg.')
            preview = sliced_preview(temporary)
            if not preview['segment_count'] or preview['printer_model_id']!='N7' or preview['nozzle_diameters']!='0.4' or preview['bed_type']!='Textured PEI Plate' or preview['layer_height']!=options['layer_height'] or preview['infill']!=str(options['infill'])+'%':
                raise ValueError('Slicerresultatet mangler visbare printbaner.')
            with platform.db() as connection:
                current = connection.execute('SELECT state,slice_token FROM jobs WHERE id=?', (job_id,)).fetchone()
                if current['state'] != 'slicing' or current['slice_token'] != job['slice_token']:
                    handler.respond(409, {'error': 'Dette slice-forsøg er udløbet.'})
                    return True
                (platform.root / (job_id + '.preview.json')).write_text(json.dumps(preview))
                temporary.replace(platform.root / (job_id + '.gcode.3mf'))
                connection.execute("UPDATE jobs SET state='queued',analysis=?,sha256=?,preview_ready=1,slice_expires=NULL WHERE id=?", (json.dumps(analysis), hashlib.sha256(data).hexdigest(),job_id))
        finally:
            temporary.unlink(missing_ok=True)
        handler.respond(201, {'id':job_id,'state':'queued'})
    else:
        handler.respond(404, {'error':'Not found'})
    return True

