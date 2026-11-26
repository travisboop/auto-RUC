"""Live service discovery after the portal's own CAS callback."""
import json
import re
from urllib.parse import urlencode, urlsplit, urlunsplit

from .auth import cas_login
from .data import fields, records, select
from .errors import QueryError
from .http import MemoryHTTP, decode_json, send
from .urls import application_url, diagnostic_url

ORIGIN = 'https://my.ruc.edu.cn'
SERVICE = ORIGIN + '/commoncallback/'
PORTAL_CONTEXT = 'YXM9MiZ0PTEmcD0xJm09TiY_'


def decode_jsonp(status, body):
    if status != 200:
        return decode_json(status, body)
    match = re.fullmatch(rb'\s*rucAgent\s*\((.*)\)\s*;?\s*', body, re.S)
    return decode_json(status, match[1] if match else body)


def public_entry(value):
    return diagnostic_url(value)


class PortalClient:
    def __init__(self, identity):
        self.http = MemoryHTTP(ORIGIN)
        try:
            cas_login(self.http, identity, ORIGIN + '/', SERVICE)
            user = self._get('/sopplus/_web/portal/api/user/loginInfo.rst',
                             {'_p': 'YXM9MiZ0PTUmZD0xMzMmcD0xJmY9NDQmbT1OJg__'})
            if not isinstance(user, dict) or not user.get('loginName'):
                raise QueryError('LOGIN_REQUIRED', '门户未返回本次登录用户。')
        except BaseException:
            self.close()
            raise

    def _get(self, path, params):
        url = ORIGIN + path + '?' + urlencode({**params, 'callback': 'rucAgent'})
        status, _, body = send(self.http.opener, url)
        data = decode_jsonp(status, body)
        if not isinstance(data, dict) or data.get('result') not in {'1', 'success', 'true'}:
            raise QueryError('PLATFORM_REJECTED', '门户未返回成功结果。')
        if 'data' not in data:
            raise QueryError('SCHEMA_CHANGED', '门户响应缺少 data。')
        return data['data']

    def applications(self, params):
        applications = {}
        for path in ['loadAllServiceApps.jsp', 'loadAllServiceAppsByGlpt.jsp']:
            data = self._get('/sopplus/_web/customized/' + path,
                             {'_p': PORTAL_CONTEXT, 'appName': '', 'parentCategoryId': '',
                              'categoryId': '', 'sortType': '', 'orgId': '', 'modeId': '', 'uniqueIden': ''})
            if not isinstance(data, dict):
                raise QueryError('SCHEMA_CHANGED', '门户应用目录格式改变。')
            apps = records(data.get('appList'))
            if 'total' in data and data['total'] != len(apps):
                raise QueryError('INCOMPLETE', '门户应用目录返回数量与总数不符。')
            for app in apps:
                if app.get('appId') is None:
                    raise QueryError('SCHEMA_CHANGED', '门户应用缺少编号。')
                applications[app['appId']] = {
                    **fields(app, ['appId', 'name', 'appName', 'isAllow', 'reason', 'applyUserScope']),
                    'appUrl': application_url(app.get('appUrl')),
                    'entranceUrl': application_url(app.get('entranceUrl'))}
        matches = select(list(applications.values()), params.get('keyword'))
        limit = params.get('limit', 100)
        return {'status': 'ok', 'totalMatches': len(matches), 'returnedCount': min(limit, len(matches)),
                'truncated': len(matches) > limit, 'applications': matches[:limit]}

    def close(self):
        self.http.close()
