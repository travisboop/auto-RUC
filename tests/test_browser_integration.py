"""Opt-in real Chromium tests. Every request is fulfilled by an in-process fixture."""
import importlib.util
import json
import os
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from ruc_agent.contracts import Registry
from ruc_agent.engine import CampusSession

ENABLED = os.environ.get('RUC_TEST_BROWSER') == '1' and importlib.util.find_spec('playwright') is not None
ORIGIN = 'https://fixture.ruc.edu.cn'
HTML = '''<html><head><meta charset="utf-8"></head><body><h1>Fixture form</h1><input aria-label="标题" id="title">
<button id="read" onclick="fetch('/read',{method:'POST'}).then(r=>r.json()).then(x=>document.querySelector('#result').textContent=x.name)">读取</button>
<button id="save" onclick="fetch('/save',{method:'POST',body:document.querySelector('#title').value}).then(r=>r.json()).then(x=>document.querySelector('#result').textContent=x.receipt)">提交</button>
<button id="cancel" onclick="fetch('/cancel')">取消预约</button><div id="result"></div></body></html>'''


@unittest.skipUnless(ENABLED, 'set RUC_TEST_BROWSER=1 for fully intercepted Chromium test')
class BrowserIntegration(unittest.TestCase):
    def test_consumed_oauth_code_is_replayed_once_only_to_matching_browser_request(self):
        from copy import deepcopy
        registry = Registry()
        profile = deepcopy(registry.profiles['career'])
        profile['auth']['tokenBindings'] = [{'source':'response','path':['token'],'header':'Authorization'}]
        profile['auth']['browserState'] = [{'storage':'local','key':'role-choice','value':'1'}]
        profile['apiOrigin'] = 'https://api-fixture.ruc.edu.cn'
        registry.profiles['career'] = profile
        origin = profile['origin']
        client = SimpleNamespace(http=SimpleNamespace(jar=[]), landing_url=origin+'/stu/#/loading?code=fixture-code',
            auth_exchange={'origin':profile['apiOrigin'],'request':{'method':'POST','path':'/exchange/fixture-code','body':{}},
                           'response':{'token':'fixture-secret'}})
        sent = []
        with CampusSession(lambda:self.fail('Offline only'), registry) as session:
            browser = session.get_browser()
            browser.context.unroute('**/*')
            def fixture(route):
                class Proxy:
                    request = route.request
                    def abort(self, *args): route.abort(*args)
                    def fulfill(self, **kwargs): route.fulfill(**kwargs)
                    def continue_(self):
                        sent.append(self.request.url)
                        route.fulfill(status=200, content_type='text/html', body='''<p>fixture</p><script>
if(location.hash.includes('code=') && localStorage.getItem('role-choice')==='1')
fetch('https://api-fixture.ruc.edu.cn/exchange/fixture-code',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})
.then(r=>r.json()).then(x=>sessionStorage.setItem('token',x.token));</script>''')
                browser._route(Proxy())
            browser.context.route('**/*', fixture)
            with patch.object(session,'prepare_identity'), patch.object(session,'contract_client',return_value=client):
                result = browser.open(profile['entry'])
            page = browser.pages[result['page']]
            self.assertEqual(page.evaluate("sessionStorage.getItem('token')"), 'fixture-secret')
            self.assertFalse(page.evaluate("fetch('https://api-fixture.ruc.edu.cn/exchange/fixture-code',{method:'POST',body:'{}'}).then(()=>true,()=>false)"))
            self.assertFalse(any('/exchange/' in url for url in sent))
            self.assertNotIn('fixture-secret', json.dumps(browser.network()))
            self.assertNotIn('fixture-code', json.dumps(browser.network()))
            self.assertFalse(browser.network()['responseIds'])

    def test_new_business_cookie_and_callback_are_hydrated_without_saved_state(self):
        registry = Registry()
        profile = registry.profiles['career']
        origin, visited = profile['origin'], []
        landing = origin+'/stu/#/loading?token=fixture-current'
        cookie = SimpleNamespace(name='fresh',value='fixture-current',domain='career.ruc.edu.cn',path='/',
                                 expires=None,is_expired=lambda:False,has_nonstandard_attr=lambda _:False)
        client = SimpleNamespace(http=SimpleNamespace(jar=[cookie]),landing_url=landing)
        with CampusSession(lambda:self.fail('No real credentials'),registry) as session:
            browser = session.get_browser()
            browser.context.unroute('**/*')
            def fixture(route):
                visited.append(route.request.url)
                route.fulfill(status=200,content_type='text/html',body='''<script>
if(location.hash.startsWith('#/loading'))sessionStorage.setItem('current',new URLSearchParams(location.hash.split('?')[1]).get('token'));
</script><p>fixture</p>''')
            browser.context.route('**/*',fixture)
            with patch.object(session,'prepare_identity'), patch.object(session,'contract_client',return_value=client):
                snapshot=browser.open(profile['entry'])
            page=browser.pages[snapshot['page']]
            self.assertEqual(page.evaluate("sessionStorage.getItem('current')"),'fixture-current')
            self.assertEqual([c['value'] for c in browser.context.cookies() if c['name']=='fresh'],['fixture-current'])
            self.assertEqual(session.identity.credential_submissions,0)
            self.assertNotIn('fixture-current',json.dumps(snapshot))

    def test_real_dom_read_write_preview_receipt_and_no_duplicate_commit(self):
        registry = Registry()
        registry.register({'profiles': [{'id': 'fixture', 'origin': ORIGIN, 'entry': ORIGIN+'/',
                          'documentPaths': ['/'], 'readRoutes': [{'method': 'POST', 'path': '/read'}]}]})
        sent = []
        with CampusSession(lambda: self.fail('Offline fixture must not ask for credentials'), registry, True) as session:
            browser = session.get_browser()
            browser.context.unroute('**/*')

            def fixture(route):
                class Proxy:
                    request = route.request
                    def abort(self, *args):
                        route.abort(*args)
                    def fulfill(self, **kwargs):
                        route.fulfill(**kwargs)
                    def continue_(self):
                        url = self.request.url
                        sent.append((self.request.method, url))
                        if url == ORIGIN+'/':
                            route.fulfill(status=200, content_type='text/html', body=HTML)
                        elif url == ORIGIN+'/read':
                            route.fulfill(status=200, content_type='application/json', body=json.dumps({'name':'fresh-fixture','token':'must-redact'}))
                        elif url == ORIGIN+'/save':
                            route.fulfill(status=200, content_type='application/json', body='{"receipt":"saved-fixture"}')
                        else:
                            route.abort()
                browser._route(Proxy())
            browser.context.route('**/*', fixture)
            snapshot = browser.open(ORIGIN+'/')
            page = snapshot['page']

            def ref(name):
                return next(e['ref'] for f in snapshot['frames'] for e in f.get('elements',[]) if e['name']==name)

            snapshot = browser.interact(page, ref('读取'), 'click')
            browser.pages[page].wait_for_function("document.querySelector('#result').textContent==='fresh-fixture'")
            snapshot = browser.snapshot(page)
            network = browser.network()
            self.assertTrue(network['responseIds'])
            self.assertEqual(browser.network(network['responseIds'][-1])['data']['token'], '<redacted>')
            snapshot = browser.interact(page, ref('取消预约'), 'click')
            self.assertNotIn(('GET', ORIGIN+'/cancel'), sent)
            snapshot = browser.interact(page, ref('标题'), 'fill', 'fixture-title')
            snapshot = browser.interact(page, ref('提交'), 'click')
            self.assertNotIn(('POST', ORIGIN+'/save'), sent)
            preview = browser.interact(page, ref('提交'), 'click', write=True)
            self.assertFalse(preview['submitted'])
            result = session.commit_action(preview['actionId'])
            self.assertEqual(result['status'], 'verification_required')
            self.assertIn('saved-fixture', result['snapshot']['frames'][0]['text'])
            self.assertEqual(sent.count(('POST', ORIGIN+'/save')), 1)
            from ruc_agent.errors import QueryError
            with self.assertRaises(QueryError):
                session.commit_action(preview['actionId'])
            self.assertEqual(session.identity.credential_submissions, 0)


if __name__ == '__main__':
    unittest.main()
