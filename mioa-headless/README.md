# mioa-headless 0.1.0

Local Mioa session service for Python 3.12. MIT licensed; see `LICENSE` and
`NOTICE`.

Provides the `mioa-headless` console command and `python -m mioa_headless`.
Stdio NDJSON methods: `state`, `frame`, `control`, `action`, `axtree`,
`shutdown`; the optional native UDS transport also supports page-specific
viewport, demand, broker capture/displayed binding/input, resize and stop.
Managed native applications use the supervisor-advertised `application_domains`
capacity, excluding Profile/Cookie/IndexedDB service roles. Each live domain has an
independent bounded normal-request FIFO (16 queued requests per lane, at most eight lanes).
Same-domain work stays ordered; ownership transfers fence all accepted Core
boundaries before granting the global human/Agent lease. Cancelled accepted
actions pause that lease with an unknown-side-effects reason, not a replay.
Both stdio and native UDS protocol 1 `ready` events omit host process IDs.
Native UDS readiness also includes `native_socket`, `frame_shm`, and
`frame_protocol`. Profile state omits host process IDs; authenticated managed
peer verification continues to use supervisor identity privately.
Supervised startup consumes and verifies its inherited Host channel before creating
the Profile home or preparing a Profile; failed preparation closes the channel.
Peer authentication alone does not authorize artifact or Profile access and does
not establish process isolation.


Standalone startup installs `mioa_os.LocalFileExports` through the public
`mioa_os_api.FileExports` protocol for Core screenshot pathname exports.
Supervised startup installs no ambient-path exporter; bytes-only capture remains
available, while `path=` needs an authorized Host implementation. This local
backend is not a sandbox, bounded native file-v1 binding or durability service.
Supervised startup separately injects `mioa_os.LocalUnixEndpoints` through the
public `UnixEndpoints` protocol. It grants only private ephemeral CDP address
allocation/disposal, not arbitrary filesystem writes. Core retains the
authenticated launch/exit release decision; OS cleanup never recursively deletes
unexpected contents or treats a lost control channel as child exit.
Supervised startup also supplies `LocalBlockingCalls` through the public
`BlockingCalls` contract. Profile/spawn/query cancellation drains actual work
before releasing its lock; late unclaimed return channels are closed. Repeated
cancellation and orderly event-loop shutdown preserve this ownership. Cancellation
may wait for existing I/O timeouts and does not establish child exit.
Supervised startup injects `LocalUnixMessages` through `UnixMessages` for bounded
framed receive and all-or-nothing socket adoption. `ProfileLeases` separately
receives Profile escrow as opaque owned copies, not sockets or bare FD lists.
OS enforces each path's descriptor types and bounds; Core retains receipt identity
and escrow/ACK ordering. Closing a local lease copy never explicitly unlocks it.
The backend drains the Darwin kernel rights ceiling before rejecting excess,
avoiding descriptor leaks caused by a protocol-sized ancillary buffer.
Host supplies `profile_command=mioa_os.profile_owner_command()` for Profile startup.
The separate `mioa_os.profile_host` process installs `LocalUnixMessages` before
entering Core, including its enrollment receiver. Core has no private OS import or
implicit module-entry backend. Registration retains donor copies through ACK;
malformed transfers drain existing domains without prematurely releasing the lease.


Standalone startup injects `mioa_os.LocalChildProcesses` through
`Engine(child_processes=...)` as explicit Host authority to execute a native child.
Core supplies argv, a complete environment and the borrowed opaque profile lease;
the local service inherits the lease without exposing its OS representation,
executes without a shell and owns creation cleanup until it transfers the child handle.
Missing authority rejects standalone native startup
before launch. This is trusted-local execution, not process isolation or a sandbox,
and it provides no process-tree termination or storage-durability guarantee.
Standalone startup also injects `mioa_os.LocalProcessEnvironment` through
`Engine(process_environment=...)`. OS supplies an independent parent-environment
copy; Core filters runtime/private-service/proxy inheritance and overlays explicit
launch values without mutating the parent. Logs redact the proxy used to launch
that child even after parent credentials rotate or disappear. Standalone also
injects `LocalProxySecrets(home)` for explicit environment-first proxy lookup and
Host-bound private storage. Supervised startup installs no ambient proxy authority
and rejects configured proxy references; it retains its
supervisor-only execution path.

Ownership, per-domain stop and demand transitions have reserved admission.
Read-only metadata never holds an all-domain execution boundary while awaiting
a provider. Legacy LPFR captures still serialize access to their one shared back
slot, independently of broker capture/input lanes. Disable revokes cached input
bases immediately; enable alone cannot restore them, and captures or bind ACKs
from a disabled demand generation cannot authorize input.

Accepted actions reread the latest Profile state and durably save only when its
URL differs from the current page. Unchanged hover/typing/scroll input performs
no state rewrite or fsync; input-triggered navigation still saves the new URL
with existing fields preserved and persistence errors propagated. Input guards
and observation consumption are unchanged; this is not a screen-latency claim.

Viewport captures return root dimensions from their existing post-pixel
document/view validation RPC, with child-frame validation and final ownership
checks still required before publication. Dimensions are not cached; actions
still consume frame authority and clear observers. This removes one remote
round trip per capture, not a GPU or screen-presentation wait.

Native invalidation notifications send the first pending batch without a
coalescing delay. A finite 8ms cooldown then merges repeated page invalidations;
events received during output backpressure remain pending for the following batch.
The notifier exits when the burst settles, with no idle capture loop. An
`invalidated` packet requests fresh observation; it is not a completed frame or
evidence of screen presentation latency.

Each native `invalidated` notification carries the connection-local monotonic
`revision` captured when its batch is taken. Successful `frame` results include
`invalidation_revision`, sampled **before** capture starts, never at completion.
The desktop can retire delayed notifications at or below that watermark; newer
or unversioned notifications and stale-gap recovery still require another capture.
These sequence numbers coalesce work only: they neither authorize input nor
replace document/view/owner validation. No polling or extra rendering is added.

Native UDS `new_application` is a page-independent control request. Its exact
params are `{"url":"about:blank","owner_revision":N}`; HTTP(S) URLs obey normal
navigation validation. The trusted service connection's bound human identity
and current ownership revision authorize creation, not provider-supplied page,
frame or image identities. The Host admits a fresh launch before Core creates
the target; the accepted creation is an ownership-fenced action on that domain.
It remains available with an empty workspace after the last application Stops.
The result is `status:"ok", page_id, execution_domain, execution_epoch, state`;
`execution_epoch` is the new supervisor launch ID. Capacity rejection is `busy`
with no spawn, stale ownership rejects before dispatch, and dispatched unknown
effects are never retried. Selection rechecks ownership after asynchronous reference
release, before changing the active page, selection epoch or dispatching focus;
revoked creation stays tracked without those later effects. Existing frame-guarded
Web actions are unchanged.
`state` returns `pages:[]` and `page_id:null` when no application remains.
Active `frame {}` likewise returns successful metadata with the sole
discriminator `empty_workspace:true`, `pages:[]`, `page_id:null`, current
`owner_revision` and `state` (plus profile/runtime/trust). It contains no image,
frame sequence, extent, pixels or LPFR lease. Consumers retire the old frame
and do not acquire a raster slot. Explicit page captures still reject stale
page IDs; an empty workspace never fabricates a page or input basis.

`page_bind_displayed` succeeds with `status: "bound"` and the exact echoed
`page_id`, `frame_id`, `document_epoch`, `visual_generation`, `scene_revision`.
Ordinary and governed wheel input require finite, current-viewport `x`, `y` plus
bounded `delta_x`, `delta_y`; neither uses a prior mouse-move position. Invalid
arguments leave the observed frame unconsumed. Legacy LPFR pointer input first tries
the displayed fingerprint through the native atomic guard, without eagerly
recapturing pixels. Only its exact, certain pre-dispatch render rejection invokes
the existing bounded rebind for animation-only changes. The original document,
viewport, focus and owner scope remain required; unknown failures never replay
input. The accepted frame is consumed before dispatch, including failed rebinds.

Physical `page_broker_action` uses `mouse.down` / `mouse.up` and
`keyboard.down` / `keyboard.up`; atomic click/press remain supported.
The existing exact basis (`page_id`, `frame_id`, `document_epoch`,
`visual_generation`, `scene_revision`, `owner_revision`, `operation`, `args`)
adds `channel_id` only for physical down/up or `input.cancel`. IDs are positive
JSON-safe integers through 2^53−1; `(id - 1) % 16` identifies the reserved lane.
Each lane has increasing, never-reused generations and at most one active
channel; different lanes may arrive out of order. The transport encodes its
actual reserved lane, so an unresolved begin cannot be displaced by later
events on another page/domain. Admission reserves terminal capacity before any
down effect; duplicate begins and duplicate queued terminals are rejected.
The ordinary FIFO cannot consume the 16 reserved terminal slots. Successful
terminal responses contain `channel_id`, original `page_id`, and
`status: "ok"` for delivered up or `"reset"` for event-free state reset.

Mouse down includes viewport `x`, `y`, optional `button` and `click_count`;
key down includes `key` (up to eight unique chord parts, e.g. `Meta+C`).
Up carries the original basis and channel ID; `args: {}` uses the retained
original key/button/count/coordinates. Supplied terminal arguments must match
the originals. `input.cancel` takes `args: {}`. Termination routes to the
retained original page/domain even after its displayed guard is consumed,
never to a newer binding. Owner, document, viewport, demand or native focus
changes reset native held state without injecting an event into a new target.
Native focus/document/viewport are sampled after down, allowing its normal
focus transition. Unknown down outcomes remain reserved until up/cancel,
which resets rather than replaying. Unknown terminal outcomes likewise cannot
replay DOM events; the next terminal only resets.

Depends on `mioa-core==0.1.0`. No native runtime, font, UI or product CLI is
included. Most tests run without a native runtime; `MIOA_COOKIE_TEST_BINARY`
explicitly enables real native Cookie-owner/Profile boundary tests.
