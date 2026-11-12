"""Principal-bound localStorage record streams over native SQLite databases.

StreamStorage requires a host-authorized spool provider. Its open_spool(principal)
returns a grant with read/write/seek, reserve(byte_count), discard_file(), and
close(). reserve must obtain additional host disk authority before bytes are written;
close releases the file, handle and all reservations. No local substitute is used.
"""
from __future__ import annotations

import base64
import binascii
import codecs
import struct
from mioa_os_api import SecureRandom
from mioa_os_api.spool import SpoolGrants
from typing import Any

_CHUNK = 192 * 1024
_MAX_VALUE = 5 * 1024 * 1024
_HEADER = struct.Struct('!QQ')


class QuotaExceeded(ValueError):
    """The localStorage value quota would be exceeded."""


class StreamStorage:
    def __init__(self, storage: Any, grants: SpoolGrants, random: SecureRandom):
        self.storage = storage
        self.grants = grants
        self.random = random
        self.streams: dict[str, dict[str, Any]] = {}

    def _open(self, principal: int, origin: str, kind: str) -> tuple[str, dict[str, Any]]:
        if self.grants is None:
            raise ValueError('authenticated storage spool grant unavailable')
        grant = self.grants.open_spool(principal)
        try:
            token = self._token()
            while token in self.streams:
                token = self._token()
            stream = {'principal': principal, 'origin': origin, 'type': kind, 'grant': grant}
            self.streams[token] = stream
            return token, stream
        except BaseException:
            grant.close()
            raise

    def _token(self) -> str:
        value = self.random.bytes(32)
        if not isinstance(value, bytes) or len(value) != 32:
            raise ValueError('invalid secure random output')
        return value.hex()

    @staticmethod
    def _append(stream: dict[str, Any], data: bytes) -> None:
        stream['grant'].reserve(len(data))
        written = stream['grant'].write(data)
        if written != len(data):
            raise OSError('short storage spool write')

    def _get(self, p: dict[str, Any], principal: int, kind: str) -> dict[str, Any]:
        token = p.get('token')
        stream = self.streams.get(token) if isinstance(token, str) else None
        if (stream is None or stream['principal'] != principal
                or stream['origin'] != p.get('origin') or stream['type'] != kind):
            raise ValueError('invalid storage stream')
        return stream

    def _drop(self, token: str) -> None:
        stream = self.streams.pop(token)
        if stream.get('retain_grant'):
            # SQLite cleanup was not established; the broker must retain its
            # tickets even after the now-unneeded spool file is closed.
            stream['grant'].discard_file()
        else:
            stream['grant'].close()

    def _drop_all(self, tokens: list[str]) -> None:
        failure = None
        for token in tokens:
            try:
                self._drop(token)
            except Exception as exc:
                if failure is None:
                    failure = exc
                else:
                    failure.add_note(f"Additional stream cleanup failure: {exc!r}")
        if failure is not None:
            raise failure

    def disconnect(self, principal: int) -> None:
        self._drop_all([token for token, stream in self.streams.items()
                        if stream['principal'] == principal])

    def close(self) -> None:
        self._drop_all(list(self.streams))

    def dispatch(self, op: str, p: dict[str, Any], principal: int) -> Any:
        fields = {
            'local.snapshot.begin': {'origin'},
            'local.snapshot.next': {'origin', 'token'},
            'local.snapshot.read': {'origin', 'token', 'offset'},
            'local.snapshot.end': {'origin', 'token'},
            'local.mutation.begin': {'origin', 'kind', 'key_length', 'value_length'},
            'local.mutation.chunk': {'origin', 'token', 'offset', 'data'},
            'local.mutation.commit': {'origin', 'token'},
            'local.mutation.abort': {'origin', 'token'},
        }
        if op not in fields or not isinstance(p, dict) or set(p) != fields[op]:
            raise ValueError('unsupported operation or invalid payload')
        origin = p['origin']
        if not isinstance(origin, str) or not origin or len(origin.encode('utf-8')) > 8192:
            raise ValueError('invalid origin')

        if op == 'local.snapshot.begin':
            token, stream = self._open(principal, origin, 'snapshot')
            try:
                db = self.storage.local(origin)
                count = 0
                rows = db.execute("select rowid,length(cast(key as blob)),length(cast(value as blob)),typeof(key),typeof(value) from entries order by rowid")
                for rowid, key_len, value_len, key_type, value_type in rows:
                    if key_type != 'text' or value_type != 'text':
                        raise ValueError('invalid storage entry')
                    self._append(stream, _HEADER.pack(key_len, value_len))
                    for column, length in (('key', key_len), ('value', value_len)):
                        if length:
                            with db.blobopen('entries', column, rowid, readonly=True) as blob:
                                remaining = length
                                while remaining:
                                    chunk = blob.read(min(_CHUNK, remaining))
                                    if not chunk:
                                        raise OSError('short storage database read')
                                    self._append(stream, chunk)
                                    remaining -= len(chunk)
                    count += 1
                stream.update(count=count, index=0, remaining=0, offset=0)
                stream['grant'].seek(0)
                return {'token': token, 'count': count}
            except BaseException:
                self._drop(token)
                raise

        if op == 'local.mutation.begin':
            key_len, value_len, kind = p['key_length'], p['value_length'], p['kind']
            if (kind not in ('set', 'remove') or type(key_len) is not int or type(value_len) is not int
                    or key_len < 0 or value_len < 0 or (kind == 'remove' and value_len != 0)):
                raise ValueError('invalid storage mutation')
            if value_len > _MAX_VALUE:
                raise QuotaExceeded('quota exceeded')
            token, stream = self._open(principal, origin, 'mutation')
            stream.update(kind=kind, key_length=key_len, value_length=value_len, offset=0)
            return {'token': token}

        kind = 'snapshot' if op.startswith('local.snapshot.') else 'mutation'
        stream = self._get(p, principal, kind)
        file = stream['grant']
        if op == 'local.snapshot.next':
            if stream['remaining'] or stream['index'] >= stream['count']:
                raise ValueError('invalid storage snapshot state')
            header = file.read(_HEADER.size)
            if len(header) != _HEADER.size:
                raise OSError('short storage spool read')
            key_len, value_len = _HEADER.unpack(header)
            stream.update(remaining=key_len + value_len, offset=0, index=stream['index'] + 1)
            return {'key_length': key_len, 'value_length': value_len}
        if op == 'local.snapshot.read':
            offset = p['offset']
            if type(offset) is not int or offset != stream['offset'] or stream['remaining'] <= 0:
                raise ValueError('invalid storage stream offset')
            chunk = file.read(min(_CHUNK, stream['remaining']))
            if not chunk:
                raise OSError('short storage spool read')
            stream['offset'] += len(chunk)
            stream['remaining'] -= len(chunk)
            return {'data': base64.b64encode(chunk).decode('ascii')}
        if op in ('local.snapshot.end', 'local.mutation.abort'):
            self._drop(p['token'])
            return None
        if op == 'local.mutation.chunk':
            offset, encoded = p['offset'], p['data']
            if (type(offset) is not int or offset != stream['offset'] or not isinstance(encoded, str)
                    or len(encoded) > ((_CHUNK + 2) // 3) * 4):
                raise ValueError('invalid storage stream chunk')
            try:
                chunk = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ValueError('invalid storage stream chunk') from exc
            if not chunk or len(chunk) > _CHUNK or len(chunk) > stream['key_length'] + stream['value_length'] - offset:
                raise ValueError('invalid storage stream chunk')
            try:
                self._append(stream, chunk)
            except BaseException:
                self._drop(p['token'])
                raise
            stream['offset'] += len(chunk)
            return None

        if stream['offset'] != stream['key_length'] + stream['value_length']:
            raise ValueError('incomplete storage mutation')
        try:
            self._commit(origin, stream)
        finally:
            try:
                if stream.get('retain_grant'):
                    self.storage.close_local(origin)
                    stream['retain_grant'] = False
            finally:
                self._drop(p['token'])
        return None

    def _commit(self, origin: str, stream: dict[str, Any]) -> None:
        db = self.storage.local(origin)
        file = stream['grant']
        file.seek(0)
        # Logical admission for temp staging and possible WAL growth. This is a
        # conservative estimate, not a filesystem quota or an exact SQLite bill.
        page_size = db.execute('pragma page_size').fetchone()[0]
        page_count = db.execute('pragma page_count').fetchone()[0]
        entry_bytes = stream['key_length'] + stream['value_length']
        stream['grant'].reserve(2 * page_size * page_count + 4 * entry_bytes + 1024 * 1024)
        stream['retain_grant'] = True
        db.execute('begin immediate')
        try:
            db.execute('create temp table local_stream_key (data blob not null, val blob not null)')
            db.execute('insert into local_stream_key values (zeroblob(?),zeroblob(?))',
                       (stream['key_length'], stream['value_length']))
            for column, length in (('data', stream['key_length']), ('val', stream['value_length'])):
                decoder = codecs.getincrementaldecoder('utf-8')('strict')
                remaining = length
                if remaining:
                    with db.blobopen('local_stream_key', column, 1, name='temp') as blob:
                        while remaining:
                            chunk = file.read(min(_CHUNK, remaining))
                            if not chunk:
                                raise OSError('short storage spool read')
                            decoder.decode(chunk, final=False)
                            blob.write(chunk)
                            remaining -= len(chunk)
                decoder.decode(b'', final=True)
            key_sql = '(select cast(data as text) from temp.local_stream_key)'
            if stream['kind'] == 'set':
                total = db.execute('select coalesce(sum(length(cast(value as blob))),0) from entries where key != ' + key_sql).fetchone()[0]
                if total + stream['value_length'] > _MAX_VALUE:
                    raise QuotaExceeded('quota exceeded')
                db.execute('insert into entries(key,value) values (' + key_sql + ',(select cast(val as text) from temp.local_stream_key)) on conflict(key) do update set value=excluded.value')
            else:
                db.execute('delete from entries where key=' + key_sql)
            db.execute('drop table temp.local_stream_key')
            db.execute('commit')
        except BaseException:
            if db.in_transaction:
                db.execute('rollback')
            raise
