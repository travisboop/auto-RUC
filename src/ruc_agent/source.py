"""Inspect current public HTML/JS as text, without running downloaded code."""
import hashlib
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit, urljoin

from .browser import school_origin
from .http import MemoryHTTP, send
from .errors import QueryError


def inspect_source(url, keyword=None, max_chars=12000):
    origin = school_origin(url)
    path = urlsplit(url).path
    if not (path.endswith(('.js', '.vue', '.html', '/')) or path == '') or urlsplit(url).query:
        raise QueryError('INVALID_ARGUMENT', '源码检查只接受无查询串的公开页面或 JS 地址。')
    http = MemoryHTTP(origin)
    try:
        status, _, raw = send(http.opener, url)
    finally:
        http.close()
    if status != 200:
        raise QueryError('SOURCE_UNAVAILABLE', f'公开源码返回 HTTP {status}；没有使用账号 Cookie。')
    try:
        content = raw.decode('utf-8')
    except UnicodeError:
        raise QueryError('INVALID_RESPONSE', '源码不是 UTF-8 文本。') from None
    class Scripts(HTMLParser):
        def __init__(self):
            super().__init__()
            self.links = set()

        def handle_starttag(self, tag, attrs):
            values = dict(attrs)
            if tag == 'script' and values.get('src'):
                self.links.add(urljoin(url, values['src']))

    parser = Scripts()
    parser.feed(content)
    scripts = sorted(parser.links)
    if keyword:
        matches = list(re.finditer(re.escape(keyword), content, re.I))
        excerpts, remaining = [], max_chars
        for match in matches[:30]:
            excerpt = content[max(0, match.start()-400):match.end()+800][:remaining]
            excerpts.append({'offset': match.start(), 'text': excerpt})
            remaining -= len(excerpt)
            if remaining <= 0:
                break
        result = {'matches': len(matches), 'excerpts': excerpts, 'truncated': len(excerpts) < len(matches)}
    else:
        result = {'text': content[:max_chars], 'truncated': len(content) > max_chars}
    return {'status': 'ok', 'source': url, 'sha256': hashlib.sha256(raw).hexdigest(),
            'bytes': len(raw), 'scripts': scripts, 'interpretation': '当前公开源码；不是业务实测。', **result}
