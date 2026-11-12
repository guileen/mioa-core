"""Trusted-local supervisor identity and opaque wire enrollment binding."""
import json
import os
import socket
import struct
from weakref import WeakKeyDictionary

from .messages import LocalUnixMessages


class _Handle:
    __slots__ = ("__weakref__",)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _read_exact(channel, size):
    data = bytearray()
    while len(data) < size:
        part = channel.recv(size - len(data))
        if not part:
            raise EOFError
        data.extend(part)
    return bytes(data)


class LocalManagedPeerIdentity:
    """Authenticate the inherited supervisor and its enrolled Darwin peers."""

    def __init__(self):
        self._messages = WeakKeyDictionary()
        self._identities = WeakKeyDictionary()
        self._supervisor = None

    def verify_supervisor(self, channel):
        if self._supervisor is not None:
            raise ValueError("managed identity is already bound")
        verified = self._verify(channel, os.geteuid())
        if verified:
            self._supervisor = channel
        return verified

    @staticmethod
    def _verify(channel, euid, pid=None):
        if channel.family != socket.AF_UNIX:
            return False
        if channel.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM:
            return False
        token = channel.getsockopt(0, 0x006, 32)  # Darwin LOCAL_PEERTOKEN
        if len(token) != 32:
            return False
        audit = struct.unpack("=8I", token)
        return audit[1] == euid and (pid is None or (audit[5] == pid and audit[7] != 0))

    def _unwrap(self, body):
        value = json.loads(body, object_pairs_hook=_unique)
        if (not isinstance(value, dict) or set(value) != {"fd_transfer_id", "payload"}
                or type(value["fd_transfer_id"]) is not int
                or not isinstance(value["payload"], dict)):
            raise ValueError("invalid supervisor descriptor receipt identity")
        message = _Handle()
        self._messages[message] = value["payload"]
        return value["fd_transfer_id"], message

    def receive_frame(self, channel):
        if self._supervisor is not channel:
            raise ValueError("channel is not the authenticated supervisor")
        body, endpoints = LocalUnixMessages().receive(channel, max_bytes=16384, max_sockets=4)
        try:
            if endpoints:
                transfer_id, message = self._unwrap(body)
                return transfer_id, message, endpoints
            value = json.loads(body, object_pairs_hook=_unique)
            if not isinstance(value, dict):
                raise ValueError("invalid supervisor response")
            # Descriptor-free spawn rejection is the only identity-phase reply.
            return None, value if set(value) == {"op", "code", "domain"} else {}, endpoints
        except BaseException:
            for endpoint in endpoints:
                endpoint.close()
            raise

    def _payload(self, message):
        try:
            return self._messages.pop(message)
        except (KeyError, TypeError) as exc:
            raise ValueError("foreign supervisor message") from exc

    def _identity(self, identity, kind):
        try:
            record = self._identities[identity]
        except (KeyError, TypeError) as exc:
            raise ValueError("foreign enrolled identity") from exc
        if record[0] != kind:
            raise ValueError("foreign enrolled identity")
        return record

    def _enroll(self, kind, launch, pid):
        identity = _Handle()
        self._identities[identity] = [kind, launch, pid, False]
        return identity

    def profile_started(self, message):
        value = self._payload(message)
        launch, pid = value.get("launch_id"), value.get("pid")
        if (value.get("op") != "profile_started" or type(launch) is not int or launch <= 0
                or type(pid) is not int or pid <= 0):
            raise ValueError("invalid supervised Profile owner identity")
        return {"op": value["op"], "launch_id": launch, "application_domains": value.get("application_domains")}, self._enroll("profile", launch, pid)

    def profile_ready(self, channel, identity):
        identity = self._identity(identity, "profile")
        length, = struct.unpack("!I", _read_exact(channel, 4))
        if not 0 < length <= 1024 * 1024:
            raise ValueError("invalid message size")
        value = json.loads(_read_exact(channel, length), object_pairs_hook=_unique)
        if (not isinstance(value, dict) or value.get("ready") is not True
                or type(value.get("pid")) is not int or value["pid"] != identity[2]):
            raise ValueError("Profile owner readiness identity mismatch")

    def owner_enrollment(self, message):
        value = self._payload(message)
        pid = value.get("pid")
        if type(pid) is not int or pid <= 0:
            raise ValueError("invalid profile owner identity")
        return {key: value.get(key) for key in ("op", "kind", "launch_id")}

    def child_enrollment(self, message):
        value = self._payload(message)
        launch, pid = value.get("launch_id"), value.get("pid")
        if (value.get("op") != "enroll" or type(launch) is not int or launch <= 0
                or type(pid) is not int or pid <= 0):
            raise ValueError("invalid supervisor enrollment identity")
        return {"op": value["op"], "launch_id": launch}, self._enroll("child", launch, pid)

    def child_ready(self, message, identity):
        identity = self._identity(identity, "child")
        value = self._payload(message)
        if (value.get("op") != "ready" or type(value.get("launch_id")) is not int
                or value["launch_id"] != identity[1] or type(value.get("pid")) is not int
                or value["pid"] != identity[2] or type(value.get("uid")) is not int
                or value["uid"] != os.geteuid()):
            raise ValueError("supervisor ready identity mismatch")
        identity[3] = True
        return {"op": "ready", "launch_id": identity[1]}

    def verify_child(self, channel, identity):
        identity = self._identity(identity, "child")
        if not identity[3]:
            raise ValueError("enrolled identity is not ready")
        return self._verify(channel, os.geteuid(), identity[2])
