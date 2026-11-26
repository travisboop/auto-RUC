"""Qiangzhi undergraduate platform, using the account's live API map.

Only the read operations observed in the university's current student pages are
callable. The API map supplies URLs, never permission to invoke arbitrary APIs.
"""
import base64
from datetime import datetime
import json
import re
from urllib.parse import quote, urlsplit

from .auth import OAUTH_SERVICE, cas_login
from .data import BEIJING, date_value, fields, paged, records, select
from .errors import QueryError
from .http import MemoryHTTP, check_url

ORIGIN = 'https://jw.ruc.edu.cn'
ENTRY = ORIGIN + '/secService/oauthlogin2'
STUDENT = 'jw.xsd.xsdInfo.controller.'
CODES = {
    'roles': 'ZYGL08_framework.security.controller.SysUserController.roleList',
    'dictionary': 'ZYGL08_com.qzdatasoft.jwPublic.controller.JwPublicController.findPublicDic',
    'grade_gate': 'XSMH0526_' + STUDENT + 'CjglKccjckController.pjxtCheck',
    'grades': 'XSMH0526_' + STUDENT + 'CjglKccjckController.findKccjList',
    'ranking_terms': 'XSMH0527_' + STUDENT + 'CjglKccjckController.getStudentJczy013',
    'rankings': 'XSMH0527_' + STUDENT + 'CjglKccjckController.professionalRankingQuery',
    'calendar': 'XSMH1706_com.qzdatasoft.jw.jczy.educationinfo.controller.WeekCalendarInfoController.findWeekCalendarList',
    'periods': 'XSMH1706_jw.pkgl.pkglInfo.controller.PksjglController.findPksjInfoByOne',
    'buildings': 'XSMH1706_' + STUDENT + 'XsdJsjyglController.getDmtJxlList',
    'building_availability': 'XSMH1706_' + STUDENT + 'XsdJsjyglController.findJxlInfo',
    'classrooms': 'XSMH1706_' + STUDENT + 'XsdJsjyglController.findKxjsList',
}


def qz_encode(value):
    """Qz.base64Encrypt, including encodeURIComponent and its eight swaps."""
    encoded = base64.b64encode(quote(value, safe="~()*!.'-").encode()).decode()
    original, chars = list(encoded), list(encoded)
    if len(chars) < 16:
        raise QueryError('INVALID_ARGUMENT', '教务查询条件长度无效。')
    for index in [1, 3, 5, 7]:
        chars[index] = original[-index - 1]
        chars[-index - 1] = original[index]
    return 'QZDATASOFT' + ''.join(chars)


def query_body(custom, page=1, size=100, order=None):
    conditions = json.dumps({'conditionGroup': [{'link': 'and', 'condition': []}]}, separators=(',', ':'))
    return {**custom, 'page': {'pageIndex': page, 'pageSize': size,
            'orderBy': json.dumps(order, separators=(',', ':')) if order else '',
            'conditions': qz_encode(conditions)}}


def academic_data(envelope):
    if not isinstance(envelope, dict):
        raise QueryError('SCHEMA_CHANGED', '教务返回格式发生变化。')
    code = envelope.get('errorCode')
    if code == '401' or (isinstance(code, str) and code.startswith('security.httpstatu.401')):
        raise QueryError('LOGIN_REQUIRED', '教务未接受本次业务会话。')
    if code != 'success':
        raise QueryError('PLATFORM_REJECTED', '教务拒绝了本次查询，需核对当前账号权限或查询条件。')
    if 'data' not in envelope:
        raise QueryError('SCHEMA_CHANGED', '教务响应缺少 data。')
    return envelope['data']


def calendar_date(value):
    """The teaching calendar uses JavaScript epoch milliseconds, in Beijing time."""
    try:
        if type(value) is int:
            return datetime.fromtimestamp(value / 1000, BEIJING).date().isoformat()
        if isinstance(value, str):
            return date_value(value[:10])
    except (ValueError, OverflowError, OSError, QueryError):
        pass
    raise QueryError('SCHEMA_CHANGED', '教学日历日期既不是毫秒时间戳，也不是有效日期。')


class AcademicClient:
    def __init__(self, identity):
        self.http = MemoryHTTP(ORIGIN)
        self.api = {}
        self.headers = {'app': 'PCWEB', 'locale': 'zh_CN', 'userAgent': '',
                        'userRoleCode': '', 'X-Requested-With': 'XMLHttpRequest',
                        'Referer': ORIGIN + '/Njw2017/index.html',
                        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                                      'AppleWebKit/537.36 (KHTML, like Gecko) '
                                      'Chrome/131.0.0.0 Safari/537.36'}
        try:
            cas_login(self.http, identity, ENTRY, OAUTH_SERVICE,
                      upgrade_http_paths={'/Njw2017/index.html'})
            self.headers['TOKEN'] = self.http.cookie('token')
            assertion = self._json('/secService/assert.json?resourceCode=resourceCode&apiCode=framework.sign.controller.SignController.asserts')
            if not isinstance(assertion, dict) or not assertion.get('authcode'):
                raise QueryError('LOGIN_REQUIRED', '教务未返回已登录的当前账号。')
            self.student_id = str(assertion['authcode'])
            self.api = self._json('/resService/data/api.json?resourceCode=resourceCode&apiCode=framework.security.controller.SysUserController.api')
            if not isinstance(self.api, dict):
                raise QueryError('SCHEMA_CHANGED', '教务未返回账号的 API 映射。')
            roles = self.rpc('roles', {})
            checked = roles.get('checked') if isinstance(roles, dict) else None
            if not isinstance(checked, dict) or checked.get('agent') or checked.get('roleCode') != 'student':
                raise QueryError('STUDENT_ROLE_REQUIRED', '当前教务角色不是本人学生角色；请在官网核对。')
            self.headers['userRoleCode'] = checked['roleCode']
        except BaseException:
            self.close()
            raise

    def _json(self, path, body=None):
        return academic_data(self.http.json(path, body, self.headers))

    def rpc(self, operation, body):
        code = CODES.get(operation)
        if code is None:
            raise QueryError('UNKNOWN_OPERATION', '此教务操作未列入只读能力。')
        return self._json(self.resolve_code(code), body)

    def resolve_code(self, code):
        path = self.api.get(code)
        if not isinstance(path, str) or not path.startswith('/') or path.startswith('//'):
            raise QueryError('CAPABILITY_UNAVAILABLE', '当前账号的教务 API 映射未提供此查询。')
        path = path.replace('{version}', 'v1')
        if re.search(r'[{}]', path):
            raise QueryError('SCHEMA_CHANGED', '教务 API 路径新增了未识别参数。')
        url = ORIGIN + '/resService' + path
        check_url(url, {ORIGIN})
        if '..' in urlsplit(url).path.split('/'):
            raise QueryError('SCHEMA_CHANGED', '教务 API 路径无效。')
        return url

    def dictionary(self, *names):
        data = self.rpc('dictionary', {name: {} for name in names})
        if not isinstance(data, dict):
            raise QueryError('SCHEMA_CHANGED', '教务字典返回格式改变。')
        for name in names:
            records(data.get(name), name)
        return data

    def terms(self, keyword=None, current=False, ranking=False):
        values = (records(self.rpc('ranking_terms', {})) if ranking else
                  self.dictionary('JwPublicXnxq')['JwPublicXnxq'])
        if any(not isinstance(t.get('id'), str) or not isinstance(t.get('name'), str) for t in values):
            raise QueryError('SCHEMA_CHANGED', '学期目录缺少编号或名称。')
        matches = ([v for v in values if v['id'] == keyword or keyword in v['name']] if keyword else
                   [v for v in values if str(v.get('sfdqxqcode')) == '1'] if current else values)
        if not matches:
            raise QueryError('NO_MATCHING_TERM', '没有匹配的开放学期。')
        if (keyword or current) and len(matches) != 1:
            raise QueryError('AMBIGUOUS_TERM', '学期名称匹配多项，请使用完整名称或编号。',
                             {'terms': [fields(v, ['id', 'name']) for v in matches]})
        return matches

    def schedule(self, term):
        data = records(self.rpc('periods', {'dqdx': 'zgrmdx', 'jczy013id': term['id']}))
        if len(data) != 1:
            raise QueryError('SCHEMA_CHANGED', '教务节次表不是唯一配置。')
        periods = records(data[0].get('pkgl00201list'))
        for row in periods:
            if any(key not in row for key in ['zyxjs', 'djshow', 'idjkssj', 'idjjssj']):
                raise QueryError('SCHEMA_CHANGED', '教务节次缺少时间或编号。')
        return periods

    def options(self, params):
        kind = params.get('kind', 'terms')
        if kind == 'rankings':
            return {'status': 'ok', 'terms': [fields(v, ['id', 'name']) for v in self.terms(ranking=True)]}
        if kind == 'terms':
            return {'status': 'ok', 'terms': [fields(v, ['id', 'name', 'sfdqxqcode']) for v in self.terms()]}
        term = self.terms(params.get('term'), current='term' not in params)[0]
        return {'status': 'ok', 'term': fields(term, ['id', 'name']),
                'campuses': [fields(v, ['id', 'name']) for v in self.dictionary('JwPublicXq')['JwPublicXq']],
                'periods': [fields(v, ['zyxjs', 'djshow', 'idjkssj', 'idjjssj']) for v in self.schedule(term)]}

    def grades(self, params):
        terms = self.terms(params.get('term'))
        gate = self.rpc('grade_gate', {})
        if not isinstance(gate, dict) or 'sfckcjcode' not in gate:
            raise QueryError('SCHEMA_CHANGED', '官网成绩查看限制返回格式改变。')
        custom = {'pyfa007id': '1', 'jczy013id': [t['id'] for t in terms] if params.get('term') else [],
                  'fxjczy005id': '', 'cjckflag': 'xsdcjck'}
        restricted = str(gate['sfckcjcode']) != '1'
        if restricted:
            if 'kthList' not in gate:
                raise QueryError('POLICY_RESTRICTED', '学校限制了成绩查看范围。')
            custom['kthList'] = gate['kthList']
        # The current undergraduate page receives a full list, including term summary rows.
        rows = records(self.rpc('grades', query_body(custom, order=[{'field': 'jczy013id', 'sortType': 'asc'}])))
        columns = ['xnxq', 'xnxq_name', 'jczy013id', 'kcname', 'jsname', 'kclbname', 'xf',
                   'zcjname1', 'jd', 'cjbzname', 'cjgl037id', 'zxf', 'sumjd', 'sumxf', 'sumxfjd',
                   'pjxfjd', 'pjxfj', 'kcbh', 'kcxzname', 'ksxzname']
        results = []
        for row in rows:
            if not row.get('kcname') and not any(k in row for k in ['zxf', 'sumxf', 'xnxq']):
                raise QueryError('SCHEMA_CHANGED', '成绩记录没有课程或汇总字段。')
            results.append({**fields(row, columns), **{k: v for k, v in row.items() if re.fullmatch(r'cjxm\d+', k)},
                            'rowType': 'course' if row.get('kcname') else 'term_summary'})
        return {'status': 'partial' if restricted else 'ok', 'scope': '本人本科主修成绩',
                'terms': [fields(v, ['id', 'name']) for v in terms], 'records': results,
                'issues': [{'code': 'POLICY_RESTRICTED', 'message': '结果遵循官网评教后的成绩可见范围。'}] if restricted else []}

    def rankings(self, params):
        all_terms = self.terms(ranking=True)
        terms = self.terms(params['term'], ranking=True) if params.get('term') else all_terms
        identifiers = [t['id'] for t in terms]
        body = {'jczy013id': ','.join(identifiers), 'kclbcode': '', 'jczy010id': [],
                'jczy013idList': [] if len(terms) == len(all_terms) else identifiers}
        rows = records(self.rpc('rankings', body))
        if any('xh' not in row for row in rows):
            raise QueryError('SCHEMA_CHANGED', '排名记录缺少本人身份核对字段。')
        own = [row for row in rows if str(row['xh']) == self.student_id]
        if rows and not own:
            raise QueryError('IDENTITY_MISMATCH', '排名结果没有当前登录学生。')
        return {'status': 'ok', 'scope': '本人专业/班级排名；学校按所选学期计算',
                'terms': [fields(v, ['id', 'name']) for v in terms],
                'records': [fields(v, ['xnxq', 'skdw_name', 'ndzy_name', 'sdxf', 'zjd',
                                      'countnum', 'pjxfjd', 'pm', 'bjpm']) for v in own]}

    def classrooms(self, params):
        day = date_value(params.get('date'))
        term = self.terms(params.get('term'), current='term' not in params)[0]
        calendar = self.rpc('calendar', {'xnxq': term['id'], 'allshow': '0', 'ismr': '1'})
        if not isinstance(calendar, dict):
            raise QueryError('SCHEMA_CHANGED', '教学日历格式改变。')
        dates = records(calendar.get('jxzllist'))
        days = [v for v in dates if calendar_date(v.get('rq')) == day]
        if not days:
            return {'status': 'date_not_open', 'date': day, 'term': fields(term, ['id', 'name']), 'rooms': []}
        if len(days) != 1 or not all(k in days[0] for k in ['zc', 'xq']):
            raise QueryError('SCHEMA_CHANGED', '日期未对应唯一教学周和星期。')
        schedule = self.schedule(term)
        periods = []
        for wanted in params['periods']:
            matches = [p for p in schedule if wanted in {str(p['zyxjs']), str(p['djshow'])}]
            if len(matches) != 1:
                raise QueryError('INVALID_PERIOD', '节次未匹配当前时间表；先查询 academic_options。')
            if matches[0] not in periods:
                periods.append(matches[0])
        campuses = select(self.dictionary('JwPublicXq')['JwPublicXq'], params.get('campus'))
        rooms, checked = [], 0
        for campus in campuses:
            buildings = select(records(self.rpc('buildings', {'jczy002id': campus['id']})), params.get('building'))
            for building in buildings:
                if not building.get('id'):
                    raise QueryError('SCHEMA_CHANGED', '教学楼缺少编号。')
                custom = {'jczy013id': term['id'], 'jczy008id': building['id'],
                          'zc': str(days[0]['zc']), 'xq': str(days[0]['xq']),
                          'jc': [p['zyxjs'] for p in periods],
                          'djsj': [{'djkssj': str(p['idjkssj']), 'djjssj': str(p['idjjssj'])} for p in periods]}
                result = paged(lambda p, n: self.rpc('classrooms', query_body(custom, p, n)), 'items', 'rowCount')
                for row in result:
                    if not row.get('js_name'):
                        raise QueryError('SCHEMA_CHANGED', '空闲教室缺少名称。')
                checked += 1
                rooms.extend(fields(row, ['js_name', 'classroomtype_name', 'jxl_name', 'xq_name']) for row in result)
        return {'status': 'ok' if checked else 'no_matching_catalog', 'date': day,
                'term': fields(term, ['id', 'name']), 'checkedBuildings': checked,
                'periods': [fields(v, ['zyxjs', 'djshow', 'idjkssj', 'idjjssj']) for v in periods], 'rooms': rooms}

    def close(self):
        self.api.clear()
        self.headers.clear()
        self.student_id = None
        self.http.close()
