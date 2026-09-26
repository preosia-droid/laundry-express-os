import datetime as dt
import io
import json
import secrets
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from hosted import LocalCloudBackend
from server import ApiError, digest
from webapp import Website


class PreorderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.api = LocalCloudBackend(Path(self.tmp.name) / 'db')
        with self.api.db() as db:
            for user in ('alice', 'bob', 'staff'):
                db.execute('INSERT INTO users VALUES(?,?,?,?)', (user, user+'@example.test', '', ''))
                db.execute('INSERT INTO sessions VALUES(?,?,?)', (digest(user), user, time.time()+3600))
        self.a = self.business('alice')
        self.b = self.business('bob')
        self.shop = self.api.dispatch('/bookings/settings', 'alice', self.a | {'enabled': True, 'services': ['Wash and Fold', 'Ironing']})['shopCode']
        self.data = dict(shopCode=self.shop, requestId=secrets.token_urlsafe(32), customerName='Test Customer',
            phone='+63 917 123 4567', email='test@example.test',
            items=[dict(service='Wash and Fold', quantity=8, estimatedWeightGrams=5000)],
            instructions='Separate whites', dropOffDate=(dt.datetime.now(dt.timezone.utc).date()+dt.timedelta(days=2)).isoformat(),
            dropOffTime='15:30', termsAccepted=True, paymentChoice='PAY_AT_SHOP')

    def business(self, user):
        return self.api.dispatch('/businesses/create', user, dict(businessName=user, branchName='Main',
            timezone='Asia/Manila', requestId='create'))

    def submit(self, **changes):
        return self.api.dispatch('/bookings/submit', '', self.data | changes)

    def inbox(self, token='alice', scope=None, **extra):
        return self.api.dispatch('/bookings/list', token, (scope or self.a) | extra)

    def test_submission_is_unpaid_and_has_no_financial_effect(self):
        result = self.submit()
        self.assertEqual('UNPAID', result['paymentStatus'])
        self.assertEqual('SUBMITTED', result['status'])
        self.assertNotIn('customerName', result)
        self.assertNotIn('phone', result)
        self.assertNotIn('requestId', result)
        self.assertEqual('Test Customer', self.inbox()['items'][0]['customerName'])
        with self.api.db() as db:
            for table in ('records', 'events', 'summaries'):
                self.assertEqual(0, db.execute('SELECT count(*) FROM '+table).fetchone()[0])
            row = dict(db.execute('SELECT * FROM preorders').fetchone())
            self.assertNotIn(self.data['requestId'], json.dumps(row))

    def test_retry_and_conflicting_retry(self):
        first = self.submit()
        self.assertEqual(first, self.submit())
        with self.assertRaises(ApiError) as ctx:
            self.submit(customerName='Changed')
        self.assertEqual(409, ctx.exception.status)
        self.assertEqual(1, len(self.inbox()['items']))

    def test_service_catalog_scope_validation_and_retry(self):
        first = self.submit()
        self.api.dispatch('/bookings/settings', 'alice', self.a | {'services': ['Ironing']})
        self.assertEqual(first, self.submit())
        with self.assertRaises(ApiError):
            self.submit(requestId=secrets.token_urlsafe(32))
        public = self.api.dispatch('/bookings/shop', '', {'shopCode': self.shop})
        self.assertEqual(['Ironing'], public['services'])
        with self.assertRaises(ApiError):
            self.api.dispatch('/bookings/settings', 'bob', self.a | {'services': ['Dry Only']})
        for invalid in [['Ironing', 'ironing'], [''], 'Ironing']:
            with self.assertRaises(ApiError):
                self.api.dispatch('/bookings/settings', 'alice', self.a | {'services': invalid})

    def test_synced_catalog_updates_without_financial_effect(self):
        reg = self.api.dispatch('/devices/register', 'alice', self.a | {'deviceName': 'Catalog POS'})
        paired = self.api.dispatch('/device/pair', reg['deviceCode'], {'installationSecret': 'c'*64, 'timezone': 'Asia/Manila'})
        scope = {k: paired[k] for k in ('businessId', 'branchId', 'deviceId')}
        def sync(revision, services):
            return self.api.dispatch('/sync', 'c'*64, scope | dict(timezone='Asia/Manila', events=[dict(
                revision=revision, kind='catalog', id='services', payload=dict(services=services, lastUpdatedAt='2026-09-26T10:00:00+00:00'))]))
        self.assertTrue(sync(1, ['Wash Only', 'Ironing'])['serviceCatalogSupported'])
        settings = self.api.dispatch('/bookings/settings', 'alice', self.a | {'usePosCatalog': True})
        self.assertEqual(['Wash Only', 'Ironing'], settings['services'])
        sync(2, ['Wash Only'])
        sync(1, ['Wash Only', 'Ironing'])  # Old retry cannot restore removed services.
        self.assertEqual(['Wash Only'], self.api.dispatch('/bookings/shop', '', {'shopCode': self.shop})['services'])
        with self.api.db() as db:
            self.assertEqual(0, db.execute('SELECT count(*) FROM summaries').fetchone()[0])
        with self.assertRaises(ApiError):
            sync(3, ['Unlisted', 'unlisted'])
        self.api.dispatch('/devices/revoke', 'alice', self.a | {'deviceId': scope['deviceId']})
        self.assertEqual([], self.api.dispatch('/bookings/shop', '', {'shopCode': self.shop})['services'])

    def test_simultaneous_retries_create_one_booking(self):
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda _: self.submit(), range(4)))
        self.assertTrue(all(r == results[0] for r in results))
        self.assertEqual(1, len(self.inbox()['items']))

    def test_disabled_by_default_pause_and_retry(self):
        self.assertEqual({'enabled': False, 'shopCode': None, 'services': [], 'manualServices': False},
                         self.api.dispatch('/bookings/settings', 'bob', self.b))
        first = self.submit()
        self.api.dispatch('/bookings/settings', 'alice', self.a | {'enabled': False})
        self.assertEqual(first, self.submit())
        with self.assertRaises(ApiError):
            self.submit(requestId=secrets.token_urlsafe(32))
        with self.assertRaises(ApiError):
            self.api.dispatch('/bookings/shop', '', {'shopCode': self.shop})
        enabled = self.api.dispatch('/bookings/settings', 'alice', self.a | {'enabled': True})
        self.assertEqual(self.shop, enabled['shopCode'])

    def test_owner_and_branch_isolation(self):
        self.submit()
        with self.api.db() as db:
            db.execute('INSERT INTO members VALUES(?,?,?,?)', ('staff', self.a['businessId'], self.a['branchId'], 'STAFF'))
        for token in ('', 'bob', 'staff'):
            for route in ('/bookings/settings', '/bookings/list'):
                with self.subTest(token=token, route=route), self.assertRaises(ApiError):
                    self.api.dispatch(route, token, self.a | {'enabled': True})
        self.assertEqual([], self.inbox('bob', self.b)['items'])
        other = self.api.dispatch('/branches/create', 'alice', self.a | dict(branchName='Second', timezone='UTC'))
        self.assertEqual([], self.inbox(scope=other)['items'])
        with self.assertRaises(ApiError):
            self.inbox(scope=self.a | {'branchId': self.b['branchId']})

    def test_only_paired_pos_can_accept_and_acceptance_is_idempotent(self):
        booking=self.submit()
        registration=self.api.dispatch('/devices/register','alice',self.a|{'deviceName':'Acceptance POS'})
        with self.assertRaises(ApiError):
            self.api.dispatch('/bookings/accept','wrong-token',self.a|{'deviceId':registration['deviceId'],'reference':booking['reference'],'orderId':'order-1'})
        paired=self.api.dispatch('/device/pair',registration['deviceCode'],{'installationSecret':'a'*64,'timezone':'Asia/Manila'})
        scope={k:paired[k] for k in ('businessId','branchId','deviceId')}
        with self.assertRaises(ApiError):
            self.api.dispatch('/bookings/accept','a'*64,scope|{'reference':booking['reference'],'orderId':'order-1'})
        self.sync_order(scope, 'a'*64, 'order-1')
        accepted=self.api.dispatch('/bookings/accept','a'*64,scope|{'reference':booking['reference'],'orderId':'order-1'})
        self.assertEqual('ACCEPTED',accepted['status'])
        self.assertEqual(accepted,self.api.dispatch('/bookings/accept','a'*64,scope|{'reference':booking['reference'],'orderId':'order-1'}))
        with self.assertRaises(ApiError):
            self.api.dispatch('/bookings/accept','a'*64,scope|{'reference':booking['reference'],'orderId':'order-2'})
        self.assertEqual('ACCEPTED',self.inbox()['items'][0]['status'])
        second = self.submit(requestId=secrets.token_urlsafe(32))
        with self.assertRaises(ApiError):
            self.api.dispatch('/bookings/accept','a'*64,scope|{'reference':second['reference'],'orderId':'order-1'})
        registration2 = self.api.dispatch('/devices/register','alice',self.a|{'deviceName':'Other POS'})
        paired2 = self.api.dispatch('/device/pair',registration2['deviceCode'],{'installationSecret':'b'*64,'timezone':'Asia/Manila'})
        scope2 = {k:paired2[k] for k in ('businessId','branchId','deviceId')}
        self.sync_order(scope2, 'b'*64, 'order-1')
        with self.assertRaises(ApiError):
            self.api.dispatch('/bookings/accept','b'*64,scope2|{'reference':booking['reference'],'orderId':'order-1'})
        self.api.dispatch('/devices/revoke','alice',self.a|{'deviceId':scope['deviceId']})
        with self.assertRaises(ApiError):
            self.api.dispatch('/bookings/accept','a'*64,scope|{'reference':booking['reference'],'orderId':'order-1'})

    def sync_order(self, scope, token, order_id):
        payload = dict(orderNumber=1, createdAt=dt.datetime.now(dt.timezone.utc).isoformat(),
            orderTotal=7500, grossTotal=7500, amountPaid=0, outstandingBalance=7500,
            paymentStatus='UNPAID', voided=False, totalClothingPieces=8, removed=False)
        self.api.dispatch('/sync', token, scope | dict(timezone='Asia/Manila',
            events=[dict(revision=1,kind='order',id=order_id,payload=payload)]))

    def test_old_booking_schema_upgrades_without_losing_requests(self):
        self.submit()
        with self.api.db() as db:
            for field in ('status','accepted_order_id','accepted_at','accepted_device'):
                db.execute('DROP INDEX IF EXISTS preorder_order_link')
                db.execute('ALTER TABLE preorders DROP COLUMN '+field)
        self.api.init_preorders()
        self.api.init_preorders()
        rows = self.inbox()['items']
        self.assertEqual(1, len(rows))
        self.assertEqual('SUBMITTED', rows[0]['status'])
        self.assertEqual('Test Customer', rows[0]['customerName'])

    def test_missing_scope_does_not_bypass_auth(self):
        for route in ('/bookings/settings', '/bookings/list'):
            for data in ({}, {'businessId': None, 'branchId': None}, {'businessId': self.a['businessId']}):
                with self.subTest(route=route, data=data), self.assertRaises(ApiError):
                    self.api.dispatch(route, 'alice', data)

    def test_public_shop_has_only_customer_safe_fields(self):
        result = self.api.dispatch('/bookings/shop', '', {'shopCode': self.shop})
        self.assertEqual({'businessName', 'branchName', 'timezone', 'paymentChoices', 'minDropOffDate', 'maxDropOffDate', 'services'}, set(result))
        self.assertEqual(['PAY_AT_SHOP'], result['paymentChoices'])
        for code in (None, self.a['businessId'], 'x'*32):
            with self.assertRaises(ApiError):
                self.api.dispatch('/bookings/shop', '', {'shopCode': code})

    def test_customer_cannot_set_paid_amount_scope_or_acceptance(self):
        for changes in ({'paymentStatus': 'PAID'}, {'amountPaid': 100}, {'businessId': self.b['businessId']},
                        {'status': 'ACCEPTED'}, {'paymentChoice': 'GCASH'}, {'termsAccepted': 'true'}):
            with self.subTest(changes=changes), self.assertRaises(ApiError):
                self.submit(**changes)

    def test_validation(self):
        cases = [{'customerName': ''}, {'customerName': 'a\nadmin'}, {'phone': '-------'}, {'email': 'x@'},
                 {'requestId': '1'}, {'dropOffDate': '2026-02-30'}, {'dropOffDate': '2000-01-01'},
                 {'dropOffTime': '25:00'}, {'items': []}, {'items': [None]},
                 {'items': [{'service': 'Wash', 'quantity': True}]},
                 {'items': [{'service': 'Wash', 'quantity': 1, 'estimatedWeightGrams': 1.5}]},
                 {'items': [{'service': 'Wash', 'quantity': 1, 'price': 1}]},
                 {'instructions': 'x'*1001}]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ApiError):
                self.submit(**changes)
        self.assertEqual([], self.inbox()['items'])

    def test_pagination_with_equal_timestamps(self):
        with patch('preorders.now_iso', return_value='2026-09-26T12:00:00+00:00'):
            for _ in range(5):
                self.submit(requestId=secrets.token_urlsafe(32))
        first = self.inbox(limit=2)
        second = self.inbox(limit=2, before=first['nextCursor'])
        third = self.inbox(limit=2, before=second['nextCursor'])
        refs = [r['reference'] for page in (first, second, third) for r in page['items']]
        self.assertEqual(5, len(set(refs)))
        self.assertIsNone(third['nextCursor'])
        for options in ({'limit': True}, {'limit': 101}, {'before': {}}, {'before': 'x'}):
            with self.assertRaises(ApiError):
                self.inbox(**options)

    def test_rate_limit_including_invalid_attempts_and_expiry(self):
        for _ in range(30):
            with self.assertRaises(ApiError):
                self.submit(termsAccepted=False)
        with self.assertRaises(ApiError) as ctx:
            self.submit()
        self.assertEqual(429, ctx.exception.status)
        with self.api.db() as db:
            db.execute("UPDATE login_limits SET until=0 WHERE key LIKE 'booking:%'")
        self.assertEqual('SUBMITTED', self.submit()['status'])

    def test_http_boundary_and_origin(self):
        website = Website(self.api, 'https://shop.example.test')
        def request(origin):
            body = json.dumps(self.data).encode()
            response = []
            result = b''.join(website(dict(PATH_INFO='/bookings/submit', REQUEST_METHOD='POST',
                CONTENT_TYPE='application/json', CONTENT_LENGTH=str(len(body)), HTTP_ORIGIN=origin,
                REMOTE_ADDR='127.0.0.1', **{'wsgi.input': io.BytesIO(body)}),
                lambda status, headers: response.append((status, dict(headers)))))
            return response[0], json.loads(result)
        self.assertEqual('403 Forbidden', request('https://elsewhere.example.test')[0][0])
        (status, headers), result = request('https://shop.example.test')
        self.assertEqual('200 OK', status)
        self.assertEqual('no-store', headers['Cache-Control'])
        self.assertEqual('UNPAID', result['paymentStatus'])


if __name__ == '__main__':
    unittest.main()
