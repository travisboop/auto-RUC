"""Fresh 微人大 authentication and its separate OAuth realm."""
from html.parser import HTMLParser
import json
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.request import Request

from .errors import QueryError
from .http import MemoryHTTP, REDIRECTS, check_url, decode_json, redirect_url, send

ORIGIN = 'https://v.ruc.edu.cn'


def oauth_consent(body, target, callback):
    """Only the observed profile/userinfo sign-in consent, never arbitrary forms."""
    class Consent(HTMLParser):
        def __init__(self):
            super().__init__()
            self.forms, self.values, self.allow = [], {}, False

        def handle_starttag(self, tag, attrs):
            fields = dict(attrs)
            if tag == 'form':
                self.forms.append((fields.get('method', '').upper(), fields.get('action', '')))
            if tag == 'input' and fields.get('type') == 'hidden':
                name = fields.get('name')
                if name in self.values:
                    raise ValueError()
                self.values[name] = fields.get('value', '')
            if tag == 'button' and fields.get('name') == 'allow' and fields.get('value') == 'true':
                self.allow = True

    try:
        form = Consent()
        form.feed(body.decode('utf-8'))
        query = parse_qs(urlsplit(target).query)
        values = form.values
        if (len(form.forms) != 1 or form.forms[0][0] != 'POST'
                or urljoin(target, form.forms[0][1]) != target or not form.allow
                or values.get('redirect_uri') != callback
                or query.get('client_id') != [values.get('client_id')]
                or values.get('response_type') != 'code'
                or set(values) - {'client_id', 'response_type', 'access_type', 'redirect_uri', 'scope', 'state'}
                or not set(values.get('scope', '').split()) <= {'profile', 'userinfo'}
                or not set(values.get('scope', '').split()) <= set(' '.join(query.get('scope', [])).split())):
            raise ValueError()
        return {**values, 'allow': 'true'}
    except (ValueError, UnicodeError):
        raise QueryError('AUTH_FLOW_CHANGED', 'OAuth 页面不是已核实的学校基本身份登录授权。') from None


def csrf_token(body):
    class Fields(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tokens = []

        def handle_starttag(self, tag, attrs):
            fields = dict(attrs)
            if tag == 'input' and fields.get('name') == 'csrftoken':
                self.tokens.append(fields.get('value'))

    parser = Fields()
    try:
        parser.feed(body.decode('utf-8'))
    except UnicodeError:
        raise QueryError('AUTH_FLOW_CHANGED', '微人大登录页面编码改变。') from None
    if len(parser.tokens) != 1 or not parser.tokens[0]:
        raise QueryError('AUTH_FLOW_CHANGED', '微人大未提供唯一的本次登录校验字段。')
    return parser.tokens[0]


class LegacyIdentity:
    def __init__(self, credentials):
        self.http = MemoryHTTP(ORIGIN)
        self._credentials = credentials
        self._pending = None
        self._failure = None
        self.authenticated = False
        self.closed = False
        self.credential_submissions = 0

    def prepare(self, verification_code=None):
        if self.closed:
            raise QueryError('SESSION_CLOSED', '微人大认证会话已经关闭。')
        if self._failure:
            raise self._failure
        if self.authenticated:
            return
        if self._pending is None:
            if verification_code:
                raise QueryError('NO_AUTH_CHALLENGE', '先取得本次验证码，再提交答案。')
            status, _, body = send(self.http.opener, ORIGIN + '/auth/login?school_code=ruc')
            if status != 200:
                raise QueryError('AUTH_FLOW_CHANGED', '无法读取微人大本次登录页。')
            token = csrf_token(body)
            challenge = self.http.request_json('/auth/captcha')
            if not isinstance(challenge, dict) or challenge.get('check') not in {'yes', 'no'}:
                raise QueryError('AUTH_FLOW_CHANGED', '微人大验证码协议发生变化。')
            self._pending = {'token': token, 'challenge': challenge}
        challenge = self._pending['challenge']
        if challenge['check'] == 'yes':
            image = challenge.get('b64s', '')
            if not isinstance(image, str) or not image.startswith('data:image/') or not challenge.get('id'):
                raise QueryError('AUTH_FLOW_CHANGED', '微人大没有返回有效的验证码图片。')
            if not verification_code:
                raise QueryError('CAPTCHA_REQUIRED', '识别图片后调用 ruc_authenticate 提交 verification_code；此时尚未发送密码。',
                                 {'realm': 'legacy', 'imageDataUrl': image, 'credentialSubmitted': False})
            if not re.fullmatch(r'[A-Za-z]{4,12}', verification_code):
                raise QueryError('INVALID_ARGUMENT', '微人大验证码应为字母。')
            # The current page verifies the captcha separately; do this before any password submission.
            status, _, body = self._json_form('/login/Index/check', {'code': verification_code})
            result = decode_json(status, body)
            if not isinstance(result, dict) or type(result.get('code')) is not int or result['code'] != 0:
                raise QueryError('VERIFICATION_REJECTED', '验证码未通过，尚未提交密码。')
        try:
            username, password = self._credentials()
            payload = {'username': 'ruc:' + username, 'password': password,
                       'code': verification_code or '', 'remember_me': 'false', 'redirect_uri': '/',
                       'twofactor_password': '', 'twofactor_recovery': '',
                       'token': self._pending['token'], 'captcha_id': challenge.get('id', '')}
            self.credential_submissions += 1
            status, _, raw = self._json_form('/auth/login', payload)
            payload = password = None
            if status not in range(200, 300):
                raise QueryError('PASSWORD_AUTH_REJECTED', f'微人大未接受此次认证（HTTP {status}），不自动重试密码。')
            result = decode_json(200, raw)
            if not isinstance(result, dict):
                raise QueryError('AUTH_FLOW_CHANGED', '微人大登录响应格式改变。')
            target = urljoin(ORIGIN, result.get('redirect_uri', ''))
            if not result.get('redirect_uri'):
                raise QueryError('AUTH_FLOW_CHANGED', '微人大没有返回登录回调。')
            for _ in range(12):
                parts = check_url(target, {ORIGIN})
                status, headers, _ = send(self.http.opener, target)
                if status in REDIRECTS and headers.get('Location'):
                    target = redirect_url(target, headers['Location'])
                elif status == 200 and parts.path not in {'/auth/login', '/account/login'}:
                    self.authenticated = True
                    return
                else:
                    raise QueryError('LOGIN_REQUIRED', '微人大未建立本次登录会话。')
            raise QueryError('AUTH_FLOW_CHANGED', '微人大登录跳转次数超过预期。')
        except QueryError as error:
            self._failure = error
            raise
        finally:
            self._pending = self._credentials = None

    def _json_form(self, path, payload):
        # The school's page sends JSON while explicitly declaring this content type.
        request = Request(ORIGIN + path, data=json.dumps(payload).encode(), method='POST',
                          headers={'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'})
        return send(self.http.opener, request)

    def login(self, http, entry, callback):
        self.prepare()
        check_url(callback, {http.origin})
        target, consented = entry, False
        for _ in range(16):
            parts = check_url(target, {ORIGIN, http.origin})
            if parts.scheme + '://' + parts.netloc == ORIGIN:
                if parts.path != '/oauth2/authorize' or parse_qs(parts.query).get('redirect_uri') != [callback]:
                    raise QueryError('AUTH_FLOW_CHANGED', '微人大 OAuth 跳转与该平台已核实的回调不一致。')
                opener = self.http.opener
            else:
                opener = http.opener
            status, headers, body = send(opener, target)
            if status == 200 and parts.netloc == urlsplit(ORIGIN).netloc and not consented:
                values = oauth_consent(body, target, callback)
                consented = True
                status, headers, _ = send(opener, Request(target, data=urlencode(values).encode(), method='POST',
                                            headers={'Content-Type': 'application/x-www-form-urlencoded'}))
            if status in REDIRECTS and headers.get('Location'):
                target = redirect_url(target, headers['Location'])
            elif status == 200 and parts.netloc == urlsplit(http.origin).netloc:
                return target, body
            else:
                raise QueryError('AUTH_FLOW_CHANGED', f'微人大 OAuth 未完成该平台的登录（HTTP {status}）。')
        raise QueryError('AUTH_FLOW_CHANGED', '微人大 OAuth 跳转次数超过预期。')

    def close(self):
        self.closed = True
        self.authenticated = False
        self._credentials = self._pending = None
        self.http.close()
