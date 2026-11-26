"""Search local platform entries without requiring an account catalog export."""
import json
from pathlib import Path

from .errors import QueryError

ROOT = Path(__file__).resolve().parents[2]


def find_apps(keyword, limit=20):
    if not isinstance(keyword, str) or not keyword.strip() or type(limit) is not int or not 1 <= limit <= 100:
        raise QueryError('INVALID_ARGUMENT', 'keyword 须为非空文本；limit 须为 1 至 100 的整数。')
    snapshot = ROOT / 'tools/applications.snapshot.json'
    if snapshot.is_file():
        data = json.loads(snapshot.read_text(encoding='utf-8'))
        items = data['applications']
        source, snapshot_date = 'tools/applications.snapshot.json', '2026-11-25'
    else:
        data = platforms()
        items = [{'id': p['id'], 'name': p['name'], 'url': p['entry']} for p in data['platforms']]
        source, snapshot_date = 'tools/platforms.json', data['updatedOn']
    matches = []
    for item in items:
        if keyword.casefold() not in (item.get('name', '') + ' ' + (item.get('description') or '')).casefold():
            continue
        result = {k: item[k] for k in ['catalog_id', 'kind', 'id', 'name', 'description'] if k in item}
        result['entryFields'] = {k: v for k, v in item.items() if 'url' in k.lower() and isinstance(v, str)}
        matches.append(result)
    return {'status': 'ok', 'live': False, 'snapshotDate': snapshot_date,
            'source': source,
            'totalMatches': len(matches), 'applications': matches[:limit]}


def platforms():
    result = json.loads((ROOT / 'tools/platforms.json').read_text(encoding='utf-8'))
    return {'status': 'ok', 'live': False, **result}
