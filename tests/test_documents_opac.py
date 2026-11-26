"""New authentication and document protocols use only in-memory school fixtures."""
from html import escape
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit

from ruc_agent.contracts import Registry, decode_response
from ruc_agent.engine import CampusSession
from ruc_agent.errors import QueryError
from ruc_agent.opac import OpacClient, ORIGIN, SERVICE, CALLBACK, ENTRY, html_redirect
from ruc_agent.pagination import collect_pages
from ruc_agent.parsing import hidden_value, html_snapshot
from ruc_agent.urls import application_target, application_url


class DocumentTests(unittest.TestCase):
    def test_server_document_tables_fields_scripts_and_auth_material(self):
        body = b'''<title>Fixture student</title><script>submitRealForm();</script>
          <a href="/Student/Edit?ticket=fixture-secret">Profile</a>
          <table><tr><th>Item</th><th>Data</th></tr><tr><td>A</td><td>
           <table><tr><td>Nested</td></tr></table>outer</td></tr></table>
          <form method=post action=/save><input name=Name value=Fixture>
          <input type=hidden name=__RequestVerificationToken value=fixture-secret>
          <input type=password name=credential value=fixture-secret>
          <select name=type><option>first<option value=2 selected>Second</select>
          <textarea name=note>one &amp; two</textarea></form>'''
        doc = html_snapshot(body, 'https://student.rmbs.ruc.edu.cn/home')
        self.assertNotIn('submitRealForm', doc['text'])
        self.assertNotIn('fixture-secret', json.dumps(doc))
        self.assertEqual(doc['tables'], [[['Item', 'Data'], ['A', 'outer']], [['Nested']]])
        self.assertEqual(doc['linkPaths'], ['/Student/Edit'])
        values = {v['name']: v['value'] for v in doc['fields']}
        self.assertEqual(values['type'], ['2'])
        self.assertEqual(values['note'], 'one & two')
        self.assertEqual(doc['forms'][0]['method'], 'POST')
        self.assertEqual(len(doc['forms'][0]['fieldIndices']), 5)

    def test_html_identity_assertion_rejects_login_page(self):
        response = Registry().profiles['business_students']['auth']['assertion']['response']
        login = html_snapshot(b'<title>Login</title><form></form>', 'https://student.rmbs.ruc.edu.cn/')
        with self.assertRaises(QueryError):
            decode_response({'response': response}, login)

    def test_redaction_applies_to_json_and_collected_pages(self):
        spec = Registry().specs['opac.loan_history']
        def fetch(params):
            return {'success': True, 'data': {'searchResult': [
                {'loanId': params['page'], 'title': 'fixture', 'password': 'private', 'nested': {'jwt': 'private'}}], 'numFound': 2}}
        one = decode_response(spec, fetch({'page': 1}))
        all_rows = collect_pages(fetch, spec, {'rows': 1, 'searchContent': ''}, 5)
        self.assertNotIn('private', json.dumps(one))
        self.assertNotIn('private', json.dumps(all_rows))
        self.assertTrue(all_rows['complete'])

    def test_portal_wrapper_keeps_actual_service_and_strips_jwt(self):
        target = 'https://service.ruc.edu.cn/v2/matter/start?id=380'
        wrapper = 'https://my.ruc.edu.cn/sopplus/_web/fusionportal/serviceCenter/details.html?' + urlencode({'id': 1390, 'entranceUrl': target})
        self.assertEqual(application_target({'appUrl': wrapper}), target)
        self.assertNotIn('private', application_url('https://opac.ruc.edu.cn/#/index?jwt=private&jwtHeader=header'))


class OpacAuthenticationTests(unittest.TestCase):
    def test_empty_jar_completes_all_three_observed_html_bridges(self):
        signed_callback = CALLBACK + '?' + urlencode({'data': 'fixture-data', 'time': '123', 'enc': 'fixture-enc'})
        passport = ORIGIN + '/passport/api/v2/casreceive?' + urlencode({'refer': signed_callback, 'signature': 'fixture'})
        def hidden(key, value):
            return ('<input type=hidden id="'+key+'" value="'+escape(value, quote=True)+'">').encode()
        sent, tickets = [], []
        def send(opener, request):
            url = request if isinstance(request, str) else request.full_url
            sent.append(url)
            if url == ENTRY:
                return 200, {}, hidden('ref', 'https://cas.ruc.edu.cn/cas/login?' + urlencode({'service': SERVICE}))
            if url == SERVICE+'?ticket=fixture-st': return 302, {'Location': SERVICE}, b''
            if url == SERVICE: return 200, {}, hidden('ref', passport)
            if url == passport: return 200, {}, hidden('status', 'true')+hidden('updatePhoneUrl', '')+hidden('refer', signed_callback)
            if url == signed_callback: return 302, {'Location': '/#/index?jwt=fixture-jwt&jwtHeader=X-Auth'}, b''
            if url == ORIGIN+'/#/index?jwt=fixture-jwt&jwtHeader=X-Auth': return 200, {}, b'<div>catalog</div>'
            self.fail('Unexpected fixture authentication request')
        identity = SimpleNamespace(ticket=lambda service, origins: (tickets.append((service, origins)) or 'fixture-st'))
        with patch('ruc_agent.auth.send', send), patch('ruc_agent.http.MemoryHTTP.request_json', return_value={'success': True, 'data': {'userId': 42}}):
            client = OpacClient(identity)
        self.assertEqual(client.headers, {'X-Auth': 'fixture-jwt'})
        self.assertEqual(tickets, [(SERVICE, {ORIGIN})])
        self.assertEqual(len(sent), 6)
        client.close()
        self.assertEqual(client.headers, {})

    def test_changed_callback_or_additional_phone_verification_is_rejected(self):
        path = urlsplit(ORIGIN+'/passport/api/v2/casreceive')
        for body in [
            b'<input type=hidden id=status value=true><input type=hidden id=updatePhoneUrl value=/phone><input type=hidden id=refer value="https://opac.ruc.edu.cn/find/sso/login/ruc/0">',
            b'<input type=hidden id=status value=true><input type=hidden id=updatePhoneUrl value=""><input type=hidden id=refer value="https://untrusted.invalid/">',
        ]:
            with self.assertRaises(QueryError): html_redirect(path, body)
        with self.assertRaises(QueryError):
            hidden_value(b'<input type=hidden id=ref value=a><input type=hidden id=ref value=b>', 'ref')

    def test_one_realm_failure_does_not_cancel_independent_legacy_read(self):
        with CampusSession(lambda: self.fail('No fixture password')) as session:
            def query(name, params):
                if name == 'grades': raise QueryError('PASSWORD_AUTH_REJECTED', 'fixture')
                return {'status': 'ok'}
            with patch.object(session, 'query', side_effect=query) as method:
                result = session.batch([{'capability': 'grades'}, {'capability': 'course.profile'}, {'capability': 'rankings'}])
            self.assertEqual([v['status'] for v in result['results']], ['error', 'ok', 'not_run'])
            self.assertEqual(method.call_count, 2)
