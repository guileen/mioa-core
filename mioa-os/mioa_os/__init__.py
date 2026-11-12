import time
"""Private OS backends; assembled by Host, never imported by application Core."""
from collections.abc import Mapping
import os
import secrets
import socket
import sys
from pathlib import PurePath
from .artifact_content import LocalArtifactContent
from .artifacts import LocalArtifactDiscovery
from .channels import LocalUnixChannels
from .host_files import LocalHostFiles
from .streams import LocalStreamConnections
from .atomic_directories import LocalAtomicDirectories
from .endpoints import LocalUnixEndpoints
from .blocking import LocalBlockingCalls
from .messages import LocalUnixMessages
from .managed_identity import LocalManagedPeerIdentity
from .processes import LocalChildProcesses
from .leases import LocalProfileLeases
from .owners import LocalProfileOwners
from .profile_data import LocalProfileDataReset
from .profile_records import LocalProfileRecordWriter
from .proxy_secrets import LocalProxySecrets
from .snapshots import LocalSnapshotDirectories


def profile_owner_command() -> tuple[str, ...]:
    """Explicit local Host bootstrap; Core appends inherited endpoint arguments."""
    return (sys.executable, "-m", "mioa_os.profile_host")


class LocalHttpRequests:
    """Ambient HTTP/proxy transport for explicitly trusted standalone Hosts."""

    def get_json(self, url: str, *, proxies: Mapping[str, str] | None, timeout: float):
        import requests
        response = requests.get(url, proxies=proxies, timeout=timeout)
        response.raise_for_status()
        return response.json()


class LocalWallClock:
    """Host wall clock; values can repeat or move backwards."""

    def unix_time_ns(self) -> int:
        return time.time_ns()


class LocalSecureRandom:
    """OS-backed cryptographic randomness for a trusted Host assembly."""

    def bytes(self, count: int) -> bytes:
        return secrets.token_bytes(count)


class LocalFileExports:
    """Ambient path export for an explicitly trusted standalone Host only."""

    def write_bytes(self, path: str | PurePath, data: bytes) -> None:
        with open(path, "wb", buffering=0) as output:
            remaining = memoryview(data)
            while remaining:
                count = output.write(remaining)
                if count is None or count <= 0:
                    raise OSError("file export made no progress")
                remaining = remaining[count:]

    def write_private_bytes(self, path: str | PurePath, data: bytes) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NONBLOCK, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.ftruncate(fd, 0)
            with open(fd, "wb", buffering=0, closefd=False) as output:
                remaining = memoryview(data)
                while remaining:
                    count = output.write(remaining)
                    if count is None or count <= 0:
                        raise OSError("file export made no progress")
                    remaining = remaining[count:]
        finally:
            os.close(fd)




class LocalProcessEnvironment:
    """Ambient inheritance for an explicitly trusted standalone Host only."""

    def derive(self, *, remove: tuple[str, ...], overlay: Mapping[str, str]) -> dict[str, str]:
        environment = os.environ.copy()
        for key in remove:
            environment.pop(key, None)
        environment.update(overlay)
        return environment


class LocalSupervisorChannel:
    """Consume the inherited channel once in a trusted local Host."""

    def __init__(self) -> None:
        self._taken = False

    def take(self) -> socket.socket:
        if self._taken:
            raise ValueError("supervisor channel already consumed")
        self._taken = True
        fd = int(os.environ.pop("MIOA_SUPERVISOR_FD"))
        try:
            return socket.socket(fileno=fd)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise


class LocalProcessIdentity:
    """Ambient process identity for an explicitly trusted standalone Host only."""

    @property
    def platform(self) -> str:
        return os.uname().sysname

    def monotonic_time(self) -> float:
        return time.monotonic()

    def current_iso_time(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


