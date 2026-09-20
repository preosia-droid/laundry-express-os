import copy
import datetime as dt
import io
import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from wsgiref.simple_server import make_server, WSGIRequestHandler

from server import Backend, ApiError, empty, packed


class QuietHandler(WSGIRequestHandler):
    def log_message(self,*args): pass


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.api=Backend(Path(self.temp.name)/'test.db')
        self.device=self.api.provision('a@example.test','correct horse battery','A','main','pos','UTC')
        self.other=self.api.provision('b@example.test','other horse battery','B','main','pos','UTC')
        self.owner=self.api.login('a@example.test','correct horse battery')['token']
        self.day=dt.datetime.now(dt.timezone.utc).date()
        self.stamp=str(self.day)+'T10:00:00+00:00'
        self.seq=0

    def tearDown(self): self.temp.cleanup()

    def order(self,id='o1',amount=50000,paid=0,stamp=None,voided=False,removed=False):
        self.seq+=1
        return {'revision':self.seq,'kind':'order','id':id,'payload':{'orderNumber':1,'createdAt':stamp or self.stamp,'orderTotal':amount,'grossTotal':50000,'amountPaid':paid,'outstandingBalance':0 if voided else max(0,amount-paid),'paymentStatus':'VOIDED' if voided else 'PAID' if paid>=amount else 'PARTIAL' if paid else 'UNPAID','voided':voided,'totalClothingPieces':12,'removed':removed}}

    def payment(self,id='p1',amount=50000,method='CASH',description='',stamp=None):
        self.seq+=1
        return {'revision':self.seq,'kind':'payment','id':id,'payload':{'orderId':'o1','amount':amount,'paymentMethod':method,'description':description,'paymentDateTime':stamp or self.stamp,'removed':False}}

    def stock(self,alert='LOW',quantity=1.5):
        self.seq+=1
        return {'revision':self.seq,'kind':'inventory','id':'detergent','payload':{'itemName':'Detergent','currentQuantity':quantity,'unit':'kg','reorderLevel':2,'alertType':alert,'lastUpdatedAt':self.stamp}}

    def body(self,events,b='A',d='pos',zone='UTC'):
        return {'businessId':b,'branchId':'main','deviceId':d,'timezone':zone,'events':events}

    def send(self,*events): return self.api.ingest(self.device,self.body(list(events)))
    def report(self,period='TODAY'):return self.api.report(self.owner,'A','main',period)

    def test_online_order_and_payment_other_exact_description(self):
        self.send(self.order(paid=50000),self.payment(method='OTHER',description='Bank Transfer | QR § exact'))
        v=self.report()['current']
        self.assertEqual((50000,50000,0,1),(v['netSales'],v['amountCollected'],v['outstandingBalance'],v['orderCount']))
        self.assertEqual(50000,v['paymentTotals']['Bank Transfer | QR § exact'])

    def test_negative_stock_does_not_block_sales_or_duplicate_retry(self):
        events=[self.order(paid=50000),self.payment(),self.stock('OUT',-0.25)]
        for _ in range(2): self.send(*events)
        current=self.report()['current']
        self.assertEqual((50000,50000,1),(current['netSales'],current['amountCollected'],current['orderCount']))
        alerts=self.api.attention(self.owner,'A','main')['alerts']
        self.assertEqual(1,len(alerts))
        self.assertEqual(-0.25,alerts[0]['currentQuantity'])
        self.assertEqual('OUT',alerts[0]['alertType'])

    def test_stock_quantity_bounds_still_reject_invalid_input(self):
        for quantity in (float('nan'),float('inf'),-float('inf'),-10**12-1,10**12+1,True):
            with self.assertRaises(ApiError): self.send(self.stock('OUT',quantity))
        event=self.stock();event['payload']['reorderLevel']=-1
        with self.assertRaises(ApiError): self.send(event)

    def test_stage_and_private_fields_rejected(self):
        e=self.order();e['payload']['laundryStage']='WASHING'
        with self.assertRaises(ApiError):self.send(e)
        e=self.order();e['payload']['customerName']='Private'
        with self.assertRaises(ApiError):self.send(e)
        self.assertEqual(0,self.report()['current']['orderCount'])

    def test_retry_is_idempotent_after_restart(self):
        events=[self.order(paid=50000),self.payment(),self.stock()]
        self.send(*events)
        self.api=Backend(self.api.path)
        for _ in range(3):self.send(*events)
        self.assertEqual(50000,self.report()['current']['amountCollected'])
        self.assertEqual(2,len(self.api.activity(self.owner,'A','main')['items']))
        self.assertEqual(1,len(self.api.attention(self.owner,'A','main')['alerts']))

    def test_refund_void_and_total_correction(self):
        self.send(self.order(paid=50000),self.payment())
        self.send(self.order(amount=45000,paid=45000),self.payment('refund',-5000))
        v=self.report()['current'];self.assertEqual((45000,45000,5000),(v['netSales'],v['amountCollected'],v['refundTotal']))
        self.send(self.order(amount=45000,paid=45000,voided=True))
        v=self.report()['current'];self.assertEqual((0,0,45000,45000),(v['netSales'],v['orderCount'],v['voidedTotal'],v['amountCollected']))
        self.send(self.payment('refund-after-void',-45000))
        self.assertEqual(0,self.report()['current']['amountCollected'])

    def test_corrected_payment_replaces_original_contribution(self):
        self.send(self.payment())
        corrected=self.payment(amount=30000,method='OTHER',description='Bank Transfer')
        self.send(corrected);self.send(corrected)
        v=self.report()['current'];self.assertEqual(30000,v['amountCollected']);self.assertEqual({'Bank Transfer':30000},v['paymentTotals'])

    def test_out_of_order_revision_cannot_restore_old_value(self):
        old=self.order();new=self.order(amount=40000)
        self.send(new);self.send(old)
        self.assertEqual(40000,self.report()['current']['netSales'])

    def test_revision_collision_rejected_and_batch_rolls_back(self):
        old=self.order();self.send(old)
        bad=copy.deepcopy(old);bad['payload']['orderTotal']=100;bad['payload']['outstandingBalance']=100
        with self.assertRaises(ApiError):self.send(self.payment(),bad)
        self.assertEqual(0,self.report()['current']['amountCollected'])

    def test_low_critical_out_and_restock_no_duplicate(self):
        for alert,q in [('LOW',1.5),('CRITICAL',0.3),('OUT',0),('RESOLVED',8)]:
            event=self.stock(alert,q);self.send(event);self.send(event)
            result=self.api.attention(self.owner,'A','main')
            self.assertEqual(0 if alert=='RESOLVED' else 1,len(result['alerts']))
            if alert!='RESOLVED':self.assertEqual({alert:1},result['counts'])

    def test_presence_and_sync_uses_server_clock(self):
        self.send()
        result=self.api.attention(self.owner,'A','main')['devices'][0]
        self.assertTrue(result['online']);self.assertIsNotNone(result['synced'])
        old=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(minutes=21)).isoformat()
        with self.api.db() as db:db.execute('UPDATE devices SET seen=? WHERE business=?',(old,'A'))
        result=self.api.attention(self.owner,'A','main')['devices'][0]
        self.assertFalse(result['online']);self.assertIsNotNone(result['synced'])

    def test_all_owner_reads_and_device_writes_deny_other_business(self):
        for action in [lambda:self.api.report(self.owner,'B','main','TODAY'),lambda:self.api.attention(self.owner,'B','main'),lambda:self.api.activity(self.owner,'B','main'),lambda:self.api.ingest(self.device,self.body([self.order()],b='B'))]:
            with self.assertRaises(ApiError) as error:action()
            self.assertEqual(403,error.exception.status)
        with self.assertRaises(ApiError):self.api.ingest(self.owner,self.body([]))
        with self.assertRaises(ApiError):self.api.report(self.device,'A','main','TODAY')

    def test_branch_role_and_revocation_enforced(self):
        with self.assertRaises(ApiError):self.api.report(self.owner,'A','other','TODAY')
        with self.api.db() as db:db.execute("UPDATE members SET role='STAFF' WHERE business='A'")
        with self.assertRaises(ApiError):self.report()
        with self.api.db() as db:db.execute("UPDATE members SET role='OWNER' WHERE business='A'");db.execute("UPDATE businesses SET capabilities='[]' WHERE id='A'")
        with self.assertRaises(ApiError):self.report()

    def test_secure_login_expiry_and_rate_limit(self):
        with self.assertRaises(ApiError):self.api.login('a@example.test','1234')
        with self.api.db() as db:db.execute('UPDATE sessions SET expires=0')
        with self.assertRaises(ApiError):self.report()
        for _ in range(9):
            with self.assertRaises(ApiError):self.api.login('a@example.test','wrong')
        with self.assertRaises(ApiError) as error:self.api.login('a@example.test','correct horse battery')
        self.assertEqual(429,error.exception.status)

    def test_branch_timezone_controls_day_not_owner_phone(self):
        with self.api.db() as db:db.execute("UPDATE branches SET zone='Asia/Manila' WHERE business='A'")
        e=self.order(stamp=str(self.day-dt.timedelta(days=1))+'T20:00:00+00:00')
        self.api.ingest(self.device,self.body([e],zone='Asia/Manila'))
        with self.api.db() as db:day=db.execute("SELECT day FROM records WHERE business='A'").fetchone()[0]
        self.assertEqual(str(self.day),day)

    def test_daily_reconciliation_repairs_aggregate_drift(self):
        yesterday=str(self.day-dt.timedelta(days=1))+'T10:00:00+00:00'
        self.send(self.order(stamp=yesterday),self.payment(stamp=yesterday))
        with self.api.db() as db:
            row=db.execute("SELECT payload FROM summaries WHERE business='A' AND period='day'").fetchone()
            wrong=json.loads(row[0]);wrong['netSales']=100
            db.execute("UPDATE summaries SET payload=?,reconciled=NULL WHERE business='A'",(packed(wrong),))
        self.send()
        with self.api.db() as db:
            rows=db.execute("SELECT payload,reconciled,period FROM summaries WHERE business='A'").fetchall()
        for row in rows:
            self.assertEqual(50000,json.loads(row['payload'])['netSales'])
            if row['period']=='day':self.assertIsNotNone(row['reconciled'])

    def test_week_month_year_and_large_history_use_summaries(self):
        # 4,000 real financial events across ~11 years. No order rows are read by report().
        events=[]
        for n in range(4000):
            stamp=str(self.day-dt.timedelta(days=n))+'T10:00:00+00:00'
            events.append(self.order(id=f'o{n}',amount=100,stamp=stamp))
        for offset in range(0,len(events),100):self.send(*events[offset:offset+100])
        original_db=self.api.db
        queries=[]
        @contextmanager
        def traced():
            with original_db() as db:
                db.set_trace_callback(queries.append)
                yield db
        self.api.db=traced
        for period in ('TODAY','WEEK','MONTH','YEAR'):
            result=self.report(period)
            days=(dt.date.fromisoformat(result['to'])-dt.date.fromisoformat(result['from'])).days+1
            self.assertEqual(days*100,result['current']['netSales'])
            self.assertEqual(days,result['current']['orderCount'])
            self.assertLessEqual(result['summaryRowsRead'],84)
        self.assertFalse(any('FROM records' in sql or 'FROM events' in sql for sql in queries))
        page=self.api.activity(self.owner,'A','main');self.assertEqual(20,len(page['items']))
        second=self.api.activity(self.owner,'A','main',page['nextCursor'])
        self.assertTrue(set(x['seq'] for x in page['items']).isdisjoint(x['seq'] for x in second['items']))

    def test_concurrent_identical_retries_apply_once(self):
        event=self.order()
        with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(lambda _:self.send(event),range(8)))
        self.assertEqual(1,self.report()['current']['orderCount'])

    def test_invalid_money_and_timestamp_rejected(self):
        for key,value in [('orderTotal',float('nan')),('orderTotal',-5),('amountPaid',True),('createdAt','2026-09-13 10:00')]:
            event=self.order();event['payload'][key]=value
            with self.assertRaises(ApiError):self.send(event)

    def test_real_http_auth_sync_and_forbidden_route(self):
        server=make_server('127.0.0.1',0,self.api,handler_class=QuietHandler)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        try:
            def call(path,data,token=''):
                request=urllib.request.Request(f'http://127.0.0.1:{server.server_port}{path}',packed(data).encode(),{'Content-Type':'application/json','Authorization':'Bearer '+token})
                with urllib.request.urlopen(request) as response:return json.load(response)
            owner=call('/login',{'email':'a@example.test','password':'correct horse battery'})['token']
            call('/sync',self.body([self.order()]),self.device)
            self.assertEqual(50000,call('/report',{'businessId':'A','branchId':'main'},owner)['current']['netSales'])
            for path in ('/report','/attention','/activity'):
                with self.assertRaises(urllib.error.HTTPError) as error:call(path,{'businessId':'B','branchId':'main'},owner)
                self.assertEqual(403,error.exception.code)
        finally:server.shutdown();worker.join();server.server_close()


if __name__=='__main__': unittest.main(verbosity=2)
