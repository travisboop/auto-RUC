"""AI 学伴 CAS code exchange, matching its current SSO frontend."""
from urllib.parse import parse_qs, urlsplit

from .auth import cas_login
from .errors import QueryError
from .http import MemoryHTTP

ORIGIN = 'https://i.ruc.edu.cn'
SERVICE = ORIGIN + '/zgrd-wrd/login.jsp'


class AiClient:
    def __init__(self, identity):
        self.http = MemoryHTTP(ORIGIN)
        self.headers = {}
        try:
            class BoundIdentity:
                def ticket(self, service):
                    if service != SERVICE:
                        raise QueryError('AUTH_FLOW_CHANGED', 'AI 学伴 CAS 回调发生变化。')
                    return identity.ticket(service, {ORIGIN})

            target = cas_login(self.http, BoundIdentity(), ORIGIN + '/', SERVICE)
            values = parse_qs(urlsplit(target).query)
            if any(len(values.get(key, [])) != 1 for key in ['code', 'page']):
                raise QueryError('AUTH_FLOW_CHANGED', 'AI 学伴未返回唯一的本次 SSO code/page。')
            code, page = values['code'][0], values['page'][0]
            account = self.http.json('/console/api/account/profile', {'code': code})
            if not isinstance(account, dict) or not account.get('email'):
                raise QueryError('LOGIN_REQUIRED', 'AI 学伴未确认本次 SSO 身份。')
            result = self.http.json('/console/api/login', {'code': code, 'page': page,
                                    'email': account['email'], 'name': account.get('name'),
                                    'department': account.get('department')})
            data = result.get('data') if isinstance(result, dict) else None
            token = data.get('access_token') if isinstance(data, dict) else None
            if not isinstance(result, dict) or result.get('result') != 'success' or not isinstance(token, str) or not token:
                raise QueryError('LOGIN_REQUIRED', 'AI 学伴未签发本次业务令牌。')
            self.headers['Authorization'] = 'Bearer ' + token
            assertion = self.http.request_json('/console/api/account/profile', headers=self.headers)
            if not isinstance(assertion, dict) or not assertion.get('id'):
                raise QueryError('LOGIN_REQUIRED', 'AI 学伴业务身份断言失败。')
        except BaseException:
            self.close()
            raise

    def close(self):
        self.headers.clear()
        self.http.close()
