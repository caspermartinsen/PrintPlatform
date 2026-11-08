import fcntl
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from deploy.updater import Updater, FILES


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.updater = Updater(root / 'state', root / 'service', root / 'data')
        self.updater.service.mkdir()
        self.updater.data.mkdir()
        for name in FILES:
            (self.updater.service / name).write_bytes(b'old = 1\n')
        with closing(sqlite3.connect(self.updater.data / 'jobs.sqlite')) as connection:
            connection.execute('CREATE TABLE jobs (id TEXT)')
            connection.commit()
        self.files = {name:b'new = 2\n' for name in FILES}
        self.updater.active = lambda unit: True
        self.commands = []
        self.updater.systemctl = lambda *args: self.commands.append(args)
        self.updater.health = lambda: {'jobs':[], 'printer':{'state':'IDLE'}}

    def tearDown(self):
        self.directory.cleanup()

    def test_success_and_unchanged_no_restart(self):
        self.assertTrue(self.updater.apply('a'*40, self.files).startswith('updated'))
        self.assertEqual((self.updater.service / 'agent.py').read_bytes(),self.files['agent.py'])
        self.assertIn(('stop','print-agent'),self.commands)
        self.assertIn(('start','print-agent'),self.commands)
        self.commands.clear()
        self.assertEqual(self.updater.apply('a'*40,self.files),'unchanged')
        self.assertEqual(self.commands,[])

    def test_busy_job_deferred(self):
        for state in ('starting','submitted','unknown'):
            self.updater.health = lambda: {'jobs':[{'state':state}], 'printer':{'state':'IDLE'}}
            self.assertIn('deferred',self.updater.apply('a'*40,self.files))
            self.assertNotIn(('stop','print-service'),self.commands)
        self.updater.health = lambda: {'jobs':[], 'printer':{'state':'RUNNING'}}
        self.assertIn('deferred',self.updater.apply('a'*40,self.files))

    def test_inflight_print_lock_deferred(self):
        with (self.updater.data / 'update.lock').open('a') as guard:
            fcntl.flock(guard,fcntl.LOCK_SH | fcntl.LOCK_NB)
            self.assertIn('deferred',self.updater.apply('a'*40,self.files))
            self.assertEqual(self.commands,[])

    def test_failed_health_restores_code_and_database(self):
        attempts = 0
        def health():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return {'jobs':[], 'printer':{'state':'IDLE'}}
            raise OSError('New API unavailable')
        self.updater.health = health
        with patch('deploy.updater.time.sleep'), self.assertRaises(RuntimeError):
            self.updater.apply('a'*40,self.files)
        self.assertEqual((self.updater.service / 'server.py').read_bytes(),b'old = 1\n')
        with closing(sqlite3.connect(self.updater.data / 'jobs.sqlite')) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],0)
        self.assertIn('rolled_back',(self.updater.root / 'version.json').read_text())
