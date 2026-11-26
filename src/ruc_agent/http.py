"""Bounded HTTPS transport. Cookies exist only in an explicitly owned memory jar."""
import http.cookiejar
import json
import urllib.error
import urllib.parse
import urllib.request

from .errors import QueryError

REDIRECTS = {301, 302, 303, 307, 308}


def redirect_url(base, location):
    if not isinstance(location, str) or any(ord(c) < 32 for c in location):
        raise QueryError('UNEXPECTED_REDIRECT', '平台重定向包含无效控制字符。')
    # Some school OAuth endpoints emit a literal space between scopes.
    return urllib.parse.quote(urllib.parse.urljoin(base, location), safe=":/?#[]@!$&'()*+,;=%")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def send(client, request):
    try:
        try:
            response = client.open(request, timeout=25)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            body = response.read(8 * 1024 * 1024 + 1)
            if len(body) > 8 * 1024 * 1024:
                raise QueryError('RESPONSE_TOO_LARGE', '官网响应超过预期大小。')
            return response.code, response.headers, body
    except (urllib.error.URLError, TimeoutError, OSError):
        raise QueryError('NETWORK_ERROR', '无法连接学校接口。') from None


def canonical_origin(url):
    parts = urllib.parse.urlsplit(url)
    try:
        host, port = parts.hostname, parts.port
    except ValueError:
        return ''
    if not host or parts.username or parts.password:
        return ''
    host = '[' + host + ']' if ':' in host else host
    default = 443 if parts.scheme == 'https' else 80 if parts.scheme == 'http' else None
    return parts.scheme + '://' + host + (':' + str(port) if port is not None and port != default else '')


def check_url(url, origins):
    if not isinstance(url, str) or any(ord(c) < 32 for c in url):
        raise QueryError('UNEXPECTED_REDIRECT', '请求地址包含无效控制字符。')
    parts = urllib.parse.urlsplit(url)
    if (parts.scheme != 'https' or parts.username or parts.password
            or not canonical_origin(url) or canonical_origin(url) not in {canonical_origin(v) for v in origins}):
        try:
            port = parts.port
        except ValueError:
            port = 'invalid'
        raise QueryError('UNEXPECTED_REDIRECT', '请求目标不在该平台已核实的 HTTPS 源站内。',
                         {'scheme': parts.scheme, 'hostname': parts.hostname, 'port': port})
    return parts._replace(netloc=urllib.parse.urlsplit(canonical_origin(url)).netloc)


def decode_json(status, body):
    if status == 401 or status in REDIRECTS:
        raise QueryError('LOGIN_REQUIRED', '平台要求重新认证；本次查询停止。')
    if status == 403:
        raise QueryError('ACCESS_DENIED', '平台未授权本次查询。')
    if status == 429:
        raise QueryError('RATE_LIMITED', '官网限制了请求频率；本次查询停止。')
    if status != 200:
        raise QueryError('HTTP_ERROR', f'官网返回 HTTP {status}。')
    try:
        return json.loads(body)
    except (ValueError, UnicodeError):
        raise QueryError('INVALID_RESPONSE', '官网未返回可解析的 JSON。') from None


class MemoryHTTP:
    def __init__(self, origin):
        self.origin = canonical_origin(origin)
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), NoRedirect())

    def json(self, path, body=None, headers=None):
        return self.request_json(path, 'POST' if body is not None else 'GET', body=body, headers=headers)

    def request_json(self, path, method='GET', query=None, body=None, headers=None, encoding='json'):
        status, _, raw = self.request_bytes(path, method, query, body, headers, encoding)
        return decode_json(status, raw)

    def request_bytes(self, path, method='GET', query=None, body=None, headers=None, encoding='json'):
        url = urllib.parse.urljoin(self.origin + '/', path)
        check_url(url, {self.origin})
        if query:
            url += ('&' if '?' in url else '?') + urllib.parse.urlencode(query, doseq=True)
        request_headers = {'Accept': 'application/json', **(headers or {})}
        if body is not None:
            if encoding == 'form':
                body = urllib.parse.urlencode(body, doseq=True).encode()
                request_headers['Content-Type'] = 'application/x-www-form-urlencoded'
            else:
                body = json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode()
                request_headers['Content-Type'] = 'application/json'
        return send(self.opener, urllib.request.Request(url, data=body, headers=request_headers, method=method))

    def cookie(self, name):
        host = urllib.parse.urlsplit(self.origin).hostname
        values = {c.value for c in self.jar if c.name == name and not c.is_expired()
                  and (host == c.domain.lstrip('.') or
                       (c.domain_specified and host.endswith('.' + c.domain.lstrip('.'))))}
        if len(values) != 1:
            raise QueryError('LOGIN_REQUIRED', '平台未建立唯一的本次业务登录 Cookie。')
        return values.pop()

    def close(self):
        self.jar.clear()
