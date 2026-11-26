import base64
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from ruc_agent.errors import QueryError
from ruc_agent.party_school import ORIGIN, PartySchoolClient, account_token
from ruc_agent.urls import application_url


def callback(value):
    return ORIGIN + '/front/login?' + urlencode({'info': base64.b64encode(json.dumps(value).encode()).decode()})


class PartySchoolTests(unittest.TestCase):
    def test_single_account_exchange_and_identity_assertion(self):
        final = callback([{'token':'fixture-token','account':'fixture-student'}])
        http = SimpleNamespace(origin=ORIGIN, close=Mock(), request_json=Mock(side_effect=[
            {'code':200,'data':{'url':'https://cas.ruc.edu.cn/cas/oauth2.0/authorize?redirect_uri='+ORIGIN+'/apihome/login/ruc/callback'}},
            {'code':200,'data':{'user_code':'fixture-student'}}]))
        def login(http, bound, entry, service):
            bound.ticket(service)
            return final
        identity = SimpleNamespace(ticket=Mock(return_value='fixture-st'))
        with patch('ruc_agent.party_school.MemoryHTTP', return_value=http), \
             patch('ruc_agent.party_school.cas_login', side_effect=login):
            client = PartySchoolClient(identity)
        self.assertEqual(client.headers, {'authkey':'fixture-token'})
        self.assertEqual(http.request_json.call_args.kwargs['headers'], client.headers)
        identity.ticket.assert_called_once()
        client.close()
        self.assertEqual(client.headers,{})

    def test_multiple_or_invalid_accounts_never_select_a_role_implicitly(self):
        for value in [[], [{'token':'one'},{'token':'two'}], {'token':'one'}, [{}], [{'token':'bad\nheader'}]]:
            with self.subTest(value=value), self.assertRaises(QueryError): account_token(callback(value))
        for url in [ORIGIN+'/front/login?info=not-base64', 'https://foreign.invalid/front/login?info=secret',
                    ORIGIN+'/front/login?error=denied']:
            with self.assertRaises(QueryError): account_token(url)

    def test_encoded_credentials_are_removed_from_application_links(self):
        url=callback([{'token':'fixture-token'}])
        self.assertEqual(application_url(url),ORIGIN+'/front/login')
        self.assertEqual(application_url(ORIGIN+'/article?info=public'),ORIGIN+'/article?info=public')
