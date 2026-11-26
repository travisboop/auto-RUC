"""Execute explicit request contracts using each platform's own authentication."""
from .academic import CODES
from .auth import CAS, OAUTH_SERVICE, cas_login
from .contracts import at, bind_context, decode_response, request_for
from .errors import QueryError
from .http import MemoryHTTP, check_url, decode_json


def auth_value(http, final_url, binding, response=None):
    """Resolve one credential from this login, never from caller parameters."""
    from urllib.parse import parse_qs, urlsplit
    parts = urlsplit(final_url)
    host = urlsplit(http.origin).hostname
    if binding['source'] == 'response':
        values = [at(response, binding['path'])]
    elif binding['source'] == 'cookie':
        values = [c.value for c in http.jar if c.name == binding['parameter']
                  and not c.is_expired() and (host == c.domain.lstrip('.')
                  or host.endswith('.' + c.domain.lstrip('.')))]
    else:
        query = parts.query if binding['source'] == 'query' else parts.fragment.partition('?')[2]
        values = parse_qs(query, keep_blank_values=True).get(binding['parameter'], [])
    if (len(values) != 1 or not isinstance(values[0], str) or not values[0]
            or any(ord(c) < 32 or ord(c) == 127 for c in values[0])):
        raise QueryError('AUTH_FLOW_CHANGED', '平台没有返回唯一的本次业务登录凭证。')
    return values[0]


def token_headers(http, final_url, bindings, response=None):
    return {b['header']: b.get('prefix', '') + auth_value(http, final_url, b, response)
            for b in bindings}


def read_request(http, request, response, headers=None):
    args = (request['path'], request.get('method', 'GET'), request.get('query'),
            request.get('body'), headers or {}, request.get('encoding', 'json'))
    if response.get('format', 'json') == 'json':
        return http.request_json(*args)
    status, _, body = http.request_bytes(*args)
    if status != 200:
        decode_json(status, body)
    from .parsing import embedded_json, html_snapshot
    if response['format'] == 'embedded_json':
        return embedded_json(body, response['variable'])
    return html_snapshot(body, http.origin + request['path'])


def form_bridge(http, body, bridge):
    from urllib.parse import urljoin
    from .http import REDIRECTS, redirect_url, send
    from .parsing import hidden_form

    target = http.origin + bridge['path']
    values = hidden_form(body, target, bridge['fields'], http.origin)
    status, headers, raw = http.request_bytes(bridge['path'], 'POST', body=values, encoding='form')
    for _ in range(12):
        if status == 200:
            return target, raw
        if status not in REDIRECTS or not headers.get('Location'):
            raise QueryError('AUTH_FLOW_CHANGED', '平台认证转交没有完成。')
        target = redirect_url(target, headers['Location'])
        check_url(target, {http.origin})
        status, headers, raw = send(http.opener, target)
    raise QueryError('AUTH_FLOW_CHANGED', '平台认证转交跳转次数超过预期。')


def bootstrap_session(http, bootstrap):
    """Finish a verified, same-origin component SSO using the new portal cookies."""
    from urllib.parse import urlsplit
    from .http import REDIRECTS, redirect_url, send

    target = http.origin + bootstrap['entry']
    paths = {bootstrap['entry'], *bootstrap['redirectPaths']}
    for _ in range(12):
        check_url(target, {http.origin})
        if urlsplit(target).path not in paths:
            raise QueryError('AUTH_FLOW_CHANGED', '站内认证跳转路径发生变化。')
        status, headers, body = send(http.opener, target)
        if status == 200:
            return target
        if status not in REDIRECTS or not headers.get('Location'):
            raise QueryError('AUTH_FLOW_CHANGED', '站内认证没有完成。')
        target = redirect_url(target, headers['Location'])
    raise QueryError('AUTH_FLOW_CHANGED', '站内认证跳转次数超过预期。')


class ContractClient:
    """A small CAS-cookie adapter for additional platforms with that auth protocol."""
    def __init__(self, identity, profile, legacy_identity=None):
        self.http = MemoryHTTP(profile.get('apiOrigin', profile['origin']))
        self.auth_http = self.http if self.http.origin == profile['origin'] else MemoryHTTP(profile['origin'])
        self.headers = dict(profile.get('headers', {}))
        self.auth_exchange = None
        try:
            auth = profile['auth']
            if auth['kind'] == 'public':
                self.landing_url = profile['entry']
                return
            if auth['kind'] not in {'cas_cookie', 'legacy_oauth'}:
                raise QueryError('AUTH_FLOW_UNSUPPORTED', '此平台需要独立认证适配器或浏览器。')
            service = auth['service']
            origins = {profile['origin']} | ({CAS} if service == OAUTH_SERVICE else set())
            check_url(service, origins)
            # Bind CAS tickets to this configured callback, not to arbitrary URLs.
            class BoundIdentity:
                def ticket(self, observed):
                    if observed != service:
                        raise QueryError('AUTH_FLOW_CHANGED', 'CAS 回调与平台配置不同。')
                    return identity.ticket(observed, origins)
            if auth['kind'] == 'legacy_oauth':
                if legacy_identity is None:
                    raise QueryError('AUTH_FLOW_UNSUPPORTED', '缺少微人大任务认证实例。')
                final, body = legacy_identity.login(self.auth_http, auth.get('entry', profile['entry']), service)
                if auth.get('formBridge'):
                    form_bridge(self.auth_http, body, auth['formBridge'])
            else:
                entry = auth.get('entry', profile['entry'])
                if 'entryDiscovery' in auth:
                    discovery = auth['entryDiscovery']
                    status, _, raw = self.auth_http.request_bytes(discovery['path'], query=discovery.get('query'))
                    if status not in discovery['acceptedStatuses']:
                        if status != 200:
                            decode_json(status, raw)
                        raise QueryError('AUTH_FLOW_CHANGED', '平台登录入口的响应状态发生变化。')
                    payload = decode_json(200, raw)
                    entry = at(payload, discovery['responsePath'])
                    if not isinstance(entry, str) or not entry:
                        raise QueryError('AUTH_FLOW_CHANGED', '平台没有返回本次登录入口。')
                final = cas_login(self.auth_http, BoundIdentity(), entry, service,
                                  accept_empty_redirect=auth.get('acceptEmptyRedirect', False),
                                  upgrade_http_paths=auth.get('upgradeHttpPaths', ()))
            if 'bootstrap' in auth:
                final = bootstrap_session(self.auth_http, auth['bootstrap'])
            exchanged = None
            if 'exchange' in auth:
                exchange = auth['exchange']
                values = {key: auth_value(self.http, final, binding)
                          for key, binding in exchange['parameters'].items()}
                request = request_for(exchange, values)
                exchanged = read_request(self.http, request, exchange['response'], self.headers)
                decode_response(exchange, exchanged)
                self.auth_exchange = {'origin': self.http.origin, 'request': request, 'response': exchanged}
            self.headers.update(token_headers(self.http, final, auth.get('tokenBindings', []), exchanged))
            assertion = auth['assertion']
            raw = read_request(self.http, assertion, assertion['response'], self.headers)
            decode_response({'response': assertion['response']}, raw)
            self.landing_url = final
        except BaseException:
            self.close()
            raise

    def close(self):
        self.landing_url = None
        self.auth_exchange = None
        self.headers.clear()
        self.http.close()
        if self.auth_http is not self.http:
            self.auth_http.close()


def execute(session, spec, params, all_pages=False, max_pages=100):
    profile = session.registry.profiles[spec['platform']]
    session.prepare_identity(profile)
    client = session.contract_client(profile)
    fetch = lambda values: request_data(client, profile, spec, values)
    if all_pages:
        from .pagination import collect_pages
        result = collect_pages(fetch, spec, params, max_pages)
    else:
        result = decode_response(spec, fetch(params))
    result['contractVerification'] = spec.get('verification', 'contract_only')
    if not spec['readOnly']:
        result['outcome'] = 'accepted'
        result['receiptQuery'] = spec.get('receiptQuery')
    return result


def request_data(client, profile, spec, params):
    # Resolve identity fields from this adapter only; caller parameters cannot
    # override user/tenant identifiers established by the current login.
    template = bind_context(spec['request'], getattr(client, 'context', {}))
    request = request_for({**spec, 'request': template}, params)
    adapter = profile.get('adapter')
    headers = {}
    if adapter == 'quiet_booths':
        http = client._session
        headers['Authorization'] = client.token
    else:
        http = client.http
        headers.update(getattr(client, 'headers', {}))
    if 'apiCode' in request:
        if adapter != 'academic':
            raise QueryError('INVALID_CONTRACT', 'apiCode 仅用于教务本次 API 映射。')
        # These two operations have semantic privacy/gating rules in their adapters.
        if request['apiCode'] in {CODES['grades'], CODES['rankings']}:
            raise QueryError('PROTECTED_OPERATION', '成绩和排名须使用专用能力，保留评教和本人过滤。')
        path = client.resolve_code(request['apiCode'])
    else:
        path = request['path']
        if adapter == 'academic':
            raise QueryError('INVALID_CONTRACT', '教务接口必须通过本次 API 映射的 apiCode 定位。')
    if adapter in {'library', 'study_spaces', 'sports', 'resources'}:
        if adapter in {'sports', 'resources'}:
            client.authenticate()
        raw = client.request(path, request['method'], request.get('query'), request.get('body'),
                             request.get('encoding', 'form' if adapter in {'sports', 'resources'} else 'json'))
    else:
        raw = read_request(http, {**request, 'path': path}, spec['response'], headers)
    return raw
