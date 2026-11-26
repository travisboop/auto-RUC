"""Library seat service, with the current frontend's CAS exchange and HMAC."""
import base64
import hashlib
import hmac
import re
import time
import uuid
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from .auth import cas_login
from .data import date_value, fields, interval, paged, records, select
from .errors import QueryError
from .http import MemoryHTTP

ORIGIN = 'https://zwlib.ruc.edu.cn'
STUDY_ORIGIN = 'https://yxkj.ruc.edu.cn'
BASE = '/jsq'
SERVICE = ORIGIN + '/rem/static/sso/webOAuthRed'


def decrypt_hmac_key(value, key, iv):
    if not value:
        return b''
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.padding import PKCS7
    except ImportError:
        raise QueryError('DEPENDENCY_REQUIRED', '图书馆签名需要 cryptography；运行 pip install -r requirements.txt。') from None
    try:
        decryptor = Cipher(algorithms.AES(key.encode()), modes.CBC(iv.encode())).decryptor()
        plain = decryptor.update(base64.b64decode(value, validate=True)) + decryptor.finalize()
        unpadder = PKCS7(128).unpadder()
        return unpadder.update(plain) + unpadder.finalize()
    except (ValueError, TypeError, AttributeError):
        raise QueryError('SCHEMA_CHANGED', '图书馆签名密钥格式改变。') from None


def signature_headers(key, method='POST', timestamp=None, request_id=None):
    stamp = int(time.time() * 1000) if timestamp is None else timestamp
    request_id = request_id or str(uuid.uuid4())
    value = f'seat::{request_id}::{stamp}::{method}'
    return {'X-request-id': request_id, 'X-request-date': str(stamp),
            'X-hmac-request-key': hmac.new(key, value.encode(), hashlib.sha256).hexdigest()}


def segment(value):
    value = str(value)
    if not re.fullmatch(r'[A-Za-z0-9_-]+', value):
        raise QueryError('SCHEMA_CHANGED', '图书馆返回了无效的目录编号。')
    return quote(value, safe='')


class LibraryClient:
    def __init__(self, identity, origin=ORIGIN):
        if origin not in {ORIGIN, STUDY_ORIGIN}:
            raise QueryError('INVALID_ARGUMENT', '未登记此座位平台源站。')
        self.http = MemoryHTTP(origin)
        self.token = None
        self.settings = {}
        self.hmac_key = b''
        try:
            self.settings = self._post('/static/public/cg/getSysSet/PC', {})
            config = self.settings.get('vueConfig', {})
            if (str(config.get('CASLOGIN')) != '1' or config.get('CASSSERVICE') != origin + '/rem'
                    or config.get('VUESERVICE', '').rstrip('/') != origin + '/jsq-v'):
                raise QueryError('CONFIG_CHANGED', '图书馆单点登录配置改变。')
            service = origin + '/rem/static/sso/webOAuthRed'
            class BoundIdentity:
                def ticket(self, observed):
                    if observed != service:
                        raise QueryError('AUTH_FLOW_CHANGED', '座位平台认证回调发生变化。')
                    return identity.ticket(observed, {origin})
            entry = origin + '/rem/static/sso/login?' + urlencode({'redirectUrl': config['VUESERVICE']})
            target = cas_login(self.http, BoundIdentity(), entry, service, token_path='/jsq-v')
            temporary = parse_qs(urlsplit(target).query).get('token', [])
            if len(temporary) != 1 or not temporary[0]:
                raise QueryError('SSO_ERROR', '图书馆回调未返回临时交换票据。')
            if temporary[0] in {'.', '..'} or len(temporary[0]) > 16384:
                raise QueryError('SSO_ERROR', '图书馆临时交换票据格式无效。')
            login = self._post('/static/public/auth/cas/' + quote(temporary[0], safe=''),
                               {'token': temporary[0], 'loginType': 'PC'})
            if not isinstance(login, dict) or not isinstance(login.get('token'), str) or not login['token']:
                raise QueryError('LOGIN_REQUIRED', '图书馆未建立业务会话。')
            self.token = login['token']
            if self.settings.get('hmac') == 1:
                encrypted = login.get('userInfoRes', {}).get('hmacKey')
                if not isinstance(encrypted, str) or not encrypted:
                    raise QueryError('SCHEMA_CHANGED', '图书馆未返回本次业务签名密钥。')
                self.hmac_key = decrypt_hmac_key(encrypted, self.settings.get('makePrefix'), self.settings.get('makeSuffix'))
            user = self._post('/static/frontApi/user/getUserInfo', {})
            if not isinstance(user, dict) or not user:
                raise QueryError('LOGIN_REQUIRED', '图书馆未确认本次登录用户。')
        except BaseException:
            self.close()
            raise

    def _post(self, path, body):
        envelope = self.request(BASE + path, 'POST', body=body)
        if isinstance(envelope, dict) and str(envelope.get('code')) == '20003':
            raise QueryError('LOGIN_REQUIRED', '图书馆业务会话已失效。')
        if not isinstance(envelope, dict) or envelope.get('status') is not True or envelope.get('code') != 200:
            raise QueryError('PLATFORM_REJECTED', '图书馆未接受本次查询。')
        if envelope.get('data') is None:
            raise QueryError('SCHEMA_CHANGED', '图书馆查询没有返回 data。')
        return envelope['data']

    def request(self, path, method='POST', query=None, body=None, encoding='json'):
        headers = {'loginType': 'PC'}
        if self.settings.get('hmac') == 1:
            headers.update(signature_headers(self.hmac_key, method))
        if self.token:
            headers['token'] = self.token
        return self.http.request_json(path, method, query, body, headers, encoding)

    def query(self, params):
        day, times = date_value(params.get('date')), interval(params)
        catalog = self._post('/static/frontApi/res/buildingFloorDate', {})
        if not isinstance(catalog, dict):
            raise QueryError('SCHEMA_CHANGED', '图书馆目录格式改变。')
        buildings = select(records(catalog.get('buildings')), params.get('building'))
        dates = catalog.get('dates')
        if not isinstance(dates, list) or any(not isinstance(v, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', v) for v in dates):
            raise QueryError('SCHEMA_CHANGED', '图书馆开放日期格式改变。')
        result = {'date': day, 'openDates': dates, 'mode': params.get('mode', 'duration'),
                  'scope': '官网当前筛选条件下的阅览室座位数量', 'rooms': []}
        if not buildings:
            return {'status': 'no_matching_catalog', **result}
        if day not in dates:
            return {'status': 'date_not_open', **result}
        mode = params.get('mode', 'duration')
        checked = 0
        for building in buildings:
            if building.get('id') is None:
                raise QueryError('SCHEMA_CHANGED', '图书馆楼宇缺少编号。')
            floors = select(records(building.get('floors')), params.get('floor')) if params.get('floor') else [{'id': 0}]
            for floor in floors:
                if floor.get('id') is None:
                    raise QueryError('SCHEMA_CHANGED', '图书馆楼层缺少编号。')
                body = {'beginMinute': times[0] if times else -1, 'endMinute': times[1] if times else 0,
                        'floorId': floor['id'], 'minMinute': 0, 'power': params.get('power', False),
                        'roomType': params.get('overnight', False), 'windows': params.get('window', False),
                        'sortField': '', 'sortType': ''}
                if mode == 'slice':
                    body['amPm'] = 0
                suffix = 'Slice' if mode == 'slice' else 'Duration'
                path = '/static/frontApi/res/findRoom' + suffix + '/' + segment(building['id']) + '/' + day
                rows = paged(lambda p, n: self._post(path, {**body, 'currentPage': p, 'pageSize': n}),
                             'pageList', 'totalCount', identity_key='id')
                for row in rows:
                    try:
                        total, free = int(row['seatTotal']), int(row['seatFree'])
                    except (KeyError, ValueError, TypeError):
                        raise QueryError('SCHEMA_CHANGED', '图书馆座位总数或余量缺失。') from None
                    if not row.get('name') or not 0 <= free <= total:
                        raise QueryError('SCHEMA_CHANGED', '图书馆座位数量或名称无效。')
                    if params.get('room_id') and str(row['id']) != params['room_id']:
                        continue
                    item = {**fields(row, ['id', 'name', 'buildingName', 'floorName']),
                            'seatTotal': total, 'seatFree': free}
                    if params.get('room_id'):
                        path_seats = '/static/frontApi/res/freeSeatIds' + suffix + '/' + segment(row['id']) + '/' + day
                        seats = self._post(path_seats, {**{k: body[k] for k in ['beginMinute', 'endMinute', 'minMinute']},
                                                      **({'amPm': 0} if mode == 'slice' else {})})
                        if not isinstance(seats, dict) or any(not isinstance(v, dict) for v in seats.values()):
                            raise QueryError('SCHEMA_CHANGED', '座位状态映射格式改变。')
                        item['seats'] = [{'id': key, **fields(v, ['status', 'seatNum', 'power', 'window', 'lock', 'scene', 'afterFree'])}
                                         for key, v in seats.items()]
                    result['rooms'].append(item)
                checked += 1
        result['checkedScopes'] = checked
        result['queryInterval'] = [params['from'], params['to']] if times else None
        return {'status': 'ok' if checked and (not params.get('room_id') or result['rooms']) else 'no_matching_catalog', **result}

    def close(self):
        self.token = None
        self.hmac_key = b''
        self.settings.clear()
        self.http.close()
