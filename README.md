# Mioa shared core — release-candidate source tree

Status: isolated release candidate. Not published, tagged, signed, uploaded or
committed. This tree contains only the shared Python core and headless service.

`mioa-core` is the shared library; `mioa-headless` is a local session service
that owns one native runtime and speaks bounded NDJSON. Both are MIT-licensed
(see `LICENSE` and `NOTICE`).

This tree deliberately excludes the native browser runtime, fonts, desktop UI,
product CLI, HTTP viewer, Camoufox/Playwright adapters, profiles, credentials,
logs, caches, virtual environments and generated artifacts.

## Layout

```
mioa-core/          shared Python package (mioa_core)
mioa-headless/      local session service (mioa_headless) + tests
LICENSE             MIT license
NOTICE              provenance, dependency and native-runtime attribution
MANIFEST.sha256     SHA-256 of every tracked file except this manifest
.gitignore          ignored build, cache, runtime and artifact paths
```

## Requirements

- CPython 3.12 only (`>=3.12,<3.13`).
- `uv` 0.13.6 for the locked environments (lock revision 5). The locks resolve
  against the canonical Python Package Index (`pypi.org`).
- macOS for real native sessions. `mioa_headless.run_service` refuses other
  platforms, and the pinned runtime is a macOS arm64 executable.
- A separately obtained native runtime executable and font files for real
  sessions. No engine, font or other native artifact is distributed here.

## Install and test

```sh
uv sync --frozen --no-editable --project mioa-core
uv sync --frozen --no-editable --project mioa-headless

# Installed-mode tests. Unset PYTHONPATH; keep the host temp dir so the
# frame-ring Unix socket stays under the 103-byte sun_path limit.
env -u PYTHONPATH mioa-headless/.venv/bin/python \
  -m unittest discover -s mioa-headless/tests -p 'test_*.py'
```

`mioa-headless/tests/test_axtree_observation.py` intentionally prepends the two
source directories to `sys.path`; the other tests import the installed wheels.
The suite covers the frame ring, icon assets, native lifecycle, guarded input,
native transport, the service protocol, and the Agent `axtree` observation.

## Supported APIs

The following surface is what this candidate installs and exercises. It is the
0.1.0 API; anything not listed is internal.

### `mioa_core.profile`

Profile/config/state storage and ownership leases.

- `store()`, `profile_dir()`, `load_config()`, `save_config()`,
  `load_state()`, `save_state()`, `clear_state()`, `remove()`,
  `list_profiles()`, `reset_data()`.
- `exclusive(name, timeout=0.0, kind="engine")`: `flock`-based lease context
  manager (`kind` is `engine`, `spawn` or `catalog`).
- `pid_alive(pid)`, `process_start(pid)`, `owner_alive(state)`: strict process
  identity (see below).
- `configure_proxy()`, `read_proxy_secret()`, `clear_proxy_secret()`:
  owner-only credential storage keyed by immutable environment id.
- `export()`, `import_archive()`, `migrate()`: schema-3 profile archives.
- `RuntimeAdapter`, `NATIVE_RUNTIMES`: boundary for product-owned runtimes;
  the core itself launches only the declared native runtimes.

### `mioa_core.native_spec`

Pinned runtime configuration, schema 3.

- `create_native_spec()`, `validate_native_spec()`, `migrate_native_spec()`.
- `identity_digest()`, `check_identity_collision()`,
  `compatibility_differences()`.
- `resolve_binary()`, `runtime_spec()`, `native_engine_config()`,
  `native_config_bytes()`, `environment_digest()`.
- `native_asset_descriptors()`, `rebind_asset_paths()`, `native_font_assets()`,
  `native_font_families()`.
- `NativeError` with stable `code` values.

The development defaults resolve `PINS` relative to the source-tree root
(`parents[2]`) and therefore expect a `browsers/engine-bench/...` layout. A
standalone installation must pass an explicit binary (`--binary`, or
`ANTI_TRACE_<RUNTIME>_BINARY`) or asset paths. The source bytes are unchanged.

### `mioa_core.native`

Bounded CDP client for the owned native runtime.

- `launch_native(name, cfg, headless) -> NativeSession`.
- `NativeSession` (process lifecycle, receipt verification, logs, close).
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

## Strict process identity

`mioa_core.profile.process_start(pid)` returns a `float` from
`psutil.Process(pid).create_time()` or `None`. `owner_alive(state)` accepts a
state only when `state["process_start"]` is an `int`/`float` and exactly equals
the live process create time. A reused pid with a different create time is not
the owner. There is no `os.kill(pid, 0)` fallback and no `None` create-time
degradation. `psutil` is a hard dependency.

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
`MANIFEST.sha256` covers every tracked file except itself. No build, cache, log
or native artifact is tracked.

## Licensing and attribution

MIT for this tree. `NOTICE` records source provenance, the licenses of the
declared Python dependencies, and the fact that the native runtime is not
included and is licensed separately. The Lightpanda runtime is AGPL-3.0
(upstream LICENSE is the GNU AGPL version 3 text); the alternate Obscura path
declares Apache-2.0. This tree grants no rights to either program.

## Known limitations

- No native runtime, font, desktop UI, product CLI or viewer is included or
  distributed.
- The headless service requires macOS for native execution and an explicitly
  supplied pinned binary/font on first profile creation.
- Package names and versions are not published to an index; wheel/sdist
  metadata is reproducible but unsigned.
- The full desktop, Camoufox and trusted-script product matrices are outside
  this tree's scope.
