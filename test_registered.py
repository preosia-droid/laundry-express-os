"""Registration behavior tests with attached public schema fixtures.

SQLite removes PostgreSQL casts; production SQL is also checked on deployment.
No real credentials or shop records are used.
"""
import sqlite3
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from postgres import Row
from registered import RegisteredShops
from hosted import LocalCloudBackend
from server import ApiError, digest


class FixtureConnection:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, params=()):
        return self.connection.execute(sql.replace('::text', ''), params)

    def executescript(self, sql):
        return self.connection.executescript(sql)


class FixtureBackend(RegisteredShops, LocalCloudBackend):
    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = lambda cursor, values: Row(
            (column[0].lower(), value) for column, value in zip(cursor.description, values))
        connection.execute('ATTACH DATABASE ? AS public', (str(self.path) + '.public',))
        try:
            with connection:
                yield FixtureConnection(connection)
        finally:
            connection.close()


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.api = FixtureBackend(Path(self.tmp.name) / 'reporting.db')
        self.device = 'fixture-device-token-with-enough-entropy'
        self.session = 'fixture-owner-session'
        with self.api.db() as db:
            db.executescript('''
                CREATE TABLE public.businesses(id TEXT PRIMARY KEY,name TEXT);
                CREATE TABLE public.branches(id TEXT PRIMARY KEY,business_id TEXT,name TEXT);
                CREATE TABLE public.pos_devices(id TEXT PRIMARY KEY,business_id TEXT,branch_id TEXT,device_name TEXT,device_code TEXT);
                CREATE TABLE public.business_members(user_id TEXT,business_id TEXT,role TEXT);
                INSERT INTO public.businesses VALUES('shop','Test Shop');
                INSERT INTO public.branches VALUES('branch','shop','Main');
                INSERT INTO public.business_members VALUES('owner','shop','OWNER');
                INSERT INTO users VALUES('owner','owner@example.test','','');
            ''')
            db.execute('INSERT INTO public.pos_devices VALUES(?,?,?,?,?)',
                       ('device', 'shop', 'branch', 'Tablet', self.device))
            db.execute('INSERT INTO sessions VALUES(?,?,?)',
                       (digest(self.session), 'owner', time.time() + 3600))

    def tearDown(self):
        self.tmp.cleanup()

    def pair(self, **extra):
        return self.api.dispatch('/device/pair', self.device, {'timezone': 'Asia/Manila', **extra})

    def test_existing_owner_access_without_invitation_and_idempotent_pairing(self):
        self.assertEqual([], self.api.memberships(self.session)['memberships'])
        identity = self.pair(businessId='attacker', branchId='other', deviceId='spoof')
        self.assertEqual('shop', identity['businessId'])
        self.assertEqual('branch', identity['branchId'])
        self.assertEqual('device', identity['deviceId'])
        self.assertEqual(identity, self.pair())
        membership = self.api.memberships(self.session)['memberships']
        self.assertEqual(1, len(membership))
        self.assertEqual('Test Shop', membership[0]['businessName'])
        for period in ('TODAY', 'WEEK', 'MONTH', 'YEAR'):
            self.assertEqual(0, self.api.report(self.session, 'shop', 'branch', period)['current']['netSales'])
        with self.api.db() as db:
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM devices').fetchone()[0])
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM members').fetchone()[0])

    def test_revocation_and_role_downgrade_take_effect_immediately(self):
        self.pair()
        with self.api.db() as db:
            db.execute("UPDATE public.business_members SET role='STAFF'")
        self.assertEqual([], self.api.memberships(self.session)['memberships'])
        with self.assertRaises(ApiError):
            self.api.report(self.session, 'shop', 'branch', 'TODAY')

    def test_cross_shop_access_and_invalid_session_denied(self):
        self.pair()
        with self.assertRaises(ApiError):
            self.api.report(self.session, 'other', 'branch', 'TODAY')
        with self.assertRaises(ApiError):
            self.api.memberships('not-a-session')
        self.api.dispatch('/logout', self.session, {})
        with self.assertRaises(ApiError):
            self.api.memberships(self.session)

    def test_invalid_token_or_timezone_does_not_provision(self):
        for token, zone in [('unknown-device-token-123456', 'Asia/Manila'),
                            (self.device, 'Invalid/Zone'), (self.device, None)]:
            with self.assertRaises(ApiError):
                self.api.pair_registered_device(token, {'timezone': zone})
        with self.api.db() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM devices').fetchone()[0])

    def test_pairing_conflict_preserves_existing_identity(self):
        self.pair()
        with self.assertRaises(ApiError):
            self.pair(timezone='UTC')
        with self.api.db() as db:
            db.execute("UPDATE devices SET token='operator-managed-token'")
        with self.assertRaises(ApiError):
            self.pair()

    def test_mismatched_registered_branch_cannot_pair(self):
        with self.api.db() as db:
            db.execute("UPDATE public.branches SET business_id='other'")
        with self.assertRaises(ApiError):
            self.pair()


if __name__ == '__main__':
    unittest.main()
