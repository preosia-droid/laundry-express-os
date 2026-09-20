"""Bridge existing Supabase shop registrations to the private reporting backend.

Existing device credentials and OWNER memberships remain the authority. No caller
can choose another business, branch, or device, or grant itself a membership.
"""
import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from server import need, digest, packed


class RegisteredShops:
    def pair_registered_device(self, token, data):
        need(isinstance(token, str) and 20 <= len(token) <= 512, 'Device access denied', 403)
        zone = data.get('timezone')
        try:
            ZoneInfo(zone)
        except (ValueError, TypeError, ZoneInfoNotFoundError):
            need(False, 'Valid timezone required')
        with self.db() as db:
            row = db.execute('''SELECT d.id,d.business_id,d.branch_id,d.device_name,
                b.name business_name,r.name branch_name FROM public.pos_devices d
                JOIN public.businesses b ON b.id=d.business_id
                JOIN public.branches r ON r.id=d.branch_id AND r.business_id=d.business_id
                WHERE d.device_code=?''', (token,)).fetchone()
            need(row is not None, 'Device access denied', 403)
            business, branch, device = map(str, (row['business_id'], row['branch_id'], row['id']))
            db.execute('INSERT OR IGNORE INTO businesses VALUES(?,?,?)',
                       (business, row['business_name'], packed(['remoteMonitoring','cloudReports'])))
            db.execute('INSERT OR IGNORE INTO branches VALUES(?,?,?,?)',
                       (business, branch, row['branch_name'], zone))
            stored = db.execute('SELECT zone FROM branches WHERE business=? AND id=?', (business, branch)).fetchone()[0]
            need(stored == zone, 'Branch timezone differs from this tablet; verify before pairing', 409)
            existing = db.execute('SELECT token FROM devices WHERE business=? AND branch=? AND id=?',
                                  (business, branch, device)).fetchone()
            need(not existing or existing['token'] == digest(token), 'Device credential requires operator reconciliation', 409)
            db.execute('INSERT OR IGNORE INTO devices VALUES(?,?,?,?,?,NULL,NULL)',
                       (business, branch, device, row['device_name'], digest(token)))
        return dict(businessId=business, branchId=branch, deviceId=device, timezone=zone)

    def owner(self, db, token, business=None, branch=None, capability='cloudReports'):
        user = super().owner(db, token)
        if business is None:
            return user
        grant = db.execute('''SELECT m.role FROM public.business_members m
            JOIN public.branches r ON r.business_id=m.business_id
            WHERE m.user_id::text=? AND m.business_id::text=? AND r.id::text=?
            AND m.role='OWNER' ''', (user, business, branch)).fetchone()
        if grant:
            caps = db.execute('SELECT capabilities FROM businesses WHERE id=?', (business,)).fetchone()
            need(caps and capability in json.loads(caps[0]), 'Capability unavailable', 403)
            return user
        return super().owner(db, token, business, branch, capability)

    def memberships(self, token):
        result = super().memberships(token)
        with self.db() as db:
            user = self.owner(db, token)
            # Read existing grants each time so revocation takes effect immediately.
            grants = db.execute('''SELECT b.id business,r.id branch,b.name businessName,
                r.name branchName,r.zone,b.capabilities FROM public.business_members m
                JOIN businesses b ON b.id=m.business_id::text
                JOIN branches r ON r.business=b.id
                JOIN public.branches p ON p.id::text=r.id AND p.business_id=m.business_id
                WHERE m.user_id::text=? AND m.role='OWNER' ''', (user,)).fetchall()
            seen = {(m['business'],m['branch']) for m in result['memberships']}
            for grant in grants:
                if (grant['business'], grant['branch']) not in seen:
                    # PostgreSQL folds unquoted aliases to lower case.
                    result['memberships'].append(dict(business=grant['business'],branch=grant['branch'],
                        businessName=grant['businessname'],branchName=grant['branchname'],
                        zone=grant['zone'],capabilities=json.loads(grant['capabilities'])))
        return result

    def dispatch(self, path, token, data, remote='local'):
        if path == '/device/pair':
            return self.pair_registered_device(token, data)
        return super().dispatch(path, token, data, remote)
