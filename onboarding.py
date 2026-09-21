"""Self-service tenants and single-installation pairing; all authority stays server-side."""
import json
import secrets
import time
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from server import need, digest, packed


class Onboarding:
    def init_onboarding(self):
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS managed_businesses(id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS setup_requests(user TEXT,request TEXT,payload TEXT,result TEXT,PRIMARY KEY(user,request));
                CREATE TABLE IF NOT EXISTS device_enrollments(code TEXT PRIMARY KEY,business TEXT,branch TEXT,device TEXT UNIQUE,name TEXT,expires REAL,claim TEXT,revoked INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS team_invitations(code TEXT PRIMARY KEY,email TEXT,business TEXT,branch TEXT,role TEXT,expires REAL);
            ''')

    @staticmethod
    def text(value, label, maximum=120):
        need(isinstance(value, str) and 1 <= len(value.strip()) <= maximum and all(ord(c) >= 32 for c in value), 'Enter a valid ' + label)
        return value.strip()

    def zone(self, value):
        value = self.text(value, 'timezone', 64)
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError):
            need(False, 'Enter a valid timezone')
        return value

    def create_business(self, token, data):
        name = self.text(data.get('businessName'), 'business name')
        branch_name = self.text(data.get('branchName'), 'branch name')
        zone = self.zone(data.get('timezone'))
        request = self.text(data.get('requestId'), 'request ID', 100)
        payload = packed([name, branch_name, zone])
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            user = self.owner(db, token)
            old = db.execute('SELECT payload,result FROM setup_requests WHERE user=? AND request=?', (user, request)).fetchone()
            if old:
                need(old['payload'] == payload, 'This request was already used for different details', 409)
                return json.loads(old['result'])
            business, branch = str(uuid.uuid4()), str(uuid.uuid4())
            db.execute('INSERT INTO businesses VALUES(?,?,?)', (business, name, packed(['remoteMonitoring','cloudReports','multipleBranches','multipleDevices'])))
            db.execute('INSERT INTO managed_businesses VALUES(?)', (business,))
            db.execute('INSERT INTO branches VALUES(?,?,?,?)', (business, branch, branch_name, zone))
            db.execute('INSERT INTO members VALUES(?,?,?,?)', (user, business, branch, 'OWNER'))
            result = dict(businessId=business, branchId=branch, businessName=name, branchName=branch_name, timezone=zone)
            db.execute('INSERT INTO setup_requests VALUES(?,?,?,?)', (user, request, payload, packed(result)))
        return result

    def create_branch(self, token, data):
        business, source = data.get('businessId'), data.get('branchId')
        name, zone = self.text(data.get('branchName'), 'branch name'), self.zone(data.get('timezone'))
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            user = self.owner(db, token, business, source)
            need(db.execute('SELECT id FROM managed_businesses WHERE id=?', (business,)).fetchone(), 'Branch setup is available for businesses created in this portal', 409)
            need(not db.execute('SELECT id FROM branches WHERE business=? AND name=?', (business,name)).fetchone(), 'A branch with this name already exists', 409)
            branch = str(uuid.uuid4())
            db.execute('INSERT INTO branches VALUES(?,?,?,?)', (business,branch,name,zone))
            db.execute('INSERT INTO members VALUES(?,?,?,?)', (user,business,branch,'OWNER'))
        return dict(businessId=business,branchId=branch)

    def register_device(self, token, data):
        business, branch = data.get('businessId'), data.get('branchId')
        name = self.text(data.get('deviceName'), 'device name', 80)
        code, device = 'LE1.' + secrets.token_urlsafe(32), str(uuid.uuid4())
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self.owner(db, token, business, branch)
            need(not db.execute('SELECT device FROM device_enrollments WHERE business=? AND branch=? AND name=? AND revoked=0', (business,branch,name)).fetchone(), 'This POS name is already registered. Revoke it before replacing it.', 409)
            db.execute('INSERT INTO device_enrollments VALUES(?,?,?,?,?,?,NULL,0)', (digest(code),business,branch,device,name,time.time()+86400))
        return dict(deviceId=device,deviceName=name,deviceCode=code,expiresIn=86400,message='Valid for 24 hours and one POS installation. Save it privately; it is shown only once.')

    def pair_managed_device(self, code, data):
        secret = data.get('installationSecret')
        need(isinstance(secret,str) and 40 <= len(secret) <= 200, 'Update the POS app to pair this device', 400)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM device_enrollments WHERE code=? AND revoked=0', (digest(code),)).fetchone()
            need(row, 'Device code is invalid or revoked', 403)
            claim = digest(secret)
            if row['claim']:
                need(row['claim'] == claim, 'Device code has already been paired to another installation', 409)
            else:
                need(row['expires'] > time.time(), 'Device code expired. Revoke it and generate a new code.', 403)
            zone = db.execute('SELECT zone FROM branches WHERE business=? AND id=?', (row['business'],row['branch'])).fetchone()[0]
            need(data.get('timezone') == zone, 'Use the branch timezone: ' + zone, 409)
            other = db.execute('SELECT id FROM devices WHERE token=?', (claim,)).fetchone()
            need(not other or other['id'] == row['device'], 'Installation is already paired to a different POS', 409)
            db.execute('UPDATE device_enrollments SET claim=? WHERE code=?', (claim,digest(code)))
            db.execute('INSERT OR IGNORE INTO devices VALUES(?,?,?,?,?,NULL,NULL)', (row['business'],row['branch'],row['device'],row['name'],claim))
        return dict(businessId=row['business'],branchId=row['branch'],deviceId=row['device'],timezone=zone,credentialType='installation')

    def list_devices(self, token, data):
        b, r = data.get('businessId'), data.get('branchId')
        with self.db() as db:
            self.owner(db,token,b,r)
            rows = db.execute('SELECT device,name,expires,claim,revoked FROM device_enrollments WHERE business=? AND branch=?', (b,r)).fetchall()
            result = [dict(deviceId=x['device'],name=x['name'],status='Revoked' if x['revoked'] else 'Paired' if x['claim'] else 'Expired' if x['expires'] <= time.time() else 'Awaiting pairing') for x in rows]
            seen = {x['deviceId'] for x in result}
            for x in db.execute('SELECT id,name FROM devices WHERE business=? AND branch=?', (b,r)).fetchall():
                if x['id'] not in seen:
                    result.append(dict(deviceId=x['id'],name=x['name'],status='Existing paired POS'))
        return {'devices':result}

    def revoke_device(self, token, data):
        b,r,d = (data.get(k) for k in ('businessId','branchId','deviceId'))
        need(all(isinstance(v,str) and v for v in (b,r,d)), 'Select a device')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self.owner(db,token,b,r)
            enrollment = db.execute('SELECT device FROM device_enrollments WHERE business=? AND branch=? AND device=?', (b,r,d)).fetchone()
            # Legacy registrations must also lose their pairing authority.
            if not enrollment:
                self.revoke_legacy_device(db,b,r,d)
            db.execute('UPDATE device_enrollments SET revoked=1 WHERE business=? AND branch=? AND device=?', (b,r,d))
            db.execute('DELETE FROM devices WHERE business=? AND branch=? AND id=?', (b,r,d))
        return {'message':'Device revoked. Its historical reports remain available; it can no longer sync.'}

    def revoke_legacy_device(self, db, business, branch, device):
        legacy = getattr(super(), 'revoke_legacy_device', None)
        need(legacy is not None, 'Unknown device', 404)
        legacy(db,business,branch,device)

    def team(self, token, data):
        b,r = data.get('businessId'),data.get('branchId')
        with self.db() as db:
            self.owner(db,token,b,r)
            need(db.execute('SELECT id FROM managed_businesses WHERE id=?',(b,)).fetchone(), 'Team setup is available for businesses created in this portal',409)
            rows = db.execute('SELECT u.id,u.email,m.role FROM members m JOIN users u ON u.id=m.user WHERE m.business=? AND m.branch=?',(b,r)).fetchall()
            invites = db.execute('SELECT email,role FROM team_invitations WHERE business=? AND branch=? AND expires>?',(b,r,time.time())).fetchall()
        return {'members':[dict(x) for x in rows],'invitations':[dict(x) for x in invites]}

    def remove_member(self, token, data):
        b,r,user = data.get('businessId'),data.get('branchId'),data.get('userId')
        need(isinstance(user,str) and user,'Select a team member')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self.owner(db,token,b,r)
            need(db.execute('SELECT id FROM managed_businesses WHERE id=?',(b,)).fetchone(), 'Team setup is available for businesses created in this portal',409)
            row = db.execute('SELECT role FROM members WHERE user=? AND business=? AND branch=?',(user,b,r)).fetchone()
            if row and row['role']=='OWNER':
                count = db.execute("SELECT count(*) FROM members WHERE business=? AND branch=? AND role='OWNER'",(b,r)).fetchone()[0]
                need(count>1,'The branch must keep at least one owner',409)
            db.execute('DELETE FROM members WHERE user=? AND business=? AND branch=?',(user,b,r))
            db.execute('DELETE FROM team_invitations WHERE business=? AND branch=? AND email=(SELECT email FROM users WHERE id=?)',(b,r,user))
        return {'message':'Branch access removed.'}

    def cancel_invitation(self, token, data):
        b,r = data.get('businessId'),data.get('branchId')
        email = self.text(data.get('email'),'email',254).lower()
        with self.db() as db:
            self.owner(db,token,b,r)
            db.execute('DELETE FROM team_invitations WHERE business=? AND branch=? AND email=?',(b,r,email))
        return {'message':'Invitation cancelled.'}

    def invite_member(self, token, data):
        b,r = data.get('businessId'),data.get('branchId')
        email = self.text(data.get('email'),'email',254).lower()
        role = data.get('role')
        need('@' in email and role in ('OWNER','ADMIN','STAFF'), 'Valid email and role required')
        code = secrets.token_urlsafe(32)
        with self.db() as db:
            self.owner(db,token,b,r)
            need(db.execute('SELECT id FROM managed_businesses WHERE id=?',(b,)).fetchone(), 'Team setup is available for businesses created in this portal',409)
            db.execute('DELETE FROM team_invitations WHERE business=? AND branch=? AND email=?',(b,r,email))
            db.execute('INSERT INTO team_invitations VALUES(?,?,?,?,?,?)',(digest(code),email,b,r,role,time.time()+86400))
        return {'invitationCode':code,'message':'Share this invitation privately with that email address. It expires in 24 hours.'}

    def redeem_team(self, token, code):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            user = self.owner(db,token)
            email = db.execute('SELECT email FROM users WHERE id=?',(user,)).fetchone()[0]
            row = db.execute('SELECT * FROM team_invitations WHERE code=? AND email=? AND expires>?',(digest(code),email,time.time())).fetchone()
            if not row:
                return None
            existing = db.execute('SELECT role FROM members WHERE user=? AND business=? AND branch=?',(user,row['business'],row['branch'])).fetchone()
            need(not existing, 'This account already belongs to this branch',409)
            db.execute('INSERT INTO members VALUES(?,?,?,?)',(user,row['business'],row['branch'],row['role']))
            db.execute('DELETE FROM team_invitations WHERE code=?',(digest(code),))
        return self.memberships(token) | {'message':'Invitation accepted. Only owners can access financial reports and device management.'}

    def dispatch(self,path,token,data,remote='local'):
        routes = {'/businesses/create':self.create_business,'/branches/create':self.create_branch,'/devices/register':self.register_device,'/devices/list':self.list_devices,'/devices/revoke':self.revoke_device,'/team/invite':self.invite_member,'/team/list':self.team,'/team/remove':self.remove_member,'/team/cancel':self.cancel_invitation}
        if path in routes:
            # Prevent omitted identity fields from accidentally requesting auth-only checks.
            if path != '/businesses/create':
                need(all(isinstance(data.get(k),str) and data[k] for k in ('businessId','branchId')), 'Select a business and branch')
            return routes[path](token,data)
        if path == '/device/pair' and token.startswith('LE1.'):
            return self.pair_managed_device(token,data)
        if path == '/redeem':
            code = data.get('code')
            need(isinstance(code,str) and 20 <= len(code) <= 200,'Invalid invitation')
            result = self.redeem_team(token,code)
            if result is not None:
                return result
        return super().dispatch(path,token,data,remote)
