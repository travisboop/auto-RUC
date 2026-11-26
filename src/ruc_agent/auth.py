"""One fresh CAS REST identity, independent business sessions, no password retries."""
import re
import urllib.parse
import urllib.request

from .errors import QueryError
from .http import NoRedirect, REDIRECTS, check_url, redirect_url, send

CAS = 'https://cas.ruc.edu.cn'
CAS_TICKETS = CAS + '/cas/v1/tickets'
OAUTH_SERVICE = CAS + '/cas/oauth2.0/callbackAuthorize'


def is_ticket_id(value):
    return value not in {'.', '..'} and re.fullmatch(r'[A-Za-z0-9._~-]{1,8192}', value) is not None


def html_cas_redirect(body):
    """Read a literal CAS-only redirect used by older school login entry pages."""
    from html import unescape
    try:
        text = body.decode('utf-8')
    except UnicodeError:
        return None
    matches = re.findall(r'''window\.location(?:\.href)?\s*=\s*['"](https://cas\.ruc\.edu\.cn/cas/login\?[^'"<>]+)['"]''', text)
    return unescape(matches[0]) if len(matches) == 1 else None


class CasIdentity:
    def __init__(self, credentials):
        self._credentials = credentials
        self._tgt_url = None
        self._failure = None
        self._pending_credentials = None
        self._closed = False
        self.credential_submissions = 0
        self.http = urllib.request.build_opener(NoRedirect())

    def prepare(self):
        if self._closed:
            raise QueryError('SESSION_CLOSED', '本次认证会话已关闭。')
        if self._failure:
            raise self._failure
        if self._tgt_url:
            return
        if self._pending_credentials is not None:
            return
        try:
            self._pending_credentials = self._credentials()
        except QueryError as error:
            self._failure = error
            raise
        finally:
            self._credentials = None

    def _authenticate(self):
        self.prepare()
        if self._tgt_url:
            return
        try:
            username, password = self._pending_credentials
            if not username or not password:
                raise QueryError('CREDENTIALS_REQUIRED', '需要学号和密码。')
            data = urllib.parse.urlencode({'username': username, 'password': password}).encode()
            request = urllib.request.Request(CAS_TICKETS, data=data,
                headers={'Content-Type': 'application/x-www-form-urlencoded'}, method='POST')
            self.credential_submissions += 1
            status, headers, _ = send(self.http, request)
            password = data = request = None
            if status in {400, 401, 403}:
                raise QueryError('PASSWORD_AUTH_REJECTED', f'CAS 未接受此次账号密码认证（HTTP {status}）。')
            if status == 429:
                raise QueryError('RATE_LIMITED', 'CAS 限制了登录频率；本程序不会自动重试密码。')
            if status != 201 or not headers.get('Location'):
                raise QueryError('REST_AUTH_UNAVAILABLE', f'CAS 未签发 REST 登录票据（HTTP {status}）。')
            target = urllib.parse.urlsplit(urllib.parse.urljoin(CAS_TICKETS + '/', headers['Location']))
            ticket_path, _, ticket_id = target.path.rpartition('/')
            if (target.scheme not in {'http', 'https'} or target.netloc != 'cas.ruc.edu.cn'
                    or target.query or target.fragment or ticket_path != '/cas/v1/tickets'
                    or not is_ticket_id(ticket_id)):
                raise QueryError('UNEXPECTED_TICKET_URL', 'CAS 返回了格式不符合预期的票据地址。')
            self._tgt_url = CAS + target.path
        except QueryError as error:
            self._failure = error
            raise
        finally:
            self._pending_credentials = None
            self._credentials = None

    def ticket(self, service, allowed_origins=None):
        # Adapters must separately match the exact registered callback before this call.
        check_url(service, allowed_origins if allowed_origins is not None else
                  {CAS, 'https://jyc.ruc.edu.cn', 'https://zwlib.ruc.edu.cn', 'https://my.ruc.edu.cn'})
        self._authenticate()
        request = urllib.request.Request(self._tgt_url,
            data=urllib.parse.urlencode({'service': service}).encode(),
            headers={'Content-Type': 'application/x-www-form-urlencoded'}, method='POST')
        status, _, body = send(self.http, request)
        if status == 429:
            raise QueryError('RATE_LIMITED', 'CAS 限制了服务票据申请频率。')
        try:
            ticket = body.decode('ascii').strip()
        except UnicodeError:
            ticket = ''
        if status != 200 or not is_ticket_id(ticket):
            raise QueryError('SERVICE_TICKET_REJECTED', f'CAS 未签发服务票据（HTTP {status}）。')
        return ticket

    def close(self):
        self._closed = True
        self._credentials = None
        self._pending_credentials = None
        url, self._tgt_url = self._tgt_url, None
        if url is None:
            return True
        try:
            status, _, _ = send(self.http, urllib.request.Request(url, method='DELETE'))
            return status in {200, 204, 404, 410}
        except QueryError:
            return False


def cas_login(http, identity, entry, service, token_path=None, upgrade_http_paths=(), html_redirect=None,
              accept_empty_redirect=False):
    """Follow an observed CAS or OAuth chain in this platform's fresh jar.

    Return the final URL only to the adapter. It can contain a one-time token and
    must never be returned to a tool caller or written to evidence.
    """
    url, issued = entry, False
    origins = {http.origin, CAS}
    def https_callback(value):
        raw = urllib.parse.urlsplit(value)
        if (raw.scheme == 'http' and 'https://' + raw.netloc == http.origin
                and raw.path in upgrade_http_paths and not raw.username and not raw.password):
            return urllib.parse.urlunsplit(raw._replace(scheme='https'))
        return value
    for _ in range(16):
        # Keep the provider's opaque redirect_uri unchanged inside the HTTPS
        # authorization request, but only contact the verified HTTPS callback.
        url = https_callback(url)
        target = check_url(url, origins)
        if target.netloc == 'cas.ruc.edu.cn':
            if target.path == '/cas/login':
                query = urllib.parse.parse_qs(target.query)
                if query.get('service') != [service] or issued:
                    raise QueryError('AUTH_FLOW_CHANGED', 'CAS 回调或认证步骤发生变化。')
                ticket = identity.ticket(service)
                url = service + ('&' if '?' in service else '?') + urllib.parse.urlencode({'ticket': ticket})
                issued = True
                continue
            if target.path not in {'/cas/oauth2.0/authorize', '/cas/oauth2.0/callbackAuthorize'}:
                raise QueryError('AUTH_FLOW_CHANGED', 'CAS 要求当前适配器未支持的认证步骤。')
            if target.path == '/cas/oauth2.0/authorize':
                redirects = urllib.parse.parse_qs(target.query).get('redirect_uri', [])
                if len(redirects) != 1:
                    raise QueryError('AUTH_FLOW_CHANGED', 'OAuth 缺少唯一回调。')
                check_url(https_callback(redirects[0]), {http.origin})
        elif issued and token_path and target.path.rstrip('/') == token_path:
            tokens = urllib.parse.parse_qs(target.query).get('token', [])
            if len(tokens) == 1 and tokens[0]:
                return url
        status, headers, body = send(http.opener, url)
        if status == 429:
            raise QueryError('RATE_LIMITED', '学校限制了认证跳转频率。')
        if status in REDIRECTS and headers.get('Location'):
            url = redirect_url(url, headers['Location'])
            continue
        if (issued and accept_empty_redirect and status in REDIRECTS
                and not headers.get('Location') and target.netloc != 'cas.ruc.edu.cn'):
            # Some callbacks establish cookies but return an empty Location. The
            # configured business identity assertion must still pass afterwards.
            return url
        if status == 200 and not issued and target.netloc != 'cas.ruc.edu.cn':
            destination = html_cas_redirect(body)
            if destination:
                url = destination
                continue
        if status == 200 and html_redirect and target.netloc == urllib.parse.urlsplit(http.origin).netloc:
            destination = html_redirect(target, body)
            if destination:
                url = redirect_url(url, destination)
                continue
        if issued and status == 200 and target.netloc != 'cas.ruc.edu.cn':
            return url
        raise QueryError('SSO_ERROR', f'平台登录回调未完成（HTTP {status}）。')
    raise QueryError('SSO_ERROR', '平台登录跳转次数超过预期。')
