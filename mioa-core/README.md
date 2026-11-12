# mioa-core 0.1.0

Shared Mioa core library for Python 3.12. MIT licensed; see `LICENSE` and
`NOTICE`.

Public modules: `profile` (profile/state storage, leases, Host-owned process
identity, proxy secrets, archives), `native_spec` (schema-3 runtime config and
digests), `native` (bounded CDP client and rebound guarded input), `refs`
(bounded DOM snapshot and opaque references), `operations` (declared operation
table, including `tools.axtree`), `control` (ownership/observation lease),
`engine` (owned headless engine), and `net` (proxy parsing and egress probes).

Hard dependency: `mioa-os-api==0.1.0`. This Python package does
not embed native executables or Desktop UI. The owning repository maintains
native webpage execution/layout/paint sources and fonts under `runtime/`,
with its locked builder and compact browser tests. Native component licenses
are separate from this Python package's MIT license.

The Host selects the Profile root explicitly (`mioa_core.profile.BASE = root`)
before any Profile operation. Core does not read `ANTI_TRACE_HOME` or choose a
home directory; an unconfigured root fails with `capability_unavailable`.
`profile.export` also requires an explicit destination; Core does not select
the current working directory as an export target.
`profile.list_profiles` ignores directories whose names are not canonical
Profile names; it does not reinterpret them as another Profile.

`profile.reset_data(name, data_reset=service)` requires the Host's
`ProfileDataReset` authority. It removes runtime data and transient state but
preserves configuration and locks. Callers are responsible for lease and owner
exclusion; failure can leave a partial reset. There is no implicit local fallback.

`net.probe_egress(..., http=service)` requires an explicitly Host-provided JSON
HTTP capability. Network failure may try the second endpoint; Host policy denial
stops immediately. The trusted-local adapter uses ambient HTTP/proxy settings;
it is not managed network enforcement.
With a proxy, errors omit its URL and provider error details to avoid exposing
credentials; Host policy denial retains its type without retaining the provider
exception as context, and does not retry.
`net.parse_proxy` preserves bracketed IPv6 authorities and explicit ports;
default ports apply only when the port is omitted.
Missing-host and invalid-authority errors do not echo proxy URLs or credentials;
invalid authorities raise `NetworkError` without the parser exception as context.

The Profile storage owner requires a Host-supplied spool-grant provider.
Temporary files and reservation tickets remain in the OS implementation;
Core uses seek/read/write operations without receiving a raw file descriptor.
If initial resource acquisition fails after adopting grants, earlier resources
are closed before the failure is reported.
The existing enrollment channel is trusted-local, not an isolation guarantee.

`Engine(..., file_exports=service)` installs Host-owned screenshot export through
the public `mioa_os_api.FileExports` protocol. Direct NativeSession users
set `session.file_exports` explicitly. `screenshot()` still returns image bytes;
`path=` requires that service and never falls back to direct filesystem access.
Peer applications resolve the root Host's service. The supplied local backend
belongs to `mioa-os`, not Core, and is for trusted standalone callers only.

Standalone native startup requires `Engine(..., process_environment=service,
process_identity=identity, child_processes=processes, secure_random=random)` or
`launch_native(..., process_environment=service, identity=identity,
child_processes=processes, random=random)`, using the public Host capability
contracts. Supervised startup also requires `SecureRandom`: Core uses it for
native transport and page, frame, and observation IDs, with no ambient fallback.
Core removes inherited runtime, private service and
ambient proxy variables from the Host-approved base; explicit launch values
override removals. Missing services yield `capability_unavailable`, not ambient
fallback. Supervised startup uses the supervisor's identity.
Standalone launch rejects a missing, closed or foreign engine lease before
Profile data preparation or credential lookup, and rechecks it before spawn.
`NativeSession(..., proxy=...)` requires the actual launch proxy (or `None` for
DIRECT), so parent credential rotation/deletion cannot change log redaction or
make session construction reread a now-missing secret. Supervised startup rejects
any configured proxy reference before resolving credentials: authenticated
proxy-secret delivery is unavailable. Standalone proxy lookup requires the
Host's `ProxySecrets` service (`proxy_secrets=` on `Engine` or `launch_native`)
when a reference is configured; DIRECT needs no secret authority unless an
owned credential must be deleted. `profile.configure_proxy`, `profile.remove`,
`profile.import_archive`, `profile.export`, and `profile.migrate` accept that
service for credential operations. Trusted-local `mioa_os.LocalProxySecrets(root)`
binds storage to the Host-selected root and preserves environment-first lookup.
Private migration snapshots and rollback use stored credentials, not environment
overrides. No managed fallback is installed.
`ChildProcesses` owns direct exec and returns a `ChildProcess` capability with
separate output streams, termination and actual-exit observation. Core keeps
runtime arguments, lease selection and the existing graceful-then-forced shutdown
policy. The Host backend holds parent-EOF stdin until exit and, if start is
cancelled before transfer, settles creation and kills/drains/reaps the unclaimed
child before cancellation escapes. The borrowed opaque engine lease remains live
through that operation. Managed startup still uses only its supervisor; this
standalone capability provides no process-tree containment or sandbox.
Neither the child capability nor `NativeSession` exposes a child PID; profile
READY state omits it. Supervised Unix transport still authenticates the exact
supervisor-provided child PID and effective UID before using the connection.

Page startup requires only the basic page/network/runtime protocol. Site-icon
subscription starts on the first `NativePage.site_icon()` call; a backend
without that extension reports `capability_unavailable` for the icon request,
not for ordinary navigation or rendering. Invalid icon state or MIME field
types produce `protocol_error` without replacing the cached icon.

Target discovery and explicit page creation share a context-owned adoption task
per target. Cancelling one waiter does not cancel that shared initialization;
task completion retires its registration, and session shutdown cancels and
drains remaining adoptions. Initialization checks target/context liveness before
emitting a page; every waiter checks it again before returning. Cached closed
pages are rejected rather than reattached. These lifecycle regressions remain
source-only until executed against the current candidate.

WebSocket upgrades require HTTP/1.1 status 101 and a distinct `Upgrade`
connection token; invalid handshakes close the transport.
Fragmented control frames and extended control lengths are rejected from the
initial two-byte header, without reading extended lengths or payload.
Valid Ping payloads up to 125 bytes still receive a masked Pong.
WebSocket close-frame delivery is best effort within its existing timeout.
The send lock fences closure: publishing a Close frame seals the channel before
the write; queued commands recheck that state under the same lock and cannot follow it.
Cancellation still closes the owned transport and drains CDP reader/event tasks;
pending requests fail rather than retaining the channel. Session shutdown also
drains log tasks and releases eligible supervisor channels when CDP close fails;
the close error propagates. Transport closure does not prove child exit or storage
durability.
Context event callback exceptions are recorded in the bounded CDP event
diagnostics; they do not stop other callbacks or terminate the transport.
Returned awaitables, including Futures, are supervised and cancelled on shutdown.


CDP response IDs must be strict integers before pending-request lookup.
Boolean or floating-point IDs terminate the reader, fail pending requests and
reject further commands; actions are not replayed. Late unmatched integer IDs
remain ignored, so timed-out responses cannot complete another request.
Matching requests retain pending ownership through response decoding and delivery.
If decoding fails, connection-loss cleanup rejects the matching caller as well as
other pending callers; none waits for its command timeout solely because its
response was removed early. A normal protocol error still rejects only its own
command and does not terminate unrelated work.
If the reader fails while a command awaits the WebSocket write lock, the
queued write is cancelled and the caller fails without waiting for that lock.
Bytes already written may have taken effect and are never replayed.

`NativePage.goto`, `reload` and `go_back` share one navigation completion path.
Only main-frame `Page.frameNavigated` or `Page.navigatedWithinDocument` advances
its commit sequence. Child-frame changes and execution-context invalidation
still invalidate observations, but cannot satisfy the main-frame commit wait.
A closed/revoked target or failed connection is checked before accepting a
commit. This sequence is not per-request/loader arbitration for concurrent
navigations.
Navigation and URL waits reject an invalid `wait_until` before sending commands.
`go_back` rejects an out-of-range history index or malformed previous entry as
`protocol_error` before navigation; index zero in a nonempty history returns none.
It also rejects target loss or a main-frame commit during the history query
rather than reporting no previous entry or navigating from a stale result.
Document responses received before the main frame is known are recovered by
their CDP loader ID, not by request ID; subresource responses cannot fill that slot.
When the final main-frame commit identifies a loader, navigation returns only
that loader's document response, or none if it was not observed.
`wait_until="commit"` returns using the committed frame URL, including a
separately reported CDP fragment, without requiring a JavaScript execution
context; later load states still query the document.
Final URL reads reject lost targets and do not overwrite a newer frame commit or
target URL update. If either overtakes a final URL read, navigation repeats its
load wait and URL read within the original deadline before returning. A new document
that remains loading cannot inherit the previous document's completed load.
Page initialization preserves a separately reported URL fragment and retries
frame-tree reads overtaken by observation or target URL changes within the
original deadline.
`wait_for_load_state` rejects unavailable pages and discards ready-state results
overtaken by a main-frame commit. `networkidle` requires 500 ms without an
observed active request, new request event or main-frame commit, including
events between polling iterations.
Native startup discovery, navigation, locator and element-handle deadlines and
the network-idle quiet interval use the owning session's injected monotonic
clock, including child frames. They do not fall back to the local host clock.
Chunked and close-delimited discovery response bodies have a one-second total
asynchronous read deadline per attempt and a 64 KiB body limit.
Discovery rejects a non-object JSON response as a protocol error.
Locator state waits reject a target lost during element-count or visibility queries.
They discard query results overtaken by a page observation change and retry
within the original deadline.
Locator value reads likewise reject lost targets and retry results overtaken by
an observation change within the original deadline.
`select_option` never replays its selection and events after an observation change;
it reports `stale_reference` because the original action may already have applied.
Page/child-frame remote-handle creation rejects results overtaken by target
invalidation and best-effort releases returned objects. If invalidation overtakes
its protocol failure, it reports `stale_reference`; stable failures are unchanged.
Title reads likewise reject old-document success or failure without caching it.
Neither path replays evaluation or waits for the new document's load completion.
Ordinary user `evaluate` errors remain unchanged, even across navigation.
Locator click and hover discard hit points overtaken by an observation change,
retrying within the original deadline; target loss during hit-testing fails.
Child-frame raw mouse move/click rejects `stale_reference` if the frame becomes
invalid while resolving coordinates, including the awaited temporary-owner release.
It does not dispatch or retry stale coordinates. This pre-dispatch check does not
replace native guarded input or make a multi-event pointer sequence atomic.
`wait_for_url` accepts an exact URL before applying existing glob matching, so
literal IPv6 brackets do not prevent an exact match.
It rejects a target lost during URL lookup without replacing the cached URL.
It discards URL lookup results overtaken by a main-frame commit or target URL
update. If either occurs while waiting for the requested load state,
it matches the URL again within the original deadline.
Frame-document enumeration rejects results overtaken by page invalidation while
reading the frame tree or resolving a child context.
Child-context creation rejects replies overtaken by invalidation before updating
the shared context map, preserving any newer document's context.
Frame attachment invalidates the observation even if its execution context
was reported first.

`RefStore` binds ordinary observations to the run/owner revisions sampled before
remote capture. Validation and capture/resolve/viewport return paths recheck
page liveness and that binding after their final remote await, including old
observation retirement. Independent page exports remain exempt from the global
lease comparison; their callers still authorize publication and input. These
checks do not replace the native atomic document/input guard.

`Engine.shutdown()` enters runtime-owner closure and local state cleanup even
when reference cleanup fails or is cancelled. Failures still propagate; an
attempted close is not proof of process exit or durable storage flush.

`profile.migrate(name, leases=..., owners=..., clock=..., directories=..., snapshots=..., binary=..., assets={"fonts": [...]})`
explicitly upgrades a native runtime and its font locations in one snapshotted
revision. Host capabilities provide wall-clock time and prepare the private snapshot
directory; rollback does not use either and requires atomic directory exchange. Environment id,
seed and storage remain owned by the profile; `rollback=snapshot` restores the
prior tree. Startup never silently accepts a different pinned binary/font.

`mioa_core.profile_service` is a private experimental Profile lease owner for
trusted adapters. It exposes separately inherited connected sockets, serializes
local-storage transactions against the native SQLite schema, and routes cookie
and IndexedDB operations to separately supervised native owners. The managed
supervisor adapter can enroll independent native processes (execution domains
0 and 4) under the same Profile owner; the second explicit page is launched in
domain 4, while script-created/synchronously coupled pages remain in their
runtime group. Domain Stop reaps only that runtime, not the shared owners.
The ordinary launcher still requires its exclusive engine lease. Managed
containment, storage semantics, resource accounting and interoperability remain
subject to integrated qualification, not inferred from these private APIs.
Loss of the service after a request may leave that request's effect unknown.
The supervisor must escrow every engine-lease descriptor before enrolling
domains, and retain the escrow until all supervised children have been reaped.
Control-channel loss stops new service work; it does not prove process exit.
Managed startup binds the supervisor with `bind_supervisor(...)` before Profile
preparation, then starts the owner on that bound supervisor. Callers that do not
need pre-start preparation may use `start_supervised_profile(...)` instead.
Host supplies public `SupervisorChannel`, `UnixEndpoints`, `BlockingCalls`,
`UnixMessages`, `ProfileLeases`, `ProcessIdentity`, and `ManagedPeerIdentity` services plus an explicit Profile Host command prefix. Core appends
endpoint/name/home arguments; it does not select a backend. The local Host entrypoint
`mioa_os.profile_host` adopts inherited endpoints, injects `UnixMessages`,
`ProfileLeases`, `SecureRandom`, and `SpoolGrants`, and emits the existing
PID-bearing readiness frame after Core initialization. There is no Core module-entry fallback.
The storage stream uses Host-provided cryptographic randomness for principal-bound tokens.
The headless service binds and verifies that inherited channel before creating
its home, acquiring profile leases, or preparing a profile, and closes it if
preparation fails. `bind_supervisor` provides this early binding; starting the
Profile owner still requires the normal handshake and lease escrow. Channel
authentication is not authorization for profile or artifact access, and does
not itself provide a GOS security boundary.
The trusted-local channel adapter consumes the inherited supervisor socket once;
Core owns it from transfer, before authenticating the peer. Missing identity
authority or failed identity lookup, including cancellation, closes the channel
and preserves the original error; no handshake is issued. `ManagedPeerIdentity`
authenticates the supervisor and parses identity-bearing enrollment and readiness
frames into Host-owned opaque handles. It verifies managed CDP against the
enrolled peer; no implicit peer-verification backend is installed. Core no longer
reads the supervisor environment variable, allocates/deletes temporary endpoint paths
or directly dispatches supervisor work to threads. These adapters do not
establish process isolation. The `mioa-os` local endpoint backend uses
unique private mode-0700 directories directly under `/tmp`, independent of HOME
and TMPDIR; persistent Profile/config/data do not move. Each execution domain
retains its endpoint until the authenticated supervisor confirms that exact
child's exit. Lifecycle receipts require an integer launch identity, not a
numerically equal boolean or float. Invalid receipts close the control channel
without releasing the domain reservation or endpoint. Never-issued requests and
explicit pre-admission rejections can release immediately. Only the socket and
empty directory are removed; unrelated files and symlinks are preserved on cleanup
error. Timeout, EOF, mismatched replies and uncertain spawn outcomes retain the
directory; spawn exceptions identify the retained path in their notes.

Supervisor Profile startup, application spawn and lifecycle queries hold their
serialization lock until the blocking operation actually ends, even after repeated
cancellation. A late successful return channel is closed if no caller can accept
it; failures during draining/cleanup annotate the cancellation. Only then is the
failed supervisor channel closed and the lock released. Cancellation may wait for
existing I/O timeouts; this does not prove child exit or authorize endpoint cleanup.
Supervisor framed receive is OS-owned across all header/body fragments. It returns
an all-or-nothing batch of connected, non-inheritable Unix stream sockets rather
than raw descriptor integers. Core validates receipt identity/count before ACK;
validation or ACK failure closes the unclaimed sockets.
Application ready receipts require exact integer launch/PID/UID identities before
publishing the returned socket. Missing identity authority or failed lookup,
including cancellation, closes that unclaimed socket and the failed supervisor
channel while preserving the original error; descriptor receipt ACK is not
semantic identity approval.
Cookie/IndexedDB `owner_started` receipts also require a strictly integer,
matching launch identity before Core waits for readiness or sends `owner_ready`.
Malformed receipts close the supervisor channel without undoing Profile-owned
registrations or treating control loss as child exit.
Profile lease receipt uses `receive_leases(..., leases=...)`, returning 1–2
opaque owned copies. Core checks reply identity/success, transfers the copies to
supervisor escrow, and closes its local copies; only the Host backend handles
native descriptors and shared-lock semantics.
The Profile owner's enrollment receiver uses `UnixMessages.reader` across header/body
fragments, retaining Core identity/operation/count checks and ACK-after-registration.
Each readable event consumes only available bytes; incomplete control frames retain
bounded OS-owned rights without blocking existing domains' RPC/storage. Control
loss and owner shutdown explicitly close the reader's pending rights.
Senders keep socket copies until ACK. Invalid control frames or resource batches
close unclaimed sockets and drain existing domains while retaining the owner lease;
control loss is not child-exit proof. These services are not isolation.


Managed `NativeContext.new_page()` creates a newly admitted application domain,
not an implicit member of the first peer. The supervisor's `profile_started`
record supplies the bounded `application_domains` inventory (application slots
exclude reserved Profile/Cookie/IndexedDB roles). Exhaustion returns `busy`
before spawning. `new_page(synchronize_with=live_page)` explicitly creates a
page in that page's execution domain for synchronous Web relationships;
runtime-created popups likewise remain in their creator's domain. Stopping
one domain revokes every page in that group, not unrelated applications.
The context retains its Profile-owner lease until engine shutdown, including
when its initial application has stopped. A slot becomes reusable only after
the authenticated supervisor reports that launch exited. Every incarnation
gets a fresh `execution_epoch` equal to its supervisor launch ID; cached pages,
events and requests from the old incarnation never regain authority.

Startup failure records retain the original allowlisted safe error code and a
fixed phase (`profile_handshake`, `spawn`, `cdp_connect`, `receipt`,
`target_initialize` or `engine_start`); Desktop adds the `bootstrap` phase for
bundle preparation failures. Diagnostic records never include exception text,
Profile paths or native payloads. A generic historical `startup_rejected`
without the source record is not enough to identify its cause: repeat the
same candidate with an isolated Profile and retain the first rejection record.
No failed or unknown launch is automatically retried.
For streamed local-storage operations, a trusted supervisor can enroll a
separate resource-grant socket alongside each domain's RPC, event, and
IndexedDB sockets. The host associates that capability with the enrolled
domain and charges its existing resource ledger before spool creation or
writes. Grants cover a bounded transfer-buffer reservation, a spool descriptor,
and logical disk reservations; they are not a physical filesystem quota or an
exact measurement of SQLite cache, journal, or WAL usage. Without a grant,
streamed operations fail closed. On disconnect or shutdown, one spool cleanup
failure does not prevent cleanup of other streams. During owner shutdown,
database, stream, grant, and owner-connection cleanup are all attempted before
the first failure is reported. Channel loss leaves outstanding charges uncertain
rather than treating EOF as proof of cleanup.

Cookie name and value together are limited to 4096 UTF-8 bytes after the
entry point's normal whitespace handling, consistently for HTTP/document
cookies, CookieStore and protocol-created cookies. CookieStore rejects an
oversized pair before jar mutation; HTTP/document parsing ignores it without
creating a cookie. This is a byte limit, not a Unicode character limit.
The shared Profile keeps one native cookie owner. Cookie events use the same
1 MiB length-prefixed JSON frame ceiling as Profile RPC, not a separate 8 KiB
limit; receivers grow their event buffer only when needed. The native owner
sends each event in an independent request-correlated frame before the final
reply, so an aggregate batch is not constrained to one frame. Cookie batches
accept at most 1024 entries (the existing owner event budget); larger batches
are rejected before any entry is applied. A valid maximum-size
name/value pair must remain readable from another enrolled domain and deliver
its ordered change event without invalidating that owner. Rejection before
mutation leaves the owner usable. Genuine transport loss or a malformed owner
reply still leaves dispatched effects unknown: neither mutations nor the owner
are automatically restarted or replayed.
Cookie constructors also reserve 512 bytes of envelope space inside that
1 MiB frame and check the ASCII-escaped name/value/domain/path representation
before insertion. An unrepresentable attribute (including a very long default
URL path) is rejected before persistence or notification, not after mutation.

The private native broker producer uses a synchronous, CPU-only
`trusted-core-cpu-v1` borrow. Success correlates the authenticated request with
its broker name and epoch and is returned only after unlock, native surface
release and send-right disposal. This trust profile is not isolation from a
compromised native executor, and does not use page termination or a snapshot
copy as publication proof. Displayed input binds a capture generation to the
confirmed displayed scene revision, verifies current pixels and document
identity in the native event-loop turn, then consumes that basis before input.
Later captures do not overwrite the displayed binding.

IndexedDB values, names and key paths use disk-spooled 128 KiB chunks rather
than the 1 MiB JSON frame as a semantic limit. Native SQLite's length ceiling
remains unchanged; actual materialization requires additional parent-ledger
memory admission, and spool creation requires a logical disk grant. Exhaustion
is reported as `QuotaExceededError`, not a silent reduced value/name limit.
Growth memory remains charged until owner closure and verified process exit;
anonymous spool charges are released only after unmap and successful close;
failed spool creation retains its charge if closing the temporary file fails.
The header-bound V8 clone shim also admits serializer-object and buffer growth
through the runtime allocator before allocation; released buffers retain their
allocator and exact capacity until disposal. Managed serialization fails closed
when built without that shim. This is routed-allocation accounting, not a hard
bound on V8 heap, allocator metadata, or total process RSS.

Managed native application execution uses the private `CPG1` task socket on
inherited FD 8 (32-byte little-endian enter/leave records). The first runnable
native task waits for the Host's exact dispatch authorization; joining Worker
tasks cannot run ahead of that authorization. The last overlapping task ends
the measured process fragment. JS entry, timers, microtask checkpoints,
foreground/idle V8 work, loader/WebSocket deliveries, control dispatch,
Profile callbacks and broker CPU paint use this gate. Blocking Profile reads,
grant exchanges, network polls and broker rendezvous yield the fragment and
reacquire before application continuation. A single mutex serializes protocol
I/O, never JS execution; actual overlapping thread CPU is summed, not represented
as one physical CPU thread. Independent Host sampling and Stop remain necessary
for non-yielding tasks; these hooks alone do not establish a hard CPU quota.

Governed broker capture uses `native-exact-v1`, not the legacy pixel fingerprint.
The native executor retains exact document/frame/loader, viewport, focus and
selection, resource, saturating visual-model and canvas-writer epochs for each
rendered frame. Capture bookends painting with that tuple; binding pins a
separate displayed record, and one input batch consumes it without rendering,
canvas readback or hashing. `visual_generation` is the capture serial, not the
model stamp. Model/canvas epochs are decimal strings, never rounded JSON floats.
Observed pending GPU writes settle before the snapshot; in-flight, raced or
failed writes cannot authorize publication or input. Borrow release and frame
stability are independent: `writer_completion.phase="borrow-released"` can
accompany `capture_stable=false`, in which case the Host can finish the trusted
borrow but must discard the candidate. Legacy raw/headless fingerprint capture
and guarded input retain their existing pixel-based contract.

Locator and element-handle `fill`, `press` and `press_sequentially` retain the
resolved DOM node and its original document before focus/selection. Their
`Input.dispatchNativeAction` batch supplies paired `expectedObjectId` and
`expectedDocumentId` remote identities in the same CDP session; it is a separate
authority mode, never mixed with displayed/pixel/channel authority. The native
executor checks attachment, current document and focus before every event.
Key-up may follow focus to another element in that document, not another frame.
Navigation, adoption, detachment or unexpected focus changes reject remaining
input; the client neither re-queries a replacement element nor replays effects.
Events already dispatched remain applied. Post-fill verification uses the same
element, not a fresh locator. A shared input-target scope attempts document and
temporary element release independently: a document acquisition/release failure
cannot skip temporary element cleanup. Caller-owned element handles are not
disposed by these operations. Page-level keyboard input still targets current
focus explicitly. These new element-bound changes await runtime verification.

## Local dependency wheels

Core, `mioa-os` and `mioa-os/api/system` declare uv cache keys for their
`pyproject.toml` and package Python sources. Source edits must invalidate local
wheels even while versions remain `0.1.0`; do not clear shared caches or bypass
missing API imports with runtime fallbacks. Source-backend Desktop smoke is a
development check, not acceptance of an unchanged packaged backend.
