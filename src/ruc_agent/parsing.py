"""Read JSON data embedded by a server in HTML; never execute page scripts."""
from html.parser import HTMLParser
import json
import re

from .errors import QueryError


def html_snapshot(body, base, max_chars=200000):
    """Extract server-rendered text, tables and form values without running scripts."""
    from urllib.parse import urlsplit
    from .redaction import SECRET_KEY
    from .urls import application_url

    class Page(HTMLParser):
        def __init__(self):
            super().__init__()
            self.text, self.titles, self.links, self.forms, self.fields, self.tables = [], [], [], [], [], []
            self.ignore, self.title, self.anchor, self.form, self.control, self.option = 0, False, None, None, None, None
            self.table, self.row, self.cell = None, None, None
            self.table_stack = []
            self.script_count = 0

        def handle_starttag(self, tag, attrs):
            values = dict(attrs)
            if tag in {'script', 'style', 'noscript', 'template'}:
                self.ignore += 1
                self.script_count += tag == 'script'
            if self.ignore:
                return
            if tag == 'title':
                self.title = True
            if tag in {'p', 'div', 'br', 'tr', 'li', 'h1', 'h2', 'h3', 'section'}:
                self.text.append('\n')
            if tag == 'a':
                url = application_url(values.get('href'), base)
                self.anchor = {'url': url, 'text': ''} if url else None
                if self.anchor:
                    self.links.append(self.anchor)
            if tag == 'form':
                self.form = {'action': application_url(values.get('action') or base, base),
                             'method': values.get('method', 'GET').upper(), 'fieldIndices': []}
                self.forms.append(self.form)
            if tag in {'input', 'select', 'textarea'}:
                field = {key: values[key] for key in ['name', 'id', 'type', 'placeholder'] if key in values}
                field.update(tag=tag, value=values.get('value', ''), required='required' in values,
                             disabled='disabled' in values, checked='checked' in values)
                if values.get('type') == 'password' or SECRET_KEY.search(values.get('name', '')):
                    field['value'] = '<redacted>'
                if tag == 'select':
                    field['options'] = []
                if self.form is not None:
                    self.form['fieldIndices'].append(len(self.fields))
                self.fields.append(field)
                self.control = field if tag in {'select', 'textarea'} else None
            if tag == 'option' and self.control is not None:
                self.option = {'value': values.get('value'), 'text': '', 'selected': 'selected' in values}
                self.control['options'].append(self.option)
            if tag == 'table':
                self.table_stack.append((self.table, self.row, self.cell))
                self.table = []
                self.row = self.cell = None
                self.tables.append(self.table)
            if tag == 'tr' and self.table is not None:
                self.row = []
                self.table.append(self.row)
            if tag in {'td', 'th'} and self.row is not None:
                self.cell = []

        def handle_endtag(self, tag):
            if tag in {'script', 'style', 'noscript', 'template'}:
                self.ignore = max(0, self.ignore - 1)
                return
            if self.ignore:
                return
            if tag == 'title': self.title = False
            if tag == 'a': self.anchor = None
            if tag == 'form': self.form = None
            if tag == 'option': self.option = None
            if tag in {'select', 'textarea'}: self.control = None
            if tag in {'td', 'th'} and self.cell is not None:
                self.row.append(' '.join(''.join(self.cell).split()))
                self.cell = None
            if tag == 'tr': self.row = None
            if tag == 'table' and self.table_stack:
                self.table, self.row, self.cell = self.table_stack.pop()

        def handle_data(self, value):
            if self.ignore:
                return
            self.text.append(value)
            if self.title: self.titles.append(value)
            if self.anchor is not None: self.anchor['text'] += value
            if self.cell is not None: self.cell.append(value)
            if self.option is not None: self.option['text'] += value
            if self.control is not None and self.control['tag'] == 'textarea':
                self.control['value'] += value

    try:
        page = Page()
        page.feed(body.decode('utf-8-sig'))
    except (UnicodeError, ValueError):
        raise QueryError('SCHEMA_CHANGED', '页面不是可读取的 UTF-8 HTML。') from None
    text = '\n'.join(' '.join(line.split()) for line in ''.join(page.text).splitlines() if line.strip())
    for link in page.links:
        link['text'] = ' '.join(link['text'].split())
    for field in page.fields:
        if field.get('type') == 'password' or SECRET_KEY.search(field.get('name', '')):
            field['value'] = '<redacted>'
        if field['tag'] == 'select':
            for option in field['options']:
                option['text'] = ' '.join(option['text'].split())
                if option['value'] is None:
                    option['value'] = option['text']
            selected = [option['value'] for option in field['options'] if option['selected']]
            field['value'] = selected or [field['options'][0]['value']] if field['options'] else []
    return {'title': ' '.join(''.join(page.titles).split()), 'text': text[:max_chars],
            'links': page.links, 'linkPaths': sorted({urlsplit(link['url']).path for link in page.links}),
            'tables': page.tables, 'forms': page.forms, 'fields': page.fields,
            'scope': 'server_html', 'scriptCount': page.script_count, 'textTruncated': len(text) > max_chars}


def hidden_value(body, identifier):
    class Inputs(HTMLParser):
        def __init__(self):
            super().__init__()
            self.values = []

        def handle_starttag(self, tag, attrs):
            values = dict(attrs)
            if tag == 'input' and values.get('type') == 'hidden' and values.get('id') == identifier:
                self.values.append(values.get('value', ''))

    try:
        parser = Inputs()
        parser.feed(body.decode('utf-8'))
        if len(parser.values) > 1:
            raise ValueError()
        return parser.values[0] if parser.values else None
    except (ValueError, UnicodeError):
        raise QueryError('AUTH_FLOW_CHANGED', '认证页面的隐藏跳转字段不唯一或不可读取。') from None


def hidden_form(body, expected_action, expected_fields, base):
    """Extract a reviewed authentication form without running its JavaScript."""
    from urllib.parse import urljoin

    class Form(HTMLParser):
        def __init__(self):
            super().__init__()
            self.forms, self.values, self.inside = [], {}, False

        def handle_starttag(self, tag, attrs):
            fields = dict(attrs)
            if tag == 'form':
                self.forms.append((fields.get('method', '').upper(), urljoin(base, fields.get('action', ''))))
                self.inside = True
            if tag == 'input' and self.inside:
                name = fields.get('name')
                if fields.get('type') != 'hidden' or name in self.values:
                    raise ValueError()
                self.values[name] = fields.get('value', '')

        def handle_endtag(self, tag):
            if tag == 'form':
                self.inside = False

    try:
        parser = Form()
        parser.feed(body.decode('utf-8'))
        if parser.forms != [('POST', expected_action)] or set(parser.values) != set(expected_fields):
            raise ValueError()
        if not all(parser.values.values()):
            raise ValueError()
        return parser.values
    except (ValueError, UnicodeError):
        raise QueryError('AUTH_FLOW_CHANGED', '页面不符合该平台已核实的认证转交表单。') from None


def embedded_json(body, variable):
    class Scripts(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=False)
            self.inside, self.parts = False, []

        def handle_starttag(self, tag, attrs):
            if tag == 'script':
                self.inside = True

        def handle_endtag(self, tag):
            if tag == 'script':
                self.inside = False

        def handle_data(self, text):
            if self.inside:
                self.parts.append(text)

    try:
        parser = Scripts()
        parser.feed(body.decode('utf-8'))
        expression = re.compile(r'\b(?:var|let|const)\s+' + re.escape(variable) + r'\s*=\s*')
        values = [json.JSONDecoder().raw_decode(script[match.end():])[0]
                  for script in parser.parts for match in expression.finditer(script)]
        if len(values) != 1:
            raise ValueError()
        return values[0]
    except (ValueError, UnicodeError):
        raise QueryError('SCHEMA_CHANGED', '页面未提供唯一的 JSON 数据变量；请核对当前页面结构。') from None
