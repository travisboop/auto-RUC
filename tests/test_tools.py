import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime
from urllib.parse import urlsplit

from test_auth import Protocol
from test_availability import ROOM
from ruc_agent import cli, tools
from ruc_agent.booths import API, BEIJING, QueryError
from ruc_agent.credentials import read_credentials


class ToolTests(unittest.TestCase):
    def test_query_tool_runs_full_fresh_flow_and_revokes_ticket(self):
        class BookingProtocol(Protocol):
            def open(self, request, timeout):
                if not isinstance(request, str) and request.full_url.startswith(API):
                    endpoint = urlsplit(request.full_url).path.rsplit('/', 1)[-1]
                    if endpoint != 'getSchoolList':
                        if request.get_header('Authorization') != 'fixture-api-token':
                            raise AssertionError('Only newly issued API token may be used')
                        self.requests.append(request)
                        data = {
                            'getVenueList': [{'id': 2, 'name': '立德楼'}],
                            'getRoomTypeList': [{'id': 3, 'name': '单人舱'}],
                            'getOpenDateList': ['2026-11-25'],
                            'getRoomList': {'currentPage': 1, 'totalCount': 1, 'totalPage': 1, 'pageList': [ROOM]},
                        }
                        if endpoint == 'getRoomList':
                            if request.get_method() != 'POST' or json.loads(request.data)['selectDate'] != '2026-11-25':
                                raise AssertionError('Room query must POST date and pagination')
                        return self.json(data[endpoint])
                return super().open(request, timeout)

        protocol = BookingProtocol()
        fixed_now = datetime(2026, 11, 25, 12, 10, tzinfo=BEIJING)
        with patch('ruc_agent.booths.urllib.request.build_opener', protocol.build_opener), \
             patch.object(tools, 'read_credentials', return_value=('fixture-student', 'fixture-password')), \
             patch('ruc_agent.availability.datetime') as clock:
            clock.now.return_value = fixed_now
            output = tools.invoke('ruc_query_booths', {'date': '2026-11-25', 'from': '19:30', 'to': '21:30'})
        self.assertEqual(output['availableRooms'], 1)
        self.assertEqual(output['groups'][0]['rooms'][0]['windows'], [['19:30', '21:30']])
        self.assertTrue(output['restTicketRevoked'])
        self.assertNotIn('fixture-api-token', json.dumps(output))
        self.assertEqual(protocol.requests[-1].get_method(), 'DELETE')

    def test_local_tools_do_not_read_credentials(self):
        with patch.object(tools, 'read_credentials', side_effect=AssertionError('No login needed')):
            output = tools.invoke('ruc_find_apps', {'keyword': '本科'})
            self.assertGreater(output['totalMatches'], 0)
            self.assertFalse(output['live'])
            self.assertIn(output['source'], {'tools/applications.snapshot.json', 'tools/platforms.json'})
            self.assertEqual(tools.invoke('ruc_platforms', {})['status'], 'ok')

    def test_offline_discovery_without_private_application_snapshot(self):
        from ruc_agent import catalog
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'tools').mkdir()
            (root/'tools/platforms.json').write_text(json.dumps({'updatedOn':'2026-11-27','platforms':[
                {'id':'fixture','name':'测试平台','entry':'https://fixture.ruc.edu.cn/'},
                {'id':'another','name':'另一个平台','entry':'https://another.ruc.edu.cn/'}]}))
            with patch.object(catalog, 'ROOT', root):
                output = catalog.find_apps('测试', 1)
            self.assertFalse(output['live'])
            self.assertEqual(output['source'], 'tools/platforms.json')
            self.assertEqual(output['snapshotDate'], '2026-11-27')
            self.assertEqual(output['totalMatches'], 1)
            self.assertEqual(output['applications'][0]['entryFields'], {'url':'https://fixture.ruc.edu.cn/'})

    def test_noninteractive_credentials_missing_is_structured(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(QueryError) as caught:
            tools.invoke('ruc_check_login', {})
        self.assertEqual(caught.exception.code, 'CREDENTIALS_REQUIRED')

    def test_unknown_tools_and_arguments_fail_before_credentials(self):
        for name, args in [('unknown', {}), ('ruc_check_login', {'password': 'test'}),
                           ('ruc_query_booths', {'from': '12:00'}), ('ruc_query_booths', {'minutes': True})]:
            with patch.object(tools, 'read_credentials', side_effect=AssertionError('Must validate first')):
                with self.assertRaises(QueryError):
                    tools.invoke(name, args)

    def test_stdio_dispatch_and_malformed_input(self):
        for data, expected in [('not json', 1), ('[]', 1),
                               ('{"name":"ruc_platforms","arguments":{}}', 0)]:
            output = io.StringIO()
            with patch.object(cli.sys, 'stdin', io.StringIO(data)), contextlib.redirect_stdout(output):
                self.assertEqual(cli.tool_main([]), expected)
            self.assertIn('status', json.loads(output.getvalue()))

    def test_each_login_uses_new_empty_cookie_jar(self):
        protocol = Protocol()
        with patch('ruc_agent.booths.urllib.request.build_opener', protocol.build_opener), \
             patch.object(tools, 'read_credentials', return_value=('fixture-student', 'fixture-password')):
            for _ in range(2):
                output = tools.invoke('ruc_check_login', {})
                self.assertTrue(output['freshSession'])
                self.assertTrue(output['restTicketRevoked'])
        self.assertEqual(len(protocol.cookie_jars), 2)
        self.assertIsNot(protocol.cookie_jars[0], protocol.cookie_jars[1])


class CredentialTests(unittest.TestCase):
    def test_explicit_file_and_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / 'credentials.json'
            file.write_text(json.dumps({'username': 'test-user', 'password': 'test-password'}))
            file.chmod(0o600)
            self.assertEqual(read_credentials(file, interactive=False), ('test-user', 'test-password'))
            if os.name == 'posix':
                file.chmod(0o644)
                with self.assertRaises(QueryError) as caught:
                    read_credentials(file, interactive=False)
                self.assertEqual(caught.exception.code, 'CREDENTIALS_PERMISSIONS')

    def test_bad_file_contents_never_appear_in_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / 'credentials.json'
            file.write_text('{secret-fixture')
            file.chmod(0o600)
            with self.assertRaises(QueryError) as caught:
                read_credentials(file, interactive=False)
            self.assertNotIn('secret-fixture', str(caught.exception))

    def test_file_does_not_accept_existing_session_material(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / 'credentials.json'
            file.write_text(json.dumps({'cookies': [], 'token': 'fixture'}))
            file.chmod(0o600)
            with self.assertRaises(QueryError):
                read_credentials(file, interactive=False)


if __name__ == '__main__':
    unittest.main()
