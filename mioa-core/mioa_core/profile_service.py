"""Private, provisional Profile storage owner for trusted runtime adapters.

This is not an authorization boundary for hostile native code: the supervisor
must provide each domain a private connected endpoint and prevent direct access
to the profile directory. Managed native sessions inherit all storage and event
capabilities from the supervisor; the ordinary launcher remains exclusive.
"""
from __future__ import annotations

import hashlib
import json
import collections
import concurrent.futures
import queue
import sqlite3
import struct
import contextvars
from contextlib import ExitStack


class _PendingEventsContext:
    def __init__(self) -> None:
        self._var: contextvars.ContextVar[list[tuple[int, dict[str, Any]]] | None] = contextvars.ContextVar("pending_events", default=None)

    @property
    def items(self) -> list[tuple[int, dict[str, Any]]]:
        val = self._var.get()
        if val is None:
            raise AttributeError("items")
        return val

    @items.setter
    def items(self, value: list[tuple[int, dict[str, Any]]]) -> None:
        self._var.set(value)

    @items.deleter
    def items(self) -> None:
        self._var.set(None)
from pathlib import PurePath
from typing import Any, Callable
from mioa_os_api import ArtifactContent, HostFiles, ProfileLeaseCopy, ProfileLeases, SecureRandom, UnixChannels, UnixMessageReader, UnixMessages
from mioa_os_api.spool import SpoolGrants

from . import profile

_MAX_MESSAGE = 1024 * 1024
_MAX_DOMAINS = 64


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _read_exact(sock: Any, size: int) -> bytes:
    parts = bytearray()
    while len(parts) < size:
        chunk = sock.recv(size - len(parts))
        if not chunk:
            raise EOFError
        parts.extend(chunk)
    return bytes(parts)


def _receive(sock: Any) -> dict[str, Any]:
    length = struct.unpack("!I", _read_exact(sock, 4))[0]
    if not 0 < length <= _MAX_MESSAGE:
        raise ValueError("invalid message size")
    value = json.loads(_read_exact(sock, length), object_pairs_hook=_unique)
    if not isinstance(value, dict):
        raise ValueError("message must be an object")
    return value

def _receive_control(reader: UnixMessageReader) -> tuple[dict[str, Any], list[Any]] | None:
    # OS owns partial frames and rights until a complete batch is available.
    frame = reader.read_available()
    if frame is None:
        return None
    body, endpoints = frame
    try:
        message = json.loads(body, object_pairs_hook=_unique)
        if not isinstance(message, dict):
            raise ValueError("control message must be an object")
        return message, endpoints
    except BaseException:
        for endpoint in endpoints:
            endpoint.close()
        raise


def enroll_domain(control: Any, endpoint: Any, request_id: int,
                  events_endpoint: Any | None = None,
                  idb_endpoint: Any | None = None,
                  grant_endpoint: Any | None = None, *,
                  messages: UnixMessages) -> None:
    """Transfer a supervisor-created domain endpoint to a live Profile owner."""
    if type(request_id) is not int or not 0 < request_id < 2**63:
        raise ValueError("invalid control request id")
    data = json.dumps({"version": 1, "id": request_id, "operation": "enroll"}, separators=(",", ":")).encode()
    endpoints = [endpoint]
    if events_endpoint is not None:
        endpoints.append(events_endpoint)
    if idb_endpoint is not None:
        if events_endpoint is None:
            raise ValueError("IndexedDB endpoint requires event endpoint")
        endpoints.append(idb_endpoint)
    if grant_endpoint is not None:
        if events_endpoint is None or idb_endpoint is None:
            raise ValueError("grant endpoint requires event and IndexedDB endpoints")
        endpoints.append(grant_endpoint)
    messages.send_sockets(control, data, endpoints, max_bytes=_MAX_MESSAGE)
    reply = _receive(control)
    if reply.get("id") != request_id or reply.get("ok") is not True:
        raise ProfileServiceError("domain enrollment rejected or outcome unknown")

def enroll_owner(control: Any, endpoint: Any, request_id: int, kind: str, *,
                 messages: UnixMessages) -> None:
    """Attach a supervisor-owned, directly supervised native semantic owner."""
    if kind not in {"cookie", "idb"} or type(request_id) is not int or not 0 < request_id < 2**63:
        raise ValueError("invalid owner enrollment")
    data = json.dumps({"version": 1, "id": request_id, "operation": "enroll_" + kind},
                      separators=(",", ":")).encode()
    messages.send_sockets(control, data, [endpoint], max_bytes=_MAX_MESSAGE)
    reply = _receive(control)
    if reply.get("id") != request_id or reply.get("ok") is not True:
        raise ProfileServiceError("owner enrollment rejected or outcome unknown")

def await_owner_ready(control: Any, request_id: int, kind: str) -> None:
    """Wait for the supervised owner's actual ready record after RELEASE."""
    if kind not in {"cookie", "idb"} or type(request_id) is not int or not 0 < request_id < 2**63:
        raise ValueError("invalid owner readiness request")
    _send(control, {"version": 1, "id": request_id, "operation": "ready_" + kind})
    reply = _receive(control)
    if reply.get("id") != request_id or reply.get("ok") is not True:
        raise ProfileServiceError(reply.get("error", "owner readiness unavailable"))


def receive_leases(control: Any, request_id: int, *,
                   leases: ProfileLeases) -> list[ProfileLeaseCopy]:
    """Receive opaque owned copies for supervisor escrow; caller closes."""
    try:
        _send(control, {"version": 1, "id": request_id, "operation": "lease_fds"})
    except OSError:
        pass
    body, copies = leases.receive(control, max_bytes=_MAX_MESSAGE)
    try:
        reply = json.loads(body, object_pairs_hook=_unique)
        if (not isinstance(reply, dict) or type(reply.get("id")) is not int
                or reply["id"] != request_id or reply.get("ok") is not True or not 1 <= len(copies) <= 2):
            raise ProfileServiceError("lease escrow transfer failed")
        return copies
    except BaseException:
        for lease in copies:
            lease.close()
        raise


def confirm_lease_escrow(control: Any, request_id: int) -> None:
    _send(control, {"version": 1, "id": request_id, "operation": "escrow_confirm"})
    reply = _receive(control)
    if reply.get("id") != request_id or reply.get("ok") is not True:
        raise ProfileServiceError("lease escrow confirmation failed")


def _send(sock: Any, value: dict[str, Any]) -> None:
    data = json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode()
    if len(data) > _MAX_MESSAGE:
        raise ValueError("response too large")
    sock.sendall(struct.pack("!I", len(data)) + data)


class ProfileServiceError(RuntimeError):
    pass


class ProfileRequestRejected(ProfileServiceError):
    def __init__(self, message: str, error_code: str | None = None):
        super().__init__(message)
        self.error_code = error_code


class ProfileClient:
    """A connected endpoint is the authority; principal never comes from payload."""
    def __init__(self, endpoint: Any, *, cookie_event_frames: bool = False):
        self.endpoint = endpoint
        self.next_id = 1
        self.cookie_event_frames = cookie_event_frames

    def request_reply(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = self.next_id
        if request_id >= 2**63:
            raise ProfileServiceError("request id exhausted")
        self.next_id += 1
        try:
            _send(self.endpoint, {"version": 1, "id": request_id, "operation": operation, "payload": payload})
            reply = _receive(self.endpoint)
            events = []
            while self.cookie_event_frames and "cookie_event" in reply:
                if (set(reply) != {"id", "cookie_event"} or reply["id"] != request_id
                        or not isinstance(reply["cookie_event"], dict) or len(events) >= 1024):
                    raise ValueError("invalid cookie event frame")
                events.append(reply["cookie_event"])
                reply = _receive(self.endpoint)
            if events:
                if "events" in reply:
                    raise ValueError("mixed cookie event framing")
                reply["events"] = events
        except (OSError, EOFError, ValueError) as exc:
            self.endpoint.close()
            raise ProfileServiceError("profile service lost; dispatched effects may be unknown") from exc
        if reply.get("id") != request_id:
            self.endpoint.close()
            raise ProfileServiceError("invalid profile service reply; effect unknown")
        if type(reply.get("ok")) is not bool:
            self.endpoint.close()
            raise ProfileServiceError("invalid profile service reply; effect unknown")
        return reply

    def request(self, operation: str, **payload: Any) -> Any:
        reply = self.request_reply(operation, payload)
        if not reply["ok"]:
            if reply.get("effect") == "unknown":
                raise ProfileServiceError(reply.get("error", "profile effect unknown"))
            raise ProfileRequestRejected(reply.get("error", "profile request failed"))
        return reply.get("result")

    def close(self) -> None:
        self.endpoint.close()


def _database(path: PurePath, schema: str, validation: str, files: HostFiles) -> sqlite3.Connection:
    if files.is_symlink(path) or (files.exists(path) and (not files.is_file(path) or files.size(path) == 0)):
        raise ValueError("invalid storage database")
    exists = files.exists(path)
    if not exists and any(files.exists(PurePath(str(path) + suffix)) for suffix in ("-wal", "-shm", "-journal")):
        raise ValueError("orphaned storage journal")
    db = sqlite3.connect(str(path), isolation_level=None, timeout=1)
    try:
        if exists and (db.execute("pragma integrity_check").fetchall() != [("ok",)] or db.execute("pragma foreign_key_check").fetchone() is not None):
            raise ValueError("invalid storage database")
        db.execute("pragma journal_mode=wal")
        db.execute("pragma synchronous=full")
        db.execute("pragma fullfsync=on")
        if exists:
            db.execute(validation)
        else:
            db.execute(schema)
        return db
    except BaseException:
        db.close()
        raise


class _Storage:
    def __init__(self, directory: PurePath, name: str, cfg: dict[str, Any], *, random: SecureRandom, grants: SpoolGrants, files: HostFiles):
        if grants is None:
            raise ValueError("Host profile spool grants are required")
        self.directory = directory
        self.name = name
        self.cfg = cfg
        self.files = files
        self.databases: dict[str, sqlite3.Connection] = {}
        self.cookie_client: ProfileClient | None = None
        self.cookie_failed = False
        self.idb_client: ProfileClient | None = None
        self.cookie_endpoint: Any | None = None
        self.idb_endpoint: Any | None = None
        self.idb_failed = False
        self.subscribers: dict[int, dict[str, Any]] = {}
        self.event_sockets: dict[int, Any] = {}
        self.pending_events = _PendingEventsContext()
        from .local_storage_stream import StreamStorage
        self.grants = grants
        self.stream = StreamStorage(self, self.grants, random)

    def close(self) -> None:
        failure = None

        def attempt(action, *args):
            nonlocal failure
            try:
                action(*args)
            except Exception as exc:
                if failure is None:
                    failure = exc
                else:
                    failure.add_note(f"Additional storage cleanup failure: {exc!r}")

        for name in list(self.databases):
            attempt(self._close_local_name, name)
        attempt(self.stream.close)
        attempt(self.grants.close)
        for client in (self.idb_client, self.cookie_client):
            if client is not None:
                attempt(client.close)
        for endpoint in (self.idb_endpoint, self.cookie_endpoint):
            if endpoint is not None:
                attempt(endpoint.close)
        if failure is not None:
            raise failure

    def local(self, origin: str) -> sqlite3.Connection:
        if not isinstance(origin, str) or not origin or len(origin.encode()) > 8192:
            raise ValueError("invalid origin")
        name = "local-" + hashlib.sha256(origin.encode()).hexdigest() + ".sqlite"
        if name not in self.databases:
            self.databases[name] = _database(self.directory / name,
                "create table entries (key text primary key, value text not null)",
                "select key,value from entries limit 0", self.files)
        return self.databases[name]

    def close_local(self, origin: str) -> None:
        self._close_local_name("local-" + hashlib.sha256(origin.encode()).hexdigest() + ".sqlite")

    def _close_local_name(self, name: str) -> None:
        db = self.databases.pop(name, None)
        if db is None:
            return
        try:
            result = db.execute("pragma wal_checkpoint(truncate)").fetchone()
            if result is None or tuple(result) != (0, 0, 0):
                raise sqlite3.OperationalError("local storage checkpoint unavailable")
            wal = self.directory / (name + "-wal")
            if self.files.exists(wal) and self.files.size(wal) != 0:
                raise sqlite3.OperationalError("local storage WAL was not truncated")
        finally:
            db.close()

    def activate_owner(self, kind: str) -> None:
        if getattr(self, kind + "_failed"):
            raise ProfileServiceError(f"{kind} owner lost")
        if getattr(self, kind + "_client") is not None:
            return
        endpoint = getattr(self, kind + "_endpoint")
        if endpoint is None:
            raise ProfileRequestRejected(f"supervised {kind} owner not enrolled")
        try:
            if _receive(endpoint).get("ready") is not True:
                raise ValueError("invalid ready record")
            setattr(self, kind + "_client", ProfileClient(endpoint, cookie_event_frames=kind == "cookie"))
            setattr(self, kind + "_endpoint", None)
        except (OSError, EOFError, ValueError):
            setattr(self, kind + "_failed", True)
            raise ProfileServiceError(f"{kind} owner lost before ready")

    def enqueue_event(self, principal: int, event: dict[str, Any]) -> None:
        buffered = getattr(self.pending_events, "items", None)
        if buffered is not None:
            buffered.append((principal, event))
            return
        subscriber = self.subscribers.get(principal)
        if subscriber is None or subscriber["lost"]:
            raise ProfileServiceError("domain event stream unavailable")
        if subscriber["next_sequence"] >= 2**63 or len(subscriber["events"]) >= 256:
            subscriber["events"].clear()
            subscriber["lost"] = True
            raise ProfileServiceError("domain event stream overflow")
        subscriber["events"].append({**event, "sequence": subscriber["next_sequence"]})
        subscriber["next_sequence"] += 1

    def idb_request(self, op: str, payload: dict[str, Any], principal: int) -> Any:
        if self.idb_failed:
            raise ProfileServiceError("IndexedDB owner lost; dispatched effects may be unknown")
        if "connection_id" in payload:
            raise ProfileRequestRejected("connection identity is service-owned")
        self.activate_owner("idb")
        try:
            reply = self.idb_client.request_reply(op, {**payload, "connection_id": principal})
            events = reply.get("events", [])
            if not isinstance(events, list) or len(events) > 1024:
                raise ProfileServiceError("invalid IndexedDB events; effect unknown")
            for event in events:
                if (not isinstance(event, dict) or event.get("type") != "idb_gate"
                        or type(event.get("connection_id")) is not int
                        or event.get("state") not in {"granted", "cancelled"}
                        or not isinstance(event.get("origin"), str)
                        or not isinstance(event.get("ticket"), str)):
                    raise ProfileServiceError("invalid IndexedDB event; effect unknown")
                target = event["connection_id"]
                self.enqueue_event(target, {k: v for k, v in event.items() if k != "connection_id"})
            if not reply["ok"]:
                code = reply.get("error")
                if not isinstance(code, str) or not code.isidentifier():
                    raise ProfileServiceError("invalid IndexedDB owner error; effect unknown")
                raise ProfileRequestRejected(code, code)
            return reply.get("result")
        except ProfileRequestRejected:
            raise
        except ProfileServiceError:
            self.idb_failed = True
            raise

    def disconnect(self, principal: int) -> None:
        if self.idb_client is not None and not self.idb_failed:
            try:
                self.idb_request("idb.disconnect", {}, principal)
            except ProfileServiceError:
                self.idb_failed = True

    def cookie_request(self, op: str, payload: dict[str, Any]) -> Any:
        if self.cookie_failed:
            raise ProfileServiceError("cookie owner lost; dispatched effects may be unknown")
        self.activate_owner("cookie")
        try:
            reply = self.cookie_client.request_reply(op, payload)
            events = reply.get("events", [])
            if not isinstance(events, list) or len(events) > 1024:
                raise ProfileServiceError("invalid cookie event response; effect unknown")
            for event in events:
                if not isinstance(event, dict) or len(json.dumps(event).encode()) > _MAX_MESSAGE:
                    raise ProfileServiceError("invalid cookie event response; effect unknown")
                for principal, subscriber in list(self.subscribers.items()):
                    if not subscriber.get("subscribed"):
                        continue
                    try:
                        self.enqueue_event(principal, {**event, "type": "cookie"})
                    except ProfileServiceError:
                        # This subscriber is failed closed; other domains keep
                        # their own independent event queues.
                        pass
            if not reply["ok"]:
                raise ProfileRequestRejected(reply.get("error", "cookie request rejected"))
            return reply.get("result")
        except ProfileRequestRejected:
            raise
        except ProfileServiceError:
            # Never automatically restart or replay a dispatched mutation.
            self.cookie_failed = True
            raise

    def dispatch(self, op: str, p: dict[str, Any], principal: int) -> Any:
        if op == "events.subscribe":
            if p:
                raise ValueError("invalid subscription")
            subscriber = self.subscribers.get(principal)
            if subscriber is None:
                subscriber = {"events": [], "lost": False, "offset": 0, "next_sequence": 1}
                self.subscribers[principal] = subscriber
            if subscriber.get("subscribed") or subscriber["lost"]:
                raise ValueError("duplicate or lost subscription")
            subscriber["subscribed"] = True
            return {"next_sequence": 1}
        if op == "events.cursor":
            if p or principal not in self.subscribers or self.subscribers[principal]["lost"]:
                raise ProfileRequestRejected("cookie event stream unavailable")
            return {"next_sequence": self.subscribers[principal]["next_sequence"]}
        if op == "events.poll":
            if p or principal not in self.subscribers:
                raise ValueError("no event subscription")
            if principal in self.event_sockets:
                raise ValueError("event stream is push-only")
            subscriber = self.subscribers[principal]
            events, subscriber["events"] = subscriber["events"], []
            return {"events": events, "lost": subscriber["lost"]}
        if op == "session.ping":
            if p:
                raise ValueError("invalid session payload")
            return {"protocol": 1}
        if (op.startswith("cookies.") or op.startswith("idb.")) and self.subscribers.get(principal, {}).get("lost"):
            raise ProfileRequestRejected("domain event stream lost")
        if op.startswith("idb.") and principal not in self.subscribers:
            raise ProfileRequestRejected("IndexedDB requires an event stream")
        if op.startswith("idb."):
            operations = {
                "capabilities", "createObjectStoreWithId", "createIndexRowWithId",
                "acquire", "poll", "cancel", "begin", "commit", "rollback", "schemaSnapshot",
                "blob_begin", "blob_append", "blob_read", "blob_drop", "rows_next", "rows_close",
                "databaseId", "databaseVersion", "databases", "indexExists", "upsertDatabase",
                "deleteDatabase", "objectStoreId", "objectStoreInfo", "nextGeneratedKey",
                "maybeBumpGenerator", "createObjectStore", "renameObjectStore", "renameIndex",
                "deleteObjectStore", "add", "put", "get", "clear", "createIndexRow",
                "deleteIndexRow", "indexInfo", "indexesForStore", "indexNames",
                "objectStoreNames", "addIndexRecord", "deleteIndexRecordsForKey",
                "clearIndexRecordsForStore", "deleteIndexRecordsForRange", "savepoint",
                "releaseSavepoint", "rollbackSavepoint", "indexGetRange", "indexGetKeyRange",
                "indexCountRange", "indexGetAllRows", "indexCursorSeek", "getRange",
                "getKeyRange", "countRange", "deleteRange", "getAllRows", "cursorSeek",
            }
            if op[4:] not in operations:
                raise ProfileRequestRejected("unsupported IndexedDB operation")
            return self.idb_request(op, p, principal)
        if op in {"cookies.header", "cookies.set", "cookies.cdp_set", "cookies.delete", "cookies.clear",
                  "cookies.snapshot", "cookies.cdp_get", "cookies.fields_set", "cookies.bidi_get",
                  "cookies.bidi_delete", "cookies.associated_get", "cookies.associated_delete",
                  "cookies.tool_get", "cookies.store_get", "cookies.store_set"}:
            return self.cookie_request(op, p)
        if op in {"local.snapshot.begin", "local.snapshot.next", "local.snapshot.read", "local.snapshot.end",
                  "local.mutation.begin", "local.mutation.chunk", "local.mutation.commit",
                  "local.mutation.abort"}:
            from .local_storage_stream import QuotaExceeded
            try:
                return self.stream.dispatch(op, p, principal)
            except QuotaExceeded as exc:
                raise ProfileRequestRejected(str(exc), "QuotaExceeded") from exc
        if op == "local.clear" and set(p) == {"origin"}:
            self.local(p["origin"]).execute("delete from entries")
            return None
        raise ValueError("unsupported operation or invalid payload")


def _serve(control: Any, domains: list[Any], name: str, home: PurePath, *,
           messages: UnixMessages, leases: ProfileLeases, random: SecureRandom, grants: SpoolGrants,
           channels: UnixChannels, files: HostFiles,
           artifact_content: ArtifactContent,
           ready: Callable[[Any], None]) -> None:
    profile.BASE = home
    with profile.exclusive(name, leases=leases) as acquired:
        if not acquired:
            raise ProfileServiceError("profile lease busy")
        cfg = profile.require(name)
        if cfg.get("runtime", {}).get("id") not in {"mioa", "lightpanda"}:
            raise ProfileServiceError("profile storage format is not supported by this service")
        from .native_spec import validate_native_spec
        validate_native_spec(cfg, name=name, artifact_content=artifact_content)
        directory = PurePath(cfg["storage"]["path"])
        if artifact_content.resolve(directory) != artifact_content.resolve(PurePath(profile.profile_dir(name)) / "data"):
            raise ProfileServiceError("invalid profile storage path")
        files.make_directory(directory, mode=0o700, parents=True)
        with ExitStack() as startup:
            storage = _Storage(directory, name, cfg, random=random, grants=grants, files=files)
            startup.callback(storage.close)
            startup.callback(channels.close)
            wake_read, wake_write = channels.pair()
            startup.callback(wake_read.close)
            startup.callback(wake_write.close)
            wake_read.setblocking(False)
            wake_write.setblocking(False)
            completed = queue.SimpleQueue()
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
            startup.callback(executor.shutdown, wait=True)
            local_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            startup.callback(local_executor.shutdown, wait=True)
            startup.pop_all()
        connections: dict[Any, dict[str, Any]] = {}
        monitored: dict[Any, Any] = {}
        ignored = object()

        def monitor(channel: Any, state: Any, *, writable: bool = False) -> None:
            if channel in monitored:
                channels.modify(channel, writable=writable)
            else:
                channels.register(channel, writable=writable)
            monitored[channel] = state

        def unmonitor(channel: Any) -> None:
            if monitored.pop(channel, ignored) is not ignored:
                channels.unregister(channel)

        def register_domain(endpoint: Any, principal: int) -> None:
            state = {"principal": principal, "last_id": 0, "busy": False, "suspended": False,
                     "input": bytearray(), "length": None}
            connections[endpoint] = state
            monitor(endpoint, state)
        lanes = {kind: {"queue": collections.deque(), "active": False} for kind in ("cookie", "idb", "local")}

        def run_job(job: dict[str, Any]) -> tuple[dict[str, Any], list]:
            storage.pending_events.items = []
            try:
                if (job.get("peer") is not None and job["kind"] != "local"
                        and getattr(storage, job["kind"] + "_failed")):
                    raise ProfileRequestRejected(f'{job["kind"]} owner unavailable')
                result = job["call"]()
                reply = {"id": job["id"], "ok": True, "result": result}
            except ProfileRequestRejected as exc:
                reply = {"id": job["id"], "ok": False, "error": str(exc), "effect": "none", "error_code": exc.error_code}
            except (ProfileServiceError, sqlite3.Error, OSError) as exc:
                reply = {"id": job["id"], "ok": False, "error": str(exc), "effect": "unknown"}
            except (ValueError, KeyError, UnicodeError) as exc:
                reply = {"id": job["id"], "ok": False, "error": str(exc), "effect": "none"}
            finally:
                events = storage.pending_events.items
                del storage.pending_events.items
            return reply, events

        def start_lane(kind: str) -> None:
            lane = lanes[kind]
            if lane["active"] or not lane["queue"]:
                return
            job = lane["queue"].popleft()
            lane["active"] = True
            future = (local_executor if kind == "local" else executor).submit(run_job, job)
            def done(future: concurrent.futures.Future) -> None:
                completed.put((kind, job, future))
                try:
                    wake_write.send(b"x")
                except (BlockingIOError, OSError):
                    pass
            future.add_done_callback(done)

        def enqueue(kind: str, job: dict[str, Any], *, cleanup: bool = False) -> bool:
            lane = lanes[kind]
            if not cleanup and len(lane["queue"]) >= 128:
                return False
            job["kind"] = kind
            lane["queue"].append(job)
            start_lane(kind)
            return True

        def reply_for(request_id: int, call: Any) -> dict[str, Any]:
            try:
                return {"id": request_id, "ok": True, "result": call()}
            except ProfileRequestRejected as exc:
                return {"id": request_id, "ok": False, "error": str(exc), "effect": "none", "error_code": exc.error_code}
            except (ProfileServiceError, sqlite3.Error, OSError) as exc:
                return {"id": request_id, "ok": False, "error": str(exc), "effect": "unknown"}
            except (ValueError, KeyError, UnicodeError) as exc:
                return {"id": request_id, "ok": False, "error": str(exc), "effect": "none"}

        def deliver(peer: Any, state: dict[str, Any], reply: dict[str, Any]) -> None:
            if peer.fileno() < 0:
                return
            try:
                _send(peer, reply)
                state["busy"] = False
                if state["suspended"]:
                    state["suspended"] = False
                    monitor(peer, state)
            except (OSError, ValueError):
                revoke_principal(state["principal"])

        def revoke_principal(principal: int) -> None:
            for endpoint, state in list(connections.items()):
                if state["principal"] == principal:
                    if not state["suspended"]:
                        unmonitor(endpoint)
                    connections.pop(endpoint)
                    endpoint.close()
            storage.subscribers.pop(principal, None)
            def release_local() -> None:
                try:
                    storage.stream.disconnect(principal)
                finally:
                    storage.grants.unbind(principal)
            for lane in lanes.values():
                lane["queue"] = collections.deque(job for job in lane["queue"] if job.get("principal") != principal)
            enqueue("local", {"id": 0, "principal": None, "call": release_local}, cleanup=True)
            if len(lanes["idb"]["queue"]) >= 128:
                storage.idb_failed = True
                endpoint = storage.idb_client.endpoint if storage.idb_client else storage.idb_endpoint
                if endpoint is not None:
                    try:
                        channels.shutdown(endpoint)
                    except OSError:
                        pass
            else:
                enqueue("idb", {"id": 0, "principal": None, "call": lambda: storage.disconnect(principal)}, cleanup=True)
            event_socket = storage.event_sockets.pop(principal, None)
            if event_socket is not None:
                unmonitor(event_socket)
                event_socket.close()
        def supervisor_lost() -> None:
            nonlocal draining
            draining = True
            if control_reader is not None:
                control_reader.close()
            unmonitor(control)
            control.close()
            for lane in lanes.values():
                retained = collections.deque()
                while lane["queue"]:
                    job = lane["queue"].popleft()
                    if job.get("peer") is not None:
                        deliver(job["peer"], job["state"],
                                {"id": job["id"], "ok": False, "error": "supervisor lost", "effect": "none"})
                    elif not job.get("control"):
                        retained.append(job)
                lane["queue"] = retained

        control_reader = None
        try:
            control_reader = messages.reader(control, max_bytes=_MAX_MESSAGE, max_sockets=4)
            monitor(control, None)
            monitor(wake_read, ("wake",))
            for index, endpoint in enumerate(domains):
                endpoint.settimeout(1.0)
                register_domain(endpoint, index)
            ready(control)
            control_id = 0
            escrow_issued = False
            escrow_confirmed = False
            next_principal = len(domains)
            draining = False
            while monitored:
                # Event sockets are separate from RPC. Never block the owner
                # on a slow listener; partial frames stay in bounded queues.
                for principal, event_socket in list(storage.event_sockets.items()):
                    subscriber = storage.subscribers.get(principal)
                    if subscriber and subscriber["lost"]:
                        revoke_principal(principal)
                        continue
                    monitor(event_socket, ("event", principal),
                            writable=bool(subscriber and subscriber["events"]))
                if (draining and not connections
                        and not any(lane["active"] or lane["queue"] for lane in lanes.values())):
                    break
                for peer, readable, writable in channels.select():
                    state = monitored.get(peer, ignored)
                    if state is ignored:
                        continue
                    if peer.fileno() < 0:
                        continue
                    if state == ("wake",):
                        try:
                            while wake_read.recv(4096):
                                pass
                        except BlockingIOError:
                            pass
                        while not completed.empty():
                            kind, job, future = completed.get()
                            lanes[kind]["active"] = False
                            try:
                                reply, events = future.result()
                            except Exception as exc:
                                setattr(storage, kind + "_failed", True)
                                reply = {"id": job["id"], "ok": False, "error": str(exc), "effect": "unknown"}
                                events = []
                            for target, event in events:
                                try:
                                    storage.enqueue_event(target, event)
                                except ProfileServiceError:
                                    pass
                            if job.get("control"):
                                if control.fileno() >= 0:
                                    try:
                                        _send(control, reply)
                                        monitor(control, None)
                                    except OSError:
                                        supervisor_lost()
                            elif job.get("peer") is not None:
                                deliver(job["peer"], job["state"], reply)
                            start_lane(kind)
                        continue
                    if state == ("control_wait",):
                        try:
                            if not channels.peek(control, 1):
                                supervisor_lost()
                            else:
                                unmonitor(control)
                        except OSError:
                            supervisor_lost()
                        continue
                    if isinstance(state, tuple):
                        principal = state[1]
                        subscriber = storage.subscribers.get(principal)
                        try:
                            if readable:
                                # This channel is server-to-client only; any
                                # input or EOF invalidates its event stream.
                                if peer.recv(1) == b"":
                                    raise EOFError
                                raise ValueError("unexpected event channel input")
                            if writable and subscriber and subscriber["events"]:
                                raw = json.dumps(subscriber["events"][0], separators=(",", ":"), ensure_ascii=True).encode()
                                if len(raw) > _MAX_MESSAGE:
                                    raise ValueError("event frame too large")
                                packet = struct.pack("!I", len(raw)) + raw
                                sent = peer.send(packet[subscriber["offset"]:])
                                if sent <= 0:
                                    raise EOFError
                                subscriber["offset"] += sent
                                if subscriber["offset"] == len(packet):
                                    subscriber["events"].pop(0)
                                    subscriber["offset"] = 0
                        except BlockingIOError:
                            pass
                        except (OSError, EOFError, ValueError):
                            revoke_principal(principal)
                        continue
                    if state is None:
                        try:
                            received = _receive_control(control_reader)
                            if received is None:
                                continue
                            request, endpoints = received
                        except (EOFError, OSError, ValueError):
                            supervisor_lost()
                            continue
                        accepted = False
                        try:
                            request_id = request.get("id")
                            operation = request.get("operation")
                            if (set(request) != {"version", "id", "operation"}
                                    or type(request["version"]) is not int or request["version"] != 1
                                    or not isinstance(operation, str)
                                    or operation not in {"enroll", "enroll_cookie", "enroll_idb", "ready_cookie", "ready_idb", "lease_fds", "escrow_confirm"}
                                    or type(request_id) is not int or not control_id < request_id < 2**63
                                    or (operation == "enroll" and
                                        len({s["principal"] for s in connections.values()}) >= _MAX_DOMAINS)
                                    or (operation.startswith("enroll_") and len(endpoints) != 1)
                                    or (operation == "enroll" and len(endpoints) not in (1, 2, 3, 4))
                                    or (operation in {"ready_cookie", "ready_idb", "lease_fds", "escrow_confirm"} and endpoints)):
                                raise ValueError("invalid enrollment")
                            control_id = request_id
                            if operation == "enroll" and len(lanes["local"]["queue"]) >= 128:
                                _send(control, {"id": request_id, "ok": False, "error": "local cleanup queue full"})
                                continue
                            if operation == "lease_fds":
                                if escrow_issued:
                                    raise ValueError("escrow already issued")
                                if not acquired:
                                    raise ValueError("lease unavailable")
                                data = json.dumps({"id": request_id, "ok": True}, separators=(",", ":")).encode()
                                leases.send(control, data, acquired, max_bytes=_MAX_MESSAGE)
                                escrow_issued = True
                                accepted = True
                                continue
                            if operation == "escrow_confirm":
                                if not escrow_issued or escrow_confirmed:
                                    raise ValueError("invalid escrow confirmation")
                                escrow_confirmed = True
                                _send(control, {"id": request_id, "ok": True})
                                accepted = True
                                continue
                            if not escrow_confirmed:
                                raise ValueError("sidecar lease escrow required")
                            if operation.startswith("ready_"):
                                kind = operation.removeprefix("ready_")
                                monitor(control, ("control_wait",))
                                enqueue(kind, {"id": request_id, "control": True,
                                               "call": lambda kind=kind: storage.activate_owner(kind)}, cleanup=True)
                                accepted = True
                                continue
                            endpoints[0].settimeout(5.0 if operation != "enroll" else 1.0)
                            if operation == "enroll_cookie":
                                if storage.cookie_endpoint or storage.cookie_client or storage.cookie_failed:
                                    raise ValueError("cookie owner already enrolled or lost")
                                storage.cookie_endpoint = endpoints[0]
                            elif operation == "enroll_idb":
                                if storage.idb_endpoint or storage.idb_client or storage.idb_failed:
                                    raise ValueError("IndexedDB owner already enrolled or lost")
                                storage.idb_endpoint = endpoints[0]
                            else:
                                register_domain(endpoints[0], next_principal)
                                if len(endpoints) >= 2:
                                    endpoints[1].setblocking(False)
                                    storage.event_sockets[next_principal] = endpoints[1]
                                    storage.subscribers[next_principal] = {
                                        "events": [], "lost": False, "offset": 0, "next_sequence": 1,
                                    }
                                if len(endpoints) >= 3:
                                    endpoints[2].settimeout(1.0)
                                    register_domain(endpoints[2], next_principal)
                                if len(endpoints) == 4:
                                    storage.grants.bind(next_principal, endpoints[3])
                                next_principal += 1
                            accepted = True
                            _send(control, {"id": request_id, "ok": True})
                        except (OSError, ValueError):
                            supervisor_lost()
                        finally:
                            if not accepted:
                                if operation == "enroll":
                                    revoke_principal(next_principal)
                                for endpoint in endpoints:
                                    endpoint.close()
                        continue
                    if state.get("busy"):
                        try:
                            if not channels.peek(peer, 1):
                                raise EOFError
                            unmonitor(peer)
                            state["suspended"] = True
                        except (OSError, EOFError):
                            revoke_principal(state["principal"])
                        continue
                    try:
                        request = None
                        while True:
                            length = state["length"]
                            needed = (4 if length is None else length) - len(state["input"])
                            chunk = channels.receive_available(peer, needed)
                            if chunk is None:
                                break
                            if not chunk:
                                raise EOFError
                            state["input"].extend(chunk)
                            if len(state["input"]) != (4 if length is None else length):
                                continue
                            if length is None:
                                state["length"] = struct.unpack("!I", state["input"])[0]
                                state["input"].clear()
                                if not 0 < state["length"] <= _MAX_MESSAGE:
                                    raise ValueError("invalid message size")
                                continue
                            request = json.loads(state["input"], object_pairs_hook=_unique)
                            state["input"].clear()
                            state["length"] = None
                            if not isinstance(request, dict):
                                raise ValueError("message must be an object")
                            break
                        if request is None:
                            continue
                        request_id = request.get("id")
                        if (set(request) != {"version", "id", "operation", "payload"} or request["version"] != 1
                                or type(request_id) is not int or not state["last_id"] < request_id < 2**63
                                or not isinstance(request["operation"], str) or not isinstance(request["payload"], dict)):
                            raise ValueError("invalid request")
                        state["last_id"] = request_id
                        state["busy"] = True
                        if draining or not escrow_confirmed:
                            message = "supervisor lost" if draining else "sidecar lease escrow required"
                            deliver(peer, state, {"id": request_id, "ok": False, "error": message, "effect": "none"})
                            continue
                        op, payload, principal = request["operation"], request["payload"], state["principal"]
                        call = lambda op=op, payload=payload, principal=principal: storage.dispatch(op, payload, principal)
                        kind = ("idb" if op.startswith("idb.") else
                                "cookie" if op.startswith("cookies.") else
                                "local" if op.startswith("local.") else None)
                        if kind is None:
                            deliver(peer, state, reply_for(request_id, call))
                        elif not enqueue(kind, {"id": request_id, "principal": principal, "peer": peer,
                                                "state": state, "call": call}):
                            deliver(peer, state, {"id": request_id, "ok": False, "error": "owner queue full", "effect": "none"})
                    except (OSError, EOFError, ValueError):
                        revoke_principal(state["principal"])
        finally:
            if control_reader is not None:
                control_reader.close()
            executor.shutdown(wait=True)
            try:
                local_executor.submit(storage.close).result()
            finally:
                local_executor.shutdown(wait=True)
            wake_read.close()
            wake_write.close()
            channels.close()


