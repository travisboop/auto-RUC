from copy import deepcopy
import io
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from ruc_agent.actions import ActionPlans
from ruc_agent.browser import BrowserPolicy, school_origin
from ruc_agent.contracts import Registry, decode_response, request_for, fingerprint
from ruc_agent.engine import CampusSession
from ruc_agent.errors import QueryError
from ruc_agent.server import jsonl
from ruc_agent.tools import ToolRuntime


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.registry = Registry()

    def test_extension_is_queryable_without_new_python_or_enum(self):
        spec = deepcopy(self.registry.specs['booths.usage_types'])
        spec['id'] = 'fixture.new_information'
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, 'custom.json').write_text(json.dumps({'capabilities': [spec]}))
            runtime = ToolRuntime(extensions=folder)
            data = {'code': 200, 'status': True, 'data': [{'fieldAddedBySchool': 123}]}
            fake = SimpleNamespace(token='fixture-token', _session=SimpleNamespace(request_json=lambda *a: data))
            with runtime, patch.object(CampusSession, 'contract_client', return_value=fake), \
                    patch('ruc_agent.auth.CasIdentity.prepare'):
                out = runtime.call('ruc_query', {'capability': spec['id']})
            self.assertEqual(out['data'][0]['fieldAddedBySchool'], 123)

    def test_defaults_templates_and_unknown_fields_before_auth(self):
        spec, values = self.registry.prepare('booths.my_reservations', {})
        self.assertEqual(request_for(spec, values)['body'], {'currentPage': 1, 'pageSize': 20})
        with self.assertRaises(QueryError):
            self.registry.prepare('booths.my_reservations', {'other_student': 'fixture'})
        spec = deepcopy(spec)
        spec['request'] = {'method': 'GET', 'path': '/rooms/{id}', 'query': {'label': {'$param': 'label'}}}
        self.assertEqual(request_for(spec, {'id': 'a/b', 'label': 'literal $()'})['path'], '/rooms/a%2Fb')
        for path in ['//evil.invalid/a', '/a/../b', '/%2e%2e/private', '/a?op=delete', '/a\\b']:
            spec['request']['path'] = path
            with self.assertRaises(QueryError):
                request_for(spec, {'label': 'fixture'})

    def test_incomplete_page_is_explicit_and_drift_is_not_empty_data(self):
        spec = self.registry.specs['booths.my_reservations']
        data = {'code': 200, 'status': True, 'data': {'pageList': [{'id': '1'}], 'totalCount': 20}}
        out = decode_response(spec, data)
        self.assertFalse(out['complete'])
        self.assertEqual(out['totalItems'], 20)
        for invalid in [{'code': 401, 'status': False}, {'code': 200, 'status': True, 'data': {'renamed': []}},
                        {'code': True, 'status': True, 'data': []}]:
            with self.assertRaises(QueryError):
                decode_response(spec, invalid)
        self.assertEqual(fingerprint([{'x': 1}]), fingerprint([{'x': 900}, {'x': 2}]))
        self.assertNotEqual(fingerprint({'x': 1}), fingerprint({'x': '1'}))

    def test_registration_is_atomic_and_cannot_replace_known_rules(self):
        before = self.registry.catalog()
        spec = deepcopy(self.registry.specs['booths.usage_types'])
        spec['id'] = 'fixture.added'
        with self.assertRaises(QueryError):
            self.registry.register({'capabilities': [spec, spec]})
        self.assertEqual(before, self.registry.catalog())

    def test_read_query_never_executes_declared_write(self):
        with patch('ruc_agent.tools.read_credentials') as credentials, ToolRuntime() as runtime:
            with self.assertRaises(QueryError):
                runtime.call('ruc_query', {'capability': 'booths.reserve', 'params': {}})
            credentials.assert_not_called()

    def test_museum_cancellation_is_only_a_frozen_offline_action(self):
        with CampusSession(lambda: self.fail('Preview must not authenticate')) as session:
            with self.assertRaises(QueryError):
                session.query('museum.cancel_reservation', {'reservation_id': 'fixture-id'})
            plan = session.prepare_action('museum.cancel_reservation', {'reservation_id': 'fixture/id'})
            self.assertIn('fixture%2Fid', json.dumps(plan))
            with self.assertRaises(QueryError) as error:
                session.commit_action(plan['actionId'])
            self.assertEqual(error.exception.code, 'WRITES_DISABLED')

    def test_action_frozen_expiring_consumed_before_send_and_disabled_by_host(self):
        plans = ActionPlans()
        source = {'params': {'roomId': 'fixture-a'}}
        with patch('ruc_agent.actions.monotonic', return_value=1):
            plan = plans.prepare(source)
        source['params']['roomId'] = 'fixture-b'
        plan['preview']['params']['roomId'] = 'fixture-c'
        with patch('ruc_agent.actions.monotonic', return_value=2):
            self.assertEqual(plans.take(plan['actionId'])['params']['roomId'], 'fixture-a')
        with self.assertRaises(QueryError):
            plans.take(plan['actionId'])
        with patch('ruc_agent.actions.monotonic', return_value=1):
            expired = plans.prepare(source)
        with patch('ruc_agent.actions.monotonic', return_value=302), self.assertRaises(QueryError):
            plans.take(expired['actionId'])
        values = {'begin': 600, 'end': 660, 'onDate': '2030-01-01', 'roomId': 'fixture', 'useType': 'fixture'}
        with CampusSession(lambda: self.fail('Preview cannot authenticate')) as session:
            plan = session.prepare_action('booths.reserve', values)
            with self.assertRaises(QueryError) as caught:
                session.commit_action(plan['actionId'])
            self.assertEqual(caught.exception.code, 'WRITES_DISABLED')
            session.allow_writes = True
            with patch('ruc_agent.operations.execute', side_effect=QueryError('NETWORK_ERROR', 'fixture')) as send:
                out = session.commit_action(plan['actionId'])
                self.assertEqual(out['status'], 'outcome_unknown')
                with self.assertRaises(QueryError):
                    session.commit_action(plan['actionId'])
                self.assertEqual(send.call_count, 1)

    def test_jsonl_survives_bad_line_and_reuses_one_runtime(self):
        with ToolRuntime() as runtime:
            source = io.StringIO('bad\n'+json.dumps({'name':'ruc_capabilities','arguments':{'keyword':'booths.'}})+'\n')
            target = io.StringIO()
            jsonl(runtime, source, target)
            results = [json.loads(line) for line in target.getvalue().splitlines()]
            self.assertEqual([r['status'] for r in results], ['error', 'ok'])
            self.assertIsNone(runtime.session)

    def test_request_action_sends_frozen_body_and_auth_once(self):
        calls = []
        def request(*args):
            calls.append(args)
            return {'code': 200, 'status': True, 'data': {'receipt': 'fixture'}}
        fake = SimpleNamespace(token='private-fixture-token', _session=SimpleNamespace(request_json=request))
        values = {'begin':600,'end':660,'onDate':'2030-01-01','roomId':'fixture','useType':'fixture',
                  'extraField':{'purpose':'original'}}
        with CampusSession(lambda: self.fail('Mock identity only'), allow_writes=True) as session:
            plan = session.prepare_action('booths.reserve', values)
            values['extraField']['purpose'] = 'changed'
            with patch.object(session.identity, 'prepare'), patch.object(session,'contract_client',return_value=fake):
                result = session.commit_action(plan['actionId'])
            self.assertEqual(result['outcome'], 'accepted')
            self.assertEqual(calls[0][0:2], ('/spa/static/api/book/saveReservation','POST'))
            self.assertEqual(calls[0][3]['extraField']['purpose'], 'original')
            self.assertEqual(calls[0][4]['Authorization'], 'private-fixture-token')
            self.assertNotIn('private-fixture-token', json.dumps(result))
            with self.assertRaises(QueryError):
                session.commit_action(plan['actionId'])
            self.assertEqual(len(calls),1)
            for bad in [{**values,'end':500}, {**values,'onDate':'2030-02-30'}]:
                with self.assertRaises(QueryError):
                    session.prepare_action('booths.reserve',bad)

    def test_additional_cas_profile_uses_observed_callback_and_assertion(self):
        from ruc_agent.operations import ContractClient
        from ruc_agent.auth import CAS
        origin, service = 'https://fixture.ruc.edu.cn', 'https://fixture.ruc.edu.cn/callback'
        profile = {'id':'fixture','origin':origin,'entry':origin+'/',
                   'auth':{'kind':'cas_cookie','service':service,'assertion':{'path':'/api/me',
                   'response':{'checks':[{'path':['authenticated'],'equals':True}]}}}}
        issued = []
        identity = SimpleNamespace(ticket=lambda callback, origins: issued.append((callback,origins)) or 'fixture-ticket')
        def send(opener,url):
            if url==origin+'/':
                return 302, {'Location':CAS+'/cas/login?'+urlencode({'service':service})}, b''
            if url==service+'?ticket=fixture-ticket':
                return 200, {}, b'fixture'
            self.fail('Unexpected authentication hop')
        with patch('ruc_agent.auth.send',send), patch('ruc_agent.http.MemoryHTTP.request_json',return_value={'authenticated':True}):
            client = ContractClient(identity,profile)
            client.close()
        self.assertEqual(issued,[(service,{origin})])

    def test_unsupported_constraints_and_protected_academic_rpc_reject_at_registration(self):
        spec = deepcopy(self.registry.specs['academic.calendar'])
        spec['id'] = 'fixture.bad'
        spec['parameters']['oneOf'] = []
        with self.assertRaises(QueryError):
            self.registry.register({'capabilities':[spec]})
        del spec['parameters']['oneOf']
        from ruc_agent.academic import CODES
        spec['request']['apiCode'] = CODES['grades']
        with self.assertRaises(QueryError):
            self.registry.register({'capabilities':[spec]})


class BrowserPolicyTests(unittest.TestCase):
    def test_post_read_allowed_get_mutation_unknown_and_external_blocked(self):
        policy = BrowserPolicy(Registry())
        base = 'https://jyc.ruc.edu.cn'
        self.assertTrue(policy.allowed(base+'/spa/static/api/book/getRoomList', 'POST', 'xhr'))
        self.assertFalse(policy.allowed(base+'/spa/static/api/user/cancelBook/1', 'GET', 'xhr'))
        self.assertFalse(policy.allowed(base+'/unknown', 'GET', 'xhr'))
        self.assertTrue(policy.allowed(base+'/space/js/new-hash.js', 'GET', 'script'))
        with self.assertRaises(QueryError):
            policy.add_entry(base+'/spa/static/api/user/cancelBook/1')
        for url in ['http://jyc.ruc.edu.cn/', 'https://ruc.edu.cn.evil.invalid/', 'https://u:p@jyc.ruc.edu.cn/', 'https://127.0.0.1/']:
            with self.assertRaises(QueryError):
                school_origin(url)


if __name__ == '__main__':
    unittest.main()
