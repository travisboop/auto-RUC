import base64
from copy import deepcopy
import importlib.util
import json
import unittest
from unittest.mock import patch

from ruc_agent.academic import AcademicClient, CODES, academic_data, calendar_date, qz_encode, query_body
from ruc_agent.data import paged
from ruc_agent.errors import QueryError
from ruc_agent.library import decrypt_hmac_key, signature_headers
from ruc_agent.portal import decode_jsonp, public_entry
from ruc_agent.sports import SportsClient, availability, signing_config, sports_data
from ruc_agent import tools


class AcademicTests(unittest.TestCase):
    def test_calendar_epoch_milliseconds_use_beijing_and_bad_schema_is_not_closed(self):
        self.assertEqual(calendar_date(1788710400000), '2026-09-07')
        self.assertEqual(calendar_date('2030-11-25'), '2030-11-25')
        for value in [None, {}, 'invalid-date', True]:
            with self.assertRaises(QueryError):
                calendar_date(value)

    def client(self, replies):
        client = AcademicClient.__new__(AcademicClient)
        client.student_id = 'fixture-student'
        client.calls = []
        def rpc(op, body):
            client.calls.append((op, deepcopy(body)))
            value = replies[op]
            return value(body) if callable(value) else deepcopy(value)
        client.rpc = rpc
        return client

    def test_query_encoding_matches_current_frontend_javascript_vector(self):
        vector = 'QZDATASOFTJddJJVIJY29uZGl0aW9uR3JvdXAlMjIlM0ElNUIlN0IlMjJsaW5rJTIyJTNBJTIyYW5kJTIyJTJDJTIyY29uZGl0aW9uJTIyJTNBJTVCJTVEJTdEyTTECTTE'
        self.assertEqual(query_body({'x': 1})['page']['conditions'], vector)
        self.assertNotIn('custom', query_body({'x': 1}))

    def test_grade_visibility_restriction_is_preserved(self):
        c = self.client({'dictionary': {'JwPublicXnxq': [{'id': 'term1', 'name': '测试学期'}]},
                         'grade_gate': {'sfckcjcode': '0', 'kthList': ['allowed-course']},
                         'grades': [{'kcname': '测试课', 'zcjname1': '90', 'token': 'never-output'}]})
        out = c.grades({})
        self.assertEqual(out['status'], 'partial')
        self.assertEqual(c.calls[-1][1]['kthList'], ['allowed-course'])
        self.assertEqual(out['records'][0]['zcjname1'], '90')
        self.assertNotIn('never-output', json.dumps(out))

    def test_rankings_return_only_self_and_preserve_school_metric(self):
        c = self.client({'ranking_terms': [{'id': 't1', 'name': '学期一'}, {'id': 't2', 'name': '学期二'}],
                         'rankings': [{'xh': 'other', 'pm': 1}, {'xh': 'fixture-student', 'pm': 4, 'bjpm': 2, 'pjxfjd': '3.8'}]})
        out = c.rankings({'term': '学期一'})
        self.assertEqual(out['records'], [{'pjxfjd': '3.8', 'pm': 4, 'bjpm': 2}])
        self.assertEqual(c.calls[-1][1]['jczy013idList'], ['t1'])
        self.assertNotIn('fixture-student', json.dumps(out))

    def test_rankings_without_identity_is_not_assumed_to_be_self(self):
        c = self.client({'ranking_terms': [{'id': 't1', 'name': '学期'}], 'rankings': [{'pm': 1}]})
        with self.assertRaises(QueryError):
            c.rankings({})

    def test_classrooms_use_calendar_and_exact_periods(self):
        c = self.client({'dictionary': {'JwPublicXnxq': [{'id': 't1', 'name': '测试学期', 'sfdqxqcode': '1'}],
                                        'JwPublicXq': [{'id': 'c1', 'name': '通州'}]},
                         'calendar': {'jxzllist': [{'rq': '2030-11-25', 'zc': 12, 'xq': 1}]},
                         'periods': [{'pkgl00201list': [{'zyxjs': '1,2', 'djshow': '第1-2节', 'idjkssj': '0800', 'idjjssj': '0930'}]}],
                         'buildings': [{'id': 'b1', 'name': '测试楼'}],
                         'classrooms': {'items': [{'js_name': 'A101', 'xq_name': '通州'}], 'rowCount': 1}})
        out = c.classrooms({'date': '2030-11-25', 'periods': ['1,2']})
        self.assertEqual(out['rooms'][0]['js_name'], 'A101')
        body = c.calls[-1][1]
        self.assertEqual((body['zc'], body['xq']), ('12', '1'))
        self.assertEqual(body['djsj'], [{'djkssj': '0800', 'djjssj': '0930'}])

    def test_backend_rejection_is_never_empty_data(self):
        for value in [{'errorCode': '401'}, {'errorCode': 'no-permission'}, {}, []]:
            with self.assertRaises(QueryError):
                academic_data(value)

    def test_rpc_cannot_invoke_unknown_or_external_api(self):
        c = AcademicClient.__new__(AcademicClient)
        c.api = {CODES['grades']: 'https://example.invalid/collect'}
        with self.assertRaises(QueryError):
            c.rpc('grades', {})
        with self.assertRaises(QueryError):
            c.rpc('delete', {})


class DataTests(unittest.TestCase):
    def test_missing_changed_and_duplicate_pages_fail(self):
        scenarios = [
            [{'items': [{'id': 1}], 'total': 2}, {'items': [], 'total': 2}],
            [{'items': [{'id': 1}], 'total': 2}, {'items': [{'id': 2}], 'total': 3}],
            [{'items': [{'id': 1}], 'total': 2}, {'items': [{'id': 1}], 'total': 2}]]
        for pages in scenarios:
            with self.assertRaises(QueryError):
                paged(lambda p, n: pages[p - 1], 'items', 'total', identity_key='id')

    def test_jsonp_is_parsed_without_executing_code(self):
        self.assertEqual(decode_jsonp(200, b'rucAgent({"result":"1"});'), {'result': '1'})
        with self.assertRaises(QueryError):
            decode_jsonp(200, b'rucAgent({});process.exit(0)')
        self.assertEqual(public_entry('https://example.edu/app?ticket=secret#/page?token=secret'),
                         'https://example.edu/app#/page')

    def test_library_hmac_matches_fixed_vector(self):
        result = signature_headers(b'test-key', timestamp=123, request_id='fixture')
        import hmac, hashlib
        self.assertEqual(result['X-hmac-request-key'], hmac.new(b'test-key', b'seat::fixture::123::POST', hashlib.sha256).hexdigest())

    @unittest.skipUnless(importlib.util.find_spec('cryptography'), 'library extra not installed in this Python')
    def test_library_decrypts_server_key_and_rejects_invalid_padding(self):
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        key, iv = '0123456789abcdef', 'abcdef0123456789'
        encryptor = Cipher(algorithms.AES(key.encode()), modes.CBC(iv.encode())).encryptor()
        encrypted = encryptor.update(b'fixture-key' + b'\x05' * 5) + encryptor.finalize()
        self.assertEqual(decrypt_hmac_key(base64.b64encode(encrypted).decode(), key, iv), b'fixture-key')
        with self.assertRaises(QueryError):
            decrypt_hmac_key('invalid-base64', key, iv)

    def test_sports_signed_config_is_data_not_evaluated_code(self):
        source = 'f121:function(){s:["c2VjcmV0"],a:["==QeltWLwBXY"]}'
        self.assertEqual(signing_config(source), ('app-key', 'secret'))
        with self.assertRaises(QueryError):
            signing_config('changed')
        with self.assertRaises(QueryError):
            signing_config('f121:function(){changed:[]}')

    def test_sports_authenticated_directory_handles_null_site_id_and_duplicates(self):
        client = SportsClient.__new__(SportsClient)
        row = {'id': 7, 'siteId': None, 'siteName': '羽毛球', 'venueName': '测试馆', 'campusName': '测试校区'}
        with patch.object(client, '_request', return_value={'venueSiteInfo': {'1': [row]}}):
            sites = client.authenticated_catalog({'site_id': 7})
        self.assertEqual(sites[0]['siteId'], 7)
        with patch.object(client, '_request', return_value={'venueSiteInfo': {'1': [row, row]}}), self.assertRaises(QueryError):
            client.authenticated_catalog({})

    def test_sports_unknown_and_login_states_are_errors(self):
        for code in [408, 250]:
            with self.assertRaises(QueryError):
                sports_data({'code': code, 'data': None})

    def test_sports_no_schedule_and_disabled_null_count_are_distinct(self):
        data = {'reservationDateList': ['2099-01-01'], 'isTakeUp': 0,
                'spaceTimeInfo': None, 'reservationDateSpaceInfo': None}
        self.assertEqual(availability(data, '2099-01-01')['status'], 'not_scheduled')
        del data['spaceTimeInfo']
        with self.assertRaises(QueryError):
            availability(data, '2099-01-01')

        data.update(reservationTotalNum=1, spaceTimeInfo=[{'id': 1, 'beginTime': '08:00', 'endTime': '09:00'}],
                    reservationDateSpaceInfo={'2099-01-01': [{'id': 1, '1': {'reservationStatus': 2, 'alreadyNum': None}}]})
        slot = availability(data, '2099-01-01')['spaces'][0]['slots'][0]
        self.assertFalse(slot['free'])
        self.assertIsNone(slot['occupied'])
        data['reservationTotalNum'] = 35
        data['reservationDateSpaceInfo']['2099-01-01'][0]['1']['reservationStatus'] = 1
        with self.assertRaises(QueryError):
            availability(data, '2099-01-01')

    def test_sports_capacity_color_is_not_selectable_and_role_restriction_wins(self):
        data = {'reservationDateList': ['2099-01-01'], 'isTakeUp': 0, 'reservationTotalNum': 35,
                'spaceTimeInfo': [{'id': 1, 'beginTime': '08:00', 'endTime': '09:00'}],
                'reservationDateSpaceInfo': {'2099-01-01': [{'id': 1, '1': {'reservationStatus': 3, 'alreadyNum': 32}}]}}
        self.assertFalse(availability(data, '2099-01-01')['spaces'][0]['slots'][0]['free'])
        data['notReserveType'] = 1
        self.assertEqual(availability(data, '2099-01-01')['status'], 'not_reservable')

    def test_sports_exact_interval_must_be_same_space_and_full_grid(self):
        data = {'reservationDateList': ['2099-01-01'], 'isTakeUp': 0, 'reservationTotalNum': 1,
                'spaceTimeInfo': [{'id': 1, 'beginTime': '08:00', 'endTime': '09:00'},
                                  {'id': 2, 'beginTime': '09:00', 'endTime': '10:00'}],
                'reservationDateSpaceInfo': {'2099-01-01': [
                    {'id': 1, 'spaceName': 'A', '1': {'reservationStatus': 1}, '2': {'reservationStatus': 4}},
                    {'id': 2, 'spaceName': 'B', '1': {'reservationStatus': 4}, '2': {'reservationStatus': 1}}]}}
        self.assertEqual(availability(data, '2099-01-01', (480, 600))['spaces'], [])
        self.assertEqual(len(availability(data, '2099-01-01', (480, 540))['spaces']), 1)
        self.assertEqual(availability(data, '2099-01-02')['status'], 'date_not_open')
        del data['reservationDateSpaceInfo']['2099-01-01'][0]['1']
        with self.assertRaises(QueryError):
            availability(data, '2099-01-01')


class NewToolTests(unittest.TestCase):
    def test_all_batch_arguments_validate_before_credentials_or_network(self):
        bad = [{'capability': 'grades'}, {'capability': 'classrooms', 'params': {'periods': []}}]
        with patch.object(tools, 'read_credentials', side_effect=AssertionError('No credentials')):
            with self.assertRaises(QueryError):
                tools.invoke('ruc_query_batch', {'requests': bad})

    def test_capability_discovery_contains_all_six_data_types(self):
        result = tools.invoke('ruc_capabilities', {})
        self.assertTrue({'quiet_booths', 'library_seats', 'sports_venues', 'classrooms', 'grades', 'rankings'}
                        <= {c['id'] for c in result['capabilities']})


if __name__ == '__main__':
    unittest.main()
