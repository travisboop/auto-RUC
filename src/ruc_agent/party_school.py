"""Party school's OAuth callback contains a base64 list of account credentials."""
import base64
import binascii
import json
from urllib.parse import parse_qs, urlsplit

from .auth import CAS, OAUTH_SERVICE, cas_login
from .errors import QueryError
from .http import MemoryHTTP, check_url

ORIGIN = 'https://zg.ruc.edu.cn'


def account_token(url):
    part = check_url(url, {ORIGIN})
    query = parse_qs(part.query)
    if part.path != '/front/login' or 'error' in query:
        raise QueryError('ACCESS_DENIED', '党校平台未授权本次账号登录。')
    info = query.get('info', [])
    if len(info) != 1 or len(info[0]) > 65536:
        raise QueryError('AUTH_FLOW_CHANGED', '党校平台未返回唯一的本次账号信息。')
    try:
        accounts = json.loads(base64.b64decode(info[0], validate=True))
    except (ValueError, UnicodeError, binascii.Error):
        raise QueryError('AUTH_FLOW_CHANGED', '党校平台认证信息无法解析。') from None
    if not isinstance(accounts, list) or len(accounts) != 1:
        raise QueryError('ACCOUNT_SELECTION_REQUIRED', '党校平台没有返回唯一账号，需明确选择后才能继续。')
    account = accounts[0]
    token = account.get('token') if isinstance(account, dict) else None
    if not isinstance(token, str) or not token or any(ord(c) < 32 or ord(c) == 127 for c in token):
        raise QueryError('AUTH_FLOW_CHANGED', '党校平台没有返回有效业务凭证。')
    return token


class PartySchoolClient:
    def __init__(self, identity):
        self.http = MemoryHTTP(ORIGIN)
        self.headers = {}
        try:
            entry = self.http.request_json('/apihome/login/ruc')
            if (not isinstance(entry, dict) or entry.get('code') != 200
                    or not isinstance(entry.get('data'), dict) or not isinstance(entry['data'].get('url'), str)):
                raise QueryError('AUTH_FLOW_CHANGED', '党校平台没有返回当前登录入口。')

            class BoundIdentity:
                def ticket(self, service):
                    if service != OAUTH_SERVICE:
                        raise QueryError('AUTH_FLOW_CHANGED', '党校平台 CAS 回调发生变化。')
                    return identity.ticket(service, {CAS})

            final = cas_login(self.http, BoundIdentity(), entry['data']['url'], OAUTH_SERVICE)
            self.headers['authkey'] = account_token(final)
            result = self.http.request_json('/apihome/user/user/info', headers=self.headers)
            if (not isinstance(result, dict) or result.get('code') != 200
                    or not isinstance(result.get('data'), dict) or not result['data'].get('user_code')):
                raise QueryError('LOGIN_REQUIRED', '党校平台未确认本次业务身份。')
        except BaseException:
            self.close()
            raise

    def close(self):
        self.headers.clear()
        self.http.close()
