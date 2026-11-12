# Mioa Core — Web execution and rendering

This repository owns the shared Web runtime: native execution, DOM/layout/paint,
the Python control/profile APIs, and the standalone headless service. It also
hosts provider-neutral [OS capability contracts](mioa-os/api/README.md).
Desktop is an independently version-pinned consumer and graphical-system Host,
not the owner of webpage rendering. Develop Core in its independent checkout,
not in a consumer's vendored submodule.

The Python integration and Graphics API specification/types are MIT-licensed. Native
engine, renderer, vendored dependencies and source fonts retain their own licenses; see `NOTICE` and
[`runtime/README.md`](runtime/README.md). Desktop UI, product CLI, profiles,
credentials, generated binaries, caches and local evidence are not included
in the source release. This migration does not itself publish a new release.

## Layout

```
mioa-core/          shared Python package (mioa_core)
mioa-headless/      local session service (mioa_headless) + tests
mioa-os/api/        independent public system and Graphics contracts
mioa-os/            OS implementations outside Core
runtime/            maintained native engine/renderer, source fonts and locked builder
tests/              compact real-engine assertions, reftests and harness regressions
LICENSE             MIT license for the integration, not native relicensing
NOTICE              provenance and component licensing boundaries
MANIFEST.sha256     SHA-256 source inventory; excludes itself and ignored outputs
.gitignore          build, cache, environment and evidence exclusions
```

## Requirements

- CPython 3.12 only (`>=3.12,<3.13`).
- `uv` 0.13.6 for the locked environments (lock revision 5). The locks resolve
  against the canonical Python Package Index (`pypi.org`).
- macOS for real native sessions. `mioa_headless.run_service` refuses other
  platforms, and the pinned runtime is a macOS arm64 executable.
- Native engine build requirements and explicit dependency inputs are documented
  in [`runtime/README.md`](runtime/README.md). Maintained source and required
  source fonts are included; executable artifacts are built, not checked in.

## Install and test

Testing priorities and claim boundaries are recorded in
[`tests/POLICY.md`](tests/POLICY.md); see the source-based
[`coverage audit`](tests/AUDIT.md) for mocks, real-native checks and known gaps.
Executable test selection/enforcement lives in the CI scripts and root Makefile.
The pinned Web standards checklist and TDD gap workflow are documented in
[`tests/STANDARDS.md`](tests/STANDARDS.md). Use `make test CHECKLIST=1` for the
official catalog, or add SPEC=dom to select one standard.
TODO means evidence is incomplete, not that the implementation is necessarily absent.

```sh
make test # default LEVEL=1: source boundary, unit regressions, coverage and next_action
make test MODULE='network storage'
make test LEVEL=2 BINARY=/absolute/path/to/lightpanda
make test LEVEL=3 BINARY=/absolute/path/to/lightpanda RECEIPT=/absolute/path/to/receipt.json
make test HELP=1
make test PLAN=1
make release # Zig engine build from pinned inputs; no Rust code is compiled (toolchain versions are still verified)
make clean   # remove build artifacts; runtime/.local input caches are kept
```

**Agent entrypoint:** always start with `make test`, then follow its `next_action`.
Levels 1/2 may narrow MODULE; level 3 always covers all modules. Mandatory failures
stop acceptance, not implementation. Boundary `next_action` identifies Core,
`mioa-os/api/` and OS implementation edit locations and the migration order.
Missing services are implementation work, not a reason to stop at the gate.
`KEEP_GOING=1` collects selected repair diagnostics after source failures while
retaining FAIL and a nonzero exit; it is not an acceptance waiver.
An explicit no-execution instruction permits file edits but leaves validation pending.
HELP/PLAN/CHECKLIST are views on the same target, not execution or approval.
A scoped delivery verdict does not establish whole-product compatibility.

### Install and package regressions

```sh
uv sync --frozen --no-editable --project mioa-core
uv sync --frozen --no-editable --project mioa-headless

# Installed-mode tests. Unset PYTHONPATH; keep the host temp dir so the
# frame-ring Unix socket stays under the 103-byte sun_path limit.
env -u PYTHONPATH mioa-headless/.venv/bin/python \
  -m unittest discover -s mioa-headless/tests -p 'test_*.py'
```

`mioa-headless/tests/test_axtree_observation.py` intentionally prepends the Core,
Headless, public API and OS source directories; other tests use installed wheels.
The suite covers the frame ring, icon assets, native lifecycle, guarded input,
native transport, the service protocol, and the Agent `axtree` observation.

Build and run the legacy source-mode regression harness:

```sh
node runtime/build.mjs build --inputs runtime/.local/inputs.json
mioa-headless/.venv/bin/python tests/run.py \
  --binary runtime/.local/build/engine/zig-out/bin/lightpanda \
  --output /tmp/mioa-core-tests
```

Create the private input configuration using `runtime/README.md`; no sibling
checkout or executable is discovered. [`tests/README.md`](tests/README.md)
documents CI modes, assertion/reftest/native cases and diagnostics. Legacy
`run.py` requires a binary for its default layers; `--layer unit` is an explicit
smaller run. Its `all` is not every native module test, WPT or a user-task corpus.

## Supported APIs

The following is the declared 0.1.0 callable Python surface; anything not listed
is internal. [OS capability contracts](mioa-os/api/README.md) contain public
C11 declarations and the implementation-free `mioa-os-api` Python package.
Native bindings declare their limited ABI, not a stable wire protocol.
`mioa-os/files.c` implements bounded native file-v1 independently of Core; its
native handle service remains unwired and refuses required isolation.

Screenshot pathname export now uses `mioa_os_api.FileExports`, with the
actual trusted-local implementation in `mioa-os/mioa_os/`. Host supplies
`Engine(file_exports=...)`; Core never directly opens the screenshot path.
Standalone Headless installs the local service; supervised Headless leaves it
unavailable rather than granting ambient pathname access. Bytes-only capture is
unchanged. This migration establishes neither a sandbox nor whole-Core portability.
The source gate remains unresolved; source-mode and installed-package evidence
must be reported separately.

Managed startup also injects `mioa_os_api.UnixEndpoints` from Host.
`mioa-os` owns temporary CDP path allocation/disposal; Core owns the decision to
release after a never-issued launch, explicit rejection or confirmed matching
exit. Unknown outcomes retain endpoints. This replaces Core's private temporary
endpoint implementation without moving persistent Profile data or adding a
direct-backend fallback.
Host also injects `BlockingCalls` for supervised Profile/spawn/query operations.
Cancellation drains actual work while Core retains its channel lock, then closes
unclaimed returned channels before propagating cancellation. This can wait for
existing I/O timeouts; it neither kills threads nor proves child exit.
Host injects `UnixMessages` for supervisor framed receive. `mioa-os` owns raw
descriptor collection and all-or-nothing connected Unix stream adoption, including
ancillary data on later fragments. Core validates receipt identity/count and ACKs
only after adoption; failed validation/ACK closes the returned sockets. Darwin
receive space covers the kernel batch ceiling before enforcing the protocol cap.
`ProfileLeases` creates and transfers opaque lease capabilities; the local OS
backend owns lock descriptors and closes each escrow copy without explicitly
unlocking another holder's shared lock.
Profile startup also receives an explicit Host command prefix from
`mioa_os.profile_owner_command()`. Its `profile_host` entrypoint injects the same
OS receiver into the Profile owner, fixing rights on later enrollment fragments.
Core retains registration policy and ACK ordering; senders keep socket copies
through ACK. Malformed control input discards unclaimed sockets and drains
existing domains while retaining the lease, rather than killing the owner early.
Its selector uses `UnixMessages.reader`: partial control frames retain bounded
OS-owned bytes/rights while existing domain RPC/storage continues. EOF/error or
owner shutdown closes pending rights; already delivered sockets stay Core-owned.

Standalone launch requires explicit `process_environment`, `process_identity`,
`child_processes` and `profile_leases` Host services. `ProcessEnvironment`
derives a private child environment; `ChildProcesses` owns direct exec and
unclaimed-child cancellation cleanup. Core keeps runtime arguments, lease
selection, reserved-key policy and shutdown decisions. The opaque lease stays
live through child start.
Headless installs the local OS implementation only for standalone launch.
Log redaction retains the actual launch proxy rather than rereading mutable
parent credentials after spawning. A configured standalone proxy requires
Host-supplied `proxy_secrets`; trusted Headless supplies the local implementation.
Supervised launches continue to reject proxy references because authenticated
secret delivery is unavailable. Other Core environment access remains migration work.

### `mioa_core.profile`

Profile/config/state storage and ownership leases.

- `store()`, `profile_dir()`, `load_config()`, `save_config()`,
  `load_state()`, `save_state()`, `clear_state()`, `remove()`,
  `list_profiles(owners=...)`, `reset_data(name, data_reset=...)`.
  Reset requires a Host-provided `ProfileDataReset`; callers must arrange
  lease and owner exclusion before destructive reset.
- `exclusive(name, timeout=0.0, kind="engine", leases=...)`: Host-provided
  lease context manager (`kind` is `engine`, `spawn` or `catalog`).
- Profile owner liveness is supplied by `ProfileOwners` (see below).
- `configure_proxy()`, `read_proxy_secret()`, `clear_proxy_secret()`:
  credential operations require an explicit Host-bound `proxy_secrets` authority.
- `export()`, `import_archive()`, `migrate()`: schema-3 profile archives;
  pass `proxy_secrets=` for credential operations; rollback snapshots retain
  stored credentials independently of environment overrides. Failed imports
  restore any prior Host credential before releasing the catalog lease and
  attempt staging cleanup even if restoration fails. Restoration errors
  propagate; the Host credential may then retain the imported value.
- `RuntimeAdapter`, `NATIVE_RUNTIMES`: boundary for product-owned runtimes;
  the core itself launches only the declared native runtimes.

### `mioa_core.native_spec`

Pinned runtime configuration, schema 3.

- `create_native_spec()`, `validate_native_spec()`, `migrate_native_spec()`.
  Profile creation requires Host-injected `random=SecureRandom`; migration
  preserves the existing identity and does not request new randomness.
- `identity_digest()`, `check_identity_collision()`,
  `compatibility_differences()`.
- `resolve_binary()`, `runtime_spec()`, `native_engine_config()`,
  `native_config_bytes()`, `environment_digest()`.
- `native_asset_descriptors()`, `rebind_asset_paths()`, `native_font_assets()`,
  `native_font_families()`.
- `NativeError` with stable `code` values.

Explicit binary and font asset paths work without discovery authority, but all
artifact reads require a Host-injected `artifact_content=ArtifactContent`:
resolution, executable validation, cache fingerprints, sizes, SHA-256 digests
and bounded reads. Missing content fails with `capability_unavailable`; read or
identity failures fail with `incompatible_profile`/`capability_unavailable`.
Trusted local callers pass `artifact_content=LocalArtifactContent()` from
`mioa_os`. Discovery is separate: to select default inputs, pass
`artifact_discovery=LocalArtifactDiscovery()`; it uses
`ANTI_TRACE_<RUNTIME>_BINARY` when set, otherwise the development `PINS` under
`runtime/.local/build/`, and discovers local fonts. Headless standalone startup
supplies both local capabilities; supervised startup supplies content and
requires explicit inputs or an existing profile with pinned paths. Existing
artifact digests still require explicit migration when executable/font bytes
change; relocation is not permission to silently replace an environment.

### `mioa_core.native`

Bounded CDP client for the owned native runtime.

- `launch_native(name, cfg, headless, *, process_environment, child_processes, identity, random, artifact_content, connections, files) -> NativeSession`.
  Launch and supervised adoption require Host `ArtifactContent`,
  `StreamConnections` and `HostFiles`; a missing capability fails with
  `capability_unavailable` before any child or directory is created. Core
  composes `PurePath` and leaves resolution, directory creation and whole-file
  reads/writes to the Host.
- `NativeSession` (process lifecycle, receipt verification, logs, close); direct
  construction requires `proxy=` with the actual launch value, or `None` for DIRECT.
- `NativeContext`, `NativePage`, `NativeFrame`, `NativeLocator`,
  `NativeMouse`, `NativeKeyboard`, `NativeElementHandle`, `NativeJSHandle`.
- `guarded_input(operation, args, fingerprint, *, rebind=None)`:
  pointer operations may retry once the live render is rebound;
  `RENDER_REBINDS = 2`. Non-pointer input keeps the capture-scoped fingerprint.
- `NativeError`, `ProtocolError`.

### `mioa_core.refs`

Opaque element references and the bounded DOM snapshot.

- `RefStore`, `ActionError`, `SNAPSHOT_JS`, `MAX_ELEMENTS = 300`,
  `MAX_TEXT = 120`.
- `RefStore.capture()` returns page/frame/observation identity, masks
  password text/value, and publishes references that `ControlState` validates.
  `RefStore` and `ScriptOperations` require Host `SecureRandom` when creating
  page, frame, or observation IDs; cached IDs require no further randomness.

### `mioa_core.operations`

- `OPERATION_SPECS`, `ScriptOperations`, `ScriptError`, `MAX_ELEMENTS`,
  `MAX_TEXT`.
- Includes the bounded `tools.axtree` observation used by the headless
  service.

### `mioa_core.control`

- `ControlState`, `ControlError`: single-loop ownership lease, run revisions,
  observation identity, human/agent transitions, and `check_write()`.

### `mioa_core.engine`

- `Engine(name, headless, initial_url, verify=True)`.
- `start(state_extra=None)`: launches, activates, observes egress if
  requested, navigates, then writes one `READY` state. `state_extra` cannot
  override engine-owned keys.
- `activate()`: binds a product-owned context to the page lease.
- `page()`, `_axtree()`, `shutdown()` and `control`/`refs`/`operations`.

### `mioa_core.net`

- `parse_proxy()`, `probe_egress()`, `browser_geography()`, `NetworkError`.

### `mioa_headless`

Console entry point: `mioa-headless` (equivalently
`python -m mioa_headless`).

- Required: `--profile`. Optional: `--home`, `--binary`, `--font-file`
  (repeatable), `--url`, `--allow-private-network`, `--native-socket`,
  `--frame-shm`. `--native-socket` and `--frame-shm` must be supplied together.
- Stdio NDJSON requests: `state`, `frame`, `control`, `action`, `axtree`,
  `shutdown`. Native UDS mode adds `resize` and `stop`. Requests are bounded to
  32 KiB (stdio) or 256 KiB (native), with a nonnegative integer `id`.
- `Session`: the bounded engine session; `axtree()` dispatches `tools.axtree`
  under the observation lease.
- `Protocol`: stdio transport. `NativeProtocol`: private UDS control plus the
  LPFR v2 raw-frame ring; installs the invalidation observer in an isolated V8
  world and coalesces event bursts at 8 ms.
- `decode_request()`, `safe_error()`, `make_frame()`, `validate_url()`,
  `default_home()`, `run_service()`.
- `FrameRing`: LPFR v2 shared-memory frame ring (`frame_atomic.c` is compiled
  on demand outside the source tree).
- `IconAssets`: bounded site-icon decoding.
- `ServiceError` with stable `code` values.

## Profile owner liveness

Host injects `ProfileOwners` into Profile operations and Engine. The trusted-local
backend records the current process identity outside profile archives, checks
its creation time to reject PID reuse, and reads legacy states for compatibility.
Malformed or inaccessible records fail closed. Liveness is not proof that
storage is flushed or that a child has exited; profile leases remain mandatory.

## Agent axtree and pointer rebinding

- `Session.axtree(params)` is the real Agent observation path: it calls the
  core `tools.axtree` operation through `ScriptOperations` and `RefStore`,
  publishes the actual page/frame/observation identity, and refuses to
  interleave with an active agent run. Limits are `max_elements` (default 300)
  and `max_text` (default 120); unknown fields are rejected.
- Pointer actions (`mouse.click`, `mouse.move`, `mouse.wheel`) may rebind to
  the current live render when a frame was captured before an animation tick.
  The service revalidates document, viewport, ownership and keyboard focus,
  recaptures the render fingerprint, and then dispatches. `POINTER_REBINDS = 2`
  and `RENDER_REBINDS = 2`. Accepted input is never replayable.

## Reproducibility

Both projects carry `uv.lock` files resolved with uv 0.13.6 against
`https://pypi.org/simple`. If `UV_DEFAULT_INDEX` points to a mirror, set it to
`https://pypi.org/simple` when running `uv lock --check --project mioa-headless`
or `uv lock --check --project mioa-core`.
`MANIFEST.sha256` covers source inputs except itself. Native dependency pins are
in `runtime/inputs.lock.json`; generated receipts bind compiler output to source
and dependency identities. SDK settings and tool versions are pinned, not full
SDK/compiler distributions. Compilation is not a bit-identical reproducibility,
Web-conformance, Desktop, performance or release-acceptance claim.

## Licensing and attribution

The Python integration is MIT. `NOTICE` records Python provenance/dependencies;
native and vendored sources retain component licenses. Lightpanda's
AGPL-3.0-only / AGPL-3.0-or-later declaration discrepancy is preserved rather
than resolved here. Obscura, font and vendored notices remain with their source.
No root/package MIT label relicenses a native component or composite executable.

## Known limitations

- Native sources and required source fonts are included, not prebuilt browser
  executables. Desktop UI, product CLI and viewer implementations stay outside.
- The qualified native input/build path currently targets macOS arm64/CGL;
  Linux/Windows and GPU-free server execution are not implied by "headless".
- The compact test suite covers selected behavior and rendering boundaries,
  not the full WPT corpus or browser compatibility matrix.
- Package names and versions are not published to an index; wheel/sdist
  metadata is reproducible but unsigned.
- The full desktop, Camoufox and trusted-script product matrices are outside
  this tree's scope.
