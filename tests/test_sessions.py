import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode

from test_auth import Protocol
from ruc_agent import auth, engine, tools
from ruc_agent.errors import QueryError


class SessionTests(unittest.TestCase):
    def test_shared_identity_is_used_once_across_platforms_and_revoked(self):
        class MultiService(Protocol):
            @staticmethod
            def assert_service(request):
                assert parse_qs(request.data.decode())['service'][0] in {
                    auth.OAUTH_SERVICE, 'https://my.ruc.edu.cn/commoncallback/'}
        protocol = MultiService()
        created, closed = [], []
        class Academic:
            def __init__(self, identity):
                created.append('academic')
                identity.ticket(auth.OAUTH_SERVICE)
            def grades(self, params): return {'status': 'ok', 'records': []}
            def rankings(self, params): return {'status': 'ok', 'records': []}
            def close(self): closed.append('academic')
        class Portal:
            def __init__(self, identity):
                created.append('portal')
                identity.ticket('https://my.ruc.edu.cn/commoncallback/')
            def applications(self, params): return {'status': 'ok', 'applications': []}
            def close(self): closed.append('portal')
        with patch.object(auth.urllib.request, 'build_opener', protocol.build_opener), \
             patch.dict(engine.FACTORIES, {'academic': Academic, 'portal': Portal}), \
             patch.object(tools, 'read_credentials', return_value=('fixture-student', 'fixture-password')) as credentials:
            out = tools.invoke('ruc_query_batch', {'requests': [
                {'capability': 'grades'}, {'capability': 'rankings'}, {'capability': 'applications'}]})
        self.assertEqual(out['status'], 'ok')
        self.assertEqual(out['credentialSubmissions'], 1)
        self.assertTrue(out['restTicketRevoked'])
        self.assertFalse(out['savedStateRead'])
        self.assertEqual(credentials.call_count, 1)
        self.assertEqual(created, ['academic', 'portal'])
        self.assertEqual(closed, created)
        self.assertEqual(sum(r.get_method() == 'DELETE' for r in protocol.requests), 1)
        self.assertNotIn('fixture-password', json.dumps(out))

    def test_missing_credentials_does_not_block_independent_public_catalog(self):
        class Public:
            def __init__(self, identity): pass
            def query(self, params): return {'status': 'ok', 'sites': []}
            def close(self): pass
        with patch.dict(engine.FACTORIES, {'sports': Public}), \
             patch.object(tools, 'read_credentials', side_effect=QueryError('CREDENTIALS_REQUIRED', 'missing')) as credentials:
            out = tools.invoke('ruc_query_batch', {'requests': [
                {'capability': 'grades'}, {'capability': 'rankings'},
                {'capability': 'sports_venues', 'params': {'catalog_only': True}}]})
        self.assertEqual([v['status'] for v in out['results']], ['error', 'not_run', 'ok'])
        self.assertEqual(credentials.call_count, 1)
        self.assertEqual(out['credentialSubmissions'], 0)

    def test_verified_proxy_redirect_is_upgraded_without_plain_http_request(self):
        http = SimpleNamespace(origin='https://jw.ruc.edu.cn', opener=object())
        identity = SimpleNamespace(ticket=lambda service: 'fixture-ticket')
        login = auth.CAS + '/cas/login?' + urlencode({'service': auth.OAUTH_SERVICE})
        seen = []
        def send(opener, url):
            seen.append(url)
            if url == http.origin + '/entry': return 302, {'Location': login}, b''
            if url.startswith(auth.OAUTH_SERVICE): return 302, {'Location': 'http://jw.ruc.edu.cn/Njw2017/index.html'}, b''
            if url == http.origin + '/Njw2017/index.html': return 200, {}, b'page'
            self.fail('Unexpected request')
        with patch.object(auth, 'send', send):
            result = auth.cas_login(http, identity, http.origin + '/entry', auth.OAUTH_SERVICE,
                                    upgrade_http_paths={'/Njw2017/index.html'})
        self.assertEqual(result, http.origin + '/Njw2017/index.html')
        self.assertTrue(all(url.startswith('https://') for url in seen))
        with patch.object(auth, 'send', send), self.assertRaises(QueryError):
            auth.cas_login(http, identity, http.origin + '/entry', auth.OAUTH_SERVICE)

    def test_oauth_external_callback_is_rejected_before_credentials(self):
        http = SimpleNamespace(origin='https://jw.ruc.edu.cn', opener=object())
        identity = SimpleNamespace(ticket=lambda service: self.fail('Must reject before ticket'))
        bad = auth.CAS + '/cas/oauth2.0/authorize?' + urlencode({'redirect_uri': 'https://example.invalid/callback'})
        with patch.object(auth, 'send', return_value=(302, {'Location': bad}, b'')), self.assertRaises(QueryError):
            auth.cas_login(http, identity, http.origin + '/entry', auth.OAUTH_SERVICE)


if __name__ == '__main__':
    unittest.main()
