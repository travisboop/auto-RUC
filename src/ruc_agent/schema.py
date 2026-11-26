"""Validation for the deliberately small JSON Schema subset used by this package."""
import re
from datetime import date
from .errors import QueryError


def check_schema(schema):
    """Reject unsupported constraints instead of silently ignoring a contract rule."""
    supported = {'type', 'properties', 'required', 'additionalProperties', 'items', 'enum',
                 'minimum', 'maximum', 'minItems', 'maxItems', 'minLength', 'maxLength',
                 'pattern', 'format', 'description', 'title', 'default'}
    if not isinstance(schema, dict) or set(schema) - supported:
        raise QueryError('INVALID_CONTRACT', '参数契约含不支持的 JSON Schema 关键字。')
    if schema.get('type') not in {None, 'object', 'array', 'string', 'integer', 'number', 'boolean', 'null'}:
        raise QueryError('INVALID_CONTRACT', '参数契约类型无效。')
    if 'pattern' in schema:
        try:
            re.compile(schema['pattern'])
        except (re.error, TypeError):
            raise QueryError('INVALID_CONTRACT', '参数契约的 pattern 无效。') from None
    if 'format' in schema and schema['format'] != 'date':
        raise QueryError('INVALID_CONTRACT', 'format 当前只支持 date。')
    if 'additionalProperties' in schema and type(schema['additionalProperties']) is not bool:
        raise QueryError('INVALID_CONTRACT', 'additionalProperties 仅支持布尔值。')
    for child in schema.get('properties', {}).values():
        check_schema(child)
    if 'items' in schema:
        check_schema(schema['items'])
    if 'default' in schema:
        validate(schema['default'], schema)


def validate(value, schema, path='arguments'):
    types = {'object': dict, 'array': list, 'string': str, 'integer': int, 'boolean': bool,
             'number': (int, float), 'null': type(None)}
    expected = schema.get('type')
    allowed = types.get(expected)
    if expected and (allowed is None or type(value) not in (allowed if isinstance(allowed, tuple) else (allowed,))):
        raise QueryError('INVALID_ARGUMENT', f'{path} 的类型应为 {expected}。')
    if 'enum' in schema and value not in schema['enum']:
        raise QueryError('INVALID_ARGUMENT', f'{path} 不是允许的选项。')
    if expected == 'object':
        properties = schema.get('properties', {})
        if schema.get('additionalProperties') is False and set(value) - set(properties):
            raise QueryError('INVALID_ARGUMENT', f'{path} 含未定义字段。')
        if set(schema.get('required', [])) - set(value):
            raise QueryError('INVALID_ARGUMENT', f'{path} 缺少必填字段。')
        for key, item in value.items():
            if key in properties:
                validate(item, properties[key], path + '.' + key)
    elif expected == 'array':
        if not schema.get('minItems', 0) <= len(value) <= schema.get('maxItems', 10000):
            raise QueryError('INVALID_ARGUMENT', f'{path} 的条目数量超出范围。')
        for i, item in enumerate(value):
            validate(item, schema.get('items', {}), f'{path}[{i}]')
    elif expected == 'string':
        if (len(value) < schema.get('minLength', 1) or len(value) > schema.get('maxLength', 4096)
                or (schema.get('minLength', 1) > 0 and not value.strip())):
            raise QueryError('INVALID_ARGUMENT', f'{path} 不能为空或超过长度上限。')
        if 'pattern' in schema and re.fullmatch(schema['pattern'], value) is None:
            raise QueryError('INVALID_ARGUMENT', f'{path} 的格式无效。')
        if schema.get('format') == 'date':
            try:
                if date.fromisoformat(value).isoformat() != value:
                    raise ValueError()
            except ValueError:
                raise QueryError('INVALID_ARGUMENT', f'{path} 须为有效的 YYYY-MM-DD 日期。') from None
    elif expected in {'integer', 'number'}:
        if not schema.get('minimum', value) <= value <= schema.get('maximum', value):
            raise QueryError('INVALID_ARGUMENT', f'{path} 超出数值范围。')
