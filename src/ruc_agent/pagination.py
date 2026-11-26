"""Bounded collection for explicitly declared page-number or offset APIs."""
from copy import deepcopy
import json

from .contracts import at, decode_response, fingerprint, page_values
from .errors import QueryError


def check_pagination(spec, max_pages):
    rule = spec.get('response', {}).get('pagination', {})
    parameter = rule.get('pageParameter')
    if not parameter or parameter not in spec['parameters']['properties']:
        raise QueryError('PAGINATION_UNSUPPORTED', '此能力没有声明翻页参数；先使用单次查询或补齐已核实契约。')
    if type(max_pages) is not int or not 1 <= max_pages <= 100:
        raise QueryError('INVALID_ARGUMENT', 'max_pages 须为 1 至 100 的整数。')
    return rule


def collect_pages(fetch, spec, params, max_pages):
    rule = check_pagination(spec, max_pages)
    rows, seen, expected = [], set(), None
    page_parameter = rule['pageParameter']
    first = rule.get('firstPage', 1)
    step = 1
    if 'pageStepParameter' in rule:
        key = rule['pageStepParameter']
        step = params.get(key, spec['parameters']['properties'][key].get('default'))
        if type(step) is not int or step < 1:
            raise QueryError('INVALID_ARGUMENT', '偏移分页必须有正整数页大小。')
    for offset in range(max_pages):
        page = first + offset * step
        from .schema import validate
        validate(page, spec['parameters']['properties'][page_parameter], page_parameter)
        raw = fetch({**deepcopy(params), page_parameter: page})
        decode_response(spec, raw)
        items, total = page_values(raw, rule)
        if expected is not None and total != expected:
            raise QueryError('CATALOG_CHANGED', '翻页期间总数改变，请重新查询以避免遗漏。')
        expected = total
        for item in items:
            key = at(item, rule['identityPath']) if 'identityPath' in rule else item
            key = json.dumps(key, ensure_ascii=False, sort_keys=True)
            if key in seen:
                raise QueryError('INCOMPLETE', '翻页返回重复记录，不能判定已获取全部数据。')
            seen.add(key)
        from .redaction import redact
        rows.extend(redact(items))
        has_next = at(raw, rule['hasNextPath']) if 'hasNextPath' in rule else None
        if ((total is not None and (len(rows) > total or (not items and len(rows) != total)))
                or (has_next is True and not items)):
            raise QueryError('INCOMPLETE', '分页数量与官网总数不符。')
        complete = len(rows) == total if total is not None else not has_next
        if complete or offset + 1 == max_pages:
            return {'status': 'ok' if complete else 'partial', 'data': rows,
                    'schemaFingerprint': fingerprint(rows), 'totalItems': total,
                    'returnedItems': len(rows), 'complete': complete, 'pagesRead': offset + 1,
                    'nextPage': None if complete else page + step, 'pageParameter': page_parameter,
                    'pagination': 'all_pages',
                    'issues': [] if complete else [{'code': 'PAGE_LIMIT', 'message': '已到达本次翻页上限。'}]}
