"""Pay-at-shop requests. Booking never creates a sale, payment or stock movement."""
import datetime as dt
import json
import re
import secrets
import time
from zoneinfo import ZoneInfo

from server import digest, need, now_iso, packed


class Preorders:
    def init_preorders(self):
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS booking_shops(
                    business TEXT,branch TEXT,code TEXT UNIQUE,enabled INTEGER,
                    PRIMARY KEY(business,branch));
                CREATE TABLE IF NOT EXISTS booking_services(
                    business TEXT,branch TEXT,services TEXT,
                    PRIMARY KEY(business,branch));
                CREATE TABLE IF NOT EXISTS booking_reservations(
                    reference TEXT PRIMARY KEY,business TEXT,branch TEXT,device TEXT,order_id TEXT,
                    UNIQUE(business,branch,device,order_id));
                CREATE TABLE IF NOT EXISTS preorders(
                    business TEXT,branch TEXT,reference TEXT PRIMARY KEY,
                    request TEXT,payload TEXT,created TEXT,status TEXT DEFAULT 'SUBMITTED',
                    accepted_order_id TEXT,accepted_at TEXT,accepted_device TEXT,
                    UNIQUE(business,branch,request));
                CREATE INDEX IF NOT EXISTS preorders_branch
                    ON preorders(business,branch,created,reference);
            ''')
            columns = (db.table_columns('preorders') if hasattr(db, 'table_columns') else
                       {row[1] for row in db.execute('PRAGMA table_info(preorders)').fetchall()})
            for name,definition in [('status',"TEXT DEFAULT 'SUBMITTED'"),('accepted_order_id','TEXT'),('accepted_at','TEXT'),('accepted_device','TEXT')]:
                if name not in columns: db.execute(f'ALTER TABLE preorders ADD COLUMN {name} {definition}')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS preorder_order_link ON preorders(business,branch,accepted_device,accepted_order_id)')

    @staticmethod
    def booking_text(value, label, maximum, optional=False):
        need(isinstance(value, str), 'Enter a valid ' + label)
        value = value.strip()
        need((optional or bool(value)) and len(value) <= maximum
             and all(ord(c) >= 32 for c in value), 'Enter a valid ' + label)
        return value

    def booking_scope(self, db, token, data):
        business, branch = data.get('businessId'), data.get('branchId')
        need(all(isinstance(v, str) and v for v in (business, branch)),
             'Select a business and branch')
        self.owner(db, token, business, branch)
        return business, branch

    def booking_settings(self, token, data):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            business, branch = self.booking_scope(db, token, data)
            if data.get('usePosCatalog') is True:
                need('services' not in data, 'Choose POS catalog or a manual list')
                db.execute('DELETE FROM booking_services WHERE business=? AND branch=?', (business, branch))
            if 'services' in data:
                services = data['services']
                need(isinstance(services, list) and len(services) <= 100, 'Choose up to 100 services')
                services = [self.booking_text(s, 'service', 80) for s in services]
                need(len({s.casefold() for s in services}) == len(services), 'Remove duplicate services')
                db.execute('''INSERT INTO booking_services VALUES(?,?,?)
                    ON CONFLICT(business,branch) DO UPDATE SET services=excluded.services''',
                    (business, branch, packed(services)))
            if 'enabled' in data:
                need(type(data['enabled']) is bool, 'Choose whether bookings are enabled')
                db.execute('''INSERT INTO booking_shops VALUES(?,?,?,?)
                    ON CONFLICT(business,branch) DO UPDATE SET enabled=excluded.enabled''',
                    (business, branch, secrets.token_urlsafe(24), int(data['enabled'])))
            row = db.execute('SELECT code,enabled FROM booking_shops WHERE business=? AND branch=?',
                             (business, branch)).fetchone()
            services = self.booking_services(db, business, branch)
            manual = db.execute('SELECT 1 FROM booking_services WHERE business=? AND branch=?', (business, branch)).fetchone() is not None
        return {'enabled': bool(row and row['enabled']), 'shopCode': row['code'] if row else None, 'services': services, 'manualServices': manual}

    def booking_services(self, db, business, branch):
        row = db.execute('SELECT services FROM booking_services WHERE business=? AND branch=?', (business, branch)).fetchone()
        # A saved owner list is explicit. Otherwise use the sole paired POS catalog.
        # Multiple POS catalogs require an owner selection to avoid merging stale lists.
        if not row:
            catalogs = db.execute('''SELECT r.payload FROM records r JOIN devices d
                ON d.business=r.business AND d.branch=r.branch AND d.id=r.device
                WHERE r.business=? AND r.branch=? AND r.kind='catalog' AND r.id='services'
                AND d.token IS NOT NULL''', (business, branch)).fetchall()
            if len(catalogs) == 1:
                return json.loads(catalogs[0]['payload'])['services']
        return json.loads(row['services']) if row else []

    def booking_shop(self, db, code, active=True):
        need(isinstance(code, str) and re.fullmatch(r'[A-Za-z0-9_-]{32}', code),
             'Booking link unavailable', 404)
        row = db.execute('''SELECT s.business,s.branch,s.enabled,b.name AS business_name,
            r.name AS branch_name,r.zone FROM booking_shops s
            JOIN businesses b ON b.id=s.business
            JOIN branches r ON r.business=s.business AND r.id=s.branch
            WHERE s.code=?''', (code,)).fetchone()
        need(row and (not active or row['enabled']), 'Booking link unavailable', 404)
        return row

    def public_booking_shop(self, data):
        with self.db() as db:
            row = self.booking_shop(db, data.get('shopCode'))
            services = self.booking_services(db, row['business'], row['branch'])
        today = dt.datetime.now(ZoneInfo(row['zone'])).date()
        return dict(businessName=row['business_name'], branchName=row['branch_name'],
                    timezone=row['zone'], paymentChoices=['PAY_AT_SHOP'], services=services,
                    minDropOffDate=today.isoformat(), maxDropOffDate=(today + dt.timedelta(days=90)).isoformat())

    def booking_payload(self, data):
        allowed = {'shopCode', 'requestId', 'customerName', 'phone', 'email', 'items',
                   'instructions', 'dropOffDate', 'dropOffTime', 'termsAccepted', 'paymentChoice'}
        need(set(data) <= allowed, 'Unexpected booking fields')
        need(data.get('paymentChoice') == 'PAY_AT_SHOP', 'Pay at Shop is currently available')
        need(data.get('termsAccepted') is True, 'Accept the service terms')
        request = data.get('requestId')
        # Client generates 32 random bytes, base64url without padding, and keeps it
        # across retries. Hash it at rest; it is not an enumerable order number.
        need(isinstance(request, str) and re.fullmatch(r'[A-Za-z0-9_-]{43}', request),
             'A secure booking request ID is required')
        text = self.booking_text
        phone = text(data.get('phone'), 'mobile number', 32)
        need(re.fullmatch(r'\+?[0-9 ()-]{7,32}', phone)
             and 7 <= sum(c.isdigit() for c in phone) <= 15, 'Enter a valid mobile number')
        email = text(data.get('email', ''), 'email', 254, True)
        need(not email or re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email), 'Enter a valid email')
        items = data.get('items')
        need(isinstance(items, list) and 1 <= len(items) <= 30, 'Add 1 to 30 laundry items')
        cleaned = []
        for item in items:
            need(isinstance(item, dict) and set(item) <= {'service', 'description', 'quantity', 'estimatedWeightGrams'},
                 'Invalid laundry item')
            quantity = item.get('quantity')
            weight = item.get('estimatedWeightGrams')
            need(type(quantity) is int and 1 <= quantity <= 1000, 'Enter a valid item quantity')
            need(weight is None or (type(weight) is int and 1 <= weight <= 1000000),
                 'Enter a valid estimated weight')
            cleaned.append(dict(service=text(item.get('service'), 'service', 80),
                                description=text(item.get('description', ''), 'item description', 200, True),
                                quantity=quantity, estimatedWeightGrams=weight))
        date = data.get('dropOffDate')
        clock = data.get('dropOffTime')
        need(isinstance(date, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', date), 'Enter a valid drop-off date')
        try:
            dt.date.fromisoformat(date)
        except ValueError:
            need(False, 'Enter a valid drop-off date')
        need(isinstance(clock, str) and re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', clock),
             'Enter a valid drop-off time')
        return request, dict(customerName=text(data.get('customerName'), 'customer name', 120),
            phone=phone, email=email, items=cleaned,
            instructions=text(data.get('instructions', ''), 'instructions', 1000, True),
            dropOffDate=date, dropOffTime=clock, termsAccepted=True, paymentChoice='PAY_AT_SHOP')

    @staticmethod
    def booking_receipt(row):
        return dict(reference=row['reference'], createdAt=row['created'],
                    status='SUBMITTED', paymentChoice='PAY_AT_SHOP', paymentStatus='UNPAID',
                    message='Request received. The shop will verify services, weight, quantities and price at drop-off.')

    def submit_booking(self, data):
        request, payload = self.booking_payload(data)
        encoded = packed(payload)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            shop = self.booking_shop(db, data.get('shopCode'), active=False)
            old = db.execute('SELECT * FROM preorders WHERE business=? AND branch=? AND request=?',
                             (shop['business'], shop['branch'], digest(request))).fetchone()
            if old:
                need(old['payload'] == encoded, 'This request ID was already used for different details', 409)
                return self.booking_receipt(old)
            # A completed request can still be acknowledged after the owner pauses
            # bookings or the preferred date passes, without inserting it again.
            need(shop['enabled'], 'Booking link unavailable', 404)
            services = self.booking_services(db, shop['business'], shop['branch'])
            need(all(item['service'] in services for item in payload['items']),
                 'A selected service is unavailable. Reload the booking page to choose current services.')
            today = dt.datetime.now(ZoneInfo(shop['zone'])).date()
            date = dt.date.fromisoformat(payload['dropOffDate'])
            need(today <= date <= today + dt.timedelta(days=90), 'Choose a drop-off date within the next 90 days')
            row = dict(reference='PRE-' + secrets.token_hex(12).upper(), created=now_iso())
            db.execute('INSERT INTO preorders(business,branch,reference,request,payload,created,status) VALUES(?,?,?,?,?,?,?)',
                       (shop['business'], shop['branch'], row['reference'], digest(request), encoded, row['created'], 'SUBMITTED'))
        return self.booking_receipt(row)

    def list_bookings(self, token, data):
        limit = data.get('limit', 20)
        need(type(limit) is int and 1 <= limit <= 100, 'Invalid page size')
        before = data.get('before')
        need(before is None or (isinstance(before, dict) and set(before) == {'createdAt', 'reference'}
             and all(isinstance(v, str) and len(v) <= 80 for v in before.values())), 'Invalid page cursor')
        with self.db() as db:
            business, branch = self.booking_scope(db, token, data)
            sql = 'SELECT * FROM preorders WHERE business=? AND branch=?'
            params = [business, branch]
            if before:
                sql += ' AND (created<? OR (created=? AND reference<?))'
                params += [before['createdAt'], before['createdAt'], before['reference']]
            rows = db.execute(sql + ' ORDER BY created DESC,reference DESC LIMIT ?', params + [limit + 1]).fetchall()
        page = rows[:limit]
        return dict(items=[self.booking_receipt(r) | json.loads(r['payload']) | {'status':r['status']} for r in page],
                    nextCursor=dict(createdAt=page[-1]['created'], reference=page[-1]['reference']) if len(rows) > limit else None)

    def reserve_booking(self, token, data):
        """Reserve before creating a local order; retries keep the same device/order."""
        reference, order_id = data.get('reference'), data.get('orderId')
        need(isinstance(reference, str) and 8 <= len(reference) <= 40, 'Select a pre-order')
        need(isinstance(order_id, str) and 1 <= len(order_id) <= 100, 'A stable POS order ID is required')
        b, r, d = (data.get(k) for k in ('businessId', 'branchId', 'deviceId'))
        need(all(isinstance(v, str) and v for v in (b, r, d)), 'Device scope is required')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            need(db.execute('SELECT id FROM devices WHERE business=? AND branch=? AND id=? AND token=?',
                            (b, r, d, digest(token))).fetchone(), 'Device access denied', 403)
            row = db.execute('SELECT * FROM preorders WHERE business=? AND branch=? AND reference=?', (b, r, reference)).fetchone()
            need(row, 'Pre-order not found', 404)
            if row['status'] == 'ACCEPTED':
                need(row['accepted_device'] == d and row['accepted_order_id'] == order_id, 'Pre-order already accepted', 409)
            else:
                need(row['status'] == 'SUBMITTED', 'Pre-order unavailable', 409)
            reservation = db.execute('SELECT * FROM booking_reservations WHERE reference=?', (reference,)).fetchone()
            if reservation:
                need(reservation['device'] == d and reservation['order_id'] == order_id, 'Another POS order is processing this booking', 409)
            else:
                used = db.execute('SELECT reference FROM booking_reservations WHERE business=? AND branch=? AND device=? AND order_id=?', (b,r,d,order_id)).fetchone()
                need(not used, 'This order is reserved for another booking', 409)
                db.execute('INSERT INTO booking_reservations VALUES(?,?,?,?,?)', (reference,b,r,d,order_id))
            return dict(reference=reference, orderId=order_id, status=row['status'], booking=json.loads(row['payload']))

    def accept_booking(self, token, data):
        """Link a request to an existing synced order belonging to this paired POS."""
        reference, order_id = data.get('reference'), data.get('orderId')
        need(isinstance(reference, str) and 8 <= len(reference) <= 40, 'Select a pre-order')
        need(isinstance(order_id, str) and 1 <= len(order_id) <= 100, 'A POS order ID is required')
        need(isinstance(data.get('businessId'), str) and isinstance(data.get('branchId'), str)
             and isinstance(data.get('deviceId'), str), 'Device scope is required')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            device=db.execute('SELECT id FROM devices WHERE business=? AND branch=? AND id=? AND token=?',
                              (data['businessId'],data['branchId'],data['deviceId'],digest(token))).fetchone()
            need(device is not None, 'Device access denied', 403)
            row=db.execute('SELECT * FROM preorders WHERE business=? AND branch=? AND reference=?',
                           (data['businessId'],data['branchId'],reference)).fetchone()
            need(row is not None, 'Pre-order not found',404)
            reservation = db.execute('SELECT device,order_id FROM booking_reservations WHERE reference=?', (reference,)).fetchone()
            need(not reservation or (reservation['device'] == data['deviceId'] and reservation['order_id'] == order_id),
                 'Another POS order is processing this booking', 409)
            if row['status']=='ACCEPTED':
                need(row['accepted_order_id']==order_id and row['accepted_device']==data['deviceId'],
                     'Pre-order already linked to another order or POS',409)
                return {'reference':reference,'status':'ACCEPTED','orderId':order_id,'acceptedAt':row['accepted_at']}
            need(row['status']=='SUBMITTED', 'Pre-order is no longer available',409)
            order = db.execute("SELECT payload FROM records WHERE business=? AND branch=? AND device=? AND kind='order' AND id=?",
                (data['businessId'], data['branchId'], data['deviceId'], order_id)).fetchone()
            need(order is not None, 'Sync the verified POS order before linking the pre-order', 409)
            payload = json.loads(order['payload'])
            need(not payload['removed'] and not payload['voided'], 'Use an active POS order', 409)
            linked = db.execute('SELECT reference FROM preorders WHERE business=? AND branch=? AND accepted_device=? AND accepted_order_id=?',
                (data['businessId'], data['branchId'], data['deviceId'], order_id)).fetchone()
            need(not linked, 'This POS order already belongs to another pre-order', 409)
            accepted=now_iso()
            db.execute('UPDATE preorders SET status=?,accepted_order_id=?,accepted_at=?,accepted_device=? WHERE reference=?',
                       ('ACCEPTED',order_id,accepted,data['deviceId'],reference))
        return {'reference':reference,'status':'ACCEPTED','orderId':order_id,'acceptedAt':accepted}

    def limit_bookings(self, path, remote):
        maximum = 30 if path == '/bookings/submit' else 120
        key, now = 'booking:' + path + ':' + digest(remote), time.time()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM login_limits WHERE until<?', (now,))
            row = db.execute('SELECT count FROM login_limits WHERE key=?', (key,)).fetchone()
            need(not row or row['count'] < maximum, 'Too many requests. Try again in 15 minutes.', 429)
            db.execute('INSERT INTO login_limits VALUES(?,1,?) ON CONFLICT(key) DO UPDATE SET count=login_limits.count+1',
                       (key, now + 900))

    def dispatch(self, path, token, data, remote='local'):
        if path in ('/bookings/shop', '/bookings/submit'):
            self.limit_bookings(path, remote)
            return self.public_booking_shop(data) if path.endswith('/shop') else self.submit_booking(data)
        if path == '/bookings/settings':
            return self.booking_settings(token, data)
        if path == '/bookings/list':
            return self.list_bookings(token, data)
        if path == '/bookings/accept':
            return self.accept_booking(token, data)
        if path == '/bookings/reserve':
            return self.reserve_booking(token, data)
        return super().dispatch(path, token, data, remote)
