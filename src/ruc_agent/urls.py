"""Separate usable application links from deliberately minimal log URLs."""
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

AUTH_PARAMETER = re.compile(
    r'token|ticket|password|passwd|secret|authorization|session|csrf|^(?:authkey|code|state|auth_code|jwt|jwtHeader|_sso)$', re.I)


def diagnostic_path(path):
    # Some login callbacks put the opaque credential in the path, not the query.
    return re.sub(r'(/(?:cas/v1/tickets|WeChat_login|pcLoginWrd|auth/cas)/)[^/]+',
                  r'\1<redacted>', path, flags=re.I)


def entry_requires_action_review(url):
    """A portal link can itself invoke an operation, such as switching a role."""
    from urllib.parse import unquote
    parts = urlsplit(url)
    values = [unquote(parts.path)] + [v for k, v in parse_qsl(parts.query)
                                    if k.lower() in {'action', 'method', 'cmd', 'operation', 'op'}]
    return any(re.search(r'(?:^|[/!._-])(?:do_)?(?:switchGroup|delete|remove|cancel|submit|save|logout)(?:[/.?_-]|$)',
                         value, re.I) for value in values)


def application_url(value, base='https://my.ruc.edu.cn', _depth=0):
    """Keep business identifiers, including hash-router parameters; remove secrets."""
    from urllib.parse import urljoin
    if not isinstance(value, str) or not value.strip() or value.strip() == '#':
        return ''
    if _depth > 4:
        return ''
    try:
        parts = urlsplit(urljoin(base, value.strip()))
        parts.port
    except ValueError:
        return ''
    if parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username or parts.password:
        return ''
    from .http import canonical_origin
    parts = parts._replace(netloc=urlsplit(canonical_origin(parts.scheme + '://' + parts.netloc)).netloc)

    def query(value):
        pairs = []
        for key, content in parse_qsl(value, keep_blank_values=True):
            if (AUTH_PARAMETER.search(key)
                    or parts.hostname == 'zg.ruc.edu.cn' and parts.path == '/front/login' and key == 'info'):
                continue
            if content.startswith(('https://', 'http://', '/')) and ('?' in content or '#' in content):
                content = application_url(content, parts.scheme + '://' + parts.netloc, _depth + 1)
                if not content:
                    continue
            pairs.append((key, content))
        return urlencode(pairs)

    route, separator, fragment_query = parts.fragment.partition('?')
    if not separator and '=' in route and not route.startswith('/'):
        return urlunsplit((parts.scheme, parts.netloc, parts.path or '/', query(parts.query), query(route)))
    fragment_query = query(fragment_query)
    fragment = route + ('?' + fragment_query if fragment_query else '')
    return urlunsplit((parts.scheme, parts.netloc, parts.path or '/', query(parts.query), fragment))


def diagnostic_url(value):
    if not isinstance(value, str):
        return ''
    parts = urlsplit(value)
    fragment = parts.fragment.split('?')[0]
    if '=' in fragment and not fragment.startswith('/'):
        fragment = ''
    return urlunsplit((parts.scheme, parts.netloc, diagnostic_path(parts.path), '', fragment))


def application_target(app):
    """Resolve the portal's observed details wrapper into its actual service entry."""
    from urllib.parse import parse_qs
    direct = application_url(app.get('entranceUrl'))
    if direct:
        return direct
    entry = application_url(app.get('appUrl'))
    parts = urlsplit(entry)
    if parts.netloc == 'my.ruc.edu.cn' and parts.path == '/sopplus/_web/fusionportal/serviceCenter/details.html':
        targets = parse_qs(parts.query).get('entranceUrl', [])
        if len(targets) == 1:
            target = application_url(targets[0])
            if target:
                return target
    return entry
