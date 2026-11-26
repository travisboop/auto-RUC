"""Small shared validators for live data; never turn malformed data into emptiness."""
from datetime import date, datetime
import re
from zoneinfo import ZoneInfo

from .errors import QueryError

BEIJING = ZoneInfo('Asia/Shanghai')


def today():
    return datetime.now(BEIJING).date().isoformat()


def date_value(value=None):
    value = value or today()
    try:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError()
        date.fromisoformat(value)
    except (ValueError, TypeError):
        raise QueryError('INVALID_ARGUMENT', '日期须为有效的 YYYY-MM-DD。') from None
    return value


def minute(value):
    if not isinstance(value, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d|24:00', value):
        raise QueryError('INVALID_ARGUMENT', '时间须为 HH:MM，最晚 24:00。')
    hour, minutes = map(int, value.split(':'))
    return hour * 60 + minutes


def interval(params):
    if ('from' in params) != ('to' in params):
        raise QueryError('INVALID_ARGUMENT', 'from 和 to 须成对提供。')
    if 'from' not in params:
        return None
    start, end = minute(params['from']), minute(params['to'])
    if start >= end:
        raise QueryError('INVALID_ARGUMENT', '结束时间须晚于开始时间。')
    return start, end


def records(value, label='记录'):
    if not isinstance(value, list) or any(not isinstance(x, dict) for x in value):
        raise QueryError('SCHEMA_CHANGED', label + '未返回对象列表。')
    return value


def select(items, keyword, field='name'):
    records(items)
    if keyword is None:
        return items
    return [x for x in items if keyword.casefold() in str(x.get(field) or '').casefold()]


def fields(item, names):
    return {key: item[key] for key in names if key in item}


def paged(fetch, items_key, total_key, page_size=100, max_pages=100, identity_key=None):
    result, expected, seen = [], None, set()
    for page in range(1, max_pages + 1):
        data = fetch(page, page_size)
        if not isinstance(data, dict):
            raise QueryError('SCHEMA_CHANGED', '分页结果不是对象。')
        rows, total = records(data.get(items_key)), data.get(total_key)
        if type(total) is not int or total < 0:
            raise QueryError('SCHEMA_CHANGED', '分页总数无效。')
        if expected is not None and total != expected:
            raise QueryError('CATALOG_CHANGED', '读取期间记录总数发生变化。')
        expected = total
        if identity_key:
            for row in rows:
                key = row.get(identity_key)
                if type(key) not in {str, int} or key == '' or key in seen:
                    raise QueryError('INCOMPLETE', '分页记录编号缺失或重复。')
                seen.add(key)
        result.extend(rows)
        if len(result) == total:
            return result
        if not rows or len(result) > total:
            raise QueryError('INCOMPLETE', '分页数量与官网总数不符。')
    raise QueryError('INCOMPLETE', '分页超过查询上限，未获得完整结果。')
