from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import quote, urlencode

from ruc_agent.contracts import Registry
from ruc_agent.errors import QueryError
from ruc_agent.future_classroom import (AUTHORIZE, CALLBACK, DOCKING, ENTRY, ORIGIN,
                                        FutureClassroomClient)
from ruc_agent.operations import request_data


class FutureAuthenticationTests(unittest.TestCase):
    def run_login(self, bind='1', target=None, assertion_id=42):
        callback = ORIGIN + CALLBACK + '?type=union'
        target = target or ORIGIN + AUTHORIZE + '?' + urlencode({
            'client_id': 'fixture-client', 'redirect_uri': callback,
            'scope': 'openid', 'response_type': 'code'})
        final = ORIGIN + '/oauth2/login?bind=' + bind + '&token=fixture-union&redirect_url=' + quote(target, safe='')
        self.sent, self.calls, self.tickets = [], [], []
        def send(opener, url):
            self.sent.append(url)
            if url == ENTRY:
                return 302, {'Location': ORIGIN + '/oauth2/login'}, b''
            if url == ORIGIN + '/oauth2/login': return 200, {}, b'<html>union</html>'
            if url == target:
                return 302, {'Location': callback + '&code=fixture-code'}, b''
            if url == callback + '&code=fixture-code':
                return 302, {'Location': DOCKING + '?username=fixture-student&token=fixture-docking'}, b''
            self.fail('Unexpected fixture authentication route')
        def cas_login(http, identity, entry, service):
            identity.ticket(service)
            return final
        def request(path, method='GET', query=None, body=None, headers=None, encoding='json'):
            self.calls.append((path, method, query, body, headers, encoding))
            if path.endswith('/dockingAuth'):
                return {'stat': 1, 'data': {'token': 'fixture-business', 'id': 42, 'region': {'id': 3}}}
            if path.endswith('/getUserById'):
                return {'stat': 1, 'data': {'id': assertion_id}}
            self.fail('Unexpected fixture business request')
        identity = SimpleNamespace(ticket=lambda *args: self.tickets.append(args) or 'fixture-st')
        with patch('ruc_agent.future_classroom.send', send), \
             patch('ruc_agent.future_classroom.cas_login', cas_login), \
             patch('ruc_agent.http.MemoryHTTP.request_json', side_effect=request):
            return FutureClassroomClient(identity)

    def test_nested_oauth_only_authenticates_and_reads_own_identity(self):
        client = self.run_login()
        self.assertEqual(client.context, {'userId': 42, 'regionId': 3})
        self.assertEqual(client.headers['accessToken'], 'fixture-business')
        self.assertEqual(len(self.tickets), 1)
        self.assertEqual([v[0] for v in self.calls], [
            '/jiakt/adminApi/user/dockingAuth', '/jiakt/adminApi/user/getUserById'])
        self.assertEqual(self.calls[0][3], {'serial': 'fixture-student', 'token': 'fixture-docking', 'loginType': 'web'})
        self.assertEqual(self.calls[0][5], 'form')
        self.assertEqual(self.calls[1][2], {'userId': 42})
        self.assertFalse(any('/setTempValue' in v or '/getLoginRecord' in v for v in self.sent))
        client.close()
        self.assertEqual(client.context, {})
        self.assertEqual(client.headers, {})

    def test_unregistered_account_stops_before_binding_or_business_exchange(self):
        with self.assertRaises(QueryError) as error:
            self.run_login(bind='0')
        self.assertEqual(error.exception.code, 'ACCOUNT_BINDING_REQUIRED')
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.sent), 2)

    def test_changed_target_callback_and_wrong_identity_are_rejected(self):
        for target in [
            'https://untrusted.invalid/oauth/authorize', ORIGIN + '/api/delete',
            ORIGIN + AUTHORIZE + '?' + urlencode({'redirect_uri': 'https://untrusted.invalid/callback'}),
            ORIGIN + AUTHORIZE + '?' + urlencode({'redirect_uri': ORIGIN + '/api/delete'}),
        ]:
            with self.subTest(target=target), self.assertRaises(QueryError):
                self.run_login(target=target)
            self.assertEqual(self.calls, [])
        with self.assertRaises(QueryError):
            self.run_login(assertion_id=99)


class SessionContextContractTests(unittest.TestCase):
    def test_authenticated_context_cannot_be_overridden_by_caller(self):
        registry = Registry()
        spec = registry.specs['future.profile']
        http = SimpleNamespace(request_json=lambda *args: args)
        client = SimpleNamespace(http=http, headers={}, context={'userId': 42})
        request = request_data(client, registry.profiles['future_classroom'], spec, {'userId': 99})
        self.assertEqual(request[2], {'userId': 42})
        with self.assertRaises(QueryError):
            registry.prepare('future.profile', {'userId': 99})
        with self.assertRaises(QueryError):
            request_data(SimpleNamespace(http=http), registry.profiles['future_classroom'], spec, {})

    def test_unknown_fields_and_write_context_are_rejected_before_login(self):
        registry = Registry()
        for changes in [{'readOnly': False}, {'request': {'method': 'GET', 'path': '/fixture',
                            'query': {'token': {'$context': 'token'}}}}]:
            spec = deepcopy(registry.specs['future.profile'])
            spec.update(id='fixture.context', **changes)
            with self.assertRaises(QueryError):
                registry.register({'capabilities': [spec]})

    def test_caller_json_is_never_interpreted_as_context_template(self):
        spec = {'request': {'method': 'POST', 'path': '/fixture', 'body': {'$param': 'payload'}}, 'response': {}}
        client = SimpleNamespace(http=SimpleNamespace(request_json=lambda *args: args),
                                 headers={}, context={'userId': 42})
        request = request_data(client, {}, spec, {'payload': {'$context': 'userId'}})
        self.assertEqual(request[3], {'$context': 'userId'})
