import json
import os
import re
import ssl
import threading
import time
import uuid


class PrinterTLS(ssl.SSLContext):
    def wrap_socket(self, *args, **kwargs):
        kwargs['server_hostname'] = self.printer_serial
        return super().wrap_socket(*args, **kwargs)


class Telemetry:
    def __init__(self):
        self.lock = threading.Lock()
        self.connected = False
        self.error = 'disabled'
        self.values = {}
        self.last_seen = None
        self.ams_slots = {}

    def update_ams(self, report, now):
        units = report.get('ams')
        if not isinstance(units, list):
            return
        try:
            present_bits = int(report['tray_exist_bits'], 16)
        except (KeyError, TypeError, ValueError):
            present_bits = None
        try:
            unit_bits = int(report['ams_exist_bits'], 16)
        except (KeyError, TypeError, ValueError):
            unit_bits = None
        if unit_bits is not None:
            self.ams_slots = {key: value for key, value in self.ams_slots.items() if unit_bits & (1 << key[0])}
        for unit in units[:16]:
            if not isinstance(unit, dict) or not str(unit.get('id', '')).isdigit():
                continue
            unit_id = int(unit['id'])
            if not 0 <= unit_id < 16 or unit_bits is not None and not unit_bits & (1 << unit_id):
                continue
            trays = unit.get('tray', [])
            if not isinstance(trays, list):
                continue
            for slot in range(4):
                fields = self.ams_slots.setdefault((unit_id, slot), {})
                if present_bits is not None:
                    present = bool(present_bits & (1 << (unit_id * 4 + slot)))
                    if not present:
                        fields.clear()
                    fields['present'] = (present, now)
            for tray in trays[:4]:
                if not isinstance(tray, dict) or str(tray.get('id', '')) not in ('0', '1', '2', '3'):
                    continue
                fields = self.ams_slots[(unit_id, int(tray['id']))]
                if fields.get('present') == (False, now):
                    continue
                if 'tray_color' in tray:
                    color = tray['tray_color']
                    fields['color'] = (color.upper() if isinstance(color, str) and re.fullmatch(r'[0-9a-fA-F]{8}', color) else None, now)
                for source, target in (('tray_type', 'material'), ('tray_sub_brands', 'name')):
                    if source in tray:
                        value = tray[source]
                        fields[target] = (value[:80] if isinstance(value, str) and value else None, now)
                if 'remain' in tray:
                    remain = tray['remain']
                    fields['remaining_percent'] = (remain if type(remain) is int and 0 <= remain <= 100 else None, now)

    def ingest(self, payload, now=None):
        now = time.time() if now is None else now
        if len(payload) > 262144:
            return
        try:
            report = json.loads(payload).get('print')
        except (ValueError, AttributeError, TypeError):
            return
        if not isinstance(report, dict):
            return
        accepted = {}
        state = report.get('gcode_state')
        if state in ('IDLE', 'RUNNING', 'PAUSE', 'FINISH', 'FAILED', 'PREPARE', 'SLICING'):
            accepted['state'] = state
        for source, target, maximum in (
                ('mc_percent', 'progress_percent', 100), ('mc_remaining_time', 'remaining_minutes', 100000),
                ('nozzle_temper', 'nozzle_c', 500), ('bed_temper', 'bed_c', 200),
                ('print_error', 'error_code', 2**32 - 1)):
            value = report.get(source)
            if type(value) in (int, float) and 0 <= value <= maximum:
                accepted[target] = value
        with self.lock:
            if isinstance(report.get('ams'), dict):
                self.update_ams(report['ams'], now)
            for key, value in accepted.items():
                self.values[key] = (value, now)
            if accepted:
                self.last_seen = now

    def snapshot(self, now=None):
        now = time.time() if now is None else now
        with self.lock:
            slots = []
            for (unit_id, slot), values in sorted(self.ams_slots.items()):
                fields = {key: value if self.connected and now - stamp <= 90 else None
                          for key, (value, stamp) in values.items()}
                slots.append({'ams_id': unit_id, 'slot': slot + 1,
                              'stale': not self.connected or not values or all(now - stamp > 90 for value, stamp in values.values()), **fields})
            fields = {key: value if self.connected and now - stamp <= 90 else None
                      for key, (value, stamp) in self.values.items()}
            return {'connected': self.connected, 'state': fields.pop('state', None) or 'unknown',
                    'last_seen': self.last_seen,
                    'stale': not self.connected or self.last_seen is None or now - self.last_seen > 90,
                    'error': self.error, 'ams_slots': slots, **fields}

    def start(self):
        if os.environ.get('TELEMETRY_ENABLED', '0') == '1':
            self.error = 'connecting'
            threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        try:
            import paho.mqtt.client as mqtt
            serial = os.environ['BAMBU_DEV_ID']
            host = os.environ['BAMBU_DEV_IP']
            password = os.environ['BAMBU_ACCESS_CODE']
            context = PrinterTLS(ssl.PROTOCOL_TLS_CLIENT)
            context.load_verify_locations(cafile=os.environ['BAMBU_TELEMETRY_CA'])
            context.printer_serial = serial
        except (ImportError, KeyError, OSError, ValueError):
            self.error = 'configuration_required'
            return
        while True:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                 client_id='print-status-' + uuid.uuid4().hex[:12])
            client.username_pw_set('bblp', password)
            client.tls_set_context(context)

            def connected(connection, _userdata, _flags, reason, _properties):
                if reason.is_failure:
                    raise ConnectionError('Authentication failed')
                connection.subscribe('device/' + serial + '/report', qos=0)
                with self.lock:
                    self.connected = True
                    self.error = None

            def message(_client, _userdata, report):
                if report.topic == 'device/' + serial + '/report' and not report.retain:
                    self.ingest(report.payload)

            client.on_connect = connected
            client.on_message = message
            try:
                client.connect(host, 8883, keepalive=30)
                last_request = 0
                while client.loop(timeout=1) == mqtt.MQTT_ERR_SUCCESS:
                    if self.connected and time.monotonic() - last_request >= 30:
                        client.publish('device/' + serial + '/request', json.dumps(
                            {'pushing': {'sequence_id': '0', 'command': 'pushall', 'version': 1}}))
                        last_request = time.monotonic()
            except Exception:
                pass
            finally:
                client.disconnect()
                with self.lock:
                    self.connected = False
                    self.error = 'connection_failed'
                    self.values.clear()
                    self.ams_slots.clear()
                time.sleep(10)
