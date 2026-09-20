"""Hosted boundary: managed authentication and email-bound owner invitations."""
import json
import os
import secrets
import time
import urllib.error
import urllib.request
import urllib.parse
from server import ApiError, Backend, digest, need


class CloudFeatures:
    def init_invitations(self):
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS invitations(token TEXT PRIMARY KEY,email TEXT,business TEXT,branch TEXT,expires REAL)')

    def auth_request(self, route, data, bearer='', method='POST'):
        base = os.environ['SUPABASE_URL'].rstrip('/')
        need(base.startswith('https://'), 'Authentication configuration unavailable', 503)
        headers = {'apikey': os.environ['SUPABASE_PUBLISHABLE_KEY'], 'Content-Type': 'application/json'}
        if bearer:
            headers['Authorization'] = 'Bearer ' + bearer
        request = urllib.request.Request(base + '/auth/v1/' + route,
            data=json.dumps(data).encode(), headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 429:
                raise ApiError(429, 'Too many attempts. Please try again later.')
            raise ApiError(400, 'Unable to continue. Check your details and email confirmation.')
        except (urllib.error.URLError, TimeoutError):
            raise ApiError(503, 'Account service is unavailable. Please try again.')

    def credentials(self, data):
        email, password = data.get('email'), data.get('password')
        need(isinstance(email, str) and 3 <= len(email) <= 254 and '@' in email, 'Enter a valid email')
        need(isinstance(password, str) and 12 <= len(password) <= 1024, 'Use a password of 12–1024 characters')
        return email.strip().lower(), password

    def managed_login(self, data):
        email, password = self.credentials(data)
        result = self.auth_request('token?grant_type=password', {'email': email, 'password': password})
        user = result.get('user', {})
        need(user.get('email_confirmed_at') and user.get('id') and user.get('email'), 'Confirm your email before signing in', 401)
        token = secrets.token_urlsafe(48)
        with self.db() as db:
            # Store only provider identity; never store the Supabase password or JWT.
            db.execute('INSERT INTO users VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET email=excluded.email',
                       (user['id'], user['email'].lower(), '', ''))
            db.execute('DELETE FROM sessions WHERE expires<?', (time.time(),))
            db.execute('INSERT INTO sessions VALUES(?,?,?)', (digest(token), user['id'], time.time()+28800))
        return {'token': token, 'expiresIn': 28800}

    def create_invitation(self, email, business, branch):
        need(isinstance(email, str) and '@' in email, 'Valid email required')
        code = secrets.token_urlsafe(32)
        with self.db() as db:
            need(db.execute('SELECT id FROM branches WHERE business=? AND id=?', (business, branch)).fetchone(), 'Unknown branch')
            db.execute('INSERT INTO invitations VALUES(?,?,?,?,?)',
                       (digest(code), email.strip().lower(), business, branch, time.time()+86400))
        return code

    def redeem(self, token, code):
        need(isinstance(code, str) and 20 <= len(code) <= 200, 'Invalid invitation')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            user = self.owner(db, token)
            email = db.execute('SELECT email FROM users WHERE id=?', (user,)).fetchone()[0]
            invitation = db.execute('SELECT * FROM invitations WHERE token=? AND email=? AND expires>?',
                                    (digest(code), email, time.time())).fetchone()
            need(invitation, 'Invitation is invalid, expired, or belongs to another email', 403)
            db.execute('INSERT OR REPLACE INTO members VALUES(?,?,?,?)',
                       (user, invitation['business'], invitation['branch'], 'OWNER'))
            db.execute('DELETE FROM invitations WHERE token=?', (digest(code),))
        return self.memberships(token)

    def dispatch(self, path, token, data, remote='local'):
        if path == '/recover':
            email = data.get('email')
            need(isinstance(email, str) and 3 <= len(email) <= 254 and '@' in email, 'Enter a valid email')
            redirect = os.environ['PUBLIC_ORIGIN'].rstrip('/') + '/reset.html'
            self.auth_request('recover?redirect_to=' + urllib.parse.quote(redirect, safe=''), {'email': email.strip().lower()})
            return {'message': 'If this email has an account, a password-reset link has been sent. Check your inbox and spam folder.'}
        if path == '/reset-password':
            password = data.get('password')
            need(isinstance(token, str) and 20 <= len(token) <= 8192, 'Open a valid password-reset email link', 401)
            need(isinstance(password, str) and 12 <= len(password) <= 1024, 'Use a password of 12–1024 characters')
            user = self.auth_request('user', {'password': password}, bearer=token, method='PUT')
            need(user.get('id'), 'Password update could not be verified', 400)
            with self.db() as db:
                db.execute('DELETE FROM sessions WHERE user=?', (user['id'],))
            return {'message': 'Password updated. Sign in with your new password.'}
        if path == '/signup':
            email, password = self.credentials(data)
            self.auth_request('signup', {'email': email, 'password': password})
            return {'message': 'Check your email to confirm your account, then sign in. Existing accounts can sign in directly.'}
        if path == '/login' and os.environ.get('SUPABASE_URL'):
            return self.managed_login(data)
        if path == '/redeem':
            return self.redeem(token, data.get('code'))
        return super().dispatch(path, token, data, remote)


class LocalCloudBackend(CloudFeatures, Backend):
    def __init__(self, path):
        super().__init__(path)
        self.init_invitations()


def production_backend():
    import psycopg
    from psycopg import sql
    from postgres import PostgresBackend
    from registered import RegisteredShops
    class HostedBackend(RegisteredShops, CloudFeatures, PostgresBackend):
        pass
    for name in ('DATABASE_URL', 'SUPABASE_URL', 'SUPABASE_PUBLISHABLE_KEY', 'PUBLIC_ORIGIN'):
        if not os.environ.get(name):
            raise RuntimeError('Missing required setting: ' + name)
    with psycopg.connect(os.environ['DATABASE_URL'], sslmode='require', connect_timeout=15) as db:
        db.execute('CREATE SCHEMA IF NOT EXISTS laundry')
        db.execute('REVOKE ALL ON SCHEMA laundry FROM PUBLIC, anon, authenticated')
    api = HostedBackend(os.environ['DATABASE_URL'])
    api.init_invitations()
    with psycopg.connect(os.environ['DATABASE_URL'], sslmode='require', connect_timeout=15) as db:
        db.execute('REVOKE ALL ON ALL TABLES IN SCHEMA laundry FROM PUBLIC, anon, authenticated')
        db.execute('REVOKE ALL ON ALL SEQUENCES IN SCHEMA laundry FROM PUBLIC, anon, authenticated')
        for (table,) in db.execute("SELECT tablename FROM pg_tables WHERE schemaname='laundry'").fetchall():
            db.execute(sql.SQL('ALTER TABLE laundry.{} ENABLE ROW LEVEL SECURITY').format(sql.Identifier(table)))
    return api
