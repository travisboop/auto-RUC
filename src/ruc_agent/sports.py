"""Sports catalog and availability. No order, payment or reservation mutations."""
import base64
from datetime import datetime
import hashlib
import json
import re
import time
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request

from .auth import OAUTH_SERVICE, cas_login
from .data import BEIJING, date_value, fields, interval, minute, records, select
from .errors import QueryError
from .http import MemoryHTTP, check_url, decode_json, send

ORIGIN = 'https://cgyy.ruc.edu.cn'
RESOURCE_ORIGIN = 'https://easybook.ruc.edu.cn'
BASE = '/venue-server'
READ_PATHS = {'/api/front/website/init', '/api/front/website/venue_information',
              '/api/reservation/campus/venue/info', '/api/reservation/day/info'}
SITE_FIELDS = ['siteName', 'venueName', 'campusName', 'sportType',
               'isSupportReservation', 'reservationHour', 'advanceDay',
               'orderAdvanceMin', 'orderMaxMin']


def signing_config(source):
    module = source.split('f121:function', 1)
    if len(module) != 2:
        raise QueryError('CONFIG_CHANGED', '体育官网签名配置模块改变。')
    values = {}
    try:
        for key in ['s', 'a']:
            match = re.search(r'\b' + key + r':\[([^\]]+)\]', module[1])
            if not match:
                raise ValueError()
            parts = json.loads('[' + match[1] + ']')
            joined = ''.join(parts)
            values[key] = base64.b64decode(joined[::-1] if key == 'a' else joined, validate=True).decode()
    except (ValueError, TypeError, UnicodeError):
        raise QueryError('CONFIG_CHANGED', '体育官网签名配置无法解析。') from None
    return values['a'], values['s']


def signature(app_key, secret, path, params, timestamp):
    def scalar(value):
        return str(value).lower() if isinstance(value, bool) else str(value)
    value = secret + path + ''.join(k + scalar(params[k]) for k in sorted(params)
                                   if params[k] is not None and params[k] != '' and not isinstance(params[k], (list, dict)))
    value += str(timestamp) + ' ' + secret
    return {'app-key': app_key, 'timestamp': str(timestamp), 'sign': hashlib.md5(value.encode()).hexdigest()}


def sports_data(envelope):
    if not isinstance(envelope, dict):
        raise QueryError('SCHEMA_CHANGED', '体育平台响应不是对象。')
    if envelope.get('code') in {401, 406, 408}:
        raise QueryError('LOGIN_REQUIRED', '体育平台要求本次业务认证。')
    message = envelope.get('message', envelope.get('msg', ''))
    opening = re.fullmatch(r'当日([0-9]{1,2})点以后才能查看当天的预约', message) if isinstance(message, str) else None
    if envelope.get('code') == 250 and opening and int(opening[1]) < 24:
        raise QueryError('QUERY_NOT_OPEN', '该项目当天的预约查询尚未开放。',
                         {'availableAfter': f'{int(opening[1]):02d}:00', 'timezone': 'Asia/Shanghai'})
    if envelope.get('code') != 200:
        raise QueryError('PLATFORM_REJECTED', '体育平台未接受本次查询。')
    if envelope.get('data') is None:
        raise QueryError('SCHEMA_CHANGED', '体育平台查询未返回 data。')
    return envelope['data']


class SportsClient:
    def __init__(self, identity, origin=ORIGIN):
        check_url(origin, {ORIGIN, RESOURCE_ORIGIN})
        self.origin = origin
        self.http = MemoryHTTP(origin)
        self.identity = identity
        self.token = None
        self.app_key = self.sign_key = None
        try:
            status, _, html = send(self.http.opener, self.origin + '/venue')
            match = re.search(rb'src="(/venue/js/chingo\.[a-f0-9]+\.js)"', html)
            if status != 200 or not match:
                raise QueryError('CONFIG_CHANGED', '体育官网主脚本入口改变。')
            status, _, source = send(self.http.opener, self.origin + match[1].decode())
            if status != 200:
                raise QueryError('CONFIG_CHANGED', '无法读取体育官网公开配置。')
            self.app_key, self.sign_key = signing_config(source.decode('utf-8'))
        except BaseException:
            self.close()
            raise

    def _request(self, path, params=None, auth_headers=None):
        login = path in {'/api/login', '/roleLogin'}
        if not login and path not in READ_PATHS:
            raise QueryError('UNKNOWN_OPERATION', '此体育接口未列入只读能力。')
        return sports_data(self.request(BASE + path, 'POST' if login else 'GET',
                           body=params if login else None, query=params if not login else None,
                           auth_headers=auth_headers, nocache=not login))

    def request(self, path, method='GET', query=None, body=None, encoding='form', auth_headers=None, nocache=True):
        if (not path.startswith(BASE + '/') or method not in {'GET', 'POST'} or encoding != 'form'
                or (query is not None and body is not None)):
            raise QueryError('INVALID_CONTRACT', '体育签名接口使用 /venue-server 路径及 GET 或表单 POST。')
        check_url(self.origin + path, {self.origin})
        signing_path = path[len(BASE):]
        params = body if method == 'POST' else query
        params = dict(params or {})
        stamp = int(time.time() * 1000)
        if nocache:
            params['nocache'] = stamp
        headers = signature(self.app_key, self.sign_key, signing_path, params, stamp)
        if self.token:
            headers['cgAuthorization'] = self.token
        headers.update(auth_headers or {})
        encoded = urlencode({k: v for k, v in params.items() if v is not None}).encode()
        url = self.origin + path
        if method == 'POST':
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
            request = Request(url, data=encoded, headers=headers, method='POST')
        else:
            request = Request(url + '?' + encoded.decode(), headers=headers)
        status, _, body = send(self.http.opener, request)
        return decode_json(status, body)

    def authenticate(self):
        if self.token:
            return
        login_path = '/oauth_login' if self.origin == RESOURCE_ORIGIN else '/loginto'
        target = cas_login(self.http, self.identity, self.origin + BASE + login_path, OAUTH_SERVICE)
        parts = urlsplit(target)
        values = parse_qs(parts.query)
        if '?' in parts.fragment:
            values.update(parse_qs(parts.fragment.split('?', 1)[1]))
        oauth = values.get('oauth_token', [])
        if len(oauth) != 1 or not oauth[0]:
            raise QueryError('SSO_ERROR', '体育回调未返回本次 OAuth 交换令牌。')
        login = self._request('/api/login', {}, {'oauth-token': oauth[0]})
        if not isinstance(login, dict):
            raise QueryError('SCHEMA_CHANGED', '体育登录结果格式改变。')
        roles = records(login.get('roles'))
        students = [v for v in roles if str(v.get('id')) == '3']
        if len(students) != 1:
            raise QueryError('STUDENT_ROLE_REQUIRED', '体育平台未提供唯一的学生角色。')
        token = login.get('token', {}).get('access_token')
        if not isinstance(token, str) or not token:
            raise QueryError('LOGIN_REQUIRED', '体育平台未返回角色交换令牌。')
        result = self._request('/roleLogin', {'roleid': students[0]['id']}, {'cgAuthorization': token})
        token = result.get('token', {}).get('access_token') if isinstance(result, dict) else None
        if not isinstance(token, str) or not token:
            raise QueryError('LOGIN_REQUIRED', '体育平台未建立学生业务会话。')
        self.token = token

    def public_catalog(self, params):
        data = self._request('/api/front/website/init', {'catalogSite': 0, 'indexPictureSize': 0,
                            'articleSize': 0, 'venueForSiteSize': 5, 'sportSize': 100, 'hotSiteSize': 0})
        if not isinstance(data, dict):
            raise QueryError('SCHEMA_CHANGED', '体育目录格式改变。')
        venues = select(records(data.get('venueList')), params.get('venue'), 'venueName')
        sites, issues = [], []
        for venue in venues:
            detail = self._request('/api/front/website/venue_information',
                                   {'isNeedSearchVenue': 0, 'venueId': venue['venueId']})
            if not isinstance(detail, dict):
                raise QueryError('SCHEMA_CHANGED', '体育场馆明细格式改变。')
            site_list = records(detail.get('siteList'))
            if type(venue.get('siteCount')) is int and venue['siteCount'] != len(site_list):
                issues.append({'code': 'CATALOG_COUNT_MISMATCH', 'venue': venue.get('venueName'),
                               'summaryCount': venue['siteCount'], 'detailCount': len(site_list)})
            sites.extend(self.site(s) for s in site_list)
        return self.select_sites(sites, params), issues

    @staticmethod
    def site(row):
        identifier = row.get('siteId') or row.get('id')
        if type(identifier) not in {str, int} or identifier == '' or not row.get('siteName'):
            raise QueryError('SCHEMA_CHANGED', '体育场地缺少编号或名称。')
        return {'siteId': identifier, **fields(row, SITE_FIELDS)}

    @staticmethod
    def select_sites(sites, params):
        sites = select(sites, params.get('venue'), 'venueName')
        for key, field in [('campus', 'campusName'), ('site', 'siteName')]:
            sites = select(sites, params.get(key), field)
        if params.get('site_id') is not None:
            sites = [s for s in sites if str(s['siteId']) == str(params['site_id'])]
        return sites

    def authenticated_catalog(self, params):
        data = self._request('/api/reservation/campus/venue/info')
        if not isinstance(data, dict) or not isinstance(data.get('venueSiteInfo'), dict):
            raise QueryError('SCHEMA_CHANGED', '体育登录后的场地目录格式改变。')
        sites = [self.site(row) for rows in data['venueSiteInfo'].values() for row in records(rows)]
        if len({str(s['siteId']) for s in sites}) != len(sites):
            raise QueryError('INCOMPLETE', '体育场地目录出现重复编号。')
        return self.select_sites(sites, params)

    def query(self, params):
        if params.get('catalog_only'):
            sites, issues = self.public_catalog(params)
            status = 'partial' if issues else 'ok' if sites else 'no_matching_catalog'
            return {'status': status, 'authentication': 'public', 'scope': '官网公开详情目录', 'sites': sites, 'issues': issues}
        self.authenticate()
        sites = self.authenticated_catalog(params)
        status, issues = ('ok' if sites else 'no_matching_catalog'), []
        if not sites:
            return {'status': status, 'sites': [], 'issues': issues}
        day, times = date_value(params.get('date')), interval(params)
        groups = []
        for site in sites:
            if site.get('isSupportReservation') == 0:
                groups.append({**site, 'status': 'not_reservable', 'spaces': []})
                continue
            try:
                data = self._request('/api/reservation/day/info', {'venueSiteId': site['siteId'], 'searchDate': day})
                groups.append({**site, **availability(data, day, times)})
            except QueryError as error:
                if error.code in {'LOGIN_REQUIRED', 'RATE_LIMITED', 'ACCESS_DENIED'}:
                    raise
                status = 'partial'
                details = error.details or {}
                issues.append({'siteId': site['siteId'], 'code': error.code, 'message': str(error), **details})
                groups.append({**site, 'status': 'query_not_open' if error.code == 'QUERY_NOT_OPEN' else 'unknown',
                               'spaces': [], **details})
        return {'status': status, 'date': day, 'scope': '当前学生角色的可预约项目目录',
                'groups': groups, 'issues': issues}

    def close(self):
        self.token = self.app_key = self.sign_key = None
        self.http.close()


def availability(data, day, interval_minutes=None):
    if not isinstance(data, dict) or not isinstance(data.get('reservationDateList'), list):
        raise QueryError('SCHEMA_CHANGED', '体育余量缺少开放日期。')
    dates = data['reservationDateList']
    if day not in dates:
        return {'status': 'date_not_open', 'openDates': dates, 'spaces': []}
    if data.get('notReserveType') == 1:
        return {'status': 'not_reservable', 'reason': 'ROLE_TIME_RESTRICTED', 'openDates': dates, 'spaces': []}
    if data.get('isTakeUp') == 1:
        return {'status': 'closed', 'openDates': dates, 'spaces': []}
    if ('spaceTimeInfo' in data and 'reservationDateSpaceInfo' in data
            and data['spaceTimeInfo'] is None and data['reservationDateSpaceInfo'] is None):
        return {'status': 'not_scheduled', 'openDates': dates, 'spaces': []}
    clock = records(data.get('spaceTimeInfo'))
    mapping = data.get('reservationDateSpaceInfo')
    if not isinstance(mapping, dict) or day not in mapping:
        raise QueryError('SCHEMA_CHANGED', '体育余量缺少所选日期的场地。')
    spaces = records(mapping[day])
    total = data.get('reservationTotalNum')
    if type(total) is not int or total < 1:
        raise QueryError('SCHEMA_CHANGED', '体育时段容量无效。')
    now = datetime.now(BEIJING)
    result = []
    for space in spaces:
        slots, free_intervals = [], []
        for slot in clock:
            if not all(k in slot for k in ['id', 'beginTime', 'endTime']):
                raise QueryError('SCHEMA_CHANGED', '体育时间格缺少编号或时间。')
            begin, end = minute(slot['beginTime'][:5]), minute(slot['endTime'][:5])
            cell = space.get(str(slot['id']))
            if not isinstance(cell, dict) or cell.get('reservationStatus') not in {1, 2, 3, 4, 33} or begin >= end:
                raise QueryError('SCHEMA_CHANGED', '体育时间格状态缺失或无效。')
            state = cell['reservationStatus']
            used = cell.get('alreadyNum')
            if used is not None and (type(used) is not int or not 0 <= used <= total):
                raise QueryError('SCHEMA_CHANGED', '体育时段已占容量无效。')
            if total > 1 and state == 1 and used is None:
                raise QueryError('SCHEMA_CHANGED', '体育多人时段缺少已占容量。')
            if state == 1 and total == 1 and used not in {None, 0}:
                raise QueryError('SCHEMA_CHANGED', '体育时段的空闲状态与已占容量冲突。')
            # The current page's reserveHandler accepts state FREE (1) only.
            # Capacity colors 3/33 must not be promoted to selectable state 1.
            free = state == 1 and (used is None or used < total)
            free = free and not cell.get('takeUp') and not cell.get('adminTake')
            if day < now.date().isoformat() or (day == now.date().isoformat() and begin * 60 <= now.hour * 3600 + now.minute * 60 + now.second):
                free = False
            slots.append({'from': slot['beginTime'], 'to': slot['endTime'], 'state': state,
                          'free': bool(free), 'capacity': total, 'occupied': used})
            if free:
                free_intervals.append((begin, end))
        if interval_minutes:
            start, end = interval_minutes
            cursor = start
            for left, right in sorted(free_intervals):
                if left == cursor and right <= end:
                    cursor = right
            if cursor != end:
                continue
        result.append({**fields(space, ['id', 'spaceName']), 'slots': slots})
    return {'status': 'checked', 'openDates': dates, 'spaces': result}
