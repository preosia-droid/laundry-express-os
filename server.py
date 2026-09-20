"""Lean owner reporting reference backend. Loopback-only development server.

Deploy behind authenticated TLS ingress and a production WSGI server; never expose
the development HTTP server. All authorization is enforced here, before queries.
Money is integer minor units. Records + audit + aggregate deltas commit together.
"""
import argparse
import calendar
import datetime as dt
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from zoneinfo import ZoneInfo
from wsgiref.simple_server import make_server

CAPABILITIES = ('remoteMonitoring', 'cloudReports', 'advancedAnalytics',
                'multipleBranches', 'multipleDevices', 'cloudBackup')
FIELDS = ('grossSales', 'netSales', 'amountCollected', 'outstandingBalance',
          'orderCount', 'refundTotal', 'voidedTotal')


class ApiError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def need(condition, message='Invalid request', status=400):
    if not condition:
        raise ApiError(status, message)


def packed(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def empty():
    return {**dict.fromkeys(FIELDS, 0), 'paymentTotals': {}}


def combine(target, value, sign=1):
    for key in FIELDS:
        target[key] += value.get(key, 0) * sign
    for key, amount in value.get('paymentTotals', {}).items():
        target['paymentTotals'][key] = target['paymentTotals'].get(key, 0) + sign * amount
        if target['paymentTotals'][key] == 0:
            del target['paymentTotals'][key]
    return target


def view(value):
    value = dict(value)
    value['averageOrderValue'] = (value['netSales'] / value['orderCount']) if value['orderCount'] else 0
    value['cashTotal'] = value['paymentTotals'].get('CASH', 0)
    return value


class Backend:
    def __init__(self, path):
        self.path = str(path)
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,email TEXT UNIQUE,salt TEXT,hash TEXT);
            CREATE TABLE IF NOT EXISTS businesses(id TEXT PRIMARY KEY,name TEXT,capabilities TEXT);
            CREATE TABLE IF NOT EXISTS branches(business TEXT,id TEXT,name TEXT,zone TEXT,PRIMARY KEY(business,id));
            CREATE TABLE IF NOT EXISTS members(user TEXT,business TEXT,branch TEXT,role TEXT,PRIMARY KEY(user,business,branch));
            CREATE TABLE IF NOT EXISTS devices(business TEXT,branch TEXT,id TEXT,name TEXT,token TEXT UNIQUE,seen TEXT,synced TEXT,PRIMARY KEY(business,branch,id));
            CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,user TEXT,expires REAL);
            CREATE TABLE IF NOT EXISTS login_limits(key TEXT PRIMARY KEY,count INTEGER,until REAL);
            CREATE TABLE IF NOT EXISTS records(business TEXT,branch TEXT,device TEXT,kind TEXT,id TEXT,revision INTEGER,day TEXT,payload TEXT,PRIMARY KEY(business,branch,device,kind,id));
            CREATE INDEX IF NOT EXISTS records_day ON records(business,branch,day,kind);
            CREATE INDEX IF NOT EXISTS records_kind ON records(business,branch,kind,id);
            CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,business TEXT,branch TEXT,device TEXT,revision INTEGER,kind TEXT,id TEXT,payload TEXT,received TEXT,UNIQUE(business,branch,device,revision));
            CREATE INDEX IF NOT EXISTS events_recent ON events(business,branch,seq DESC);
            CREATE TABLE IF NOT EXISTS summaries(business TEXT,branch TEXT,period TEXT,key TEXT,payload TEXT,updated TEXT,reconciled TEXT,PRIMARY KEY(business,branch,period,key));
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def provision(self, email, password, business, branch, device, zone='Asia/Manila',
                  name='Laundry Express', branch_name='Main Branch'):
        need(len(password) >= 12, 'Use at least 12 characters')
        ZoneInfo(zone)
        with self.db() as db:
            existing = db.execute('SELECT * FROM users WHERE email=?', (email.lower(),)).fetchone()
            if existing:
                need(hmac.compare_digest(existing['hash'], hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(existing['salt']), 600000).hex()), 'Existing account password mismatch', 403)
                user = existing['id']
            else:
                user, salt = str(uuid.uuid4()), secrets.token_hex(16)
                ph = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 600000).hex()
                db.execute('INSERT INTO users VALUES(?,?,?,?)', (user, email.lower(), salt, ph))
            # Provisioning is an operator command, never a public sign-up/entitlement endpoint.
            db.execute('INSERT OR IGNORE INTO businesses VALUES(?,?,?)', (business, name, packed(['remoteMonitoring','cloudReports'])))
            db.execute('INSERT OR IGNORE INTO branches VALUES(?,?,?,?)', (business, branch, branch_name, zone))
            db.execute('INSERT OR REPLACE INTO members VALUES(?,?,?,?)', (user, business, branch, 'OWNER'))
            token = secrets.token_urlsafe(48)
            db.execute('INSERT INTO devices VALUES(?,?,?,?,?,NULL,NULL) ON CONFLICT(business,branch,id) DO UPDATE SET token=excluded.token', (business, branch, device, 'Main POS', digest(token)))
        return token

    def login(self, email, password, remote='local'):
        need(isinstance(email, str) and isinstance(password, str) and len(password) <= 1024)
        keys = ['email:' + email.lower(), 'ip:' + remote]
        with self.db() as db:
            for key in keys:
                limit = db.execute('SELECT * FROM login_limits WHERE key=?', (key,)).fetchone()
                need(not limit or limit['until'] < time.time() or limit['count'] < 10, 'Try again later', 429)
            row = db.execute('SELECT * FROM users WHERE email=?', (email.lower(),)).fetchone()
            salt = bytes.fromhex(row['salt']) if row else b'constant-dummy-salt'
            ph = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 600000).hex()
            valid = bool(row and hmac.compare_digest(row['hash'], ph))
            if not valid:
                for key in keys:
                    db.execute('INSERT INTO login_limits VALUES(?,1,?) ON CONFLICT(key) DO UPDATE SET count=CASE WHEN until<? THEN 1 ELSE count+1 END,until=CASE WHEN until<? THEN excluded.until ELSE until END', (key,time.time()+900,time.time(),time.time()))
            else:
                db.execute('DELETE FROM login_limits WHERE key=?', (keys[0],))
                db.execute('DELETE FROM sessions WHERE expires<?', (time.time(),))
                token = secrets.token_urlsafe(48)
                db.execute('INSERT INTO sessions VALUES(?,?,?)', (digest(token), row['id'], time.time()+3600*8))
        need(valid, 'Invalid account or password', 401)
        return {'token':token, 'expiresIn':28800}

    def owner(self, db, token, business=None, branch=None, capability='cloudReports'):
        session = db.execute('SELECT user FROM sessions WHERE token=? AND expires>?', (digest(token),time.time())).fetchone()
        need(session is not None, 'Sign in again', 401)
        if business is not None:
            member = db.execute('SELECT role FROM members WHERE user=? AND business=? AND branch=?', (session['user'],business,branch)).fetchone()
            need(member and member['role']=='OWNER', 'Access denied', 403)
            caps = db.execute('SELECT capabilities FROM businesses WHERE id=?', (business,)).fetchone()
            need(caps and capability in json.loads(caps[0]), 'Capability unavailable', 403)
        return session['user']

    def memberships(self, token):
        with self.db() as db:
            user = self.owner(db, token)
            rows = db.execute('SELECT m.business,m.branch,b.name businessName,r.name branchName,r.zone,b.capabilities FROM members m JOIN businesses b ON b.id=m.business JOIN branches r ON r.business=m.business AND r.id=m.branch WHERE m.user=? AND m.role=?', (user,'OWNER')).fetchall()
            return {'memberships':[dict(row) | {'capabilities':json.loads(row['capabilities'])} for row in rows]}

    def validate(self, event, zone):
        need(set(event)=={'revision','kind','id','payload'}, 'Unexpected event fields')
        need(type(event['revision']) is int and 0 < event['revision'] < 2**63)
        need(isinstance(event['id'],str) and 1 <= len(event['id']) <= 100)
        kind, p = event['kind'], event['payload']
        need(isinstance(p,dict))
        if kind=='order':
            allowed={'orderNumber','createdAt','orderTotal','grossTotal','amountPaid','outstandingBalance','paymentStatus','voided','totalClothingPieces','removed'}
            need(set(p)==allowed, 'Unexpected order fields')
            for key in ('orderTotal','grossTotal','amountPaid','outstandingBalance'):
                need(type(p[key]) is int and 0 <= p[key] <= 10**12, 'Invalid money')
            need(type(p['voided']) is bool and type(p['removed']) is bool)
            need(p['outstandingBalance']==(0 if p['voided'] else max(0,p['orderTotal']-p['amountPaid'])))
            need(p['paymentStatus'] in ('PAID','PARTIAL','UNPAID','VOIDED'))
            need(type(p['orderNumber']) is int and p['orderNumber']>0)
            need(p['totalClothingPieces'] is None or (type(p['totalClothingPieces']) is int and 0 <= p['totalClothingPieces'] <= 1000000))
        elif kind=='payment':
            need(set(p)=={'orderId','amount','paymentMethod','description','paymentDateTime','removed'}, 'Unexpected payment fields')
            need(type(p['amount']) is int and abs(p['amount'])<=10**12)
            need(isinstance(p['orderId'],str) and len(p['orderId'])<=100)
            need(type(p['removed']) is bool)
            need(p['paymentMethod'] in ('CASH','GCASH','CARD','OTHER'))
            need(isinstance(p['description'],str) and len(p['description'])<=240)
            need(p['paymentMethod']!='OTHER' or bool(p['description']))
        elif kind=='inventory':
            need(set(p)=={'itemName','currentQuantity','unit','reorderLevel','alertType','lastUpdatedAt'}, 'Unexpected inventory fields')
            need(p['alertType'] in ('LOW','CRITICAL','OUT','RESOLVED'))
            need(isinstance(p['itemName'],str) and 0 < len(p['itemName'])<=240)
            need(isinstance(p['unit'],str) and len(p['unit'])<=50)
            # Offline sales may consume stock before an opening balance is entered.
            # Preserve the shortage so its alert cannot block financial delivery.
            need(type(p['currentQuantity']) in (int,float) and -10**12 <= p['currentQuantity'] <= 10**12, 'Invalid stock quantity')
            need(type(p['reorderLevel']) in (int,float) and 0 <= p['reorderLevel'] <= 10**12, 'Invalid reorder level')
        else:
            raise ApiError(400,'Unsupported event kind')
        stamp = p.get('createdAt',p.get('paymentDateTime',p.get('lastUpdatedAt')))
        try:
            instant=dt.datetime.fromisoformat(stamp)
            need(instant.tzinfo is not None,'Timestamp must include offset')
            return instant.astimezone(ZoneInfo(zone)).date().isoformat()
        except (ValueError,TypeError):
            raise ApiError(400,'Invalid timestamp')

    @staticmethod
    def contribution(kind, p):
        v=empty()
        if p.get('removed'):
            return v
        if kind=='order':
            if p['voided']:
                v['voidedTotal']=p['orderTotal']
            else:
                v.update(grossSales=p['grossTotal'],netSales=p['orderTotal'],outstandingBalance=p['outstandingBalance'],orderCount=1)
        elif kind=='payment':
            v['amountCollected']=p['amount']
            v['refundTotal']=max(0,-p['amount'])
            label=p['description'] if p['paymentMethod']=='OTHER' else p['paymentMethod']
            v['paymentTotals'][label]=p['amount']
        return v

    def delta(self, db, business, branch, day, value, sign):
        for period,key in [('day',day),('month',day[:7])]:
            row=db.execute('SELECT payload FROM summaries WHERE business=? AND branch=? AND period=? AND key=?',(business,branch,period,key)).fetchone()
            current=json.loads(row[0]) if row else empty()
            combine(current,value,sign)
            db.execute('INSERT INTO summaries VALUES(?,?,?,?,?,?,NULL) ON CONFLICT(business,branch,period,key) DO UPDATE SET payload=excluded.payload,updated=excluded.updated,reconciled=NULL',(business,branch,period,key,packed(current),now_iso()))

    def ingest(self, token, data):
        need(set(data)=={'businessId','branchId','deviceId','timezone','events'})
        need(isinstance(data['events'],list) and len(data['events'])<=100)
        b,r,d=data['businessId'],data['branchId'],data['deviceId']
        with self.db() as db:
            # Acquire write lock before reads: concurrent retries cannot both apply deltas.
            db.execute('BEGIN IMMEDIATE')
            device=db.execute('SELECT * FROM devices WHERE business=? AND branch=? AND id=? AND token=?',(b,r,d,digest(token))).fetchone()
            need(device is not None,'Device access denied',403)
            zone=db.execute('SELECT zone FROM branches WHERE business=? AND id=?',(b,r)).fetchone()[0]
            need(data['timezone']==zone,'Branch timezone mismatch; configure before syncing',409)
            accepted=[]
            for event in data['events']:
                day=self.validate(event,zone)
                rev,kind,rid,p=event['revision'],event['kind'],event['id'],event['payload']
                previous_event=db.execute('SELECT kind,id,payload FROM events WHERE business=? AND branch=? AND device=? AND revision=?',(b,r,d,rev)).fetchone()
                if previous_event:
                    need(previous_event['kind']==kind and previous_event['id']==rid and previous_event['payload']==packed(p),'Revision reused with different data',409)
                    accepted.append(rev)
                    continue
                old=db.execute('SELECT * FROM records WHERE business=? AND branch=? AND device=? AND kind=? AND id=?',(b,r,d,kind,rid)).fetchone()
                db.execute('INSERT INTO events(business,branch,device,revision,kind,id,payload,received) VALUES(?,?,?,?,?,?,?,?)',(b,r,d,rev,kind,rid,packed(p),now_iso()))
                if not old or rev>old['revision']:
                    if kind!='inventory':
                        if old:
                            self.delta(db,b,r,old['day'],self.contribution(kind,json.loads(old['payload'])),-1)
                        self.delta(db,b,r,day,self.contribution(kind,p),1)
                    db.execute('INSERT INTO records VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(business,branch,device,kind,id) DO UPDATE SET revision=excluded.revision,day=excluded.day,payload=excluded.payload',(b,r,d,kind,rid,rev,day,packed(p)))
                accepted.append(rev)
            stamp=now_iso()
            # A completed catch-up (short final batch) is required to advertise successful sync.
            db.execute('UPDATE devices SET seen=?,synced=CASE WHEN ? THEN ? ELSE synced END WHERE business=? AND branch=? AND id=?',(stamp,len(data['events'])<100,stamp,b,r,d))
            self.reconcile(db,b,r,zone)
        return {'accepted':accepted,'serverTime':stamp}

    def reconcile(self,db,b,r,zone):
        today=dt.datetime.now(ZoneInfo(zone)).date().isoformat()
        # Reconcile dirty closed days only, bounded per request. New historical corrections
        # invalidate reconciliation, so a closed summary can never freeze a wrong total.
        days=db.execute("SELECT key,payload FROM summaries WHERE business=? AND branch=? AND period='day' AND key<? AND reconciled IS NULL ORDER BY key LIMIT 7",(b,r,today)).fetchall()
        for row in days:
            total=empty()
            records=db.execute('SELECT kind,payload FROM records WHERE business=? AND branch=? AND day=?',(b,r,row['key'])).fetchall()
            for rec in records:
                combine(total,self.contribution(rec['kind'],json.loads(rec['payload'])))
            difference=combine(total.copy() | {'paymentTotals':dict(total['paymentTotals'])},json.loads(row['payload']),-1)
            self.delta(db,b,r,row['key'],difference,1)
            db.execute("UPDATE summaries SET reconciled=? WHERE business=? AND branch=? AND period='day' AND key=?",(now_iso(),b,r,row['key']))

    def report(self,token,b,r,period):
        need(period in ('TODAY','WEEK','MONTH','YEAR'))
        with self.db() as db:
            self.owner(db,token,b,r)
            branch=db.execute('SELECT * FROM branches WHERE business=? AND id=?',(b,r)).fetchone()
            today=dt.datetime.now(ZoneInfo(branch['zone'])).date()
            if period=='TODAY':
                start,end=today,today
                prev_start,prev_end=today-dt.timedelta(days=1),today-dt.timedelta(days=1)
            elif period=='WEEK':
                start=today-dt.timedelta(days=today.weekday());end=today
                prev_start,prev_end=start-dt.timedelta(days=7),today-dt.timedelta(days=7)
            elif period=='MONTH':
                start=today.replace(day=1);end=today
                prev_start=(start-dt.timedelta(days=1)).replace(day=1)
                prev_end=prev_start.replace(day=min(today.day,calendar.monthrange(prev_start.year,prev_start.month)[1]))
            else:
                start=today.replace(month=1,day=1);end=today
                prev_start=start.replace(year=start.year-1)
                prev_end=today.replace(year=today.year-1,day=min(today.day,calendar.monthrange(today.year-1,today.month)[1]))

            def read(first,last,monthly=False):
                # A year uses 11 completed month rows plus <=31 daily rows for the partial
                # current month. This preserves a fair same-elapsed-period comparison.
                if monthly and first.month < last.month:
                    rows=db.execute("SELECT key,payload,updated,reconciled FROM summaries WHERE business=? AND branch=? AND period='month' AND key>=? AND key<? ORDER BY key",(b,r,first.isoformat()[:7],last.isoformat()[:7])).fetchall()
                    day_first=last.replace(day=1)
                else:
                    rows=[];day_first=first
                daily=db.execute("SELECT key,payload,updated,reconciled FROM summaries WHERE business=? AND branch=? AND period='day' AND key>=? AND key<=? ORDER BY key",(b,r,day_first.isoformat(),last.isoformat())).fetchall()
                result=[{'date':x['key'],**json.loads(x['payload']),'lastUpdatedAt':x['updated'],'reconciledAt':x['reconciled']} for x in rows]
                if monthly:
                    v=empty()
                    for x in daily:combine(v,json.loads(x['payload']))
                    result.append({'date':last.isoformat()[:7],**v})
                else:result.extend({'date':x['key'],**json.loads(x['payload']),'lastUpdatedAt':x['updated'],'reconciledAt':x['reconciled']} for x in daily)
                total=empty()
                for x in result:combine(total,x)
                return view(total),result,len(rows)+len(daily)

            current,trend,n=read(start,end,period=='YEAR')
            previous,_,pn=read(prev_start,prev_end,period=='YEAR')
            return {'period':period,'timezone':branch['zone'],'from':str(start),'to':str(end),
                    'previousFrom':str(prev_start),'previousTo':str(prev_end),'current':current,
                    'previous':previous,'comparisonPercent':((current['netSales']-previous['netSales'])/previous['netSales']*100 if previous['netSales'] else None),
                    'trend':trend,'summaryRowsRead':n+pn,'serverTime':now_iso()}

    def attention(self,token,b,r,cursor='',limit=20):
        need(1<=limit<=50)
        with self.db() as db:
            self.owner(db,token,b,r,capability='remoteMonitoring')
            rows=db.execute("SELECT device,id,payload FROM records WHERE business=? AND branch=? AND kind='inventory' AND device||':'||id>? AND json_extract(payload,'$.alertType')!='RESOLVED' ORDER BY device||':'||id LIMIT ?",(b,r,cursor,limit+1)).fetchall()
            counts=db.execute("SELECT json_extract(payload,'$.alertType') alert,COUNT(*) n FROM records WHERE business=? AND branch=? AND kind='inventory' GROUP BY alert",(b,r)).fetchall()
            devices=db.execute('SELECT id,name,seen,synced FROM devices WHERE business=? AND branch=?',(b,r)).fetchall()
            return {'alerts':[dict(json.loads(x['payload']),inventoryItemId=x['id'],deviceId=x['device']) for x in rows[:limit]],
                    'nextCursor':(rows[limit-1]['device']+':'+rows[limit-1]['id']) if len(rows)>limit else None,
                    'counts':{x['alert']:x['n'] for x in counts if x['alert']!='RESOLVED'},
                    'devices':[dict(x) | {'online':bool(x['seen'] and (dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(x['seen'])).total_seconds()<1200)} for x in devices], 'serverTime':now_iso()}

    def activity(self,token,b,r,cursor=2**63-1,limit=20):
        need(1<=limit<=50)
        with self.db() as db:
            self.owner(db,token,b,r)
            rows=db.execute("SELECT seq,kind,id,payload,received FROM events WHERE business=? AND branch=? AND kind IN ('order','payment') AND seq<? ORDER BY seq DESC LIMIT ?",(b,r,cursor,limit+1)).fetchall()
            return {'items':[dict(x) | {'payload':json.loads(x['payload'])} for x in rows[:limit]],'nextCursor':rows[limit-1]['seq'] if len(rows)>limit else None}

    def dispatch(self, path, token, data, remote='local'):
        if path=='/login':return self.login(data.get('email'),data.get('password'),remote)
        if path=='/sync':return self.ingest(token,data)
        if path=='/memberships':return self.memberships(token)
        if path=='/logout':
            with self.db() as db:db.execute('DELETE FROM sessions WHERE token=?',(digest(token),))
            return {}
        b,r=data.get('businessId'),data.get('branchId')
        need(isinstance(b,str) and isinstance(r,str))
        if path=='/report':return self.report(token,b,r,data.get('period','TODAY'))
        if path=='/attention':return self.attention(token,b,r,str(data.get('cursor','')),int(data.get('limit',20)))
        if path=='/activity':return self.activity(token,b,r,int(data.get('cursor',2**63-1)),int(data.get('limit',20)))
        raise ApiError(404,'Not found')

    def __call__(self, env, start_response):
        try:
            need(env['REQUEST_METHOD']=='POST','POST required',405)
            size=int(env.get('CONTENT_LENGTH') or 0)
            need(0<size<=512000,'Request size limit',413)
            data=json.loads(env['wsgi.input'].read(size))
            need(isinstance(data,dict))
            token=env.get('HTTP_AUTHORIZATION','').removeprefix('Bearer ')
            result=self.dispatch(env['PATH_INFO'],token,data,env.get('REMOTE_ADDR','unknown'))
            status=200
        except ApiError as e:status,result=e.status,{'error':e.message}
        except (ValueError,TypeError,KeyError,OverflowError):status,result=400,{'error':'Invalid request'}
        except Exception:status,result=500,{'error':'Request failed; retry safely'}
        body=packed(result).encode()
        start_response(f'{status} '+{200:'OK',400:'Bad Request',401:'Unauthorized',403:'Forbidden',404:'Not Found',405:'Method Not Allowed',409:'Conflict',413:'Too Large',429:'Too Many Requests',500:'Server Error'}.get(status,'Error'),[('Content-Type','application/json'),('Content-Length',str(len(body))),('Cache-Control','no-store'),('X-Content-Type-Options','nosniff')])
        return [body]


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--db',default='development.db')
    parser.add_argument('--port',type=int,default=8787)
    args=parser.parse_args()
    print(f'Development API on http://127.0.0.1:{args.port}',flush=True)
    with make_server('127.0.0.1',args.port,Backend(args.db)) as server:
        server.serve_forever()
