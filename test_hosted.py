import io
import json
import tempfile
import unittest
from pathlib import Path
from hosted import LocalCloudBackend
from server import ApiError
from webapp import Website


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.api = LocalCloudBackend(Path(self.tmp.name) / 'test.db')
        self.api.provision('a@example.test','correct horse battery','A','main','pos','UTC')
        self.api.provision('b@example.test','another horse battery','B','main','pos','UTC')
        self.a = self.api.login('a@example.test','correct horse battery')['token']
        self.b = self.api.login('b@example.test','another horse battery')['token']
        self.web = Website(self.api,'https://shop.example.test')

    def tearDown(self):
        self.tmp.cleanup()

    def request(self,path,method='GET',data=None,token='',origin=None):
        body=json.dumps(data or {}).encode()
        env={'PATH_INFO':path,'REQUEST_METHOD':method,'wsgi.input':io.BytesIO(body),'CONTENT_LENGTH':str(len(body)),'CONTENT_TYPE':'application/json','HTTP_AUTHORIZATION':'Bearer '+token}
        if origin: env['HTTP_ORIGIN']=origin
        result={}
        def start(status,headers):result.update(status=status,headers=dict(headers))
        result['body']=b''.join(self.web(env,start))
        return result

    def test_static_files_and_source_boundary(self):
        self.assertTrue(self.request('/owner')['status'].startswith('200'))
        for path in ['/../server.py','/server.py','/.env','/test.db']:
            self.assertTrue(self.request(path)['status'].startswith('404'))

    def test_origin_and_unauthorized_data(self):
        self.assertTrue(self.request('/memberships','POST',token=self.a,origin='https://evil.test')['status'].startswith('403'))
        r=self.request('/report','POST',{'businessId':'A','branchId':'main'})
        self.assertTrue(r['status'].startswith('401'))
        self.assertEqual('no-store',r['headers']['Cache-Control'])
        self.assertTrue(self.request('/report','POST',{'businessId':'A','branchId':'main'},self.b)['status'].startswith('403'))

    def test_invitation_email_binding_single_use_and_logout(self):
        code=self.api.create_invitation('b@example.test','A','main')
        with self.assertRaises(ApiError):self.api.redeem(self.a,code)
        result=self.api.redeem(self.b,code)
        self.assertEqual(2,len(result['memberships']))
        with self.assertRaises(ApiError):self.api.redeem(self.b,code)
        self.api.dispatch('/logout',self.b,{})
        with self.assertRaises(ApiError):self.api.memberships(self.b)

    def test_unconfirmed_account_denied(self):
        self.api.auth_request=lambda *args: {'user':{'id':'new','email':'new@example.test'}}
        with self.assertRaises(ApiError):self.api.managed_login({'email':'new@example.test','password':'new horse battery'})

    def test_confirmed_signup_does_not_grant_membership(self):
        self.api.auth_request=lambda *args: {'user':{'id':'new','email':'new@example.test','email_confirmed_at':'2026-09-14'}}
        result=self.api.managed_login({'email':'new@example.test','password':'new horse battery'})
        self.assertEqual([],self.api.memberships(result['token'])['memberships'])


if __name__=='__main__':unittest.main()
