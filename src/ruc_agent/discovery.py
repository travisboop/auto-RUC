"""Account-specific platform coverage derived from the current application catalog."""
from collections import Counter
from urllib.parse import urlsplit

from .urls import application_target, entry_requires_action_review
from .http import canonical_origin


def inventory(result, registry):
    groups, without_entry = {}, []
    for app in result['applications']:
        entry = application_target(app)
        row = {'id': app.get('appId'), 'name': app.get('name') or app.get('appName'),
               'allowed': app.get('isAllow'), 'entry': entry,
               'entryRequiresActionReview': entry_requires_action_review(entry)}
        if not entry:
            without_entry.append(row)
            continue
        parts = urlsplit(entry)
        origin = canonical_origin(entry)
        profile = registry.profile_for_url(entry)
        identifier = profile['id'] if profile else origin
        if identifier not in groups:
            specs = [s for s in registry.specs.values() if profile and s['platform'] == identifier]
            groups[identifier] = {'id': identifier, 'origin': origin, 'applications': [],
                                  'readCapabilities': [s['id'] for s in specs if s.get('readOnly', True)],
                                  'actionCapabilities': [s['id'] for s in specs if not s.get('readOnly', True)],
                                  'coverage': 'registered' if specs else 'entry_only',
                                  'transport': 'https' if parts.scheme == 'https' else 'http_entry_requires_review'}
        groups[identifier]['applications'].append(row)
    values = sorted(groups.values(), key=lambda g: g['origin'])
    for group in values:
        group['applicationCount'] = len(group['applications'])
        group['allowedCount'] = sum(a['allowed'] is True for a in group['applications'])
    return {'status': 'ok', 'live': True, 'asOf': result['asOf'], 'source': result['source'],
            'totalApplications': result['totalMatches'], 'catalogComplete': not result['truncated'],
            'accessCounts': dict(Counter('allowed' if a.get('isAllow') is True else
                                        'denied' if a.get('isAllow') is False else 'unknown'
                                        for a in result['applications'])),
            'platformCount': len(values), 'platforms': values, 'withoutEntry': without_entry,
            'meaning': '当前账号门户目录。registered 仅表示存在已登记能力，具体范围和验收状态见 capabilities；入口不等于业务已接通。'}
