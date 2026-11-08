import argparse
import json
from pathlib import Path


FILAMENTS = {'pla-basic': 'Bambu PLA Basic @BBL P2S', 'pla-matte': 'Bambu PLA Matte @BBL P2S',
             'pla-translucent': 'Bambu PLA Translucent @BBL P2S 0.4 nozzle'}


def prepare(source, destination):
    source, destination = Path(source), Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    def resolve(category, name, stack=()):
        if name in stack or len(stack) > 20:
            raise ValueError('Profile inheritance cycle')
        path = source / category / (name + '.json')
        data = json.loads(path.read_text())
        result = resolve(category, data['inherits'], (*stack, name)) if data.get('inherits') else {}
        for included in data.get('include', []):
            result.update(resolve(category, included, (*stack, name)))
        result.update({key: value for key, value in data.items() if key not in ('inherits', 'include')})
        return result
    machine = resolve('machine', 'Bambu Lab P2S 0.4 nozzle')
    machine['printer_settings_id'] = machine['name']
    (destination / 'machine.json').write_text(json.dumps(machine))
    process = resolve('process', '0.20mm Standard @BBL P2S')
    process.update(print_settings_id=process['name'], enable_arc_fitting='0')
    (destination / 'process.json').write_text(json.dumps(process))
    for key, name in FILAMENTS.items():
        filament = resolve('filament', name)
        filament['filament_settings_id'] = [name]
        (destination / (key + '.json')).write_text(json.dumps(filament))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('source')
    parser.add_argument('destination')
    args = parser.parse_args()
    prepare(args.source, args.destination)
