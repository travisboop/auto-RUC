"""Data-only capability contracts. No eval, imports, or executable templates."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, quote, urlsplit, unquote

from .catalog import ROOT
from .errors import QueryError
from .http import check_url
from .schema import check_schema, validate


def at(value, path):
    try:
        for key in path:
            value = value[key]
        return value
    except (KeyError, IndexError, TypeError):
        raise QueryError('SCHEMA_CHANGED', '响应缺少契约要求的字段。', {'path': path}) from None


def shape(value):
    if isinstance(value, dict):
        return {k: shape(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        # Ignore counts and values; heterogeneous rows remain distinguishable.
        return sorted({json.dumps(shape(v), sort_keys=True) for v in value})
    return type(value).__name__


def fingerprint(value):
    return hashlib.sha256(json.dumps(shape(value), sort_keys=True).encode()).hexdigest()


def safe_path(path):
    parts = urlsplit(path)
    decoded = unquote(path)
    if (not path.startswith('/') or path.startswith('//') or parts.scheme or parts.netloc
            or parts.query or parts.fragment or '\\' in decoded
            or any(c in decoded for c in '\r\n\x00') or '..' in decoded.split('/')):
        raise QueryError('INVALID_CONTRACT', '接口必须使用不含查询串、片段或跳级的绝对路径。')
    return path


def render(value, params):
    if isinstance(value, dict):
        if set(value) == {'$param'}:
            if value['$param'] not in params:
                raise QueryError('INVALID_ARGUMENT', '接口模板缺少参数。', {'parameter': value['$param']})
            return deepcopy(params[value['$param']])
        if value == {'$params': True}:
            return deepcopy(params)
        return {k: render(v, params) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, params) for v in value]
    return value


def request_for(spec, params):
    request = render(spec['request'], params)
    if 'path' in request:
        def replace(match):
            key = match[1]
            if key not in params or type(params[key]) not in {str, int}:
                raise QueryError('INVALID_ARGUMENT', '接口路径缺少标量参数。')
            return quote(str(params[key]), safe='')
        request['path'] = safe_path(re.sub(r'\{([a-zA-Z_][a-zA-Z_0-9]*)\}', replace, request['path']))
    return request


def context_keys(value):
    """List authentication-derived placeholders without resolving caller input."""
    if isinstance(value, dict):
        if '$context' in value:
            if set(value) != {'$context'} or not isinstance(value['$context'], str):
                raise QueryError('INVALID_CONTRACT', '会话字段必须使用单独的 $context 模板。')
            return {value['$context']}
        return set().union(*(context_keys(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(context_keys(v) for v in value))
    return set()


def bind_context(value, context):
    if isinstance(value, dict):
        if set(value) == {'$context'}:
            key = value['$context']
            if key not in context or type(context[key]) not in {str, int, bool}:
                raise QueryError('AUTH_FLOW_CHANGED', '认证结果缺少契约要求的会话字段。', {'field': key})
            return context[key]
        return {k: bind_context(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [bind_context(v, context) for v in value]
    return value


class Registry:
    def __init__(self, extensions=None):
        base = json.loads((ROOT / 'tools/capabilities.json').read_text(encoding='utf-8'))
        self.version = base['version']
        self.specs = {s['id']: s for s in base['capabilities']}
        self.profiles = {}
        self.browser_reads = []
        files = [ROOT / 'tools/contracts.json', *sorted((ROOT / 'tools/contracts').glob('*.json'))]
        if extensions:
            folder = Path(extensions).expanduser().resolve()
            if not folder.is_dir():
                raise QueryError('INVALID_CONTRACT', '扩展目录不存在。')
            files += sorted(folder.glob('*.json'))
        for file in files:
            try:
                if file.stat().st_size > 1024 * 1024:
                    raise ValueError()
                data = json.loads(file.read_text(encoding='utf-8'))
                self.register(data)
            except (OSError, ValueError, TypeError, KeyError, QueryError) as error:
                details = {'file': file.name}
                if isinstance(error, QueryError):
                    details['cause'] = error.details or {'code': error.code, 'message': str(error)}
                raise QueryError('INVALID_CONTRACT', '扩展配置结构无效或编号重复。', details) from None

    def _read_routes(self, routes):
        for route in routes:
            if set(route) != {'origin', 'path', 'method', 'evidence'} or not route['evidence']:
                raise ValueError()
            check_url(route['origin'], {origin for p in self.profiles.values()
                                       for origin in [p['origin'], p.get('apiOrigin', p['origin'])]})
            safe_path(route['path'])
            if route['method'] not in {'GET', 'POST'}:
                raise ValueError()
            self.browser_reads.append(deepcopy(route))

    def register(self, document):
        """Atomic task-local extension; the agent supplies reviewed metadata, never code."""
        profiles, specs, routes = deepcopy(self.profiles), deepcopy(self.specs), deepcopy(self.browser_reads)
        section, item_id = 'document', None
        try:
            if set(document) - {'profiles', 'capabilities', 'browserReads', 'version'}:
                raise ValueError()
            for profile in document.get('profiles', []):
                section, item_id = 'profiles', profile.get('id')
                profile = deepcopy(profile)
                if profile['id'] in self.profiles:
                    raise ValueError()
                check_url(profile['origin'], {profile['origin']})
                if urlsplit(profile['origin']).path:
                    raise ValueError()
                from .http import canonical_origin
                profile['origin'] = canonical_origin(profile['origin'])
                if 'apiOrigin' in profile:
                    check_url(profile['apiOrigin'], {profile['apiOrigin']})
                    if urlsplit(profile['apiOrigin']).path or profile.get('adapter'):
                        raise ValueError()
                    profile['apiOrigin'] = canonical_origin(profile['apiOrigin'])
                check_url(profile['entry'], {profile['origin']})
                for path in profile.get('documentPaths', []) + profile.get('authPaths', []):
                    safe_path(path)
                for rule in profile.get('readRoutes', []):
                    safe_path(rule['path'])
                    if rule['method'] not in {'GET', 'POST'}:
                        raise ValueError()
                adapter = profile.get('adapter')
                adapter_origins = {'quiet_booths': 'https://jyc.ruc.edu.cn', 'academic': 'https://jw.ruc.edu.cn',
                                   'library': 'https://zwlib.ruc.edu.cn', 'sports': 'https://cgyy.ruc.edu.cn',
                                   'portal': 'https://my.ruc.edu.cn', 'ai': 'https://i.ruc.edu.cn',
                                   'resources': 'https://easybook.ruc.edu.cn', 'legacy': 'https://v.ruc.edu.cn',
                                   'opac': 'https://opac.ruc.edu.cn', 'study_spaces': 'https://yxkj.ruc.edu.cn',
                                   'future_classroom': 'https://k.ruc.edu.cn', 'party_school': 'https://zg.ruc.edu.cn'}
                if adapter and adapter_origins.get(adapter) != profile['origin']:
                    raise ValueError()
                context_fields = profile.get('contextFields', [])
                if (not isinstance(context_fields, list)
                        or any(not isinstance(k, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,60}', k)
                               for k in context_fields)
                        or context_fields and not adapter):
                    raise ValueError()
                if 'auth' in profile:
                    self._profile_auth(profile)
                for key, value in profile.get('headers', {}).items():
                    if (not isinstance(value, str) or any(c in key + value for c in '\r\n')
                            or key.lower() in {'authorization', 'cookie', 'host'}):
                        raise ValueError()
                self.profiles[profile['id']] = deepcopy(profile)
            for spec in document.get('capabilities', []):
                section, item_id = 'capabilities', spec.get('id')
                if spec['id'] in self.specs:
                    raise ValueError()
                spec = deepcopy(spec)
                self._check(spec)
                self.specs[spec['id']] = spec
            section, item_id = 'browserReads', None
            self._read_routes(document.get('browserReads', []))
        except (KeyError, ValueError, TypeError, AttributeError, QueryError) as error:
            self.profiles, self.specs, self.browser_reads = profiles, specs, routes
            details = {'section': section}
            if isinstance(item_id, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,99}', item_id):
                details['id'] = item_id
            if isinstance(error, QueryError):
                details.update(code=error.code, message=str(error))
            raise QueryError('INVALID_CONTRACT', '契约不合法或编号重复；本次注册未生效。', details) from None
        return {'status': 'ok', 'registered': [s['id'] for s in document.get('capabilities', [])],
                'browserReadRoutesAdded': len(document.get('browserReads', [])), 'persistence': 'task_memory'}

    def _profile_auth(self, profile):
        auth = profile['auth']
        if auth.get('kind') == 'public':
            if set(auth) != {'kind'}:
                raise ValueError()
            return
        if set(auth) - {'kind', 'service', 'entry', 'assertion', 'acceptEmptyRedirect',
                        'upgradeHttpPaths', 'tokenBindings', 'formBridge', 'exchange', 'entryDiscovery', 'browserState', 'bootstrap'}:
            raise ValueError()
        if auth['kind'] not in {'cas_cookie', 'legacy_oauth'}:
            raise ValueError()
        from .auth import CAS, OAUTH_SERVICE
        check_url(auth['service'], {profile['origin']} | ({CAS} if auth['service'] == OAUTH_SERVICE else set()))
        if auth['kind'] == 'legacy_oauth' and auth['service'] == OAUTH_SERVICE:
            raise ValueError()
        entry = auth.get('entry', profile['entry'])
        if auth['service'] == OAUTH_SERVICE and entry.startswith(CAS + '/'):
            parts = check_url(entry, {CAS})
            redirects = parse_qs(parts.query).get('redirect_uri', [])
            if parts.path != '/cas/oauth2.0/authorize' or len(redirects) != 1:
                raise ValueError()
            check_url(redirects[0], {profile['origin']})
        else:
            check_url(entry, {profile['origin']})
        if 'entryDiscovery' in auth:
            discovery = auth['entryDiscovery']
            if (auth['kind'] != 'cas_cookie' or set(discovery) - {'path', 'query', 'responsePath', 'acceptedStatuses'}
                    or not {'path', 'responsePath', 'acceptedStatuses'} <= set(discovery)
                    or not isinstance(discovery['responsePath'], list) or not discovery['responsePath']
                    or any(type(k) not in {str, int} for k in discovery['responsePath'])
                    or not isinstance(discovery['acceptedStatuses'], list) or not discovery['acceptedStatuses']
                    or any(type(s) is not int or s not in {200, 401} for s in discovery['acceptedStatuses'])):
                raise ValueError()
            safe_path(discovery['path'])
            if ('query' in discovery and (not isinstance(discovery['query'], dict)
                    or any(not isinstance(k, str) or type(v) not in {str, int, bool}
                           for k, v in discovery['query'].items()))):
                raise ValueError()
        if 'bootstrap' in auth:
            bootstrap = auth['bootstrap']
            if (set(bootstrap) != {'entry', 'redirectPaths'} or 'exchange' in auth
                    or not isinstance(bootstrap['redirectPaths'], list)
                    or not 1 <= len(bootstrap['redirectPaths']) <= 10):
                raise ValueError()
            for path in [bootstrap['entry'], *bootstrap['redirectPaths']]:
                safe_path(path)
        assertion = auth['assertion']
        self._auth_request({k: v for k, v in assertion.items() if k != 'response'}, assertion['response'])
        if 'exchange' in auth:
            exchange = auth['exchange']
            if set(exchange) != {'parameters', 'request', 'response'} or not exchange['parameters']:
                raise ValueError()
            values = {}
            for name, binding in exchange['parameters'].items():
                if not re.fullmatch(r'[A-Za-z][A-Za-z_0-9]{0,80}', name):
                    raise ValueError()
                self._auth_source(binding)
                if binding['source'] == 'response':
                    raise ValueError()
                values[name] = 'fixture-code'
            self._auth_request(request_for(exchange, values), exchange['response'])
            if exchange['response'].get('format', 'json') != 'json':
                raise ValueError()
        if any(b.get('source') == 'response' for b in auth.get('tokenBindings', [])) and 'exchange' not in auth:
            raise ValueError()
        browser_state = auth.get('browserState', [])
        if not isinstance(browser_state, list) or len(browser_state) > 8:
            raise ValueError()
        for item in browser_state:
            if (set(item) - {'storage', 'key', 'keyFrom', 'value'}
                    or item['storage'] not in {'local', 'session'}
                    or ('key' in item) == ('keyFrom' in item)
                    or not isinstance(item['value'], str) or len(item['value']) > 1000):
                raise ValueError()
            if 'keyFrom' in item:
                self._auth_source(item['keyFrom'])
                if item['keyFrom']['source'] not in {'query', 'fragment_query'}:
                    raise ValueError()
            elif not isinstance(item['key'], str) or not 1 <= len(item['key']) <= 100:
                raise ValueError()
        if type(auth.get('acceptEmptyRedirect', False)) is not bool:
            raise ValueError()
        for path in auth.get('upgradeHttpPaths', []):
            safe_path(path)
        bindings = auth.get('tokenBindings', [])
        if not isinstance(bindings, list) or len(bindings) > 8:
            raise ValueError()
        bound_headers = set()
        for binding in bindings:
            self._auth_source({k: v for k, v in binding.items() if k not in {'header', 'prefix'}})
            if (not re.fullmatch(r'[A-Za-z][A-Za-z_0-9-]{0,80}', binding['header'])
                    or binding['header'].lower() in {'cookie', 'host', 'set-cookie'}
                    or binding['header'].lower() in bound_headers
                    or any(ord(c) < 32 or ord(c) == 127 for c in binding.get('prefix', ''))):
                raise ValueError()
            bound_headers.add(binding['header'].lower())
        if 'formBridge' in auth:
            bridge = auth['formBridge']
            safe_path(bridge['path'])
            if (auth['kind'] != 'legacy_oauth' or set(bridge) != {'path', 'fields'}
                    or not isinstance(bridge['fields'], list) or not bridge['fields']
                    or any(not isinstance(key, str) or not key for key in bridge['fields'])):
                raise ValueError()

    @staticmethod
    def _auth_source(binding):
        if binding['source'] == 'response':
            if (set(binding) != {'source', 'path'} or not isinstance(binding['path'], list)
                    or any(type(k) not in {str, int} for k in binding['path'])):
                raise ValueError()
        elif (set(binding) != {'source', 'parameter'}
              or binding['source'] not in {'query', 'fragment_query', 'cookie'}
              or not re.fullmatch(r'[A-Za-z][A-Za-z_0-9-]{0,80}', binding['parameter'])):
            raise ValueError()

    def _auth_request(self, request, response):
        safe_path(request['path'])
        if (set(request) - {'path', 'method', 'query', 'body', 'encoding'}
                or request.get('method', 'GET') not in {'GET', 'POST'}
                or request.get('encoding', 'json') not in {'json', 'form'}
                or ('body' in request and request.get('method', 'GET') == 'GET')
                or 'pagination' in response):
            raise ValueError()
        self._response_checks(response)
        self._response_format(response)

    @staticmethod
    def _response_checks(response):
        if not isinstance(response.get('checks'), list) or not response['checks']:
            raise ValueError()
        for check in response['checks']:
            if (set(check) - {'path', 'equals', 'type', 'contains'}
                    or not isinstance(check.get('path'), list)
                    or not ({'equals', 'type', 'contains'} & set(check))
                    or any(type(k) not in {str, int} for k in check['path'])):
                raise ValueError()
            if 'type' in check:
                check_schema({'type': check['type']})
        if (not isinstance(response.get('dataPath', []), list)
                or any(type(k) not in {str, int} for k in response.get('dataPath', []))):
            raise ValueError()

    def _check(self, spec):
        required = {'id', 'name', 'description', 'platform', 'readOnly', 'parameters', 'request', 'response', 'evidence'}
        if not required <= set(spec) or type(spec['readOnly']) is not bool:
            raise ValueError()
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,99}', spec['id']):
            raise ValueError()
        if spec['platform'] not in self.profiles or spec['parameters'].get('type') != 'object':
            raise ValueError()
        check_schema(spec['parameters'])
        request = spec['request']
        if request.get('method') not in {'GET', 'POST', 'PUT', 'PATCH', 'DELETE'}:
            raise ValueError()
        if set(request) - {'method', 'path', 'apiCode', 'query', 'body', 'encoding'}:
            raise ValueError()
        if ('path' in request) == ('apiCode' in request):
            raise ValueError()
        profile = self.profiles[spec['platform']]
        bindings = context_keys(request)
        if bindings and (not spec['readOnly'] or not bindings <= set(profile.get('contextFields', []))):
            raise ValueError()
        if not profile.get('adapter') and not profile.get('auth'):
            raise ValueError()
        if profile.get('adapter') == 'academic':
            from .academic import CODES
            if 'apiCode' not in request or request['apiCode'] in {CODES['grades'], CODES['rankings']}:
                raise ValueError()
        elif 'apiCode' in request:
            raise ValueError()
        if 'path' in request:
            safe_path(request['path'])
        if request.get('encoding', 'json') not in {'json', 'form'}:
            raise ValueError()
        if 'body' in request and request['method'] == 'GET':
            raise ValueError()
        self._response_checks(spec['response'])
        pagination = spec['response'].get('pagination')
        if pagination is not None:
            if (not isinstance(pagination, dict) or not spec['readOnly']
                    or set(pagination) - {'itemsPath', 'totalPath', 'hasNextPath', 'identityPath', 'pageParameter', 'firstPage', 'totalEncoding', 'pageStepParameter'}
                    or pagination.get('totalEncoding', 'integer') not in {'integer', 'decimal_string'}):
                raise ValueError()
            if ('totalPath' in pagination) == ('hasNextPath' in pagination):
                raise ValueError()
            if 'totalEncoding' in pagination and 'totalPath' not in pagination:
                raise ValueError()
            for key in ['itemsPath', *[k for k in ['totalPath', 'hasNextPath', 'identityPath'] if k in pagination]]:
                if (not isinstance(pagination.get(key), list)
                        or any(type(part) not in {str, int} for part in pagination[key])):
                    raise ValueError()
            if 'pageParameter' in pagination:
                page_schema = spec['parameters']['properties'][pagination['pageParameter']]
                first = pagination.get('firstPage', 1)
                if page_schema.get('type') != 'integer' or type(first) is not int or first < 0:
                    raise ValueError()
                validate(first, page_schema)
                if 'pageStepParameter' in pagination:
                    step_schema = spec['parameters']['properties'][pagination['pageStepParameter']]
                    if step_schema.get('type') != 'integer' or step_schema.get('minimum', 0) < 1:
                        raise ValueError()
                    if pagination['pageStepParameter'] == pagination['pageParameter']:
                        raise ValueError()
            elif 'firstPage' in pagination or 'pageStepParameter' in pagination:
                raise ValueError()
        response_format = self._response_format(spec['response'])
        if response_format != 'json':
            if (not spec['readOnly'] or request['method'] not in {'GET', 'POST'}
                    or profile.get('adapter') in {'academic', 'library', 'study_spaces', 'sports', 'resources'}):
                raise ValueError()
        for rule in spec.get('constraints', []):
            if (set(rule) != {'left', 'op', 'right'} or rule['op'] != 'lt'
                    or any(rule[key] not in spec['parameters'].get('properties', {}) for key in ['left', 'right'])):
                raise ValueError()
        spec.setdefault('verification', 'contract_only')
        spec.setdefault('entry', self.profiles[spec['platform']]['entry'])

    @staticmethod
    def _response_format(response):
        kind = response.get('format', 'json')
        if kind not in {'json', 'embedded_json', 'html'}:
            raise ValueError()
        if kind == 'embedded_json' and not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,80}', response.get('variable', '')):
            raise ValueError()
        return kind

    def catalog(self, keyword=None):
        specs = list(self.specs.values())
        if keyword:
            specs = [s for s in specs if keyword.casefold() in json.dumps(s, ensure_ascii=False).casefold()]
        return {'status': 'ok', 'live': False, 'version': self.version, 'capabilities': deepcopy(specs)}

    def profile_for_url(self, url):
        """Choose the closest declared page when an origin has multiple profiles."""
        from .http import canonical_origin
        parts = urlsplit(url)
        authority = urlsplit(canonical_origin(url)).netloc
        profiles = [p for p in self.profiles.values()
                    if urlsplit(canonical_origin(p['origin'])).netloc == authority]
        def score(profile):
            paths = [urlsplit(profile['entry']).path or '/', *profile.get('documentPaths', [])]
            return max((len(path) for path in paths if parts.path == path
                        or path.endswith('/') and parts.path.startswith(path)), default=-1)
        return max(profiles, key=score) if profiles else None

    def prepare(self, capability, params, write=False):
        if capability not in self.specs:
            raise QueryError('UNKNOWN_CAPABILITY', '未定义的能力；先调用 ruc_capabilities，未知平台使用发现和浏览器入口。')
        spec = self.specs[capability]
        if spec.get('readOnly', True) == write:
            raise QueryError('WRONG_OPERATION_MODE', '读取用 query；写入用 prepare_action 和 commit_action。')
        validate(params, spec['parameters'], 'params')
        values = {k: deepcopy(v['default']) for k, v in spec['parameters'].get('properties', {}).items() if 'default' in v}
        values.update(deepcopy(params))
        validate(values, spec['parameters'], 'params')
        for rule in spec.get('constraints', []):
            left, right = values.get(rule['left']), values.get(rule['right'])
            if type(left) is not type(right) or type(left) not in {int, float, str} or not left < right:
                raise QueryError('INVALID_ARGUMENT', f"{rule['left']} 必须小于 {rule['right']}。")
        if 'request' in spec:
            request_for(spec, values)  # All template errors before authentication.
        return spec, values


def page_values(raw, pagination):
    rows = at(raw, pagination['itemsPath'])
    if 'hasNextPath' in pagination:
        if not isinstance(rows, list) or type(at(raw, pagination['hasNextPath'])) is not bool:
            raise QueryError('SCHEMA_CHANGED', '分页记录或下一页标记结构无效。')
        return rows, None
    total = at(raw, pagination['totalPath'])
    if pagination.get('totalEncoding') == 'decimal_string':
        if not isinstance(total, str) or not re.fullmatch(r'[0-9]{1,16}', total):
            raise QueryError('SCHEMA_CHANGED', '分页总数不是约定的十进制整数字符串。')
        total = int(total)
    if not isinstance(rows, list) or type(total) is not int or total < len(rows):
        raise QueryError('SCHEMA_CHANGED', '分页数量或记录结构无效。')
    return rows, total


def decode_response(spec, raw):
    response = spec['response']
    for check in response['checks']:
        value = at(raw, check['path'])
        if 'equals' in check and (type(value) is not type(check['equals']) or value != check['equals']):
            raise QueryError('PLATFORM_REJECTED', '平台未返回契约要求的成功标记。', {'path': check['path']})
        if 'contains' in check and (not isinstance(value, (str, list))
                                   or (isinstance(value, str) and not isinstance(check['contains'], str))
                                   or check['contains'] not in value):
            raise QueryError('PLATFORM_REJECTED', '页面未包含契约要求的身份或内容标记。', {'path': check['path']})
        if 'type' in check:
            try:
                validate(value, {'type': check['type']}, 'response')
            except QueryError:
                raise QueryError('SCHEMA_CHANGED', '响应字段类型发生变化。', {'path': check['path']}) from None
    from .redaction import redact
    data = redact(at(raw, response.get('dataPath', [])))
    result = {'status': 'ok', 'data': data, 'schemaFingerprint': fingerprint(data)}
    if 'pagination' in response:
        pagination = response['pagination']
        rows, total = page_values(raw, pagination)
        result.update({'pageItems': len(rows), 'totalItems': total,
                       'complete': total == len(rows) if total is not None else None,
                       'pagination': 'explicit_page'})
        if 'hasNextPath' in pagination:
            result['hasNext'] = at(raw, pagination['hasNextPath'])
    return result
