# Core tests

A small, source-runnable test system for Core-owned webpage semantics and native
rendering. Desktop is not a test dependency. Nothing imports ATB, a Desktop
checkout, a product CLI, Playwright, NumPy or SciPy. Python 3.12 and the existing
Core/headless dependencies are sufficient; the production LPFR ring uses its
existing C11 helper/compiler or `MIOA_FRAME_ATOMICS` prebuilt library.

`tests/toon.py` is the standard-library evidence encoder. Harness regressions
cover empty-object list items, anonymous nested object arrays, and the content
depth of a list carried on an object's first-field line (TOON §9.4 / §10).
These preserve value types and scope; they are not full codec conformance or
native-browser qualification.

`evidence_view.py` projects validator-owned outcomes without changing verdicts.
Malformed script `subtests` remain ERROR diagnostics, not rendering exceptions
or invented pass counts; valid assertion failures retain counts and diagnostics.
The projection depends only on the standard library and `toon.py`, not the
controller, standards ledger, OS providers or Desktop.

`NavigationHistoryTests` drives the real Core page/CDP client over a socket pair.
Back navigation rejects a history reply overtaken by a main-frame or same-document
navigation, without dispatching the old history entry or replacing the new URL.
Stable history and child-frame-only changes still permit Back. These are scripted
peer regressions, not native-browser task or OS-isolation qualification.

`WebSocketMessageBoundTests` drives the production frame parser with a real
`StreamReader` and a reduced data-message limit. Interleaved Pong payloads neither
consume nor reset the fragmented message budget; genuine data overflow remains
a protocol error. This is parser regression evidence, not native-engine or
live-peer interoperability qualification.

`fixture_server` closes active clients and reaps their handlers before awaiting
server shutdown; Python 3.12+ includes active connections in `wait_closed`.
Real unfinished-HTTP regressions cover failure and cancellation without masking
the original outcome or requiring client EOF. This is fixture cleanup evidence,
not qualification of streaming HTML or independent-window semantics.

## One entry, explicit layers

From the Core checkout, using the Python environment in which Core/headless and
their dependencies are installed:

```sh
python tests/run.py --binary runtime/.local/build/engine/zig-out/bin/lightpanda --output /tmp/mioa-core-tests
python tests/run.py --layer unit --output /tmp/mioa-core-unit
python tests/run.py --layer web --binary /absolute/path/to/lightpanda --output /tmp/mioa-core-web
```

`all` is the default: existing `mioa-headless/tests/test_*.py` Core/headless
regressions, the harness self-tests in this directory, then real native web
cases. Existing unit tests are discovered, not duplicated. Each unit suite runs
in its own Python process; the umbrella writes `results.json`, unit logs and
`web/results.json`. There is no full-suite success without a native binary.
`--layer unit` intentionally makes the smaller claim only.

Both entries add only `mioa-core/` and `mioa-headless/` under this Core checkout
to the developer source import path. This is source mode, not installed-package
verification. Use installed-package tests separately when checking a release.
The explicit binary must be the actual built executable; neither entry builds,
downloads, searches another checkout or falls back to another browser.

The web entry can also be used directly:

```sh
python tests/run_web.py --list
python tests/run_web.py --kind reftest --list
python tests/run_web.py --binary /absolute/path/to/lightpanda --filter layout --output /tmp/mioa-core-layout
python tests/run_web.py --binary /absolute/path/to/lightpanda --kind native --output /tmp/mioa-core-frame
```

`--filter` matches a case-id substring; repeated filters are ORed. `--kind`
selects `assertion`, `reftest` or `native`. An empty selection is an error.
`--output` is a new or empty directory; the default is a newly created system
temporary directory. Evidence may be stored outside source or under ignored
`core/.local`, never alongside tracked tests. Existing artifacts are not
silently overwritten. No test launches user windows or uses an existing profile.

## Layers and current contract

- **Unit/regression:** existing transport, ownership, stale-reference, frame-ring,
  lifecycle, network and service tests, plus harness error-path self-tests.
- **JavaScript assertions:** viewport-relative and font-relative layout units;
  DOM mutation invalidating geometry and hit testing; nested scrollIntoView,
  scrolling geometry and elementFromPoint; native pointer/text insertion,
  input events and focus transfer; exact Canvas backing pixels, clearing and
  save/restore state. Assertions compare concrete values, not just API presence.
- **Pixel reftests:** flex layout versus independently positioned squares,
  overflow clipping versus a smaller unclipped square, and Canvas versus CSS.
  Entire 320x240 frames must match exactly, including alpha. Independent solid
  pixel probes also reject an engine that renders both sides blank. No baseline
  PNGs or baseline-update command exist. No pixel tolerance is currently allowed.
- **Native frame contract:** production `capture_raw_frame` and LPFR ring pixels;
  exact dimensions, stride and DPR; red-to-green paint mutation; stable repeated
  captures; invalidated render fingerprint; stale native input rejects without
  dispatch; a current click reaches the same painted/hit-tested target; focused
  native document/element identity and bounded caret geometry; navigation
  replaces document observation and focused rendering identity.

Each web case gets a fresh private temporary profile and owned native process.
Core `create_native_spec`, committed configuration, engine lease,
`launch_native` and receipt verification remain authoritative. The screen is
320x320 (the schema minimum), with a 320x240 CSS viewport and consistent DPR 1
in both configuration and native emulation. The server binds only a
kernel-selected loopback port and serves a fixed in-memory set of fixture
HTML/JS/CSS bytes, not profiles, outputs or the repository. Private networking is
explicitly enabled only in these isolated test configurations.

Readiness is document `load` completion plus `window.caseReady === true`, font
readiness if exposed and two animation frames. Polling, startup, page operations,
whole-case work, server I/O, and native shutdown have bounded deadlines. There
are no arbitrary "settling" sleeps. `Runtime.exceptionThrown` is retained and
fails a case; assertion evaluation errors also fail. Unsupported capabilities
are errors, never skips. Native exit during a live case is `CRASH`; startup
failure is an error carrying Core's native startup diagnostics. `TIMEOUT`,
`ERROR`, `CRASH`, `FAIL` and shutdown failure all produce nonzero exit status.

## Results and failure controls

The direct runner emits one JSON status line per case and writes:

- `results.json`: suite status, individual results, binary SHA-256 and source
  SHA-256 map for the harness, fixtures and Core/headless adapters;
- `<case>/result.json`: status, elapsed time, runtime binary specification,
  exact fixture/script digests, verified native receipt and measured results;
- `<case>/native.log`: bounded native output retained after shutdown;
- reftest `actual.png` and `reference.png`, plus magenta-on-black `diff.png` on
  unequal pixels, with changed-pixel count and maximum channel difference;
- native contract `before.png` and `mutated.png`, saved before pixel assertions.

PNG diagnostics use the Python standard library. Images are native raw pixels,
not substitute browser screenshots. Failure classification depends on typed
results and numeric comparisons, not a substring such as `PASS` in output.

The **controls suite is intentionally failing**, not expected-failure metadata:

```sh
python tests/run_web.py --binary /absolute/path/to/lightpanda --suite controls --output /tmp/mioa-core-controls
```

It must return nonzero and record `control-assertion-failure: FAIL`,
`control-visual-mismatch: FAIL`, `control-timeout: TIMEOUT`, and
`control-script-error: ERROR`. Each control can be selected with `--filter` to
prove its independent nonzero exit. Never add a control to the default success
suite or normalize its failure to PASS. Unit self-tests exercise actual pixel
differences, PNG encoding/checksums, result rejection, exception/crash
classification, a real canceled async deadline, missing-binary report writing,
and a real loopback server, without fake browser responses.

## Authoring and extensions

`web/cases.json` schema 1 has `cases` and a separate `controls` list. Each case
has a unique safe `id`, `kind`, relative `test` fixture path and optional finite
`timeout` in seconds (default 15, maximum 120). Paths stay below `web/`.

1. Add a minimal HTML fixture under `web/fixtures/` with an explicit readiness
   marker after its intended setup. Do not make readiness conditional on an
   assertion passing: a real failure should be FAIL, not an accidental timeout.
2. For `assertion`, add a JavaScript **async function expression** under
   `web/scripts/`. It calls `await check('name', () => { ... })` with `equal`,
   `near` or `assert` inside. `assert` requires the boolean `true`; `equal` is
   strict equality; `near` permits at most **1/64 CSS px**, solely for fractional
   layout rounding. List the exact subtest names in order in `assertions`.
   Missing, duplicate, renamed, empty and zero-assertion subtests cannot pass.
   Set `input: true` only for the defined native click/text prelude at (40,35).
   Other driver interactions belong in a small explicit driver case, not a new
   simulated DOM input path.
3. For `reftest`, add a `reference` using different primitives from the feature
   under test and `probes`: `[x, y, "rrggbbaa"]` points that anchor the authored
   visible shapes. Keep solid opaque integer-pixel shapes and no text, system
   controls, animations or external resources. Compare the complete frame. If
   future genuine antialiasing needs tolerance, review both a numeric channel
   limit and a differing-pixel budget with a specific justification; do not
   loosen these exact tests.
4. Extend `native` driver cases for engine-only frame, identity, invalidation or
   lifecycle contracts. Keep assertions attached to observed native state and
   actual pixels. Extend unit regressions for harness/parser/protocol failures,
   not as substitutes for native behavior tests.

Future categories can cover navigation/network, storage, accessibility,
subframes, lifecycle/crashtests and additional CSS/Canvas features. Keep the
server and driver small; add origin/resource features only for a concrete test.
This is a local Core harness, **not WPT-compatible**: `assert.js` is not WPT's
`testharness.js`, and its manifest is not a WPT/Firefox manifest.

## Design sources and fixture attribution

The separation of script tests, independently expressed rendering references,
and native/driver-specific tests follows these primary sources (consulted
2026-11-07; no test framework code copied):

- [WPT reftests](https://web-platform-tests.org/writing-tests/reftests.html):
  simpler independent references, paint readiness and explicitly bounded fuzz.
- [Chromium web tests](https://chromium.googlesource.com/chromium/src/+/main/docs/testing/web_tests.md):
  script/render output, separate failed/crashed/timed-out outcomes and diagnostic
  artifacts. This harness intentionally omits automatic rebaselining.
- [Firefox WPT integration](https://firefox-source-docs.mozilla.org/web-platform/index.html):
  distinct assertion, rendering and driver categories, test-id filtering and
  separate product metadata. This harness intentionally has no expected-failure
  pass conversion or automatic skip list.

`web/fixtures/relative-box-edges.html` and `scroll-into-view.html` are adapted
from Obscura `render-repros/` in the existing local rendering repro corpus,
upstream project [h4ckf0r0day/obscura](https://github.com/h4ckf0r0day/obscura),
Apache-2.0. Only an attribution comment and Core readiness marker were added;
the authored geometry and scrolling behavior are preserved. The original
Apache license is retained as `web/LICENSE.obscura`. New assertion scripts and
references exercise those fixtures without copying their paired-corpus,
NumPy/SciPy or baseline machinery. No claim of upstream WPT conformance or of
coverage of the entire web platform is made.
