"""Match browser requests against complete read contracts, including selectors."""
import json
import re
from urllib.parse import parse_qs, unquote, urlsplit

from .contracts import safe_path
from .errors import QueryError
from .http import canonical_origin
from .schema import validate


def _form_value(value, schema):
    if schema.get('type') == 'array':
        return [_form_value([v], schema.get('items', {})) for v in value]
    if not isinstance(value, list) or len(value) != 1:
        raise ValueError()
    value = value[0]
    kind = schema.get('type')
    if kind == 'integer':
        if not re.fullmatch(r'-?\d+', value):
            raise ValueError()
        return int(value)
    if kind == 'number':
        return float(value)
    if kind == 'boolean':
        if value not in {'true', 'false'}:
            raise ValueError()
        return value == 'true'
    return value


def _match(value, template, parameters, captured, form=False):
    if isinstance(template, dict) and set(template) == {'$param'}:
        key = template['$param']
        schema = parameters['properties'][key]
        value = _form_value(value, schema) if form else value
        validate(value, schema)
        if key in captured and captured[key] != value:
            raise ValueError()
        captured[key] = value
    elif isinstance(template, dict):
        if template == {'$params': True}:
            template = {k: {'$param': k} for k in value}
        if not isinstance(value, dict) or set(value) != set(template):
            raise ValueError()
        for key, child in template.items():
            _match(value[key], child, parameters, captured, form)
    elif isinstance(template, list):
        if not isinstance(value, list) or len(value) != len(template):
            raise ValueError()
        for actual, expected in zip(value, template):
            _match([actual] if form else actual, expected, parameters, captured, form)
    else:
        if form:
            types = {str: 'string', int: 'integer', float: 'number', bool: 'boolean'}
            value = _form_value(value, {'type': types.get(type(template))})
        if type(value) is not type(template) or value != template:
            raise ValueError()


class ReadRequest:
    def __init__(self, origin, spec):
        self.origin, self.spec = origin, spec
        path = spec['request']['path']
        fragments, last = [], 0
        for match in re.finditer(r'\{([A-Za-z_][A-Za-z_0-9]*)\}', path):
            fragments += [re.escape(path[last:match.start()]), '(?P<' + match[1] + '>[^/]+)']
            last = match.end()
        self.path = re.compile(''.join(fragments) + re.escape(path[last:]))

    def matches_route(self, url, method):
        """Identify a CORS preflight target; actual requests still match all data."""
        return (canonical_origin(url) == canonical_origin(self.origin)
                and method == self.spec['request']['method']
                and self.path.fullmatch(urlsplit(url).path) is not None)

    def allows(self, url, method, post_data=None):
        parts, request = urlsplit(url), self.spec['request']
        match = self.path.fullmatch(parts.path)
        if canonical_origin(url) != canonical_origin(self.origin) or method != request['method'] or not match:
            return False
        try:
            safe_path(parts.path)
            captured, parameters = {}, self.spec['parameters']
            for key, value in match.groupdict().items():
                _match([unquote(value)], {'$param': key}, parameters, captured, True)
            _match(parse_qs(parts.query, keep_blank_values=True), request.get('query', {}), parameters, captured, True)
            if 'body' in request:
                form = request.get('encoding', 'json') == 'form'
                body = parse_qs(post_data or '', keep_blank_values=True) if form else json.loads(post_data or '')
                _match(body, request['body'], parameters, captured, form)
            elif post_data:
                return False
            for rule in self.spec.get('constraints', []):
                if not captured[rule['left']] < captured[rule['right']]:
                    return False
            return True
        except (ValueError, TypeError, KeyError, QueryError):
            return False
