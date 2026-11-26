"""Quiet-booth adapter; CAS identity is shared by the task session."""
import json
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

from .auth import CAS, CAS_TICKETS, CasIdentity, is_ticket_id
from .errors import QueryError
from .http import MemoryHTTP, decode_json, send

ORIGIN = 'https://jyc.ruc.edu.cn'
API = ORIGIN + '/spa/static/api/book/'
PUBLIC = ORIGIN + '/spa/static/public/api/'
CAS_SERVICE = ORIGIN + '/rem/static/sso/webOAuthRed'
BEIJING = ZoneInfo('Asia/Shanghai')
QUERY_METHODS = {'getSchoolList': 'GET', 'getVenueList': 'GET',
                 'getRoomTypeList': 'GET', 'getOpenDateList': 'GET',
                 'getRoomList': 'POST'}


def read_json(client, request):
    status, _, body = send(client, request)
    value = decode_json(status, body)
    if isinstance(value, dict) and value.get('code') == 401:
        raise QueryError('LOGIN_REQUIRED', '学校登录会话已失效。')
    if not isinstance(value, dict) or value.get('code') != 200 or value.get('status') is not True or value.get('data') is None:
        raise QueryError('INVALID_RESPONSE', '官网未返回成功的查询结果。')
    return value['data']


class BoothClient:
    def __init__(self, username=None, password=None, identity=None):
        self.token = None
        self._owns_identity = identity is None
        self.identity = identity or CasIdentity(lambda: (username, password))
        self._session = MemoryHTTP(ORIGIN)
        self.sso = self._session.opener
        self.http = self.identity.http
        try:
            service = self._discover_service()
            self._exchange_service_ticket(service)
        except BaseException:
            self.close()
            raise

    def _discover_service(self):
        config = read_json(self.http, PUBLIC + 'getFrontVueConfig')
        if not isinstance(config, dict) or config.get('casLogin') != 1 or config.get('casLoginUrl') != ORIGIN + '/rem' or config.get('pcCasServiceUrl') not in {ORIGIN+'/space', ORIGIN+'/space/'}:
            raise QueryError('CONFIG_CHANGED', '官网单点登录配置发生变化，需要重新核对。')
        login = config['casLoginUrl'] + '/static/sso/login?' + urllib.parse.urlencode({'redirectUrl': config['pcCasServiceUrl']})
        status, headers, _ = send(self.sso, login)
        target = urllib.parse.urlsplit(urllib.parse.urljoin(login, headers.get('Location', '')))
        service = urllib.parse.parse_qs(target.query).get('service', [])
        if status != 302 or target.scheme != 'https' or target.netloc != 'cas.ruc.edu.cn' or target.path != '/cas/login' or service != [CAS_SERVICE]:
            raise QueryError('CONFIG_CHANGED', '学校返回的 CAS 回调地址与已核实配置不符。')
        return service[0]

    def _exchange_service_ticket(self, service):
        ticket = self.identity.ticket(service)
        url = service + '?' + urllib.parse.urlencode({'ticket': ticket})
        for _ in range(8):
            target = urllib.parse.urlsplit(url)
            if target.scheme != 'https' or target.netloc != 'jyc.ruc.edu.cn':
                raise QueryError('UNEXPECTED_REDIRECT', '学校服务票据未完成静音舱登录。')
            tokens = urllib.parse.parse_qs(target.query).get('token', [])
            if target.path.rstrip('/') == '/space' and len(tokens) == 1 and tokens[0]:
                ticket = tokens[0]
                break
            status, headers, _ = send(self.sso, url)
            if status not in {301, 302, 303, 307, 308} or not headers.get('Location'):
                raise QueryError('SSO_ERROR', f'学校回调未返回静音舱票据（HTTP {status}）。')
            url = urllib.parse.urljoin(url, headers['Location'])
        else:
            raise QueryError('SSO_ERROR', '学校登录跳转次数超过预期。')
        exchange = PUBLIC + 'remoteCasLogin?' + urllib.parse.urlencode({'token': ticket, 'noAuth': 'true'})
        data = read_json(self.http, exchange)
        self.token = data.get('token') if isinstance(data, dict) else None
        if not isinstance(self.token, str) or not self.token:
            raise QueryError('LOGIN_REQUIRED', '正常 CAS 交换没有返回静音舱会话。')

    def close(self):
        self.token = None
        self._session.close()
        return self.identity.close() if self._owns_identity else True

    def query(self, endpoint, params=None):
        if endpoint not in QUERY_METHODS:
            raise ValueError('Only the five booking availability queries are supported.')
        if not self.token:
            raise QueryError('LOGIN_REQUIRED', '本次静音舱客户端已经关闭或尚未认证。')
        params = params or {}
        headers = {'Accept': 'application/json', 'Authorization': self.token}
        url = API + endpoint
        body = None
        if QUERY_METHODS[endpoint] == 'POST':
            body = json.dumps(params).encode()
            headers['Content-Type'] = 'application/json'
        elif params:
            url += '?' + urllib.parse.urlencode(params)
        return read_json(self.http, urllib.request.Request(url, data=body, headers=headers, method=QUERY_METHODS[endpoint]))

    def rooms(self, params):
        page, last, expected = 1, 1, None
        expected_pages = None
        rooms = {}
        while page <= last:
            data = self.query('getRoomList', {**params, 'currentPage': page, 'pageSize': 150})
            if not isinstance(data, dict) or data.get('currentPage') != page or not isinstance(data.get('pageList'), list):
                raise QueryError('SCHEMA_CHANGED', '舱位分页结构发生变化。')
            total, last = data.get('totalCount'), data.get('totalPage')
            if type(total) is not int or total < 0 or type(last) is not int or not 0 <= last <= 1000:
                raise QueryError('SCHEMA_CHANGED', '舱位分页数量无效。')
            if (expected is not None and expected != total) or (expected_pages is not None and expected_pages != last):
                raise QueryError('CATALOG_CHANGED', '读取期间舱位目录数量变化，请重新查询。')
            if (total > 0 and (last < page or not data['pageList'])) or (total == 0 and last > 1):
                raise QueryError('SCHEMA_CHANGED', '舱位分页与总数不一致。')
            expected, expected_pages = total, last
            for room in data['pageList']:
                if not isinstance(room, dict):
                    raise QueryError('SCHEMA_CHANGED', '舱位数据不是对象。')
                key = room.get('id')
                if type(key) not in {int, str} or key == '' or key in rooms:
                    raise QueryError('SCHEMA_CHANGED', '舱位编号缺失或重复。')
                rooms[key] = room
            page += 1
        if len(rooms) != expected:
            raise QueryError('INCOMPLETE', '舱位返回数量与官网总数不符。')
        return list(rooms.values())

