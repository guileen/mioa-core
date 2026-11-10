"""TOON encoder for agent-facing evidence.

JSON remains the canonical machine record (validators, CI, external contracts).
Reports an Agent reads are rendered as TOON: fewer tokens than pretty JSON and
`[N]` length headers make a truncated or padded array structurally detectable.
"""
import json
import re

NUMBER = re.compile(r'^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$')
CONTROL = re.compile(r'[\x00-\x1f]')


class ToonError(ValueError):
    pass


def _quote(value, delimiter=','):
    if not isinstance(value, str):
        return False
    if value == '' or value != value.strip():
        return True
    if value in ('true', 'false', 'null'):
        return True
    if NUMBER.match(value):
        return True
    if value == '-' or value.startswith('-') or value.startswith('#'):
        return True
    if CONTROL.search(value) or any(char in value for char in ':"\\[]{}'):
        return True
    return bool(delimiter) and delimiter in value


def _string(value, delimiter=','):
    if not _quote(value, delimiter):
        return value
    out = []
    for char in value:
        if char == '\\':
            out.append('\\\\')
        elif char == '"':
            out.append('\\"')
        elif char == '\n':
            out.append('\\n')
        elif char == '\r':
            out.append('\\r')
        elif char == '\t':
            out.append('\\t')
        elif ord(char) < 0x20:
            out.append('\\u%04x' % ord(char))
        else:
            out.append(char)
    return '"' + ''.join(out) + '"'


def scalar(value, delimiter=','):
    if value is None:
        return 'null'
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    if isinstance(value, str):
        return _string(value, delimiter)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float('inf'), float('-inf')):
            raise ToonError('non-finite float cannot be represented')
        return json.dumps(value)
    raise ToonError('unsupported scalar type: ' + type(value).__name__)


def _key(value):
    return _string(str(value), '\x00')


def _uniform(rows):
    if not rows or not all(isinstance(row, dict) and row for row in rows):
        return None
    keys = list(rows[0])
    if any(list(row) != keys for row in rows):
        return None
    if any(isinstance(value, (dict, list)) for row in rows for value in row.values()):
        return None
    return keys


def _array(key, rows, depth, lines):
    pad = ' ' * (depth * 2)
    header = _key(key) if key is not None else ''
    if not rows:
        lines.append(pad + (header + ': ' if header else '') + '[]')
        return
    if all(not isinstance(row, (dict, list)) for row in rows):
        lines.append(pad + header + '[' + str(len(rows)) + ']: ' +
                     ','.join(scalar(row) for row in rows))
        return
    fields = _uniform(rows)
    if fields is not None:
        head = ','.join(_string(field, ',') for field in fields)
        lines.append(pad + header + '[' + str(len(rows)) + ']{' + head + '}:')
        for row in rows:
            lines.append(pad + '  ' + ','.join(scalar(row[field]) for field in fields))
        return
    lines.append(pad + header + '[' + str(len(rows)) + ']:')
    for row in rows:
        _item(row, depth + 1, lines)


def _object(value, depth, lines):
    pad = ' ' * (depth * 2)
    for key, item in value.items():
        name = _key(key)
        if isinstance(item, dict):
            lines.append(pad + name + ':')
            if item:
                _object(item, depth + 1, lines)
        elif isinstance(item, list):
            _array(key, item, depth, lines)
        else:
            lines.append(pad + name + ': ' + scalar(item))


def _item_list(prefix, name, rows, depth, lines):
    header = '' if name is None else _key(name)
    if not rows:
        lines.append(prefix + (header + ': ' if header else '') + '[]')
        return
    if all(not isinstance(row, (dict, list)) for row in rows):
        lines.append(prefix + header + '[' + str(len(rows)) + ']: ' +
                     ','.join(scalar(row) for row in rows))
        return
    fields = _uniform(rows) if name is not None else None
    if fields is not None:
        head = ','.join(_string(field, ',') for field in fields)
        lines.append(prefix + header + '[' + str(len(rows)) + ']{' + head + '}:')
        rowpad = ' ' * ((depth + 2) * 2)
        for row in rows:
            lines.append(rowpad + ','.join(scalar(row[field]) for field in fields))
        return
    lines.append(prefix + header + '[' + str(len(rows)) + ']:')
    for row in rows:
        _item(row, depth + 1 + (name is not None), lines)


def _item(value, depth, lines):
    pad = ' ' * (depth * 2)
    if isinstance(value, dict):
        if not value:
            lines.append(pad + '-')
            return
        keys = list(value)
        flat = next((key for key in keys if not isinstance(value[key], (dict, list))), None)
        if flat is not None and keys[0] != flat:
            keys.remove(flat)
            keys.insert(0, flat)
        first = keys[0]
        inner = depth + 1
        if isinstance(value[first], dict):
            lines.append(pad + '- ' + _key(first) + ':')
            if value[first]:
                _object(value[first], inner + 1, lines)
        elif isinstance(value[first], list):
            _item_list(pad + '- ', first, value[first], depth, lines)
        else:
            lines.append(pad + '- ' + _key(first) + ': ' + scalar(value[first]))
        for key in keys[1:]:
            _object({key: value[key]}, inner, lines)
        return
    if isinstance(value, list):
        if not value:
            lines.append(pad + '- []')
        else:
            _item_list(pad + '- ', None, value, depth, lines)
        return
    lines.append(pad + '- ' + scalar(value))


def encode(value):
    lines = []
    if isinstance(value, dict):
        if value:
            _object(value, 0, lines)
    elif isinstance(value, list):
        _array(None, value, 0, lines)
    else:
        lines.append(scalar(value))
    return '\n'.join(lines)
