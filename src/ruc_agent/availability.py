"""Validate booking data and turn time slices into bookable windows."""
from dataclasses import dataclass
from datetime import date, datetime
import re

from .booths import BEIJING, QueryError


def clock(value):
    return f'{value // 60:02d}:{value % 60:02d}'


def minutes(value):
    if not isinstance(value, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d|24:00', value):
        raise QueryError('INVALID_ARGUMENT', '时间须为 HH:MM，最晚为 24:00。')
    hour, minute = map(int, value.split(':'))
    return hour * 60 + minute


@dataclass(frozen=True)
class Query:
    date: str
    campus: str = None
    venue: str = None
    type: str = None
    minutes: int = 0
    start: int = None
    end: int = None

    @classmethod
    def parse(cls, values, now=None):
        now = now or datetime.now(BEIJING)
        allowed = {'date', 'campus', 'venue', 'type', 'minutes', 'from', 'to'}
        if not isinstance(values, dict) or set(values) - allowed:
            raise QueryError('INVALID_ARGUMENT', '查询包含未知参数。')
        selected = values.get('date', now.date().isoformat())
        try:
            parsed = date.fromisoformat(selected)
        except (TypeError, ValueError):
            raise QueryError('INVALID_ARGUMENT', '日期须为有效的 YYYY-MM-DD。') from None
        if parsed.isoformat() != selected or parsed < now.date():
            raise QueryError('INVALID_ARGUMENT', '查询日期须为北京时间的今天或未来日期。')
        duration = values.get('minutes', 0)
        if type(duration) is not int or not 0 <= duration <= 1440:
            raise QueryError('INVALID_ARGUMENT', 'minutes 须为 0 至 1440 的整数。')
        filters = {key: values.get(key) for key in ['campus', 'venue', 'type']}
        if any(v is not None and (not isinstance(v, str) or not v.strip()) for v in filters.values()):
            raise QueryError('INVALID_ARGUMENT', '校区、楼宇和舱型须为非空字符串。')
        start, end = values.get('from'), values.get('to')
        if ('from' in values) != ('to' in values):
            raise QueryError('INVALID_ARGUMENT', 'from 和 to 需要成对指定。')
        if 'from' in values:
            start, end = minutes(start), minutes(end)
            if start >= end:
                raise QueryError('INVALID_ARGUMENT', '结束时间须晚于开始时间。')
        return cls(selected, **filters, minutes=duration, start=start, end=end)


def availability(room, query, now):
    if not isinstance(room, dict) or not isinstance(room.get('name'), str):
        raise QueryError('SCHEMA_CHANGED', '舱位名称缺失。')
    if room.get('timeType') != 'SLICE' or room.get('seatOpen') != 'CLOSE' or room.get('status') != 0:
        raise QueryError('SCHEMA_CHANGED', '舱位预约模式或状态需要核对。')
    policy = room.get('bookPolicy')
    if not isinstance(policy, dict):
        raise QueryError('SCHEMA_CHANGED', '舱位预约规则缺失。')
    limits = [policy.get(k) for k in ['minTime', 'maxTime', 'minUser', 'maxUser']]
    if not all(type(v) is int for v in limits) or not 0 < limits[0] <= limits[1] or not 0 < limits[2] <= limits[3]:
        raise QueryError('SCHEMA_CHANGED', '舱位时长或人数规则缺失。')
    timelines = room.get('roomTimeSliceDtoList')
    if not isinstance(timelines, list) or not all(isinstance(s, dict) for s in timelines):
        raise QueryError('SCHEMA_CHANGED', '舱位时间轴格式发生变化。')
    slices = [s for s in timelines if s.get('date') == query.date]
    if not slices:
        raise QueryError('INCOMPLETE', '官网没有返回该舱所查日期的时间轴。')
    earliest = 0
    if query.date < now.date().isoformat():
        earliest = 1440
    elif query.date == now.date().isoformat():
        earliest = now.hour * 60 + now.minute + int(bool(now.second or now.microsecond))
    windows = set()
    for item in slices:
        start, end, step = [item.get(k) for k in ['openTime', 'closeTime', 'timeSlice']]
        disabled = item.get('disableTime')
        if not all(type(v) is int for v in [start, end, step]) or not 0 <= start <= end <= 1440 or step <= 0 or not isinstance(disabled, list):
            raise QueryError('SCHEMA_CHANGED', '官网时间轴格式发生变化。')
        if any(not isinstance(r, list) or len(r) != 2 or any(type(v) is not int for v in r) or r[0] > r[1] for r in disabled):
            raise QueryError('SCHEMA_CHANGED', '官网不可约区间格式发生变化。')
        spans = []
        for a in range(start, end - step + 1, step):
            b = a + step
            if a < earliest or any(x < y and x < b and y > a for x, y in disabled):
                continue
            if spans and spans[-1][1] == a:
                spans[-1][1] = b
            else:
                spans.append([a, b])
        needed = ((max(limits[0], query.minutes) + step - 1) // step) * step
        if needed > limits[1]:
            continue
        for a, b in spans:
            if b - a < needed:
                continue
            if query.start is not None:
                duration = query.end - query.start
                if (query.start < a or query.end > b or not needed <= duration <= limits[1]
                        or (query.start - start) % step or duration % step):
                    continue
                a, b = query.start, query.end
            windows.add((a, b))
    return {'id': room['id'], 'name': ' '.join(room['name'].split()), 'people': limits[2:],
            'bookingMinutes': limits[:2],
            'windows': [[clock(a), clock(b)] for a, b in sorted(windows)]}


def catalog(data):
    if not isinstance(data, list) or not all(
        isinstance(x, dict) and type(x.get('id')) in {int, str} and x['id'] != ''
        and isinstance(x.get('name'), str) for x in data
    ):
        raise QueryError('SCHEMA_CHANGED', '官网目录格式发生变化。')
    return data


def collect(client, query, now=None):
    groups, issues = [], []
    checked = 0

    def record(scope, error):
        if error.code in {'LOGIN_REQUIRED', 'RATE_LIMITED'}:
            raise error
        issues.append({**scope, 'code': error.code, 'reason': str(error)})

    campuses = catalog(client.query('getSchoolList'))
    matched = []
    for campus in campuses:
        if query.campus and query.campus not in campus['name']:
            continue
        matched.append(campus['name'])
        scope = {'campus': campus['name']}
        try:
            venues = catalog(client.query('getVenueList', {'schoolId': campus['id']}))
        except QueryError as error:
            record(scope, error)
            continue
        for venue in venues:
            if query.venue and query.venue not in venue['name']:
                continue
            where = {**scope, 'venue': venue['name']}
            try:
                types = catalog(client.query('getRoomTypeList', {'venueId': venue['id']}))
            except QueryError as error:
                record(where, error)
                continue
            for room_type in types:
                if not re.search(r'(?:静音|单人|多人)[舱仓]', room_type['name']):
                    continue
                if query.type and query.type not in room_type['name']:
                    continue
                group = {**where, 'type': room_type['name']}
                groups.append(group)
                params = {'venueId': venue['id'], 'roomTypeId': room_type['id']}
                try:
                    dates = client.query('getOpenDateList', params)
                    if not isinstance(dates, list) or not all(isinstance(d, str) for d in dates):
                        raise QueryError('SCHEMA_CHANGED', '官网开放日期格式发生变化。')
                    group['openDates'] = dates
                    if query.date not in dates:
                        group['status'] = 'date_not_open'
                        continue
                    rooms = client.rooms({**params, 'selectDate': query.date})
                    group.update(status='checked', roomCount=len(rooms), rawRooms=rooms)
                except QueryError as error:
                    group['status'] = 'unknown'
                    record({**where, 'type': room_type['name']}, error)
                    continue
    # Filter elapsed slots after network reads, using one common observation time.
    now = now or datetime.now(BEIJING)
    for group in groups:
        if group['status'] != 'checked':
            continue
        rooms = group.pop('rawRooms')
        group['rooms'] = []
        for room in rooms:
            try:
                result = availability(room, query, now)
                checked += 1
                if result['windows']:
                    group['rooms'].append(result)
            except QueryError as error:
                group['status'] = 'partial'
                scope = {k: group[k] for k in ['campus', 'venue', 'type']}
                record({**scope, 'room': room.get('name')}, error)
        group['rooms'].sort(key=lambda r: (r['windows'][0][0], r['name']))
    return {'status': 'partial' if issues else 'ok' if groups else 'no_matching_catalog',
            'asOf': now.isoformat(timespec='seconds'), 'date': query.date,
            'authentication': 'cas_rest_password', 'freshSession': True,
            'transport': 'python_stdlib_http', 'campuses': matched,
            'listedRooms': sum(g.get('roomCount', 0) for g in groups), 'checkedRooms': checked,
            'availableRooms': sum(len(g.get('rooms', [])) for g in groups),
            'groups': groups, 'issues': issues}
