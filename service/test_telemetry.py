import unittest
import json
import ssl
from unittest.mock import patch
from telemetry import Telemetry, PrinterTLS


class TelemetryTests(unittest.TestCase):
    def test_ams_partial_update_empty_and_stale(self):
        monitor = Telemetry()
        monitor.connected = True
        monitor.ingest(json.dumps({'print': {'ams': {'ams_exist_bits': '1', 'tray_exist_bits': '1',
            'ams': [{'id': '0', 'tray': [{'id': '0', 'tray_color': '0047BB80',
                'tray_type': 'PLA', 'tray_sub_brands': 'PLA Translucent', 'remain': 90,
                'tag_uid': 'private', 'tray_uuid': 'private'}]}]}}}).encode(), now=100)
        slots = monitor.snapshot(now=100)['ams_slots']
        self.assertEqual(len(slots), 4)
        self.assertEqual(slots[0]['slot'], 1)
        self.assertEqual(slots[0]['color'], '0047BB80')
        self.assertFalse(slots[1]['present'])
        self.assertNotIn('private', json.dumps(slots))
        monitor.ingest(b'{"print":{"ams":{"ams":[{"id":"0","tray":[{"id":"0","remain":85}]}]}}}', now=110)
        slot = monitor.snapshot(now=110)['ams_slots'][0]
        self.assertEqual(slot['color'], '0047BB80')
        self.assertEqual(slot['remaining_percent'], 85)
        self.assertIsNone(monitor.snapshot(now=195)['ams_slots'][0]['color'])
        self.assertTrue(monitor.snapshot(now=210)['ams_slots'][0]['stale'])
        monitor.ingest(b'{"print":{"ams":{"tray_exist_bits":"0","ams":[{"id":"0","tray":[]}]}}}', now=220)
        slot = monitor.snapshot(now=220)['ams_slots'][0]
        self.assertFalse(slot['present'])
        self.assertNotIn('color', slot)
        monitor.ingest(b'{"print":{"ams":{"ams_exist_bits":"0","ams":[]}}}', now=230)
        self.assertEqual(monitor.snapshot(now=230)['ams_slots'], [])

    def test_invalid_ams_fields(self):
        monitor = Telemetry()
        monitor.connected = True
        monitor.ingest(b'{"print":{"ams":{"ams":[null,{"id":"0","tray":[{"id":"0","tray_color":"url(bad)","remain":-1},{"id":"9"}]}]}}}')
        slot = monitor.snapshot()['ams_slots'][0]
        self.assertIsNone(slot['color'])
        self.assertIsNone(slot['remaining_percent'])
    def test_tls_validates_serial_instead_of_ip(self):
        context = PrinterTLS(ssl.PROTOCOL_TLS_CLIENT)
        context.printer_serial = 'SERIAL'
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        with patch.object(ssl.SSLContext, 'wrap_socket') as wrap:
            context.wrap_socket(object(), server_hostname='192.168.0.191')
            self.assertEqual(wrap.call_args.kwargs['server_hostname'], 'SERIAL')

    def test_partial_reports_and_stale_state(self):
        monitor = Telemetry()
        monitor.connected = True
        monitor.ingest(b'{"print":{"gcode_state":"RUNNING","mc_percent":42,"print_error":0}}', now=100)
        monitor.ingest(b'{"print":{"nozzle_temper":210}}', now=180)
        self.assertEqual(monitor.snapshot(now=180)['progress_percent'], 42)
        self.assertEqual(monitor.snapshot(now=195)['state'], 'unknown')
        self.assertIsNone(monitor.snapshot(now=195)['progress_percent'])
        self.assertEqual(monitor.snapshot(now=195)['nozzle_c'], 210)
        self.assertTrue(monitor.snapshot(now=300)['stale'])

    def test_invalid_and_disconnect(self):
        monitor = Telemetry()
        monitor.connected = True
        for payload in (b'bad', b'[]', b'{"print":null}', b'{"print":{"mc_percent":101,"gcode_state":"FAKE"}}'):
            monitor.ingest(payload)
        self.assertEqual(monitor.snapshot()['state'], 'unknown')
        self.assertIsNone(monitor.snapshot()['last_seen'])
        monitor.ingest(b'{"print":{"gcode_state":"IDLE"}}')
        monitor.connected = False
        self.assertEqual(monitor.snapshot()['state'], 'unknown')
        self.assertTrue(monitor.snapshot()['stale'])
