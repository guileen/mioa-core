"""Provider-neutral Python contracts; no OS implementation or implicit backend."""
from asyncio import StreamReader, StreamWriter
from collections.abc import Callable, Mapping, Sequence
from pathlib import PurePath
from socket import socket
from typing import Any, ContextManager, Protocol, TypeVar

T = TypeVar("T")

class HttpRequestDenied(PermissionError):
    """The Host denied the request; callers must not retry another destination."""


class HttpRequests(Protocol):
    """Host-authorized JSON GET, including any requested proxy.

    Check authority for the destination and proxy on every request. Return parsed
    JSON after a successful HTTP status, or raise. Policy denial raises
    HttpRequestDenied. Timeout semantics are backend-defined. This contract
    grants no implicit network access or managed sandbox.
    """

    def get_json(self, url: str, *, proxies: Mapping[str, str] | None, timeout: float) -> Any: ...


class AtomicExchangeUnavailable(RuntimeError):
    """The Host cannot atomically exchange directories on this platform."""


class AtomicDirectories(Protocol):
    """Exchange two directory entries atomically or raise; never copy/delete.

    Success establishes atomic visibility, not durability or isolation.
    Paths and authorization are supplied by the trusted Host assembly.
    """

    def exchange(self, left: str | PurePath, right: str | PurePath) -> None: ...


class WallClock(Protocol):
    """Unix-epoch wall time in nanoseconds; not a monotonic clock or unique ID."""

    def unix_time_ns(self) -> int: ...


class SecureRandom(Protocol):
    """Cryptographically secure random bytes; return exactly count bytes or raise.

    This supplies randomness, not access to files, processes, or other resources.
    """

    def bytes(self, count: int) -> bytes: ...



class ArtifactDiscovery(Protocol):
    """Host-selected native artifact candidates, not artifact authorization.

    Core validates the selected binary and fonts, including their actual pins.
    Explicit paths need no discovery service; no backend is implicit.
    """

    def default_binary(self, runtime: str, repository_default: PurePath) -> str | PurePath: ...

    def font_paths(self) -> list[str]: ...


class ArtifactContent(Protocol):
    """Host-mediated reads of explicitly selected artifact bytes.

    This grants no discovery, provenance or policy authority: Core still pins
    and compares bytes. Every method raises ``OSError`` on I/O failure or an
    invalid target. ``digest`` and ``read`` verify file identity before and
    after the operation and raise instead of returning bytes that changed.
    """

    def resolve(self, path: str | PurePath) -> str: ...

    def executable(self, path: str | PurePath) -> str: ...

    def fingerprint(self, path: str | PurePath) -> tuple[Any, ...]: ...

    def size(self, path: str | PurePath) -> int: ...

    def digest(self, path: str | PurePath) -> str: ...

    def read(self, path: str | PurePath, offset: int, length: int) -> bytes: ...


class StreamConnections(Protocol):
    """Host-installed loopback and inherited-channel stream connectors.

    connect_* return owned async byte streams; failures propagate and carry no
    implicit retry or ambient network fallback. adopt_unix takes ownership of a
    connected, already-authenticated channel. reserve_loopback_port returns a
    port free at observation time; another process may claim it afterwards.
    """

    async def connect_tcp(self, host: str, port: int, *,
                          limit: int) -> tuple[StreamReader, StreamWriter]: ...

    async def adopt_unix(self, channel: Any, *,
                         limit: int) -> tuple[StreamReader, StreamWriter]: ...

    def reserve_loopback_port(self) -> int: ...


class HostFiles(Protocol):
    """Host-mediated directory and whole-file operations for Core-owned paths.

    The Host resolves paths. make_directory creates missing parents, applies the
    requested mode to new directories and accepts an existing directory.
    read_bytes returns the complete file. write_bytes creates or truncates the
    file with no durability guarantee. exists observes the pathname; is_symlink
    does not follow it; is_file reports a regular file; size requires a regular
    file. remove deletes one file or symlink, never a directory tree. Errors
    propagate.
    """

    def make_directory(self, path: str | PurePath, *, mode: int, parents: bool = False) -> None: ...

    def read_bytes(self, path: str | PurePath) -> bytes: ...

    def write_bytes(self, path: str | PurePath, data: bytes) -> None: ...

    def exists(self, path: str | PurePath) -> bool: ...

    def is_symlink(self, path: str | PurePath) -> bool: ...

    def is_file(self, path: str | PurePath) -> bool: ...

    def size(self, path: str | PurePath) -> int: ...

    def remove(self, path: str | PurePath, *, missing_ok: bool = False) -> None: ...


class UnixChannels(Protocol):
    """Host-owned readiness and nonblocking primitives for connected Unix streams.

    Handles are opaque, hashable and already connected; the same handles are
    accepted by ``UnixMessages`` and ``ProfileLeases``. ``pair`` returns two
    owned connected handles. ``register``/``modify``/``unregister`` choose the
    handles reported by ``select``; ``select`` blocks until at least one
    registered handle is ready and reports readability and writability
    separately. ``peek`` returns available bytes without consuming them.
    ``receive_available`` returns bytes when readable, or ``None`` when it would
    block, and leaves the handle on the profile protocol's one-second receive
    timeout. ``shutdown`` disables further sends and receives. ``close`` closes
    the readiness state only; the caller still owns every channel handle.
    Errors propagate; there is no implicit backend, retry or fake readiness.
    """

    def pair(self) -> tuple[Any, Any]: ...
    def register(self, channel: Any, *, writable: bool = False) -> None: ...
    def modify(self, channel: Any, *, writable: bool) -> None: ...
    def unregister(self, channel: Any) -> None: ...
    def select(self) -> list[tuple[Any, bool, bool]]: ...
    def peek(self, channel: Any, size: int) -> bytes: ...
    def receive_available(self, channel: Any, size: int) -> bytes | None: ...
    def shutdown(self, channel: Any) -> None: ...
    def close(self) -> None: ...


class FileExports(Protocol):
    """Host-installed synchronous export authority for trusted local callers.

    Write all bytes, creating/truncating the requested destination, or raise.
    Relative paths resolve at the Host. No parent creation, atomic replacement,
    durability, cancellation or sandbox guarantee. Failure may leave partial data.
    Private exports set POSIX mode bits to 0600 before truncation and writing;
    this alone does not remove ACLs or provide isolation.
    Managed Hosts must not install ambient pathname authority for untrusted Core.
    This path adapter is separate from the bounded native file-v1 handle ABI.
    """

    def write_bytes(self, path: str | PurePath, data: bytes) -> None: ...

    def write_private_bytes(self, path: str | PurePath, data: bytes) -> None: ...


class UnixEndpoint(Protocol):
    """One Host-owned ephemeral pathname, not a connected socket or exit proof.

    The factory guarantees a private directory and an address suitable for the
    negotiated local Unix transport. The caller releases only before issuing
    launch, after authenticated rejection, or after confirmed matching exit.
    Disposal removes only this pathname and its empty directory, never a tree.
    Failure propagates; successful repeated disposal must not touch a new owner.
    No automatic finalizer, timeout cleanup or persistent Profile storage.
    """

    @property
    def path(self) -> PurePath: ...

    @property
    def directory(self) -> PurePath: ...

    def dispose(self) -> None: ...


class UnixEndpoints(Protocol):
    """Host-installed factory; Core must not choose an ambient temp directory."""

    def create(self) -> UnixEndpoint: ...


class ProxySecretError(RuntimeError):
    """A private credential store is unsafe or corrupt."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


class ProxySecrets(Protocol):
    """Host-bound proxy lookup and durable private credential storage.

    ``identity`` is an immutable profile id; None forbids stored lookup.
    resolve is environment-first; read always reads stored data only.
    Implementations may reject or raise and Core supplies no implicit backend.
    """

    def resolve(self, name: str, identity: str | None) -> str | None: ...
    def read(self, identity: str) -> str | None: ...
    def write(self, identity: str, value: str) -> None: ...
    def delete(self, identity: str) -> None: ...


class ProcessEnvironment(Protocol):
    """Host-authorized child environment derivation, not process isolation.

    Return an independently mutable snapshot of the Host-approved base.
    Remove named keys, then apply overlay (overlay wins). Never mutate the
    parent, retain the result, or modify overlay. Errors propagate before spawn.
    No credential lookup or implicit backend; Core owns runtime-specific policy.
    """

    def derive(self, *, remove: tuple[str, ...], overlay: Mapping[str, str]) -> dict[str, str]: ...


class ProcessOutput(Protocol):
    """One owned child output stream; EOF is not process-exit evidence."""

    async def read(self, count: int = -1) -> bytes: ...


class ChildProcess(Protocol):
    """Authority over one child, not a host process-table representation.

    Output streams remain readable through EOF. wait() observes actual exit;
    cancelling a waiter does not terminate the child or close its streams.
    terminate() requests normal termination; kill() forces exit. Neither proves
    exit or durable flush. The backend keeps parent-EOF stdin alive until exit.
    The receiver owns termination, output drainage and reaping after transfer.
    """

    @property
    def returncode(self) -> int | None: ...

    @property
    def stdout(self) -> ProcessOutput | None: ...

    @property
    def stderr(self) -> ProcessOutput | None: ...

    async def wait(self) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


class ProfileDataReset(Protocol):
    """Host-authorized destructive reset of one Profile's runtime data.

    Remove its data tree if it is a directory, recreate that directory, then
    unlink engine.log, engine.sock and state.json if present. Preserve config
    and lock files. Errors propagate; partial effects are possible. Callers
    must arrange any necessary lease and owner exclusion. This grants neither
    isolation nor authority to reset other Profiles.
    """
    def reset(self, directory: str | PurePath) -> None: ...


class ProfileOwners(Protocol):
    """Host authority for persisted profile owner liveness, not lease acquisition.

    Publish while holding the profile/spawn lease, before publishing state.
    No state means not alive even when an owner has already been published.
    Backends recognize legacy state identities and propagate uncertain I/O or
    identity failures rather than treating them as proof of exit.
    """
    def publish(self, home: str | PurePath, name: str) -> None: ...
    def alive(self, home: str | PurePath, name: str) -> bool: ...


class ProfileLease(Protocol):
    """Opaque held profile lease. Closing releases only this owner's copy."""
    def authorizes(self, home: str | PurePath, name: str, kind: str) -> bool: ...
    def close(self) -> None: ...


class ProfileLeaseCopy(Protocol):
    """Opaque received escrow copy; caller must close on every outcome."""
    def close(self) -> None: ...


class ProfileLeases(Protocol):
    """Host authority for compatible store and legacy locks.

    acquire returns a context manager yielding an owned lease, or None when
    busy. Existing profile directories require legacy engine/spawn locks even
    when their lock files are absent. Partial acquisition is rolled back.
    Closing a shared lease or an
    escrow copy never unlocks another owner's copy. Transfer preserves order;
    all received copies are caller-owned and must be closed on every outcome.
    """
    def acquire(self, home: str | PurePath, name: str, kind: str = "engine",
                timeout: float = 0.0) -> ContextManager[ProfileLease | None]: ...
    def send(self, channel: socket, body: bytes, lease: ProfileLease, *, max_bytes: int) -> None: ...
    def receive(self, channel: socket, *, max_bytes: int) -> tuple[bytes, list[ProfileLeaseCopy]]: ...
    def send_copy(self, channel: socket, body: bytes, lease: ProfileLeaseCopy, *, max_bytes: int) -> None: ...


class ChildProcesses(Protocol):
    """Host-installed exec authority with explicit environment and opaque lease.

    Execute argv directly, never via a shell. Snapshot argv and environment
    before suspension; do not inherit ambient environment or other descriptors.
    A profile lease is borrowed through creation and inherited without exposing
    its OS representation. Its holder must remain live until start completes.
    Create separate stdout/stderr pipes and a held-open parent-EOF stdin.
    Successful return transfers one child. On cancellation, settle creation and
    kill/drain/reap any unclaimed child before propagating cancellation, including
    repeated cancellation. Creation errors propagate without an implicit retry.
    Cleanup failure must be reported, never represented as confirmed exit.
    No containment, process-tree termination or storage-durability guarantee.
    """

    async def start(self, argv: Sequence[str], *, environment: Mapping[str, str],
                    profile_lease: ProfileLease | None = None) -> ChildProcess: ...


class SupervisorChannel(Protocol):
    """One-shot trusted-local Unix stream binding, not a portable wire protocol.

    take() transfers ownership of the connected socket to the caller exactly once.
    Missing or consumed authority raises; it never connects an ambient fallback.
    The caller authenticates the peer and closes the socket on failure.
    The Host installs this adapter before invoking Core; it provides no isolation.
    """

    def take(self) -> socket: ...


class BlockingCalls(Protocol):
    """Host-installed execution of bounded synchronous work, not isolation.

    Normal completion returns the result or propagates the operation's error.
    Cancellation (including repeats) waits for actual completion; then discard
    is called once for a successful, unclaimed result before cancellation escapes.
    A failed operation has no result to discard. Drain/cleanup errors are attached
    to cancellation, not substituted for it. Callers keep locks and borrowed
    resources alive through run; operations need their own finite I/O bounds.
    No kill, rollback, process-exit evidence or implicit backend is provided.
    """

    async def run(self, operation: Callable[[], T], *,
                  discard: Callable[[T], None] | None = None) -> T: ...


class UnixMessageReader(Protocol):
    """Incremental socket-frame receiver; single owner, no concurrent reads.

    read_available() never waits for missing bytes and returns None while
    incomplete. It transfers one validated frame and owned sockets at a time,
    without reading ahead. Pending bytes/rights stay reader-owned and bounded.
    EOF/error is terminal and closes pending rights. close() is idempotent,
    discards pending rights, never closes or changes the borrowed channel.
    Already returned sockets remain caller-owned. Reading after close raises
    ValueError. No finalizer; caller must close on every exit.
    """

    def read_available(self) -> tuple[bytes, list[socket]] | None: ...

    def close(self) -> None: ...


class UnixMessages(Protocol):
    """Bounded length-prefixed receive on a borrowed connected Unix stream.

    Read one 4-byte big-endian length and body, collecting SCM_RIGHTS across
    all fragments. Bounds: 1 <= max_bytes <= 1048576, 0 <= max_sockets <= 4.
    Return bytes and owned, non-inheritable connected Unix stream sockets.
    Reject invalid/truncated ancillary data, other descriptor types, EOF,
    invalid length or excess descriptors; close the entire received batch.
    Raise ValueError, OSError or EOFError. Never close the borrowed channel;
    after receive failure the caller must discard it, not resume framing.
    Senders retain transferred socket copies until the caller's receipt ACK;
    premature close can invalidate cross-process socketpair handoff on Darwin.
    No JSON policy, acknowledgement, complete peer authentication or isolation.
    """

    def send_sockets(self, channel: socket, body: bytes, endpoints: Sequence[socket],
                     *, max_bytes: int) -> None:
        """Send one bounded frame and 0–4 borrowed connected socket endpoints.

        max_bytes is 1–1048576 and the body must contain 1–max_bytes bytes.
        The channel and endpoints remain caller-owned, including on failure.
        Rights are attached once; any short write is completed without resending
        rights. Failure may leave a partial frame: discard the channel. This
        method does not wait for an ACK; retain donor copies through receipt ACK.
        """
        ...

    def receive(self, channel: socket, *, max_bytes: int,
                max_sockets: int) -> tuple[bytes, list[socket]]: ...

    def reader(self, channel: socket, *, max_bytes: int,
               max_sockets: int) -> UnixMessageReader:
        """Same bounds/adoption rules, incremental instead of blocking.

        Channel must have timeout None or 0 for the reader's entire lifetime;
        positive-timeout sockets are rejected before consuming data. Per-call
        nonblocking receive never changes shared descriptor flags. The caller
        owns readiness scheduling and must not read this channel elsewhere.
        """
        ...


class ManagedPeerIdentity(Protocol):
    """Host-owned parsing and authentication for the existing supervisor wire.

    ``receive_frame`` reads only the previously verified supervisor channel and
    returns its receipt sequence, opaque one-use message and owned sockets.
    Core acknowledges receipt before decoding the message. Enrollment decoders
    retain peer credentials and return
    only noncredential fields and an authority-bound opaque handle. Profile and
    child READY must correlate with their respective enrollment. Child READY
    additionally checks the local effective user. Supervisor verification uses
    only the peer effective user; child verification requires the enrolled PID,
    local effective user, and a nonzero kernel audit-token version.

    Borrow channels; reject foreign, unready, or invalid handles and malformed
    messages. All failures propagate. This capability does not provide isolation.
    """
    def verify_supervisor(self, channel: socket) -> bool: ...
    def receive_frame(self, channel: socket) -> tuple[int | None, object, list[socket]]: ...
    def profile_started(self, message: object) -> tuple[dict[str, Any], object]: ...
    def profile_ready(self, channel: socket, identity: object) -> None: ...
    def owner_enrollment(self, message: object) -> dict[str, Any]: ...
    def child_enrollment(self, message: object) -> tuple[dict[str, Any], object]: ...
    def child_ready(self, message: object, identity: object) -> dict[str, Any]: ...
    def verify_child(self, channel: socket, identity: object) -> bool: ...


class ProcessIdentity(Protocol):
    """Host-authorized platform and clock observations.

    Core must not query host process identity or clock APIs directly.
    """

    @property
    def platform(self) -> str: ...

    def monotonic_time(self) -> float: ...

    def current_iso_time(self) -> str: ...


