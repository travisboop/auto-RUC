"""Offline protocol checks. These do not claim a real password login succeeded."""
import contextlib
import io
import json
import unittest
from email.message import Message
from unittest.mock import patch
from urllib.parse import parse_qs

from ruc_agent import booths as app
from ruc_agent import cli


class Response(io.BytesIO):
    def __init__(self, code, body=b'', **headers):
        super().__init__(body)
        self.code = code
        self.headers = Message()
        for key, value in headers.items():
            self.headers[key] = value


class Protocol:
    """CAS documented responses plus the observed RUC service callback."""
    def __init__(self, mode='success', tgt='TGT-fixture.cas01', service_ticket='ST-fixture.cas01'):
        self.mode = mode
        self.tgt = tgt
        self.service_ticket = service_ticket
        self.requests = []
        self.cookie_jars = []

    def build_opener(self, *handlers):
        for handler in handlers:
            if isinstance(handler, app.urllib.request.HTTPCookieProcessor):
                self.cookie_jars.append(handler.cookiejar)
        return self

    def open(self, request, timeout):
        if isinstance(request, str):
            request = app.urllib.request.Request(request)
        method, url = request.get_method(), request.full_url
        self.requests.append(request)
        if url == app.PUBLIC + 'getFrontVueConfig':
            config = {'casLogin': 1, 'casLoginUrl': app.ORIGIN + '/rem',
                      'pcCasServiceUrl': app.ORIGIN + '/space'}
            if self.mode == 'changed_config':
                config['casLoginUrl'] = 'https://example.invalid'
            return self.json(config)
        if url.startswith(app.ORIGIN + '/rem/static/sso/login?'):
            return Response(302, Location=app.CAS + '/cas/login?service=' +
                            app.urllib.parse.quote(app.CAS_SERVICE, safe=''))
        if url == app.CAS_TICKETS and method == 'POST':
            if self.mode == 'rejected':
                return Response(401, b'private-server-error-never-print')
            target = app.CAS_TICKETS + '/' + self.tgt
            if self.mode == 'hostile_location':
                target = 'https://example.invalid/TGT-fixture'
            if self.mode == 'proxy_http_location':
                target = target.replace('https:', 'http:')
            return Response(201, Location=target)
        if url == app.CAS_TICKETS + '/' + self.tgt:
            if method == 'DELETE':
                return Response(204)
            self.assert_service(request)
            if self.mode == 'service_rejected':
                return Response(403)
            return Response(200, self.service_ticket.encode('ascii'))
        if url == app.CAS_SERVICE + '?' + app.urllib.parse.urlencode({'ticket': self.service_ticket}):
            if self.mode == 'challenge_redirect':
                return Response(302, Location=app.CAS + '/cas/login')
            return Response(302, Location=app.ORIGIN + '/space/?token=fixture-exchange')
        if url == app.PUBLIC + 'remoteCasLogin?token=fixture-exchange&noAuth=true':
            return self.json({'token': 'fixture-api-token', 'userInfo': {'private': 'discard'}})
        if url == app.API + 'getSchoolList':
            if request.get_header('Authorization') != 'fixture-api-token':
                raise AssertionError('API must receive the newly issued token')
            return self.json([{'id': 'fixture-campus', 'name': '测试校区'}])
        raise AssertionError('Unexpected request: ' + method + ' ' + url)

    @staticmethod
    def json(data):
        return Response(200, json.dumps({'code': 200, 'status': True, 'data': data}).encode())

    @staticmethod
    def assert_service(request):
        if parse_qs(request.data.decode()) != {'service': [app.CAS_SERVICE]}:
            raise AssertionError('Service must match the observed callback exactly')


class PasswordFlowTests(unittest.TestCase):
    def test_ticket_identifiers_do_not_require_conventional_prefixes(self):
        for tgt, service_ticket in [
                ('opaque-session-fixture', 'ST-fixture.cas01'),
                ('TGT-fixture.cas01', 'opaque-service-fixture'),
                ('opaque-session-fixture', 'opaque-service-fixture')]:
            with self.subTest(tgt=tgt, service_ticket=service_ticket):
                protocol = Protocol(tgt=tgt, service_ticket=service_ticket)
                with patch.object(app.urllib.request, 'build_opener', protocol.build_opener):
                    client = app.BoothClient('fixture-student', 'fixture-password')
                    self.assertEqual(client.query('getSchoolList')[0]['name'], '测试校区')
                    self.assertTrue(client.close())
                self.assertEqual(protocol.requests[-1].full_url, app.CAS_TICKETS + '/' + tgt)
                self.assertEqual(protocol.requests[-1].get_method(), 'DELETE')

    def test_ticket_location_still_rejects_path_traversal_and_extra_segments(self):
        for tgt in ['', '.', '..', 'segment/extra', 'segment%2Fextra', 'ticket?extra=value', 'ticket#fragment']:
            with self.subTest(tgt=tgt):
                protocol = Protocol(tgt=tgt)
                with patch.object(app.urllib.request, 'build_opener', protocol.build_opener):
                    with self.assertRaises(app.QueryError) as caught:
                        app.BoothClient('fixture-student', 'fixture-password')
                self.assertEqual(caught.exception.code, 'UNEXPECTED_TICKET_URL')
                self.assertEqual(protocol.requests[-1].full_url, app.CAS_TICKETS)

    def run_mode(self, mode):
        protocol = Protocol(mode)
        with patch.object(app.urllib.request, 'build_opener', protocol.build_opener):
            with self.assertRaises(app.QueryError) as caught:
                app.BoothClient('fixture-student', 'fixture-password')
        return protocol, caught.exception

    def test_fresh_password_flow_without_file_access(self):
        protocol = Protocol()
        with patch.object(app.urllib.request, 'build_opener', protocol.build_opener), \
             patch('builtins.open', side_effect=AssertionError('No stored state may be read')):
            client = app.BoothClient('fixture-student', 'fixture-password')
            self.assertEqual(client.query('getSchoolList')[0]['name'], '测试校区')
            self.assertTrue(client.close())
        self.assertEqual(len(protocol.cookie_jars), 1)
        self.assertEqual(len(protocol.cookie_jars[0]), 0)
        credentials = [r for r in protocol.requests if r.full_url == app.CAS_TICKETS]
        self.assertEqual(len(credentials), 1)
        self.assertEqual(parse_qs(credentials[0].data.decode()),
                         {'username': ['fixture-student'], 'password': ['fixture-password']})
        for request in protocol.requests:
            self.assertNotIn('captcha', request.full_url.lower())
            self.assertNotIn('/cas/login', request.full_url)
            if request is not credentials[0]:
                self.assertNotIn(b'fixture-password', request.data or b'')
        self.assertEqual(protocol.requests[-1].get_method(), 'DELETE')
        self.assertIsNone(client.token)

    def test_password_rejection_has_no_retry_and_no_response_leak(self):
        protocol, error = self.run_mode('rejected')
        self.assertEqual(error.code, 'PASSWORD_AUTH_REJECTED')
        self.assertNotIn('private-server-error', str(error))
        self.assertEqual(sum(r.get_method() == 'POST' for r in protocol.requests), 1)

    def test_unexpected_ticket_location_is_never_followed(self):
        protocol, error = self.run_mode('hostile_location')
        self.assertEqual(error.code, 'UNEXPECTED_TICKET_URL')
        self.assertFalse(any('example.invalid' in r.full_url for r in protocol.requests))

    def test_changed_configuration_stops_before_password_submission(self):
        protocol, error = self.run_mode('changed_config')
        self.assertEqual(error.code, 'CONFIG_CHANGED')
        self.assertFalse(any(r.get_method() == 'POST' for r in protocol.requests))

    def test_service_rejection_revokes_our_new_ticket(self):
        protocol, error = self.run_mode('service_rejected')
        self.assertEqual(error.code, 'SERVICE_TICKET_REJECTED')
        self.assertEqual(protocol.requests[-1].get_method(), 'DELETE')

    def test_challenge_redirect_stops_without_opening_login_page(self):
        protocol, error = self.run_mode('challenge_redirect')
        self.assertEqual(error.code, 'UNEXPECTED_REDIRECT')
        self.assertFalse(any('/cas/login' in r.full_url for r in protocol.requests))
        self.assertEqual(protocol.requests[-1].get_method(), 'DELETE')

    def test_proxy_location_is_normalized_to_https(self):
        protocol = Protocol('proxy_http_location')
        with patch.object(app.urllib.request, 'build_opener', protocol.build_opener):
            client = app.BoothClient('fixture-student', 'fixture-password')
            client.close()
        self.assertTrue(all(r.full_url.startswith('https://') for r in protocol.requests))

    def test_cli_result_does_not_contain_credentials_or_tickets(self):
        protocol, output = Protocol(), io.StringIO()
        with patch.object(app.urllib.request, 'build_opener', protocol.build_opener), \
             patch.object(cli.sys, 'argv', ['ruc', 'check-login', '--username', 'fixture-student']), \
             patch('ruc_agent.credentials.getpass.getpass', return_value='fixture-password'), \
             contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(), 0)
        result = json.loads(output.getvalue())
        self.assertTrue(result['freshSession'])
        self.assertEqual(result['campuses'], ['测试校区'])
        for secret in ['fixture-student', 'fixture-password', 'TGT-fixture', 'ST-fixture', 'fixture-api-token']:
            self.assertNotIn(secret, output.getvalue())


if __name__ == '__main__':
    unittest.main()
