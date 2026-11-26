from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from ruc_agent.ai import AiClient, ORIGIN as AI_ORIGIN
from ruc_agent.browser import BrowserPolicy
from ruc_agent.contracts import Registry
from ruc_agent.discovery import inventory
from ruc_agent.engine import CampusSession
from ruc_agent.errors import QueryError
from ruc_agent.http import redirect_url
from ruc_agent.legacy import LegacyIdentity, ORIGIN, oauth_consent
from ruc_agent.operations import form_bridge
from ruc_agent.pagination import collect_pages
from ruc_agent.parsing import embedded_json, hidden_form
from ruc_agent.source import inspect_source
from ruc_agent.tools import ToolRuntime
from ruc_agent.urls import application_url, diagnostic_url


class CatalogAndPaginationTests(unittest.TestCase):
    def test_entry_keeps_business_id_without_leaking_nested_auth(self):
        value = 'https://service.ruc.edu.cn/v2/matter/start?id=463&ticket=secret#/view?type=3&token=secret'
        actual = application_url(value)
        self.assertIn('id=463', actual)
        self.assertIn('type=3', actual)
        self.assertNotIn('secret', actual)
        nested = 'https://service.ruc.edu.cn/login?' + urlencode({'redirect': '/v2/item?id=3&token=secret'})
        clean = parse_qs(urlsplit(application_url(nested)).query)['redirect'][0]
        self.assertEqual(clean, 'https://service.ruc.edu.cn/v2/item?id=3')
        self.assertNotIn('secret', application_url('https://my.ruc.edu.cn/#access_token=secret&state=secret'))
        self.assertEqual(diagnostic_url(value), 'https://service.ruc.edu.cn/v2/matter/start#/view')
        self.assertEqual(application_url('https://my.ruc.edu.cn'), 'https://my.ruc.edu.cn/')
        for bad in ['javascript:alert(1)', 'https://u:p@my.ruc.edu.cn/', 'https://bad:port/', 'https://[bad/']:
            self.assertEqual(application_url(bad), '')

    def test_inventory_preserves_denied_and_unadapted_scope(self):
        result = {'asOf': 'fixture', 'source': 'fixture', 'totalMatches': 4, 'truncated': False, 'applications': [
            {'appId': 1, 'name': '表单', 'isAllow': True, 'appUrl': 'https://my.ruc.edu.cn/easyform3/api/page/static/2'},
            {'appId': 2, 'name': '门户', 'isAllow': False, 'appUrl': 'https://my.ruc.edu.cn/'},
            {'appId': 3, 'name': '未知', 'isAllow': True, 'appUrl': 'http://fixture.ruc.edu.cn/'},
            {'appId': 4, 'name': '无入口', 'isAllow': False}]}
        out = inventory(result, Registry())
        self.assertEqual(out['accessCounts'], {'allowed': 2, 'denied': 2})
        self.assertEqual(len(out['withoutEntry']), 1)
        by_id = {p['id']: p for p in out['platforms']}
        self.assertIn('forms.schema', by_id['forms']['readCapabilities'])
        self.assertEqual(by_id['http://fixture.ruc.edu.cn']['coverage'], 'entry_only')

    def test_callback_path_and_fragment_credentials_are_removed_from_diagnostics(self):
        from ruc_agent.redaction import redact
        for route in ['/abroad/WeChat_login/opaque-value','/cas/v1/tickets/opaque-value',
                      '/public/auth/cas/opaque-value','/pcLoginWrd/opaque-value']:
            self.assertNotIn('opaque-value',diagnostic_url('https://fixture.ruc.edu.cn'+route))
        self.assertEqual(diagnostic_url('https://fixture.ruc.edu.cn/#access_token=opaque-value'),
                         'https://fixture.ruc.edu.cn/')
        self.assertEqual(redact({'authkey':'opaque','sessionId':'opaque','csrf':'opaque','name':'fixture'}),
                         {'authkey':'<redacted>','sessionId':'<redacted>','csrf':'<redacted>','name':'fixture'})

    def test_inventory_matches_explicit_default_https_port(self):
        result = {'asOf':'fixture','source':'fixture','totalMatches':1,'truncated':False,
                  'applications':[{'appId':1,'name':'访客','isAllow':True,
                                   'appUrl':'https://guest.ruc.edu.cn:443/iportal/'}]}
        group = inventory(result,Registry())['platforms'][0]
        self.assertEqual(group['id'],'visitor')
        self.assertEqual(group['origin'],'https://guest.ruc.edu.cn')
        self.assertIn('visitor.records',group['readCapabilities'])

    def test_all_pages_is_bounded_and_detects_moving_or_repeated_records(self):
        spec = Registry().specs['resources.applications']
        seen = []
        def fetch(values):
            seen.append(values['page'])
            return {'code': 200, 'data': {'content': [{'id': values['page']}], 'totalElements': 2}}
        out = collect_pages(fetch, spec, {'page': 9, 'size': 1, 'keyword': ''}, 3)
        self.assertEqual(seen, [0, 1])
        self.assertTrue(out['complete'])
        self.assertEqual(out['pagesRead'], 2)
        limited = collect_pages(fetch, spec, {}, 1)
        self.assertEqual((limited['status'], limited['nextPage']), ('partial', 1))
        for invalid in [
            lambda v: {'code': 200, 'data': {'content': [{'id': 1}], 'totalElements': 2}},
            lambda v: {'code': 200, 'data': {'content': [{'id': v['page']}], 'totalElements': 2 + v['page']}},
            lambda v: {'code': 200, 'data': {'content': [], 'totalElements': 2}},
        ]:
            with self.assertRaises(QueryError):
                collect_pages(invalid, spec, {}, 3)

    def test_bad_pagination_and_unsupported_collection_fail_before_login(self):
        registry = Registry()
        bad = deepcopy(registry.specs['resources.applications'])
        bad['id'] = 'fixture.bad_page'
        bad['response']['pagination']['pageParameter'] = 'keyword'
        with self.assertRaises(QueryError):
            registry.register({'capabilities': [bad]})
        with ToolRuntime() as runtime, patch('ruc_agent.tools.read_credentials') as credentials:
            with self.assertRaises(QueryError) as error:
                runtime.call('ruc_query', {'capability': 'course.profile', 'all_pages': True})
            self.assertEqual(error.exception.code, 'PAGINATION_UNSUPPORTED')
            credentials.assert_not_called()


class BrowserReadContractTests(unittest.TestCase):
    def test_runtime_registration_refreshes_public_authentication_policy(self):
        with ToolRuntime() as runtime:
            policy = BrowserPolicy(runtime.registry)
            runtime.session = SimpleNamespace(browser=SimpleNamespace(policy=policy), close=lambda: None)
            origin = 'https://fixture-public.ruc.edu.cn'
            runtime.call('ruc_register_contract', {'document': {'profiles': [{
                'id': 'fixture_public', 'origin': origin, 'entry': origin+'/',
                'auth': {'kind': 'public'}}]}})
            with self.assertRaises(QueryError) as error:
                policy.require_authentication(origin+'/callback')
            self.assertEqual(error.exception.code, 'AUTHENTICATION_DISABLED')
            # A later reviewed private profile on that origin must also refresh
            # the derived set rather than retain the old anonymous-only flag.
            private = deepcopy(runtime.registry.profiles['media_account'])
            private.update(id='fixture_private', origin=origin, entry=origin+'/user.html')
            runtime.call('ruc_register_contract', {'document': {'profiles': [private]}})
            policy.require_authentication(origin+'/callback')

    def test_public_pages_do_not_select_private_login_or_issue_cas_tickets(self):
        registry = Registry()
        self.assertEqual(registry.profile_for_url('https://mediaview.ruc.edu.cn/')['id'], 'media_works')
        self.assertEqual(registry.profile_for_url('https://mediaview.ruc.edu.cn/project.html')['id'], 'media_account')
        policy = BrowserPolicy(registry)
        with self.assertRaises(QueryError) as error:
            policy.require_authentication('https://cssd.ruc.edu.cn/callback')
        self.assertEqual(error.exception.code, 'AUTHENTICATION_DISABLED')
        policy.require_authentication('https://mediaview.ruc.edu.cn/')

    def test_full_request_not_only_method_and_path_controls_mixed_endpoint(self):
        registry = Registry()
        spec = deepcopy(registry.specs['booths.usage_types'])
        spec.update(id='fixture.mixed', platform='digital_portal', parameters={
            'type': 'object', 'properties': {'id': {'type': 'integer', 'minimum': 1}},
            'required': ['id'], 'additionalProperties': False})
        spec['request'] = {'method': 'POST', 'path': '/fixture/rpc', 'body': {'action': 'list', 'id': {'$param': 'id'}}}
        get = deepcopy(spec)
        get['id'] = 'fixture.mixed_get'
        get['request'] = {'method': 'GET', 'path': '/fixture/{id}', 'query': {'action': 'list'}}
        registry.register({'capabilities': [spec, get]})
        policy = BrowserPolicy(registry)
        root = 'https://my.ruc.edu.cn'
        self.assertTrue(policy.allowed(root+'/fixture/rpc', 'POST', 'xhr', post_data='{"action":"list","id":2}'))
        for body in ['{"action":"save","id":2}', '{"action":"list","id":2,"delete":true}',
                     '{"action":"list","id":0}', '{"action":"list","id":"2"}']:
            self.assertFalse(policy.allowed(root+'/fixture/rpc', 'POST', 'xhr', post_data=body))
        self.assertTrue(policy.allowed(root+'/fixture/2?action=list', 'GET', 'xhr'))
        for url in ['/fixture/2?action=delete', '/fixture/0?action=list', '/fixture/2?action=list&action=delete']:
            self.assertFalse(policy.allowed(root+url, 'GET', 'xhr'))

    def test_discovered_document_does_not_allow_query_mutation(self):
        policy = BrowserPolicy(Registry())
        entry = 'https://fixture.ruc.edu.cn/index?id=3'
        policy.add_entry(entry, [entry])
        self.assertTrue(policy.allowed(entry, 'GET', 'document'))
        self.assertFalse(policy.allowed(entry+'&action=delete', 'GET', 'document'))
        policy.add_entry('https://fixture.ruc.edu.cn', ['https://fixture.ruc.edu.cn'])
        self.assertTrue(policy.allowed('https://fixture.ruc.edu.cn/', 'GET', 'document'))


class ParsingAndActionTests(unittest.TestCase):
    def test_embedded_form_json_is_not_evaluated(self):
        data = embedded_json(b'<script>var PAGE_JSON={"id":3,"pageDefine":{"futureField":true}};</script>', 'PAGE_JSON')
        self.assertTrue(data['pageDefine']['futureField'])
        for source in [b'<div>var PAGE_JSON={};</div>', b'<script>var PAGE_JSON=run();</script>',
                       b'<script>var PAGE_JSON={};var PAGE_JSON={};</script>']:
            with self.assertRaises(QueryError):
                embedded_json(source, 'PAGE_JSON')

    def test_source_inspection_accepts_current_vue_and_unquoted_assets(self):
        with patch('ruc_agent.source.send', return_value=(200, {}, b'<script src=/assets/current.js></script>')):
            out = inspect_source('https://course.ruc.edu.cn/a.vue')
        self.assertEqual(out['scripts'], ['https://course.ruc.edu.cn/assets/current.js'])

    def test_authentication_bridge_cannot_submit_business_form(self):
        target = 'https://course.ruc.edu.cn/onLineClass/rdmooc/simulationLoginOne'
        html = ('<form method=post action="'+target+'"><input type=hidden name=usercode value=fixture></form>').encode()
        self.assertEqual(hidden_form(html, target, ['usercode'], target), {'usercode': 'fixture'})
        for bad in [html.replace(b'simulationLoginOne', b'createReservation'), html.replace(b'type=hidden', b'type=text'),
                    html.replace(b'</form>', b'<input type=hidden name=amount value=1></form>')]:
            with self.assertRaises(QueryError):
                hidden_form(bad, target, ['usercode'], target)
        http = SimpleNamespace(origin='https://course.ruc.edu.cn', opener=object())
        sent = []
        http.request_bytes = lambda *a, **k: (sent.append((a, k)) or (302, {'Location': '/home'}, b''))
        with patch('ruc_agent.http.send', return_value=(200, {}, b'fixture home')):
            url, body = form_bridge(http, html, {'path': '/onLineClass/rdmooc/simulationLoginOne', 'fields': ['usercode']})
        self.assertEqual(url, http.origin+'/home')
        self.assertEqual(sent[0][1]['body'], {'usercode': 'fixture'})

    def test_form_actions_keep_distinct_workflow_and_plain_payloads_and_send_once(self):
        sent = []
        http = SimpleNamespace(request_json=lambda *a: (sent.append(a) or {'code': 'success', 'result': {'data': {}}}))
        with CampusSession(lambda: self.fail('Offline only'), allow_writes=True) as session:
            with patch.object(session, 'prepare_identity'), patch.object(session, 'contract_client', return_value=SimpleNamespace(http=http)):
                for capability, params, expected in [
                    ('forms.submit', {'actId': 3, 'submission': {'bizData': {'newField': 'fixture'}, 'nextFlow': [{'id': 'fixture-flow'}]}},
                     {'bizData': {'newField': 'fixture'}, 'nextFlow': [{'id': 'fixture-flow'}]}),
                    ('forms.create', {'actId': 4, 'bizData': {'newField': 'fixture'}}, {'newField': 'fixture'})
                ]:
                    plan = session.prepare_action(capability, params)
                    params.clear()
                    out = session.commit_action(plan['actionId'])
                    self.assertEqual(out['outcome'], 'accepted')
                    self.assertEqual(sent[-1][3], expected)
                    with self.assertRaises(QueryError):
                        session.commit_action(plan['actionId'])
        self.assertEqual(len(sent), 2)


class AuthenticationExpansionTests(unittest.TestCase):
    def test_ai_code_is_exchanged_into_only_its_own_bearer_session(self):
        class HTTP:
            def __init__(self, origin): self.origin, self.calls = origin, []
            def json(self, path, body):
                self.calls.append((path, body))
                return {'email': 'fixture@invalid'} if path.endswith('/profile') else {
                    'result': 'success', 'data': {'access_token': 'fixture-private-bearer'}}
            def request_json(self, path, **kwargs):
                self.calls.append((path, kwargs))
                return {'id': 'fixture'}
            def close(self): pass
        with patch('ruc_agent.ai.MemoryHTTP', HTTP), patch('ruc_agent.ai.cas_login', return_value=AI_ORIGIN+'/home?code=fixture-code&page=home'):
            client = AiClient(object())
        self.assertEqual(client.http.origin, AI_ORIGIN)
        self.assertEqual(client.http.calls[0][1], {'code': 'fixture-code'})
        self.assertEqual(client.http.calls[-1][1]['headers']['Authorization'], 'Bearer fixture-private-bearer')
        client.close()
        self.assertEqual(client.headers, {})

    def test_legacy_captcha_precedes_password_and_identity_is_reused(self):
        calls = []
        html = b'<input type=hidden name=csrftoken value=fixture-csrf>'
        def send(opener, request):
            url = request if isinstance(request, str) else request.full_url
            calls.append(url)
            if url.endswith('/auth/login?school_code=ruc'): return 200, {}, html
            if url.endswith('/auth/captcha'): return 200, {}, json.dumps({'check': 'yes', 'id': 'fixture-captcha', 'b64s': 'data:image/png;base64,Zml4dHVyZQ=='}).encode()
            if url.endswith('/login/Index/check'): return 200, {}, b'{"code":0}'
            if url.endswith('/auth/login'):
                self.assertEqual(request.get_header('Content-type'), 'application/x-www-form-urlencoded')
                self.assertEqual(json.loads(request.data)['username'], 'ruc:fixture-student')
                return 200, {}, b'{"redirect_uri":"/me"}'
            if url.endswith('/me'): return 200, {}, b'home'
            self.fail('Unexpected fixture request')
        with patch('ruc_agent.legacy.send', send), patch('ruc_agent.http.send', send):
            credentials = unittest.mock.Mock(return_value=('fixture-student', 'fixture-password'))
            identity = LegacyIdentity(credentials)
            with self.assertRaises(QueryError) as challenge:
                identity.prepare()
            self.assertEqual(challenge.exception.code, 'CAPTCHA_REQUIRED')
            credentials.assert_not_called()
            identity.prepare('abcd')
            identity.prepare()
            self.assertEqual(identity.credential_submissions, 1)
            self.assertEqual(calls.count(ORIGIN+'/auth/login'), 1)
            identity.close()

    def test_legacy_password_rejection_is_sticky(self):
        def send(opener, request):
            url = request if isinstance(request, str) else request.full_url
            if '?school_code=' in url: return 200, {}, b'<input type=hidden name=csrftoken value=fixture>'
            if url.endswith('/auth/captcha'): return 200, {}, b'{"check":"no"}'
            return 403, {}, b'{}'
        with patch('ruc_agent.legacy.send', send), patch('ruc_agent.http.send', send):
            identity = LegacyIdentity(lambda: ('fixture-student', 'fixture-password'))
            for _ in range(2):
                with self.assertRaises(QueryError) as error:
                    identity.prepare()
                self.assertEqual(error.exception.code, 'PASSWORD_AUTH_REJECTED')
            self.assertEqual(identity.credential_submissions, 1)
            identity.close()

    def test_oauth_consent_is_restricted_to_registered_identity_callback(self):
        callback = 'https://course.ruc.edu.cn/callback'
        fields = {'client_id': 'fixture', 'response_type': 'code', 'redirect_uri': callback, 'scope': 'profile'}
        target = ORIGIN+'/oauth2/authorize?'+urlencode(fields)
        html = ('<form method=post>'+''.join('<input type=hidden name="'+k+'" value="'+v+'">' for k, v in fields.items())+
                '<button name=allow value=true>允许</button></form>').encode()
        self.assertEqual(oauth_consent(html, target, callback)['allow'], 'true')
        with self.assertRaises(QueryError):
            oauth_consent(html.replace(b'value="profile"', b'value="profile write"'), target, callback)
        with self.assertRaises(QueryError):
            oauth_consent(html, target, 'https://other.ruc.edu.cn/callback')
        self.assertIn('userinfo%20profile', redirect_url(ORIGIN, '/oauth2/authorize?scope=userinfo profile'))
        with self.assertRaises(QueryError):
            redirect_url(ORIGIN, '/a\nLocation:evil')


if __name__ == '__main__':
    unittest.main()
