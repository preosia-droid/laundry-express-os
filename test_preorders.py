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
        self.shop = self.api.dispatch('/bookings/settings', 'alice', self.a | {'enabled': True})['shopCode']
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

    def test_simultaneous_retries_create_one_booking(self):
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda _: self.submit(), range(4)))
        self.assertTrue(all(r == results[0] for r in results))
        self.assertEqual(1, len(self.inbox()['items']))

    def test_disabled_by_default_pause_and_retry(self):
        self.assertEqual({'enabled': False, 'shopCode': None},
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

    def test_missing_scope_does_not_bypass_auth(self):
        for route in ('/bookings/settings', '/bookings/list'):
            for data in ({}, {'businessId': None, 'branchId': None}, {'businessId': self.a['businessId']}):
                with self.subTest(route=route, data=data), self.assertRaises(ApiError):
                    self.api.dispatch(route, 'alice', data)

    def test_public_shop_has_only_customer_safe_fields(self):
        result = self.api.dispatch('/bookings/shop', '', {'shopCode': self.shop})
        self.assertEqual({'businessName', 'branchName', 'timezone', 'paymentChoices'}, set(result))
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
