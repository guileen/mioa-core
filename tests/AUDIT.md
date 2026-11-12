# Test coverage audit

**Source-review snapshot, 2026-11-08; no tests, builds, browsers or campaigns were
run for this audit.** Test sources are changing concurrently; inventories below
describe the reviewed snapshot, not a frozen execution manifest. Descriptions
establish intent/wiring, not successful execution, measured coverage or release
readiness. `[INFERENCE]` marks judgment beyond source observations.
[POLICY.md](POLICY.md) records priorities; executable CI manifests/plans control
the current selection and enforcement.

## What the existing entries actually cover

| Source / inventory | Actual evidence boundary | Important limitation |
| --- | --- | --- |
| [`run.py`](run.py) | Discovers `mioa-headless/tests/test_*.py` and `tests/test_*.py` in separate source-mode Python processes; `all` then calls `run_web.py` | Not installed-package acceptance, Zig/Rust module execution, quality campaigns, WPT or a target-task corpus |
| [`web/cases.json`](web/cases.json), [`run_web.py`](run_web.py) | Ten normal cases: six script assertions, three exact reftests, one native-frame contract; four separate intentionally failing controls | Small local fixture set, not representative website/task compatibility |
| [`test_web_harness.py`](test_web_harness.py) | Real local HTTP serving, async cancellation, pixel/PNG comparisons and failure classification; feeds authored result records to validators | No engine execution; crash classification uses a supplied exit code, not an induced browser crash |
| [`quality_native.py`](quality_native.py), [`quality/native_contracts.json`](quality/native_contracts.json) | Compiles actual Zig primitive tests/probes and a C/libpng decoder; pins thirteen identities (including two import roots), 2231 entity entries and thirteen mutations | Isolated Entities/PNG closure; no browser/V8/Rust integration or general HTML/CSS coverage |
| [`quality_browser.py`](quality_browser.py), [`quality_browser/manifest.json`](quality_browser/manifest.json) | Explicit native or pinned reference-browser adapter: 80 html5lib entity cases projected into DOM observations, five supplements, finite Canvas export observations | Does not expose tokenizer error events, run WPT, or establish current-source provenance for a historical binary |
| [`quality_fuzz.py`](quality_fuzz.py), [`quality_regressions/`](quality_regressions/) | Bounded deterministic PNG chunk mutations, independent-decoder comparison, reduction/promotion/replay; a retained invalid-chunk-type oracle regression | Not coverage-guided browser fuzzing or general PNG conformance; a saved regression record is not a fresh execution |
| [`test_quality_native.py`](test_quality_native.py), [`test_quality_browser.py`](test_quality_browser.py), [`test_quality_fuzz.py`](test_quality_fuzz.py), [`test_png_exports.py`](test_png_exports.py) | Evidence parser/verdict, corpus hash/inventory, mutation boundary, reducer and PNG oracle regressions | Python harness success does not execute the native/differential campaigns it describes |

The web runner launches the explicitly selected real binary through
`create_native_spec` / `launch_native`, with a fresh private profile and receipt
verification. Its native contract checks actual capture pixels, fingerprint
invalidation, stale-input rejection before dispatch, current input, focused caret
bounds and navigation identity. Its solid-pixel probes prevent two blank images
from satisfying a reftest. Missing capabilities, script errors, crashes, timeouts
and shutdown failures are nonpassing in this harness. These are useful retained
integration boundaries, not a blanket product-compatibility score.

## Python module test inventory: mocks versus real resources

Sources are in [`../mioa-headless/tests/`](../mioa-headless/tests/):

- `test_service.py`, `test_native_service.py`, `test_native_guarded_input.py`:
  exercise real validation/control/session/dispatch code, often with `AsyncMock`,
  `SimpleNamespace`, authored observer snapshots or fake CDP responses. Useful
  authority and ordering regressions; they do not prove actual DOM/native input.
  Element-input cleanup regressions use real locator/element-handle methods with
  fake CDP replies to cover document acquisition/release failure, input failure
  and cancellation, and owned versus borrowed handle lifetime.
- `test_axtree_observation.py`: explicitly uses a fake page/frame/handle and
  patched native launch through real Engine/RefStore/control code. Covers
  masking/stale ownership, not native accessibility-tree completeness or quality.
- `test_native_lifecycle.py`: combines fake processes/engines and patched service
  collaborators with actual socket/authentication/descriptor lifecycle checks.
  Navigation regressions drive real `NativePage` state with authored CDP events
  and a stubbed ready document; they do not execute a native browser.
  Core shutdown regressions use real `Engine`/`NativeSession` code with a simulated
  process and temporary READY state, not evidence of native process termination.
  Adoption regressions drive real `NativeContext`/`NativePage` and shutdown code
  with gated fake CDP replies and a simulated process; cancellation isolation and
  target-loss assertions are not native target-lifecycle qualification.
  Darwin peer-audit-token tests are platform-conditional; using Python as a
  configured executable does not make these real browser sessions.
- `test_frame_ring.py`: actual mapped frame-ring publication, C11 atomic helper,
  contention, permissions, bounds and owner locks; loader tests mock loading and
  compilation. Valuable transport integrity, distinct from actual renderer output.
- `test_profile_service.py`: starts the actual profile service and tests real
  principal-bound stream RPC/storage, including isolated client processes. Its
  normal configuration writes dummy runtime/font bytes. Four native-cookie-owner
  tests require `MIOA_COOKIE_TEST_BINARY` and skip without it; a zero-exit unit
  run can therefore omit that native evidence.
- `test_profile_migration.py`: real temporary profile trees and dummy runtime/font
  files test preservation and rollback, not executable startup after migration.
- `test_profile_resource_grant.py`: real socket-pair grant/release and EOF behavior;
  `test_profile_service_events.py` uses authored owner replies to test event
  sequencing, overflow and principal binding.
- `test_net.py` patches `requests.get`: credential/scheme transport construction,
  not an actual proxy/TLS/egress workflow. `test_icon_assets.py` exercises real
  private content-addressed files and invalid/oversized input handling.

## Native module inventory outside the legacy umbrella

[`../runtime/engine/build.zig`](../runtime/engine/build.zig) declares separate
`test` and `native-tests` steps. `src/native_tests.zig` imports **only** Entities
and PNG; it must not be confused with the broader engine module suite. The full
engine's `src/test_runner.zig` supports `TEST_FILTER`, skip accounting and HTML
subtests. Source declarations include Frame navigation/load cancellation,
Session history/sessionStorage, Runner readiness/network idle, ScriptManager
teardown, ImportMap, MIME/URL, CSS parser/media/cascade and ExactVisualGuard tests.
These existing module tests are candidates for focused reuse; they were not run
here and their existence does not prove every declaration is selected/executed
by a given built artifact.

The renderer crate inventory under
[`../runtime/renderer/crates/`](../runtime/renderer/crates/) also contains separate
Rust tests, including browser native-event/input/fetch boundaries; CDP frames,
execution contexts, navigation/connections and input; JS response streams and
isolate teardown; render layout/text/shaping/allocation tests; DOM slot-query
allocations; and network compressed-response/interception tests. The Python
`run.py` discovery does not invoke Cargo. File inventory and sampled engine test
declarations are not a measured, complete executed module-coverage map.

## Gaps and prioritization findings

1. **No product task denominator.** `web/cases.json` has no target-user task
   population, usage weighting, acceptance threshold or representative workflow
   corpus. `[INFERENCE]` Its pass rate cannot answer whether enough target tasks
   work, particularly multi-step navigation/forms/authentication/resource loading.
2. **No Mioa WPT evidence.** The browser manifest explicitly records WPT
   `NOT_RUN` and the missing supported product integration. The local `assert.js`
   harness is not WPT `testharness.js`; neither reference-browser execution nor
   primitive conformance closes that gap.
3. **Coverage investment is skewed.** Two of ten local web cases and the dedicated
   native mutation, fuzz, roundtrip and browser-differential tooling focus on
   Entities/PNG. `[INFERENCE]` This is deeper evidence infrastructure than exists
   for representative target tasks; assertion/corpus counts overstate breadth if
   presented without that scope. Retain these regressions, but prioritize missing
   task and P0/P1 boundary evidence rather than further PNG expansion by default.
4. **Mocks leave integration gaps.** Existing Python authority/lifecycle tests are
   useful and often sophisticated, but native accessibility, real proxy/network,
   profile upgrade startup and end-to-end storage behavior need their own actual
   artifact/task evidence. Broader native source tests are not absent merely
   because the Python umbrella omits them.
5. **Legacy green can contain omissions.** `run.py` judges each unittest process
   by return code and does not promote individual skips into gate failures.
   Optional native-cookie and non-Darwin checks can be skipped. Its `all` also
   omits isolated quality execution and broader Zig/Rust suites. Report the
   precise plan and omissions; do not call it comprehensive release acceptance.

**Retain:** authority/isolation regressions, frame-ring contention and ownership,
profile RPC/migration safety, bounded failure controls, independent rendering
references, primitive allocation/callback tests and confirmed oracle regressions.
**Release conclusion:** no candidate was exercised in this audit. Current-source
baseline pass, task sufficiency, performance/reliability rates and absolute
release acceptance remain unestablished by this document.

## Standards inventory boundary

[`standards/catalog.json`](standards/catalog.json) pins all **831** entries from
[W3C browser-specs](https://w3c.github.io/browser-specs/index.json), retrieved
2026-11-08T17:37:46Z, raw-byte SHA256
`3f2f142f6e4e064070b5eb72ba1bfe032d73ded6e2dde06566ec40e1facf9a28`.
The transformation retains 715 browser-category and 116 uncategorized entries,
including versions, discontinued documents, registries and guidance. It is
complete relative to that pinned source, not every RFC or every possible Web
standard, and it does not require Core to implement every listed technology.

`make test CHECKLIST=1` provides the
per-standard checklist, official links, available WPT paths, gaps and next TDD
actions; see [STANDARDS.md](STANDARDS.md). `--format json` is the machine export.
The initial empty decisions were a historical state; current intended scopes
are in `standards/decisions.json`. Neither missing decisions nor TODO prove
absent implementation. A limited HTML/CSS/input fixture cannot establish
whole-standard support; associated failures remain failures.
Broader existing Zig/Rust suites still need comprehensive runner integration and
candidate-bound execution evidence. Neither a catalog listing nor documentation
closes that gap. `native-frame-contract` cannot be waived by deploy acceptance.

## 执行器验证记录

The source-review snapshot above is distinct from these historical executions.
The commands/target names below describe that past run, not the current interface;
use `make test` now. No current-source browser build or release claim follows:

- `omh context` discovered the Makefile's initial five testing targets.
- `make test-dev MODULE=network` passed `unit/test_net`; its report remained
  **INCOMPLETE** because an explicit evidence gap remained.
- Against an explicitly selected historical binary, the input path passed
  `unit/test_native_guarded_input` and `web/input-focus`. The report remained
  **INCOMPLETE**, with source binding **NOT_VERIFIED**; this does not establish
  a current-build result.
- Real CI smoke execution exposed missing `PYTHONPATH` propagation and skipped
  unittest `TestCase` JSON serialization; those executor defects were fixed.
  Temporary-root fixtures were corrected to resolve macOS `/var` symlinks.
- Final `make test-diagnose` completed with **18 PASS suites, one INCOMPLETE
  suite, ten NOT_RUN browser cases, zero failed suites**. Python reported 235
  test records, including four native-cookie tests skipped for lack of a rebuilt
  native owner. `test_ci` and `test_standards` passed. This is deliberately
  **INCOMPLETE**, not a product pass; no browser binary was supplied for this run.
- Diagnostic report: `/var/folders/2x/093qdj3n0kx8mp4_0m_p20gm0000gn/T/mioa-ci-k2p5ba7k/report.json`.
  Adjacent `standards.json`/`standards.md` contain all 831 catalog entries:
  TODO P0 **11**, TODO P1 **26**, TODO P2 **678**, TODO P3 **116**. These count
  evidence gaps, not absent implementations or weighted user compatibility.
- `make test-standards SPEC=html REPORT=...` successfully validated and consumed
  that diagnostic. `make test-deploy` without binary/receipt correctly refused
  deployment and retained a full NOT_RUN checklist.
- Final `omh context` discovered all six Makefile targets, including
  `test-standards`. No official WPT, full native suite, or release candidate was
  executed by these observations.

Retained local copies: `.local/test-ci/baseline/report.json`,
`.local/test-ci/baseline/standards.json` and `.local/test-ci/baseline/standards.md`.
Execution logs remain at the original temporary paths recorded in the report.

## Agent preflight and first registered DOM scope

At that historical preflight snapshot, `make test` audited 831 catalog entries,
nine with associated tests (13 web cases), one declared finite scope and 830
undecided scopes. P0 then had 11 entries, seven without registered tests and ten
without a scope decision. These counts are not the current inventory.
Unregistered existing native tests must be inspected/reused before adding tests.

Four unmodified official DOM WPT pages are pinned at
`dcfd7ea3c7074466c6a9a671865fa147e0f0bd85`. Actual native execution passed appendChild
11/11, removeChild 28/28, contains 1482/1482 and textContent 81/81.
Evidence: `.local/test-ci/dom-wpt-final/report.json` and adjacent standards/coverage
artifacts. The binary was not bound to the current source receipt: result remains
INCOMPLETE, not current-product conformance. Full wptrunner remains unintegrated.

The module command now emits the standards ledger from the same execution report.
Numeric WPT subtest counts are kept separate from local string-status assertions;
a real smoke run exposed that distinction and its regression now passes. All
seven harness suites passed after these changes.

## Agent-facing evidence

Primary entries are now `report.toon`, `coverage.txt`, `standards.toon`/`standards.md`
and native `results.toon`. JSON is retained only as validator input or explicit
machine export; redundant per-case JSON and default ledger/coverage JSON exports
were removed. TOON evidence is digest-bound and checked against the machine record.
Tests reject both modified evidence and contradictory machine outcomes. Seven
harness suites and the actual DOM subset passed this change; the unbound candidate
remains INCOMPLETE. Historical sample: `.local/test-ci/dom-readable-evidence/report.txt`
(text form used before the TOON migration).

## Source-only task-boundary repairs — 2026-11-08 through 2026-11-09

**Execution: NOT_RUN.** File operations only were permitted for these repairs;
no compiler, Python/JS runner, browser, build or test command was invoked.

| Boundary | Saved change / authored regression |
| --- | --- |
| History URL authorization | `History.zig` checks serialized rewrite authority before mutation; `history_url_update.html` asserts rejection leaves URL, state and entries unchanged |
| Element input | `native.py` retains node/document remote identities; native input checks them before each event; `input.zig` regressions cover focus drift, detachment, navigation and adoption |
| Element input resource ownership | `NativeLocator` shares one input-target scope across fill/press/sequential input; nested cleanup attempts temporary element release even when document acquisition/release fails, retaining caller-owned handles. `test_native_guarded_input.py` adds success/failure/cancellation ownership regressions; execution remains NOT_RUN |
| Navigation completion | `NativePage` separates main-frame commits from observation invalidation and shares completion across goto/reload/back; `NativeNavigationTests` in `test_native_lifecycle.py` covers child/context noise, same-document commit/reply ordering and detach after commit |
| Observation binding | `RefStore` samples leases before remote capture and rechecks binding at validation/return boundaries; `test_axtree_observation.py` covers delayed capture, document/node validation, retirement and viewport dimensions, retaining independent-export lease semantics |
| Shutdown ownership | `Engine.shutdown` closes its runtime owner and clears local state even after reference-cleanup failure/cancellation; `EngineShutdownTests` retains the failure while checking simulated native closure and READY removal |
| Target adoption ownership | `NativeContext` uses one shielded task per target with context-owned completion; `NativeSession` cancels/drains remaining adoptions. Publication, waiter return and cache reuse reject lost targets. `NativeAdoptionTests` covers either waiter cancelling, target loss during final initialization or page notification, cached target loss and shutdown after the last waiter cancels. Execution remains NOT_RUN |
| Form target | `Frame.zig` treats empty action/formaction as document URL; `frames/form_empty_action.html` covers both attributes and GET/POST with a conflicting base |
| Fragment traversal | `Location.zig` uses push; `event/hashchange.html` covers new entries, no-op assignment and back/forward |
| IndexedDB false PASS | `storage-idb.js` rejects errors/blocked/timeouts and requires upgrade/store creation plus success |
| Standards false attribution | Unrelated associations/declarations removed; retained scopes bounded to actual assertions; offline-audio skip removed; `test_standards.py` adds unrelated-PASS rejection cases |

These source regressions are not passing evidence. Prior receipts, including
previous IndexedDB PASS and standards-support totals, do not validate the edited
source. Missing mappings/declarations mean evidence TODO, not product non-support.
Existing explicit compatibility declines remain separate product decisions.
The representative weighted task denominator and OS-owner migration remain
unresolved; neither is closed by these localized repairs.

`mioa_core.net.probe_egress` requires a Host `HttpRequests` capability; the
trusted-local requests adapter lives in `mioa-os`. Explicit Host policy denial
does not trigger fallback to another provider. This does not establish a managed
network authority, OS-boundary pass or task-success improvement.
