"""Authentication edge cases observed on the current school applications."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from ruc_agent import auth
from ruc_agent.browser import BrowserPolicy
from ruc_agent.contracts import Registry
from ruc_agent.errors import QueryError
from ruc_agent.engine import CampusSession
from ruc_agent.operations import ContractClient, token_headers, bootstrap_session
from ruc_agent.pagination import collect_pages
from ruc_agent.sports import sports_data
from ruc_agent.http import check_url, canonical_origin
from ruc_agent.urls import application_url


class ProfileAuthenticationTests(unittest.TestCase):
    def test_pagination_uses_explicit_has_next_without_inventing_total(self):
        spec = Registry().specs['virtual_labs.projects']
        def fetch(values):
            return {'articles':[{'id':values['page']}],'hasNext':values['page'] < 2}
        result = collect_pages(fetch,spec,{},3)
        self.assertTrue(result['complete'])
        self.assertEqual(result['returnedItems'],2)
        self.assertIsNone(result['totalItems'])
        self.assertEqual(collect_pages(fetch,spec,{},1)['nextPage'],2)
        from ruc_agent.contracts import decode_response
        last_page = decode_response(spec,{'articles':[{'id':2}],'hasNext':False})
        self.assertIsNone(last_page['complete'])
        self.assertFalse(last_page['hasNext'])
        for bad in [{'articles':[],'hasNext':True},{'articles':[],'hasNext':'false'}]:
            with self.assertRaises(QueryError): collect_pages(lambda v:bad,spec,{},3)

    def test_component_sso_follows_only_declared_same_origin_get_paths(self):
        profile = deepcopy(Registry().profiles['visitor'])
        http = SimpleNamespace(origin=profile['origin'], opener=object())
        bootstrap = profile['auth']['bootstrap']
        with patch('ruc_agent.http.send', side_effect=[
                (302, {'Location':'/iportal/cas/login?service=fixture'}, b''),
                (302, {'Location':'/visitor/app/reserveNew?ticket=fixture'}, b''),
                (200, {}, b'fixture')]) as send:
            self.assertTrue(bootstrap_session(http,bootstrap).endswith('ticket=fixture'))
            self.assertEqual(send.call_count,3)
            self.assertTrue(all(len(c.args)==2 for c in send.call_args_list))
        for target in ['https://foreign.invalid/login','/visitor/order/cancel']:
            with patch('ruc_agent.http.send', return_value=(302,{'Location':target},b'')) as send:
                with self.assertRaises(QueryError): bootstrap_session(http,bootstrap)
                self.assertEqual(send.call_count,1)
        profile['id']='fixture.bootstrap'
        profile['auth']['bootstrap']['redirectPaths']=['//foreign.invalid/login']
        with self.assertRaises(QueryError): Registry().register({'profiles':[profile]})

    def test_auth_entry_discovery_checks_status_and_origin_before_ticket(self):
        import json
        profile = deepcopy(Registry().profiles['computing_cloud'])
        identity = SimpleNamespace(ticket=Mock(side_effect=AssertionError('No ticket for untrusted metadata')))
        http = SimpleNamespace(origin=profile['origin'], close=Mock(), request_bytes=Mock())
        for status, value in [(200, {'ssoUrl':auth.CAS+'/cas/login'}),
                              (401, {'ssoUrl':'https://foreign.invalid/login'}), (401, {'ssoUrl':42})]:
            http.request_bytes.return_value = (status, {}, json.dumps(value).encode())
            with patch('ruc_agent.operations.MemoryHTTP',return_value=http), self.assertRaises(QueryError):
                ContractClient(identity,profile)
        identity.ticket.assert_not_called()

    def test_frontend_and_api_use_separate_jars_and_one_code_exchange(self):
        profile = deepcopy(Registry().profiles['museum'])
        profile['id'] = 'fixture.split'
        profile['apiOrigin'] = 'https://api-fixture.ruc.edu.cn'
        Registry().register({'profiles':[profile]})
        front = SimpleNamespace(origin=profile['origin'], jar=['frontend-cookie'], close=Mock())
        api = SimpleNamespace(origin=profile['apiOrigin'], jar=[], close=Mock(), request_json=Mock(side_effect=[
            {'code':200,'status':True,'data':{'token':'fixture-token'}},
            {'code':200,'status':True,'data':{'user':'fixture'}}]))
        final = profile['origin']+'/#/login?code=fixture-code'
        with patch('ruc_agent.operations.MemoryHTTP',side_effect=lambda origin:front if origin==front.origin else api), \
             patch('ruc_agent.operations.cas_login',return_value=final) as login:
            client = ContractClient(object(),profile)
        self.assertIs(login.call_args.args[0],front)
        self.assertEqual(api.jar,[])
        self.assertEqual(api.request_json.call_args.args[4]['Authorization'],'Bearer fixture-token')
        self.assertEqual(client.auth_exchange['origin'],api.origin)
        client.close()
        front.close.assert_called_once()
        api.close.assert_called_once()

    def test_discovered_login_entry_uses_the_declared_query(self):
        import json
        profile = deepcopy(Registry().profiles['media_account'])
        http = SimpleNamespace(origin=profile['origin'], jar=[], close=Mock(),
            request_bytes=Mock(return_value=(200, {}, json.dumps({'data': {'auth_url':
                auth.CAS + '/cas/oauth2.0/authorize?redirect_uri=https%3A%2F%2Fmediaview.ruc.edu.cn%2F'}}).encode())),
            request_json=Mock(side_effect=[{'code':0,'data':{'token':'fixture-token'}},
                                          {'code':0,'data':{'ID':42}}]))
        with patch('ruc_agent.operations.MemoryHTTP', return_value=http), \
             patch('ruc_agent.operations.cas_login', return_value=profile['origin']+'/?code=fixture-code'):
            client = ContractClient(object(), profile)
        http.request_bytes.assert_called_once_with('/studentUser/weirda/auth-url', query={'type':2})
        self.assertEqual(client.headers, {'x-token':'fixture-token'})
        client.close()
        profile['id']='fixture.invalid_discovery'
        profile['auth']['entryDiscovery']['query']={'type':{'$param':'caller'}}
        with self.assertRaises(QueryError): Registry().register({'profiles':[profile]})

    def test_offset_pagination_uses_page_size_and_reports_resume_offset(self):
        spec = Registry().specs['datasets.search']
        offsets = []
        def fetch(values):
            offsets.append(values['start'])
            return {'status':'OK','data':{'items':[{'id':n} for n in range(values['start'], min(5,values['start']+2))], 'total_count':5}}
        result = collect_pages(fetch,spec,{'per_page':2},2)
        self.assertEqual(offsets,[0,2])
        self.assertFalse(result['complete'])
        self.assertEqual(result['nextPage'],4)
        self.assertEqual(result['pageParameter'],'start')
        offsets.clear()
        self.assertTrue(collect_pages(fetch,spec,{'per_page':2},3)['complete'])
        self.assertEqual(offsets,[0,2,4])
        with self.assertRaises(QueryError): collect_pages(fetch,spec,{'per_page':0},3)

    def test_public_platform_does_not_read_credentials_or_follow_failed_auth(self):
        with CampusSession(lambda: self.fail('Public data needs no credentials')) as session:
            with patch('ruc_agent.http.MemoryHTTP.request_json', return_value={'status': 'OK', 'data': []}):
                result = session.query('datasets.featured')
            self.assertEqual(result['data'], [])
            self.assertEqual(session.identity.credential_submissions, 0)
            self.assertIsNone(session._legacy_identity)
            with patch('ruc_agent.operations.execute', side_effect=[QueryError('LOGIN_REQUIRED', 'fixture'),
                       {'status': 'ok', 'data': []}]):
                batch = session.batch([{'capability': 'datasets.featured'}, {'capability': 'datasets.featured'}])
            self.assertEqual([r['status'] for r in batch['results']], ['error', 'ok'])
        profile = deepcopy(Registry().profiles['social_science_data'])
        profile['id'] = 'fixture.public'
        profile['auth']['service'] = 'https://cssd.ruc.edu.cn/callback'
        with self.assertRaises(QueryError): Registry().register({'profiles': [profile]})

    def test_observed_http_oauth_callback_is_only_contacted_over_https(self):
        origin = 'https://fixture.ruc.edu.cn'
        entry = auth.CAS + '/cas/oauth2.0/authorize?' + urlencode({'redirect_uri':'http://fixture.ruc.edu.cn/auth'})
        http = SimpleNamespace(origin=origin, opener=object())
        calls = []
        def send(opener, url):
            calls.append(url)
            self.assertTrue(url.startswith('https://'))
            if url == entry:
                return 302, {'Location':auth.CAS+'/cas/login?'+urlencode({'service':auth.OAUTH_SERVICE})}, b''
            if url.startswith(auth.OAUTH_SERVICE):
                return 302, {'Location':'http://fixture.ruc.edu.cn/auth?code=fixture'}, b''
            self.assertEqual(url, origin+'/auth?code=fixture')
            return 200, {}, b'fixture'
        identity = SimpleNamespace(ticket=Mock(return_value='fixture-ticket'))
        with patch.object(auth,'send',send):
            final = auth.cas_login(http,identity,entry,auth.OAUTH_SERVICE,upgrade_http_paths=['/auth'])
            self.assertEqual(final,origin+'/auth?code=fixture')
            with self.assertRaises(QueryError): auth.cas_login(http,identity,entry,auth.OAUTH_SERVICE)
        self.assertEqual(calls[0], entry)

    def test_oauth_code_exchange_is_bound_checked_and_cleared(self):
        profile = deepcopy(Registry().profiles['career'])
        profile['id'] = 'fixture.exchange'
        profile['auth']['exchange'] = {
            'parameters': {'code': {'source': 'fragment_query', 'parameter': 'code'}},
            'request': {'method': 'GET', 'path': '/exchange',
                        'query': {'code': {'$param': 'code'}, 'state': 'web'}},
            'response': {'checks': [{'path': ['ok'], 'equals': True}]}}
        profile['auth']['tokenBindings'] = [{'source': 'response', 'path': ['token'],
                                            'header': 'Authorization', 'prefix': 'Bearer '}]
        Registry().register({'profiles': [profile]})
        final = profile['origin'] + '/#/login?code=fixture-code'
        legacy = SimpleNamespace(login=lambda *args: (final, b''))
        http = SimpleNamespace(origin=profile['origin'], close=Mock(), request_json=Mock(
            side_effect=[{'ok': True, 'token': 'fixture-token'}, {'code': 0, 'data': {'role': 'school_user'}}]))
        with patch('ruc_agent.operations.MemoryHTTP', return_value=http):
            client = ContractClient(object(), profile, legacy)
        self.assertEqual(http.request_json.call_args_list[0].args[2], {'code': 'fixture-code', 'state': 'web'})
        self.assertEqual(client.headers['Authorization'], 'Bearer fixture-token')
        self.assertEqual(client.auth_exchange['request']['query']['code'], 'fixture-code')
        client.close()
        self.assertIsNone(client.auth_exchange)
        for final in [profile['origin'] + '/#/login', profile['origin'] + '/#/login?code=a&code=b',
                      profile['origin'] + '/#/login?code=&code=b']:
            http.request_json.reset_mock()
            with patch('ruc_agent.operations.MemoryHTTP', return_value=http), self.assertRaises(QueryError):
                ContractClient(object(), profile, legacy)
            http.request_json.assert_not_called()

    def test_exchange_cannot_target_other_origin_or_bind_unchecked_response(self):
        profile = deepcopy(Registry().profiles['career'])
        profile['id'] = 'fixture.exchange'
        profile['auth']['tokenBindings'] = [{'source': 'response', 'path': ['token'], 'header': 'X-Token'}]
        with self.assertRaises(QueryError): Registry().register({'profiles': [profile]})
        profile['auth']['tokenBindings'] = []
        profile['auth']['exchange'] = {
            'parameters': {'code': {'source': 'query', 'parameter': 'code'}},
            'request': {'method': 'POST', 'path': '//foreign.invalid/login', 'body': {'code': {'$param': 'code'}}},
            'response': {'checks': [{'path': ['ok'], 'equals': True}]}}
        with self.assertRaises(QueryError): Registry().register({'profiles': [profile]})
        profile['auth']['exchange']['request']['path'] = '/exchange'
        profile['auth']['exchange']['response']['checks'] = []
        with self.assertRaises(QueryError): Registry().register({'profiles': [profile]})

    def test_default_https_port_is_the_same_origin_but_other_ports_are_not(self):
        self.assertEqual(check_url('https://guest.ruc.edu.cn:443/callback',{'https://guest.ruc.edu.cn'}).netloc,'guest.ruc.edu.cn')
        self.assertEqual(application_url('https://guest.ruc.edu.cn:443/a?id=3'),'https://guest.ruc.edu.cn/a?id=3')
        self.assertEqual(application_url('/a?id=3'),'https://my.ruc.edu.cn/a?id=3')
        self.assertEqual(canonical_origin('https://[::1]:443/'),'https://[::1]')
        with self.assertRaises(QueryError):check_url('https://guest.ruc.edu.cn:444/callback',{'https://guest.ruc.edu.cn'})

    def test_sports_query_window_is_distinct_from_unavailable_seats(self):
        with self.assertRaises(QueryError) as error:
            sports_data({'code':250,'message':'当日8点以后才能查看当天的预约','data':None})
        self.assertEqual(error.exception.code,'QUERY_NOT_OPEN')
        self.assertEqual(error.exception.details['availableAfter'],'08:00')
        with self.assertRaises(QueryError) as other:
            sports_data({'code':250,'message':'fixture rejection','data':None})
        self.assertEqual(other.exception.code,'PLATFORM_REJECTED')

    def test_expired_cached_business_session_renews_once_without_password(self):
        with CampusSession(lambda:self.fail('Must reuse the task identity')) as session:
            session.identity._tgt_url = 'fixture'
            session.identity.close = Mock(return_value=True)
            old = SimpleNamespace(close=Mock())
            session.clients['contract:licensed_software'] = old
            with patch('ruc_agent.operations.execute',side_effect=[QueryError('LOGIN_REQUIRED','expired'),{'status':'ok'}]) as read:
                result=session.query('software.catalog')
            self.assertTrue(result['businessSessionRenewed'])
            self.assertEqual(read.call_count,2)
            old.close.assert_called_once()
            self.assertEqual(session.identity.credential_submissions,0)

    def test_initial_login_and_second_expiry_are_not_retried(self):
        with CampusSession(lambda:self.fail('Offline fixture')) as session:
            with patch('ruc_agent.operations.execute',side_effect=QueryError('LOGIN_REQUIRED','fixture')) as read:
                with self.assertRaises(QueryError):session.query('software.catalog')
                self.assertEqual(read.call_count,1)
                session.clients['contract:licensed_software']=SimpleNamespace(close=Mock())
                session.identity._tgt_url='fixture'
                session.identity.close=Mock(return_value=True)
                read.reset_mock()
                with self.assertRaises(QueryError):session.query('software.catalog')
                self.assertEqual(read.call_count,2)

    def test_literal_html_redirect_obeys_exact_service_binding(self):
        origin = 'https://fixture.ruc.edu.cn'
        http = SimpleNamespace(origin=origin, opener=object())
        service = origin + '/callback'
        ticket = Mock(return_value='opaque-ticket')
        identity = SimpleNamespace(ticket=ticket)
        login = auth.CAS + '/cas/login?' + urlencode({'service': service})
        def send(opener, url):
            if url == origin + '/entry':
                return 200, {}, ('<script>window.location.href="'+login+'";</script>').encode()
            self.assertEqual(url, service+'?ticket=opaque-ticket')
            return 200, {}, b'authenticated'
        with patch.object(auth, 'send', send):
            self.assertEqual(auth.cas_login(http, identity, origin+'/entry', service), service+'?ticket=opaque-ticket')
        ticket.assert_called_once_with(service)
        ticket.reset_mock()
        with patch.object(auth, 'send', return_value=(200, {}, ('<script>window.location="'+login+'";</script>').encode())):
            with self.assertRaises(QueryError):
                auth.cas_login(http, identity, origin+'/entry', origin+'/different')
        ticket.assert_not_called()

    def test_empty_location_is_opt_in_and_only_after_a_ticket(self):
        origin = 'https://fixture.ruc.edu.cn'
        http = SimpleNamespace(origin=origin, opener=object())
        service = origin+'/callback'
        identity = SimpleNamespace(ticket=lambda _: 'fixture')
        login = auth.CAS+'/cas/login?'+urlencode({'service':service})
        def send(opener, url):
            return (302, {'Location':login}, b'') if url.endswith('/entry') else (302, {'Location':''}, b'')
        with patch.object(auth, 'send', send):
            with self.assertRaises(QueryError):
                auth.cas_login(http, identity, origin+'/entry', service)
            self.assertEqual(auth.cas_login(http, identity, origin+'/entry', service, accept_empty_redirect=True), service+'?ticket=fixture')
        with patch.object(auth, 'send', return_value=(302, {'Location':''}, b'')):
            with self.assertRaises(QueryError):
                auth.cas_login(http, identity, origin+'/entry', service, accept_empty_redirect=True)

    def test_callback_token_is_private_and_assertion_must_pass(self):
        profile = Registry().profiles['career']
        final = profile['origin']+'/stu/#/loading?token=fixture-private'
        legacy = SimpleNamespace(login=lambda *args: (final, b''))
        http = SimpleNamespace(origin=profile['origin'], close=Mock(), request_json=Mock(
            return_value={'code':0, 'data':{'role':'school_user'}}))
        with patch('ruc_agent.operations.MemoryHTTP', return_value=http):
            client = ContractClient(object(), profile, legacy)
        self.assertEqual(client.headers['Authorization'], 'Bearer fixture-private')
        self.assertEqual(client.landing_url, final)
        self.assertEqual(http.request_json.call_args.args[4]['Authorization'], 'Bearer fixture-private')
        client.close()
        self.assertIsNone(client.landing_url)
        self.assertEqual(client.headers, {})
        http.request_json.return_value = {'code':0,'data':{'role':'anonymous'}}
        with patch('ruc_agent.operations.MemoryHTTP', return_value=http), self.assertRaises(QueryError):
            ContractClient(object(), profile, legacy)

    def test_post_identity_assertion_and_html_rpc_keep_fixed_commands(self):
        registry = Registry()
        profile = registry.profiles['office_workflow']
        http = SimpleNamespace(origin=profile['origin'], close=Mock(), request_json=Mock(return_value=[]))
        with patch('ruc_agent.operations.MemoryHTTP', return_value=http), patch('ruc_agent.operations.cas_login', return_value=profile['entry']):
            client = ContractClient(object(), profile)
        args = http.request_json.call_args.args
        self.assertEqual((args[0],args[1],args[3]['cmd'],args[5]),
                         ('/r/w','POST','com.awspaas.user.apps.gwskins_searchFun','form'))
        policy = BrowserPolicy(registry)
        body = urlencode({'sid':'ck','cmd':'COM_ZTHF_CLIENT_USER_HOME','skinValue':'com.awspaas.user.apps.gwskins','flag':''})
        self.assertTrue(policy.allowed(profile['origin']+'/r/w','POST','document',post_data=body))
        self.assertFalse(policy.allowed(profile['origin']+'/r/w','POST','document',post_data=body.replace('COM_ZTHF_CLIENT_USER_HOME','deleteFlow')))
        client.close()

    def test_cas_oauth_profile_only_accepts_the_official_callback(self):
        profile = deepcopy(Registry().profiles['library_resources'])
        for invalid in ['https://cas.ruc.edu.cn/cas/other', 'https://elsewhere.ruc.edu.cn/callback']:
            profile['id'] = 'fixture.profile'
            profile['auth']['service'] = invalid
            with self.assertRaises(QueryError): Registry().register({'profiles':[profile]})

    def test_prefetch_only_allows_static_code_assets(self):
        policy = BrowserPolicy(Registry())
        self.assertTrue(policy.allowed('https://career.ruc.edu.cn/stu/js/fixture.js','GET','other'))
        self.assertFalse(policy.allowed('https://career.ruc.edu.cn/api/resume/remove','GET','other'))
        entry = 'https://research.ruc.edu.cn/userAction!do_switchGroup.action?switchGroupId=2'
        with self.assertRaises(QueryError): policy.add_entry(entry,[entry])

    def test_cookie_binding_rejects_foreign_expired_or_ambiguous_credentials(self):
        binding = {'source':'cookie','parameter':'Admin-Token','header':'X-Token'}
        cookie = lambda domain, value, expired=False: SimpleNamespace(
            name='Admin-Token',domain=domain,value=value,is_expired=lambda:expired)
        http = SimpleNamespace(origin='https://cardview.ruc.edu.cn', jar=[
            cookie('foreign.invalid','untrusted'),cookie('cardview.ruc.edu.cn','old',True),
            cookie('cardview.ruc.edu.cn','current')])
        self.assertEqual(token_headers(http,http.origin,[binding]),{'X-Token':'current'})
        http.jar.append(cookie('.ruc.edu.cn','ambiguous'))
        with self.assertRaises(QueryError): token_headers(http,http.origin,[binding])

    def test_declared_decimal_pagination_preserves_completeness(self):
        spec = Registry().specs['warm.navigation']
        def fetch(params):
            return {'content':[{'id':params['page']}],'totalElements':'2'}
        result = collect_pages(fetch,spec,{},5)
        self.assertTrue(result['complete'])
        self.assertEqual(result['totalItems'],2)
        for bad in ['2.0','-1',True,2,'2e0']:
            with self.assertRaises(QueryError):
                collect_pages(lambda _: {'content':[],'totalElements':bad},spec,{},5)

