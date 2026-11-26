"""Library discovery authentication: CAS → campus passport → OPAC JWT."""
import re
from urllib.parse import parse_qs, urlencode, urlsplit

from .auth import cas_login
from .errors import QueryError
from .http import MemoryHTTP
from .parsing import hidden_value

ORIGIN = 'https://opac.ruc.edu.cn'
SERVICE = ORIGIN + '/sso-cas/cas/index/ruc'
CALLBACK = ORIGIN + '/find/sso/login/ruc/0'
ENTRY = ORIGIN + '/sso-cas/cas/ruc/init?' + urlencode({'refer': CALLBACK})


def check_callback(value):
    parts = urlsplit(value or '')
    query = parse_qs(parts.query)
    return (parts.scheme == 'https' and parts.netloc == 'opac.ruc.edu.cn'
            and parts.path == '/find/sso/login/ruc/0' and not parts.fragment
            and set(query) <= {'data', 'time', 'enc'} and all(len(v) == 1 for v in query.values()))


def html_redirect(target, body):
    if target.path == '/sso-cas/cas/ruc/init':
        destination = hidden_value(body, 'ref')
        parts = urlsplit(destination or '')
        if (parts.scheme != 'https' or parts.netloc != 'cas.ruc.edu.cn' or parts.path != '/cas/login'
                or parse_qs(parts.query).get('service') != [SERVICE]):
            raise QueryError('AUTH_FLOW_CHANGED', '馆藏系统 CAS 隐藏跳转与已核实的回调不同。')
        return destination
    if target.path == '/sso-cas/cas/index/ruc':
        destination = hidden_value(body, 'ref')
        parts = urlsplit(destination or '')
        query = parse_qs(parts.query)
        if (parts.scheme != 'https' or parts.netloc != 'opac.ruc.edu.cn'
                or parts.path != '/passport/api/v2/casreceive' or len(query.get('refer', [])) != 1
                or not check_callback(query['refer'][0])):
            raise QueryError('AUTH_FLOW_CHANGED', '馆藏 CAS 返回了未经核实的身份转交地址。')
        return destination
    if target.path == '/passport/api/v2/casreceive':
        destination = hidden_value(body, 'refer')
        if (hidden_value(body, 'status') != 'true' or hidden_value(body, 'updatePhoneUrl') != ''
                or not check_callback(destination)):
            raise QueryError('AUTH_FLOW_CHANGED', '馆藏身份转交要求额外验证或回调发生变化。')
        return destination
    return None


class OpacClient:
    def __init__(self, identity):
        self.http, self.headers = MemoryHTTP(ORIGIN), {}
        try:
            class BoundIdentity:
                def ticket(self, service):
                    if service != SERVICE:
                        raise QueryError('AUTH_FLOW_CHANGED', '馆藏 CAS 服务地址改变。')
                    return identity.ticket(service, {ORIGIN})
            final = cas_login(self.http, BoundIdentity(), ENTRY, SERVICE, html_redirect=html_redirect)
            tokens = parse_qs(urlsplit(final).fragment.partition('?')[2])
            names, values = tokens.get('jwtHeader', []), tokens.get('jwt', [])
            if (len(names) != 1 or len(values) != 1 or not values[0]
                    or not re.fullmatch(r'[A-Za-z][A-Za-z_0-9-]{0,80}', names[0])
                    or names[0].lower() in {'cookie', 'host', 'set-cookie'}):
                raise QueryError('AUTH_FLOW_CHANGED', '馆藏系统未返回唯一的本次 JWT 业务凭证。')
            self.headers[names[0]] = values[0]
            user = self.http.request_json('/oga/userinfo', headers=self.headers)
            if (not isinstance(user, dict) or user.get('success') is not True
                    or not isinstance(user.get('data'), dict) or not user['data'].get('userId')):
                raise QueryError('LOGIN_REQUIRED', '馆藏系统未确认本次用户身份。')
        except BaseException:
            self.close()
            raise

    def close(self):
        self.headers.clear()
        self.http.close()
