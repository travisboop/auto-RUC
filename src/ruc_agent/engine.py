"""Task-scoped runtime shared by CLI, function tools and Python callers."""
from datetime import datetime
import json

from .academic import AcademicClient
from .ai import AiClient
from .auth import CasIdentity
from .availability import Query, catalog, collect
from .booths import BoothClient
from .catalog import ROOT
from .actions import ActionPlans
from .contracts import Registry
from .data import BEIJING, date_value, interval
from .errors import QueryError
from .future_classroom import FutureClassroomClient
from .library import LibraryClient, STUDY_ORIGIN
from .legacy import LegacyIdentity
from .opac import OpacClient
from .party_school import PartySchoolClient
from .portal import PortalClient
from .sports import SportsClient, RESOURCE_ORIGIN

FACTORIES = {'quiet_booths': lambda identity: BoothClient(identity=identity),
             'library': LibraryClient, 'sports': SportsClient,
             'academic': AcademicClient, 'portal': PortalClient, 'ai': AiClient,
             'resources': lambda identity: SportsClient(identity, RESOURCE_ORIGIN), 'opac': OpacClient,
             'study_spaces': lambda identity: LibraryClient(identity, STUDY_ORIGIN),
             'future_classroom': FutureClassroomClient, 'party_school': PartySchoolClient}
AUTH_STOPS = {'CREDENTIALS_REQUIRED', 'CREDENTIALS_INVALID', 'CREDENTIALS_PERMISSIONS',
              'PASSWORD_AUTH_REJECTED', 'REST_AUTH_UNAVAILABLE', 'RATE_LIMITED'}


def capabilities(keyword=None, registry=None):
    return (registry or Registry()).catalog(keyword)


def prepare(capability, params, registry=None):
    spec, _ = (registry or Registry()).prepare(capability, params)
    if capability == 'quiet_booths':
        Query.parse(params)
    if capability in {'library_seats', 'sports_venues', 'classrooms'}:
        date_value(params.get('date'))
        interval(params)
    return spec


def error_result(error):
    result = {'status': 'error', 'code': error.code, 'message': str(error)}
    if error.details is not None:
        result['details'] = error.details
    return result


class CampusSession:
    """Use as a context manager; no on-disk sessions and no credential caching."""
    def __init__(self, credentials, registry=None, allow_writes=False):
        self.identity = CasIdentity(credentials)
        self._credentials = credentials
        self._legacy_identity = None
        self.registry = registry or Registry()
        self.allow_writes = allow_writes
        self.actions = ActionPlans()
        self.browser = None
        self.discovered_entries = set()
        self.clients = {}
        self.failures = {}
        self.closed = False
        self.revoked = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @property
    def legacy_identity(self):
        if self.closed:
            raise QueryError('SESSION_CLOSED', '任务会话已经关闭。')
        if self._legacy_identity is None:
            self._legacy_identity = LegacyIdentity(self._credentials)
        return self._legacy_identity

    def prepare_identity(self, profile):
        if profile.get('auth', {}).get('kind') == 'public':
            return
        if profile.get('adapter') == 'legacy' or profile.get('auth', {}).get('kind') == 'legacy_oauth':
            self.legacy_identity.prepare()
        else:
            self.identity.prepare()

    def client(self, platform):
        if self.closed:
            raise QueryError('SESSION_CLOSED', '任务会话已经关闭。')
        if platform in self.failures:
            raise self.failures[platform]
        if platform == 'legacy':
            self.legacy_identity.prepare()
            return self.legacy_identity
        if platform not in self.clients:
            try:
                self.clients[platform] = FACTORIES[platform](self.identity)
            except QueryError as error:
                self.failures[platform] = error
                raise
        return self.clients[platform]

    def query(self, capability, params=None, all_pages=False, max_pages=100):
        params = {} if params is None else params
        spec = prepare(capability, params, self.registry)
        _, params = self.registry.prepare(capability, params)
        if all_pages:
            from .pagination import check_pagination
            check_pagination(spec, max_pages)
        profile = self.registry.profiles.get(spec['platform'], {})
        client_key = spec.get('adapter') or profile.get('adapter') or 'contract:' + spec['platform']
        existing = self.clients.get(client_key)
        legacy = profile.get('auth', {}).get('kind') == 'legacy_oauth'
        identity_ready = (self._legacy_identity and self._legacy_identity.authenticated) if legacy else bool(self.identity._tgt_url)

        def read():
            if 'request' in spec:
                from .operations import execute
                return execute(self, spec, params, all_pages, max_pages)
            else:
                if not (capability == 'sports_venues' and params.get('catalog_only')):
                    self.identity.prepare()
                client = self.client(spec['adapter'])
                if capability == 'quiet_booths':
                    return collect(client, Query.parse(params))
                return getattr(client, spec['method'])(params)

        try:
            output = read()
        except QueryError as error:
            if error.code != 'LOGIN_REQUIRED' or existing is None or not identity_ready:
                raise
            # A previously working business session can expire independently of
            # the task identity. Rebuild it once; never resubmit the password or
            # retry a write, initial login, transport error or permission denial.
            self.clients.pop(client_key, None)
            self.failures.pop(client_key, None)
            existing.close()
            output = read()
            output['businessSessionRenewed'] = True
        if capability == 'applications':
            for app in output.get('applications', []):
                if app.get('isAllow') not in {False, 'false', '0', 0}:
                    self.discovered_entries.update(app[k] for k in ['appUrl', 'entranceUrl'] if app.get(k))
                    from .urls import application_target
                    target = application_target(app)
                    if target:
                        self.discovered_entries.add(target)
        return {'capability': capability, 'platform': spec['platform'], 'live': True,
                'asOf': datetime.now(BEIJING).isoformat(timespec='seconds'), 'freshSession': True,
                'source': spec['entry'], **output}

    def check_login(self):
        self.identity.prepare()
        client = self.client('quiet_booths')
        schools = catalog(client.query('getSchoolList'))
        return {'status': 'ok', 'asOf': datetime.now(BEIJING).isoformat(timespec='seconds'),
                'authentication': 'cas_rest_password', 'freshSession': True,
                'campuses': [school['name'] for school in schools]}

    def batch(self, requests):
        # Reject the complete plan before opening any platform or submitting a password.
        specs = [prepare(item['capability'], item.get('params', {}), self.registry) for item in requests]
        results, stopped = [], {}
        for request, spec in zip(requests, specs):
            name, params = request['capability'], request.get('params', {})
            profile = self.registry.profiles.get(spec['platform'], {})
            public = (name == 'sports_venues' and params.get('catalog_only')
                      or profile.get('auth', {}).get('kind') == 'public')
            realm = 'legacy' if (profile.get('adapter') == 'legacy'
                                or profile.get('auth', {}).get('kind') == 'legacy_oauth') else 'cas'
            if realm in stopped and not public:
                results.append({'capability': name, 'status': 'not_run', 'code': stopped[realm]})
                continue
            try:
                results.append(self.query(name, params))
            except QueryError as error:
                results.append({'capability': name, **error_result(error)})
                identity = self._legacy_identity if realm == 'legacy' else self.identity
                if not public and (error.code in AUTH_STOPS | {'CAPTCHA_REQUIRED'} or (identity and identity._failure)):
                    stopped[realm] = error.code
        failed = any(v['status'] in {'error', 'partial', 'not_run'} for v in results)
        return {'status': 'partial' if failed else 'ok', 'results': results}

    def contract_client(self, profile):
        if profile.get('adapter'):
            return self.client(profile['adapter'])
        name = 'contract:' + profile['id']
        if self.closed:
            raise QueryError('SESSION_CLOSED', '任务会话已经关闭。')
        if name in self.failures:
            raise self.failures[name]
        if name not in self.clients:
            from .operations import ContractClient
            try:
                legacy = self.legacy_identity if profile['auth']['kind'] == 'legacy_oauth' else None
                self.clients[name] = ContractClient(self.identity, profile, legacy)
            except QueryError as error:
                if error.code != 'CAPTCHA_REQUIRED':
                    self.failures[name] = error
                raise
        return self.clients[name]

    def discover(self, platform=None, keyword=None):
        if platform is None:
            return {'status': 'ok', 'profiles': list(self.registry.profiles.values()),
                    'contractFormat': json.loads((ROOT / 'tools/contract-format.json').read_text(encoding='utf-8')),
                    'next': '使用 applications 查实时门户入口；academic 可发现本次账号的 API 映射。'}
        if platform == 'academic':
            self.identity.prepare()
            client = self.client('academic')
            from .academic import CODES
            rows = [{'code': code, 'path': path, 'callableViaBuiltin': code in CODES.values()}
                    for code, path in client.api.items()
                    if not keyword or keyword.casefold() in code.casefold()]
            return {'status': 'ok', 'live': True, 'operations': rows,
                    'meaning': '账号 API 目录只用于定位；未知接口的参数和读写语义须核对当前页面后登记契约。'}
        if platform == 'portal':
            return self.query('applications', {'keyword': keyword, 'limit': 1000} if keyword else {'limit': 1000})
        profile = self.registry.profiles.get(platform)
        if profile is None:
            raise QueryError('UNKNOWN_PLATFORM', '没有该平台配置；从门户发现入口后可使用 browser_open。')
        return {'status': 'ok', 'profile': profile,
                'capabilities': [s for s in self.registry.catalog(keyword)['capabilities'] if s['platform'] == platform]}

    def platform_inventory(self):
        from .discovery import inventory
        return inventory(self.query('applications', {'limit': 1000}), self.registry)

    def prepare_action(self, capability, params=None):
        spec, values = self.registry.prepare(capability, params or {}, write=True)
        from .contracts import request_for
        if 'request' not in spec:
            raise QueryError('INVALID_CONTRACT', '写入能力必须具有请求契约。')
        return self.actions.prepare({'kind': 'request', 'capability': capability, 'params': values,
                                     'origin': self.registry.profiles[spec['platform']].get('apiOrigin',
                                               self.registry.profiles[spec['platform']]['origin']),
                                     'source': spec['entry'], 'request': request_for(spec, values),
                                     'receiptQuery': spec.get('receiptQuery')})

    def commit_action(self, action_id):
        if not self.allow_writes:
            raise QueryError('WRITES_DISABLED', '宿主未启用写入；用户授权操作任务后，以 --allow-writes 启动。')
        payload = self.actions.take(action_id)
        try:
            if payload['kind'] == 'browser':
                return self.get_browser().commit(payload)
            from .operations import execute
            spec, values = self.registry.prepare(payload['capability'], payload['params'], write=True)
            return {'capability': spec['id'], 'submitted': True, **execute(self, spec, values)}
        except QueryError as error:
            # No second send: even an unreadable response can follow a successful write.
            return {'status': 'outcome_unknown', 'submitted': 'unknown', 'retrySafe': False,
                    'code': error.code, 'message': str(error), 'receiptQuery': payload.get('receiptQuery')}

    def get_browser(self):
        if self.closed:
            raise QueryError('SESSION_CLOSED', '任务会话已经关闭。')
        if self.browser is None:
            from .browser import CampusBrowser
            self.browser = CampusBrowser(self)
        return self.browser

    def close(self):
        if self.closed:
            return self.revoked
        try:
            self.actions.clear()
            if self.browser is not None:
                self.browser.close()
            for client in self.clients.values():
                client.close()
            if self._legacy_identity is not None:
                self._legacy_identity.close()
        finally:
            self.clients.clear()
            self._credentials = None
            self.revoked = self.identity.close()
            self.closed = True
        return self.revoked

    def metadata(self):
        legacy_submissions = self._legacy_identity.credential_submissions if self._legacy_identity else 0
        return {'freshSession': True, 'savedStateRead': False,
                'credentialSubmissions': self.identity.credential_submissions + legacy_submissions,
                'authenticationRealms': {'cas': self.identity.credential_submissions,
                                         'legacy': legacy_submissions},
                'restTicketRevoked': self.revoked}
