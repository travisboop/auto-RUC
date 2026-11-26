#!/usr/bin/env python3
"""Explicit fresh-session acceptance run. Saves counts and redacted HTTP shapes only."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from time import perf_counter
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from ruc_agent import auth, booths, http, portal, sports
from ruc_agent.credentials import read_credentials
from ruc_agent.data import BEIJING
from ruc_agent.engine import AUTH_STOPS, CampusSession, error_result
from ruc_agent.errors import QueryError


def shape(value, depth=0):
    if isinstance(value, dict) and depth < 3:
        keys = [k for k in value if k not in {'token', 'password', 'hmacKey'}]
        result = {k: shape(value[k], depth + 1) for k in keys[:30]}
        if len(keys) > 30:
            result['_omittedKeyCount'] = len(keys) - 30
        return result
    if isinstance(value, list):
        return {'type': 'array', 'count': len(value), 'firstShape': shape(value[0], depth + 1) if value and depth < 3 else None}
    return type(value).__name__


def safe_path(path):
    if '/cas/v1/tickets/' in path:
        return '/cas/v1/tickets/<ticket>'
    if '/static/public/auth/cas/' in path:
        return '/jsq/static/public/auth/cas/<ticket>'
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credentials-file', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--capabilities', nargs='+', default=['quiet_booths', 'library_seats', 'sports_venues',
                                                           'grades', 'rankings', 'classrooms', 'applications'])
    args = parser.parse_args()
    trace, results = [], []
    original = http.send
    def traced(client, request):
        url = request if isinstance(request, str) else request.full_url
        parts = urlsplit(url)
        status, headers, body = original(client, request)
        item = {'host': parts.hostname, 'path': safe_path(parts.path),
                'method': 'GET' if isinstance(request, str) else request.get_method(), 'httpStatus': status}
        if headers.get('Location'):
            target = urlsplit(headers['Location'])
            item['redirect'] = {'scheme': target.scheme, 'host': target.hostname, 'path': safe_path(target.path),
                                'queryKeys': sorted(parse_qs(target.query))}
        try:
            value = json.loads(body)
            item['responseShape'] = shape(value)
            for key in ['code', 'errorCode', 'status', 'result']:
                if isinstance(value, dict) and value.get(key) in {200, 201, 401, 403, 408, 429, True, False, 'success', '1', '0', 'true', 'false'}:
                    item[key] = value[key]
        except (ValueError, UnicodeError, TypeError):
            pass
        trace.append(item)
        return status, headers, body
    for module in [http, auth, booths, portal, sports]:
        module.send = traced
    session = CampusSession(lambda: read_credentials(args.credentials_file, interactive=False))
    started = datetime.now(BEIJING).isoformat(timespec='seconds')
    clock = perf_counter()
    try:
        with session:
            for capability in args.capabilities:
                params = {'campus': '中关村', 'type': '单人', 'minutes': 60} if capability == 'quiet_booths' else {}
                if capability == 'applications':
                    params = {'keyword': '图书馆'}
                before = len(trace)
                query_clock = perf_counter()
                try:
                    if capability == 'classrooms':
                        options = session.query('academic_options', {'kind': 'classrooms'})
                        if not options['periods']:
                            raise QueryError('SCHEMA_CHANGED', '官网没有返回节次。')
                        params = {'periods': [str(options['periods'][-1]['zyxjs'])], 'campus': '中关村'}
                    result = session.query(capability, params)
                    item = {'capability': capability, 'status': result['status'], 'requestCount': len(trace) - before}
                    if 'data' in result:
                        item['dataShape'] = shape(result['data'])
                        for key in ['schemaFingerprint', 'pageItems', 'totalItems', 'complete']:
                            if key in result:
                                item[key] = result[key]
                    if capability == 'library_seats' and result.get('rooms'):
                        detail = session.query(capability, {'room_id': str(result['rooms'][0]['id'])})
                        item['seatDetailStatus'] = detail['status']
                        item['seatDetailCount'] = sum(len(v.get('seats', [])) for v in detail.get('rooms', []))
                        item['requestCount'] = len(trace) - before
                    for key in ['listedRooms', 'checkedRooms', 'availableRooms', 'checkedBuildings', 'totalMatches']:
                        if key in result:
                            item[key] = result[key]
                    for key in ['groups', 'rooms', 'records', 'applications']:
                        if isinstance(result.get(key), list):
                            item[key + 'Count'] = len(result[key])
                    if isinstance(result.get('groups'), list):
                        item['groupStatuses'] = [g.get('status') for g in result['groups']]
                    if capability == 'grades':
                        item['courseRecordsCount'] = sum(r.get('rowType') == 'course' for r in result.get('records', []))
                    if capability == 'classrooms':
                        item['date'] = result['date']
                        item['periods'] = result.get('periods', [])
                    item['issueCodes'] = [v.get('code') for v in result.get('issues', [])]
                except QueryError as error:
                    item = {'capability': capability, **error_result(error), 'requestCount': len(trace) - before}
                item['elapsedSeconds'] = round(perf_counter() - query_clock, 3)
                results.append(item)
                print(json.dumps(item, ensure_ascii=False), flush=True)
                if item.get('code') in AUTH_STOPS or session.identity._failure:
                    break
    finally:
        session.close()
        for module in [http, auth, booths, portal, sports]:
            module.send = original
        report = {'startedAt': started, 'finishedAt': datetime.now(BEIJING).isoformat(timespec='seconds'),
                  'elapsedSeconds': round(perf_counter() - clock, 3),
                  **session.metadata(), 'personalValuesSaved': False, 'results': results, 'httpTrace': trace}
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return 1 if any(v['status'] == 'error' for v in results) else 0


if __name__ == '__main__':
    sys.exit(main())
