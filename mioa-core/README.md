# mioa-core 0.1.0

Shared Mioa core library for Python 3.12. MIT licensed; see `LICENSE` and
`NOTICE`.

Public modules: `profile` (profile/state storage, leases, strict psutil process
identity, proxy secrets, archives), `native_spec` (schema-3 runtime config and
digests), `native` (bounded CDP client and rebound guarded input), `refs`
(bounded DOM snapshot and opaque references), `operations` (declared operation
table, including `tools.axtree`), `control` (ownership/observation lease),
`engine` (owned headless engine), and `net` (proxy parsing and egress probes).

Hard dependencies: `psutil==7.2.2`, `requests==2.34.2`. This Python package does
not embed native executables or Desktop UI. The owning repository maintains
native webpage execution/layout/paint sources and fonts under `runtime/`,
with its locked builder and compact browser tests. Native component licenses
are separate from this Python package's MIT license.

`net.parse_proxy` preserves bracketed IPv6 authorities, including when adding
default ports or separating credentials.

Page startup requires only the basic page/network/runtime protocol. Site-icon
subscription starts on the first `NativePage.site_icon()` call; a backend
without that extension reports `capability_unavailable` for the icon request,
not for ordinary navigation or rendering.

`profile.migrate(name, binary=..., assets={"fonts": [...]})` explicitly upgrades
a native runtime and its font locations in one snapshotted revision. Environment
id, seed and storage remain owned by the profile; `rollback=snapshot` restores
the prior tree. Startup never silently accepts a different pinned binary/font.

The send lock fences closure: publishing a Close frame seals the channel before
the write; queued commands recheck that state under the same lock and cannot follow it.

Fragmented control frames and extended control lengths are rejected from the
initial two-byte header, without reading extended lengths or payload.
Valid Ping payloads up to 125 bytes still receive a masked Pong.
WebSocket close-frame delivery is best effort within its existing timeout.
Cancellation still closes the owned transport and drains CDP reader/event tasks;
pending requests fail rather than retaining the channel. Session shutdown also
drains log tasks when CDP close fails and propagates the close error.
Transport closure does not prove child exit or storage durability.

CDP response IDs must be strict integers before pending-request lookup.
Boolean or floating-point IDs terminate the reader, fail pending requests and
reject further commands; actions are not replayed. Late unmatched integer IDs
remain ignored, so timed-out responses cannot complete another request.
Matching requests retain pending ownership through response decoding and delivery.
If decoding fails, connection-loss cleanup rejects the matching caller as well as
other pending callers; none waits for its command timeout solely because its
response was removed early. A normal protocol error still rejects only its own
command and does not terminate unrelated work.
