import unittest
import ssl
from unittest.mock import patch
from telemetry import Telemetry, PrinterTLS


class TelemetryTests(unittest.TestCase):
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
