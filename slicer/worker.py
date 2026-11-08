import hashlib
import json
import os
from pathlib import Path
import resource
import re
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cloud.model_files import stl_info
from slicer.profiles import FILAMENTS


class SliceError(RuntimeError):
    def __init__(self, code):
        super().__init__('Slicer failed')
        self.exit_code = code


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def normalize_model(data, path):
    stl_info(data)
    count = struct.unpack_from('<I', data, 80)[0]
    if 84 + count * 50 != len(data):
        path.write_bytes(data)
        return
    with path.open('w') as target:
        target.write('solid model\n')
        for triangle in struct.iter_unpack('<12fH', data[84:]):
            target.write('facet normal 0 0 0\nouter loop\n')
            for offset in (3,6,9):
                target.write('vertex ' + ' '.join(str(value) for value in triangle[offset:offset+3]) + '\n')
            target.write('endloop\nendfacet\n')
        target.write('endsolid model\n')


def limits():
    resource.setrlimit(resource.RLIMIT_CPU, (300,300))
    resource.setrlimit(resource.RLIMIT_FSIZE, (128 * 1024**2,128 * 1024**2))


def run_slice(data, options, destination, profiles, binary):
    if options.get('profile') not in FILAMENTS or options.get('layer_height') not in (0.12,0.2,0.28) or options.get('infill') not in (10,15,20,40,100) or not re.fullmatch(r'#[0-9A-Fa-f]{6}',str(options.get('color',''))):
        raise ValueError('Invalid trusted slicing options')
    destination.mkdir(parents=True,exist_ok=True)
    model = destination / 'model.stl'
    normalize_model(data, model)
    process = json.loads((profiles / 'process.json').read_text())
    process.update(layer_height=str(options['layer_height']),sparse_infill_density=str(options['infill'])+'%',enable_arc_fitting='0',enable_support='0')
    process_path = destination / 'process.json'
    process_path.write_text(json.dumps(process))
    filament = json.loads((profiles / (options['profile'] + '.json')).read_text())
    filament['filament_colour'] = [options['color']]
    filament_path = destination / 'filament.json'
    filament_path.write_text(json.dumps(filament))
    command = [str(binary),'--datadir',str(destination / 'config'),'--load-settings',
        str(profiles / 'machine.json')+';'+str(process_path),'--load-filaments',str(filament_path),
        '--curr-bed-type','Textured PEI Plate','--arrange','1','--ensure-on-bed','--slice','1','--export-3mf','result.gcode.3mf',
        '--outputdir',str(destination),str(model)]
    environment = {'PATH':os.environ.get('PATH','/usr/bin:/bin'), 'HOME':str(destination),
                   'LANG':'C.UTF-8','DISPLAY':os.environ.get('DISPLAY',':99'), 'LIBGL_ALWAYS_SOFTWARE':'1'}
    with (destination / 'cli.log').open('wb') as log:
        with subprocess.Popen(command,env=environment,stdout=log,stderr=log,start_new_session=True,preexec_fn=limits) as child:
            try:
                code = child.wait(timeout=300)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid,signal.SIGKILL)
                child.wait()
                raise TimeoutError('Slicing timeout')
    result = destination / 'result.gcode.3mf'
    if code != 0 or not result.is_file() or result.stat().st_size > 100 * 1024**2:
        raise SliceError(code)
    return result


class Worker:
    def __init__(self, url, token, profiles, binary):
        parsed=urllib.parse.urlsplit(url)
        if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Invalid platform URL')
        if parsed.scheme=='http' and os.environ.get('SLICER_ALLOW_INTERNAL_HTTP')!='1':
            raise ValueError('Use HTTPS or explicitly enable private internal HTTP')
        if len(token)<32:
            raise ValueError('SLICER_TOKEN must be at least 32 characters')
        self.url=url.rstrip('/')
        self.token=token
        self.profiles=Path(profiles)
        self.binary=binary

    def request(self,path,body=None,raw=False,headers=None):
        request_headers={'Authorization':'Bearer '+self.token,**(headers or {})}
        if isinstance(body,dict):
            body=json.dumps(body).encode()
            request_headers['Content-Type']='application/json'
        request=urllib.request.Request(self.url+path,data=body,headers=request_headers)
        with urllib.request.build_opener(NoRedirect()).open(request,timeout=30) as response:
            data=response.read(100 * 1024**2 + 1)
        if len(data)>100 * 1024**2:
            raise ValueError('Response too large')
        return data if raw else json.loads(data)

    def tick(self):
        job=self.request('/slicer/poll',{})['job']
        if not job:
            return
        headers={'X-Slice-Token':job['slice_token']}
        try:
            data=self.request('/slicer/jobs/'+job['id']+'/model',raw=True)
            if hashlib.sha256(data).hexdigest()!=json.loads(job['model_info']).get('sha256'):
                raise ValueError('Source checksum mismatch')
            with tempfile.TemporaryDirectory(prefix='print-slice-') as directory:
                result=run_slice(data,json.loads(job['slice_options']),Path(directory),self.profiles,self.binary)
                self.request('/slicer/jobs/'+job['id']+'/result',result.read_bytes(),headers=headers)
        except urllib.error.HTTPError as error:
            print('Slicer request rejected: HTTP '+str(error.code),flush=True)
            if error.code != 409:
                self.request('/slicer/jobs/'+job['id']+'/failed',{'error':'invalid_result'},headers=headers)
        except Exception as error:
            reason='timeout' if isinstance(error,TimeoutError) else 'slicer_failed'
            print(json.dumps({'job':job['id'],'error':reason,'exit_code':getattr(error,'exit_code',None)}),flush=True)
            self.request('/slicer/jobs/'+job['id']+'/failed',{'error':reason},headers=headers)


if __name__=='__main__':
    os.umask(0o077)
    worker=Worker(os.environ['PLATFORM_URL'],os.environ['SLICER_TOKEN'],os.environ.get('PROFILES_DIR','/profiles'),os.environ.get('SLICER_BIN','/usr/local/bin/bambu-headless'))
    def heartbeat():
        while True:
            try:
                worker.request('/slicer/heartbeat',{})
                Path('/tmp/slicer-heartbeat').touch()
            except Exception:
                pass
            time.sleep(15)
    threading.Thread(target=heartbeat,daemon=True).start()
    while True:
        try:
            worker.tick()
            Path('/tmp/slicer-heartbeat').touch()
        except Exception as error:
            detail='HTTP '+str(error.code) if isinstance(error,urllib.error.HTTPError) else type(error).__name__
            print('Slicer connection unavailable: '+detail+'; no print commands are sent.',flush=True)
        time.sleep(5)
