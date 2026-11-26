"""Future classroom's nested union OAuth and existing-account docking login."""
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from .auth import CAS, OAUTH_SERVICE, cas_login
from .errors import QueryError
from .http import MemoryHTTP, REDIRECTS, check_url, redirect_url, send

ORIGIN = 'https://k.ruc.edu.cn'
AUTHORIZE = '/oauth2/api/oauth/authorize'
CALLBACK = '/jiakt/login/oauth/callback'
DOCKING = '/docking_auto.html'
ENTRY = ORIGIN + '/jiakt/login/oauth?' + urlencode({
    'type': 'union', 'redirect': ORIGIN + DOCKING + '?auto=1&targetUrl=' + ORIGIN + '/'})


def one(query, key):
    values = query.get(key, [])
    if (len(values) != 1 or not values[0]
            or any(ord(c) < 32 or ord(c) == 127 for c in values[0])):
        raise QueryError('AUTH_FLOW_CHANGED', '未来课堂没有返回唯一的认证字段。')
    return values[0]


def follow(http, url, paths, landing, stop_before=False):
    for _ in range(8):
        part = check_url(url, {ORIGIN})
        if part.path not in paths:
            raise QueryError('AUTH_FLOW_CHANGED', '未来课堂站内认证路径发生变化。')
        if part.path == landing and stop_before:
            return url
        status, headers, _ = send(http.opener, url)
        if status in REDIRECTS and headers.get('Location'):
            url = redirect_url(url, headers['Location'])
        elif status == 200 and part.path == landing:
            return url
        else:
            raise QueryError('SSO_ERROR', f'未来课堂站内认证没有完成（HTTP {status}）。')
    raise QueryError('SSO_ERROR', '未来课堂站内认证跳转次数超过预期。')


class FutureClassroomClient:
    def __init__(self, identity):
        self.http = MemoryHTTP(ORIGIN)
        self.headers, self.context = {}, {}
        try:
            follow(self.http, ENTRY, {'/jiakt/login/oauth', AUTHORIZE, '/oauth2/login'}, '/oauth2/login')

            class BoundIdentity:
                def ticket(self, service):
                    if service != OAUTH_SERVICE:
                        raise QueryError('AUTH_FLOW_CHANGED', '未来课堂 CAS 回调发生变化。')
                    return identity.ticket(service, {CAS})

            final = cas_login(self.http, BoundIdentity(), ORIGIN + '/oauth2/api/login/oauth?type=ruc', OAUTH_SERVICE)
            parts = check_url(final, {ORIGIN})
            if parts.path != '/oauth2/login':
                raise QueryError('AUTH_FLOW_CHANGED', '未来课堂未返回联合认证页面。')
            query = parse_qs(parts.query)
            if one(query, 'bind') != '1':
                raise QueryError('ACCOUNT_BINDING_REQUIRED', '未来课堂要求注册或绑定账号；本次登录停止。')
            # Its current handleSiteJump decodes the entire suffix, including
            # authorize query fields outside redirect_url's first ampersand.
            if final.count('redirect_url=') != 1:
                raise QueryError('AUTH_FLOW_CHANGED', '未来课堂缺少唯一的站内 OAuth 跳转。')
            target = unquote(final.partition('redirect_url=')[2])
            target_parts = check_url(target, {ORIGIN})
            if target_parts.path != AUTHORIZE:
                raise QueryError('AUTH_FLOW_CHANGED', '未来课堂联合认证目标发生变化。')
            callback = one(parse_qs(target_parts.query), 'redirect_uri')
            callback_parts = check_url(callback, {ORIGIN})
            if callback_parts.path != CALLBACK or parse_qs(callback_parts.query).get('type') != ['union']:
                raise QueryError('AUTH_FLOW_CHANGED', '未来课堂业务回调发生变化。')
            landing = follow(self.http, target, {AUTHORIZE, CALLBACK, DOCKING}, DOCKING, True)
            values = parse_qs(urlsplit(landing).query)
            token = one(values, 'token')
            result = self.http.request_json('/jiakt/adminApi/user/dockingAuth', 'POST',
                body={'serial': one(values, 'username'), 'token': token, 'loginType': 'web'},
                headers={'accessToken': token, 'Authorization': 'Bearer ' + token}, encoding='form')
            data = result.get('data') if isinstance(result, dict) else None
            if (not isinstance(data, dict) or result.get('stat') != 1
                    or not isinstance(data.get('token'), str) or not data['token']
                    or type(data.get('id')) is not int
                    or not isinstance(data.get('region'), dict) or type(data['region'].get('id')) is not int):
                raise QueryError('LOGIN_REQUIRED', '未来课堂未建立本次业务身份。')
            self.headers = {'accessToken': data['token'], 'Authorization': 'Bearer ' + data['token']}
            self.context = {'userId': data['id'], 'regionId': data['region']['id']}
            assertion = self.http.request_json('/jiakt/adminApi/user/getUserById',
                query={'userId': self.context['userId']}, headers=self.headers)
            if (not isinstance(assertion, dict) or assertion.get('stat') != 1
                    or not isinstance(assertion.get('data'), dict)
                    or assertion['data'].get('id') != self.context['userId']):
                raise QueryError('LOGIN_REQUIRED', '未来课堂本人资料身份断言失败。')
        except BaseException:
            self.close()
            raise

    def close(self):
        self.headers.clear()
        self.context.clear()
        self.http.close()
