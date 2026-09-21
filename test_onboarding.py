import json
import tempfile
import time
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from hosted import LocalCloudBackend
from server import ApiError, digest


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.api = LocalCloudBackend(Path(self.tmp.name)/'db')
        with self.api.db() as db:
            for name in ('alice','bob','staff'):
                db.execute('INSERT INTO users VALUES(?,?,?,?)',(name,name+'@example.test','',''))
                db.execute('INSERT INTO sessions VALUES(?,?,?)',(digest(name),name,time.time()+3600))
        self.a = self.create('alice')
        self.b = self.create('bob')

    def tearDown(self):
        self.tmp.cleanup()

    def create(self,user):
        return self.api.dispatch('/businesses/create',user,dict(businessName=user,branchName='Main',timezone='Asia/Manila',requestId='first'))

    def register(self,token='alice',scope=None,name='Tablet'):
        return self.api.dispatch('/devices/register',token,(scope or self.a)|dict(deviceName=name))

    def pair(self,code,secret='a'*64,**fields):
        return self.api.dispatch('/device/pair',code,dict(installationSecret=secret,timezone='Asia/Manila',**fields))

    def test_creation_idempotent_and_caller_identity_ignored(self):
        self.assertEqual(self.a,self.create('alice'))
        self.assertNotEqual(self.a['businessId'],self.b['businessId'])
        with self.assertRaises(ApiError):
            self.api.dispatch('/businesses/create','alice',dict(businessName='Changed',branchName='Main',timezone='UTC',requestId='first'))
        with self.api.db() as db:
            self.assertEqual(2,db.execute('SELECT count(*) FROM businesses').fetchone()[0])

    def test_scope_isolation_and_missing_scope(self):
        for path in ('/devices/register','/devices/list','/devices/revoke','/branches/create','/team/invite','/report','/attention','/activity'):
            with self.subTest(path=path),self.assertRaises(ApiError):
                self.api.dispatch(path,'bob',self.a|dict(deviceName='X',deviceId='X',branchName='X',timezone='UTC',email='staff@example.test',role='OWNER'))
        for data in ({},dict(businessId=None,branchId=None)):
            with self.assertRaises(ApiError):self.api.dispatch('/devices/register','alice',data|dict(deviceName='X'))
        self.assertEqual(self.a['businessId'],self.api.memberships('alice')['memberships'][0]['business'])
        self.assertEqual(1,len(self.api.memberships('alice')['memberships']))

    def test_one_installation_retry_and_revocation(self):
        d=self.register();code=d['deviceCode']
        identity=self.pair(code,businessId='forged',deviceId='forged')
        self.assertEqual(self.a['businessId'],identity['businessId'])
        self.assertEqual(d['deviceId'],identity['deviceId'])
        self.assertEqual(identity,self.pair(code))
        with self.assertRaises(ApiError):self.pair(code,'b'*64)
        payload={k:identity[k] for k in ('businessId','branchId','deviceId','timezone')}|{'events':[]}
        self.api.dispatch('/sync','a'*64,payload)
        with self.assertRaises(ApiError):self.api.dispatch('/sync',code,payload)
        with self.assertRaises(ApiError):self.api.dispatch('/sync','a'*64,payload|self.b)
        self.api.dispatch('/devices/revoke','alice',self.a|d)
        with self.assertRaises(ApiError):self.api.dispatch('/sync','a'*64,payload)
        with self.assertRaises(ApiError):self.pair(code)
        self.assertEqual('Revoked',self.api.dispatch('/devices/list','alice',self.a)['devices'][0]['status'])

    def test_expiration_and_no_plaintext_credentials(self):
        d=self.register()
        with self.api.db() as db:
            row=db.execute('SELECT * FROM device_enrollments').fetchone()
            self.assertNotIn(d['deviceCode'],list(row))
            db.execute('UPDATE device_enrollments SET expires=0')
        with self.assertRaises(ApiError):self.pair(d['deviceCode'])

    def test_concurrent_claim_only_one_wins(self):
        d=self.register()
        def claim(secret):
            try:self.pair(d['deviceCode'],secret);return True
            except ApiError:return False
        with ThreadPoolExecutor(2) as pool:
            self.assertEqual(1,sum(pool.map(claim,['a'*64,'b'*64])))

    def test_branch_and_multiple_pos(self):
        branch=self.api.dispatch('/branches/create','alice',self.a|dict(branchName='Second',timezone='UTC'))
        self.assertEqual(2,len(self.api.memberships('alice')['memberships']))
        d=self.register(scope=branch)
        with self.assertRaises(ApiError):self.pair(d['deviceCode'])
        for name,secret in [('First','a'*64),('Second','b'*64)]:
            self.pair(self.register(name=name)['deviceCode'],secret)
        self.assertEqual(2,len(self.api.dispatch('/devices/list','alice',self.a)['devices']))

    def test_roles_and_email_bound_single_use_invitation(self):
        invitation=self.api.dispatch('/team/invite','alice',self.a|dict(email='staff@example.test',role='STAFF'))
        with self.assertRaises(ApiError):self.api.dispatch('/redeem','bob',{'code':invitation['invitationCode']})
        self.api.dispatch('/redeem','staff',{'code':invitation['invitationCode']})
        self.assertEqual([],self.api.memberships('staff')['memberships'])
        with self.assertRaises(ApiError):self.register('staff')
        with self.assertRaises(ApiError):self.api.dispatch('/report','staff',self.a)
        with self.assertRaises(ApiError):self.api.dispatch('/redeem','staff',{'code':invitation['invitationCode']})


if __name__=='__main__':unittest.main()
