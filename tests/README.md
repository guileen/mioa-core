# Core tests

**Start with [`POLICY.md`](POLICY.md)** for compatibility/task priorities and
release-claim boundaries; [`AUDIT.md`](AUDIT.md) records the reviewed test sources,
mocks versus actual-native evidence and gaps. Documentation records intent;
`ci.py`, its manifest and the root Makefile control selection and enforcement.

`make test` is the single quality entrypoint. Default LEVEL=1 runs the mandatory
OS-reference gate, then source-runnable unit regressions and coverage/gap reporting.
A boundary failure stops execution by default, including MODULE-filtered runs.
`KEEP_GOING=1` continues selected repair diagnostics without waiving any failure
or nonzero exit. Source failure remains the first repair obligation; preflight
errors retain structured ERROR reports. Missing evidence never becomes PASS.
CLI stdout defaults to TOON for agents, including plans and execution summaries;
`FORMAT=json` is the validator/CI path. Primary evidence is `report.toon` /
`results.toon`, with `coverage.txt`, `standards.toon` and `standards.md` for gap
discovery. CI artifacts default to `.local/test-ci/`. `report.json` / `results.json`
are the validators' machine records; do not read them by hand. TOON and JSON
views are both digest-bound, and the validator rejects a contradictory TOON view.

A small, source-runnable test system for Core-owned webpage semantics and native
rendering. Desktop is not a test dependency. Nothing imports ATB, a Desktop
checkout, a product CLI, NumPy or SciPy. The default entries do not import
Playwright; the optional reference-browser adapter below does. Python 3.12 and the
existing Core/headless dependencies are sufficient; the production LPFR ring uses its
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

`test_native_clock` uses an injected deterministic session clock to exercise
navigation deadlines, network-idle resets, frame locator and element-handle
timeouts, and missing-identity refusal. It is not a native browser run.

## One controller, three levels

```sh
make test
make test MODULE='network storage'
make test LEVEL=2 BINARY=/absolute/path/to/lightpanda
make test LEVEL=3 BINARY=/absolute/path/to/lightpanda RECEIPT=/absolute/path/to/receipt.json
make test HELP=1
```

- **LEVEL=1 (default):** source boundary, selected unit suites, standards/coverage
  gaps. Without MODULE, all registered modules and discovered unit suites are
  selected. No runtime artifact or browser launch is required.
- **LEVEL=2:** repeats the source/unit prerequisites and runs real runtime cases.
  Supply BINARY; RECEIPT optionally binds the observed binary to current sources.
- **LEVEL=3:** full candidate-bound delivery gate; BINARY and RECEIPT required,
  MODULE narrowing forbidden. Reviewed ACCEPTANCE exceptions remain scoped and
  visible; source/unit/infrastructure failures cannot be waived.

At LEVEL=2/3, `NATIVE_INPUTS=/absolute/machine-inputs.json` explicitly invokes the
pinned offline engine builder before the controller, with
`NATIVE_OUTPUT=runtime/.local/test-build` by default. BINARY and RECEIPT are then
bound automatically and cannot also be supplied. Build failure stops execution;
the source boundary and all selected checks still apply. HELP/PLAN/CHECKLIST
never build. This uses the existing target, not a shell-command passthrough.

MODULE selects changed modules and affected neighbors at levels 1/2. PLAN=1
prints the exact plan without execution. CHECKLIST=1 reads standards only, with
SPEC/MODULE/PRIORITY/REPORT filters; neither view is an execution or approval.
Security scope includes the OS authorization coordinator regressions.
HELP=1 lists flags. There are no separate dev/diagnose/deploy/OS-check targets.
The existing Python scripts are internal workers, not alternative Agent entrypoints.

The controller stops acceptance on a mandatory failure, not implementation.
It records unexecuted checks as NOT_RUN and reports a concrete next action:
repair the boundary/regression, supply an artifact, advance a level or review gaps.
Boundary repair lists Core call sites, `mioa-os/api/` contracts and `mioa-os/`
implementations. Missing capability implementations must be written, not treated
as a prohibition on coding. Runtime isolation and source migration are distinct
obligations; moving code or adding an API does not pass either by itself.
A printed plan proves no behavior. Missing required evidence cannot silently
become PASS. WPT and representative target-user task sufficiency remain explicit
gaps; a scoped baseline/deploy verdict is not whole-product release readiness.

`leak-direct-os-api` rejects direct platform/OS references and private Graphics/OS
implementation imports in the Core package and native engine sources. It runs
first in every CI mode, has no grandfathered baseline or release waiver, and needs
no browser binary. `mioa-os/` is OS-domain implementation, not an ignore for code
that Core imports directly. Source findings are migration work, not evidence of
runtime confinement; see [POLICY.md](POLICY.md#risk-floors-and-module-priorities).
`test_os_files` is a source-native regression suite: it explicitly compiles only
`mioa-os/files.c` into a temporary local library using `CC` (default `cc`) and
exercises real temporary-file I/O. It requires a C11 compiler and 64-bit POSIX;
missing prerequisites fail rather than skip. It builds no browser/runtime and
proves neither Core integration nor sandbox enforcement. The CI inventory includes
OS sources. Use `KEEP_GOING=1` to exercise repairs while the source gate still fails.
The append-mutation regression checks the current host's positional-write behavior;
it does not establish Linux safety when run on macOS. File grant duplicates share
status flags, and the Host's no-append-mutation obligation is not enforced.
`test_file_exports` exercises real screenshot file effects through NativePage and
the OS path adapter, using fixture CDP image bytes. It covers overwrite/create,
missing authority, invalid images without truncation, I/O errors, root Host
binding for peer contexts and short writes. It does not launch a browser, install
wheels or establish managed confinement.

2026-11-09 source-mode export migration verification (not installed/runtime proof):
`make test MODULE="harness dom media lifecycle control" KEEP_GOING=1 FORMAT=json`
recorded 142 passing application-side assertions in `dev-hkpab3v0`; its controller
fixture failed and was corrected. The affected harness was then rerun with
`make test MODULE=harness KEEP_GOING=1 FORMAT=json`: all 111 assertions passed,
including 7 real-file screenshot exports and 8 native file-service scenarios
(`.local/test-ci/dev-4geo_s1b/report.json`). Source boundary remained FAIL in both
reports; no acceptance, sandbox or measured target-task improvement is established.

2026-11-09 P0 endpoint migration:
`make test MODULE="harness lifecycle control" KEEP_GOING=1 FORMAT=json` passed all
245 assertions across 15 unit suites, with no skips. `test_os_endpoints` used
actual Unix sockets to cover short/private unique addresses, nonrecursive cleanup,
regular-file/symlink preservation and stale-lease disposal after pathname reuse.
The 36 lifecycle regressions include real child processes and confirmed-exit,
EOF/timeout, wrong-child and uncertain-spawn paths. Evidence:
`.local/test-ci/dev-0y62paz3/report.json`. The global source boundary still failed.
This is resource/lifecycle evidence, not an installed-wheel/browser-task run or
managed sandbox qualification; target-task success remains unmeasured.

2026-11-09 P0 launch environment/credential consistency:
the real-child log regression failed before the fix
(`.local/test-ci/dev-pt87jace/unit-test_native_lifecycle/execution.log`).
`make test MODULE="harness lifecycle dom control" KEEP_GOING=1 FORMAT=json`
then passed 259 tests across 17 suites, with no skips
(`.local/test-ci/dev-xc3nrlwx/report.json`). `test_process_environment` launches
real Python children and an intentionally failing executable fixture through
`launch_native`; it checks snapshot independence, removal/overlay precedence,
missing-service refusal, DIRECT inheritance and startup diagnostics after parent
credentials rotate or disappear. The fixture is not a browser and supplies no
ready/receipt response. The original real-child log regression also passes.
Source boundary remains FAIL/nonzero; installed packages, browser startup success,
sandboxing and target-task success are not established by these runs.

2026-11-09 P0 supervisor blocking-call ownership:
the real Unix-channel regression first reproduced cancellation releasing Core's
lock while its thread remained live
(`.local/test-ci/dev-kbucprhp/unit-test_native_lifecycle/execution.log`).
After OS `BlockingCalls` migration,
`make test MODULE="harness lifecycle control" KEEP_GOING=1 FORMAT=json` passed
256 tests across 17 suites, without skips
(`.local/test-ci/dev-0rs98_14/report.json`). The lifecycle regression now passes.
Five `test_blocking_calls` cases exercise real threads/sockets, normal resource
transfer and I/O failure, repeated cancellation, unclaimed-resource disposal,
cleanup-error notes and orderly Runner shutdown. Core's three supervisor work
paths use the injected service; no implicit local fallback was added.
Source boundary remains FAIL/nonzero. This is cancellation/resource ownership
evidence, not child-exit, browser-task success, installed-package or sandbox proof.

2026-11-09 P0 supervisor descriptor adoption:
the real pipe-reader leak failed before migration
(`.local/test-ci/dev-y2gx847k/unit-test_native_lifecycle/execution.log`).
An additional oversized-rights oracle exposed Darwin ancillary truncation leaks
(`.local/test-ci/dev-tt7le1mm/unit-test_unix_messages/execution.log`).
OS `UnixMessages` now drains the kernel batch ceiling before enforcing the
protocol cap and returns an all-or-nothing batch of connected Unix stream sockets.
`make test MODULE="harness lifecycle control" KEEP_GOING=1 FORMAT=json` passed
268 tests across 18 suites without skips
(`.local/test-ci/dev-isg3jm97/report.json`). Six OS cases use actual SCM_RIGHTS,
socket peers and pipe writers to verify ordering, fragment-carried rights,
partial adoption, wrong types, oversized batches (2/16/200 rights), EOF and
length rejection. Lifecycle regressions verify replay/count/JSON rejection,
failed ACK cleanup and valid ACK-before-sender-close ordering. Core and Host
callers require the injected service; other Profile lease paths are unchanged.
Source boundary remains FAIL/nonzero; no browser-task, installed-package or
sandbox qualification is inferred from this resource evidence.

2026-11-09 P0 Profile lease receipt:
the real SCM_RIGHTS regression reproduced a lease attached to a body fragment
being lost by the old recv-based body reader
(`.local/test-ci/dev-tp_3n3bu/unit-test_profile_lease_transfer/execution.log`).
`UnixMessages.receive_files` now shares the bounded OS frame receiver and returns
owned regular-file copies. `receive_leases(..., messages=...)` replaces the raw-FD
helper; Core validates the reply and closes local copies after escrow without
explicitly unlocking. Seven real file/socket/flock cases cover fragmented receipt,
wire order, offsets/status flags, retained locks across donor/recipient close,
partial adoption, wrong types, 200-right rejection, EOF/length/JSON/identity
failure, empty batches and repeated close after descriptor reuse.

`make test MODULE="harness lifecycle storage control" KEEP_GOING=1 FORMAT=json`
observed 295 tests: 291 passed, four native-cookie-owner cases skipped for the
missing rebuilt owner. All 22 other unit suites passed; `test_profile_service`
remains INCOMPLETE, although its real process/storage/lease scenarios passed.
Stable-input evidence: `.local/test-ci/dev-jybm6m7x/report.json`.
The earlier `.local/test-ci/dev-n7sks01e/report.json` is ERROR because inputs
changed during execution; do not use it as candidate qualification.
The source boundary remains FAIL/nonzero. No browser-task, sandbox or
installed-package success is inferred. Enrollment receive was not migrated at that checkpoint.

2026-11-09 P0 Profile enrollment receive:
`.local/test-ci/dev-vgp87tb5/unit-test_profile_service/execution.log` reproduced
lost rights on later enrollment fragments and owner exit. Host now supplies an
explicit Profile command prefix; `mioa_os.profile_host` installs `UnixMessages`
before entering Core. Header/body fragment enrollment preserves existing storage;
invalid JSON, partial socket/pipe adoption and replayed control identity close
unclaimed resources, refuse ACK and drain domains while retaining the owner lease.
Socket donors stay open through ACK (or control-loss confirmation for rejection);
premature donor close reproduced Darwin EOF and was not a valid receipt oracle.
Temporary ownership tracing was removed.

`make test MODULE="harness lifecycle storage control" KEEP_GOING=1 FORMAT=json`
observed 301 tests: 297 passed, four native-cookie-owner cases skipped for the
missing rebuilt owner; zero failures/errors. All 22 other unit suites passed.
`test_profile_service` remains INCOMPLETE solely for those skips, including 13
passing real Profile subprocess scenarios. Stable-input evidence:
`.local/test-ci/dev-g4zdunfp/report.json`.
Source boundary remains FAIL/nonzero; no browser-task, sandbox, installed-package
or north-star task-success gain is inferred.

2026-11-09 P0 incomplete Profile control-frame starvation:
`.local/test-ci/dev-w4ozgdyu/unit-test_profile_service/execution.log` reproduced
existing-domain ping timing out while enrollment was held at a partial header.
The owner now uses the public incremental Unix message reader. Four real OS
socket/descriptor cases cover incomplete reads, frame reuse/no read-ahead,
pending-right cleanup on close/EOF/excess, retained returned sockets and rejected
positive timeouts without consumption. Two real Profile subprocess cases exercise
ping/storage before header/body completion and EOF cleanup with lease retention.
The initial passing execution `.local/test-ci/dev-a_talhs2/report.json` detected
concurrent input changes and is not stable-input qualification.
Stable-input rerun `make test MODULE="harness lifecycle storage control"
KEEP_GOING=1 FORMAT=json`: `.local/test-ci/dev-gqe9h6f7/report.json` observed 313
tests in 23 suites, 309 passed, four unchanged native-cookie-owner skips, zero
failures/errors. Profile's 15 real subprocess cases passed; its suite remains
INCOMPLETE for the four skips. The intermediate `dev-jr5t5c9t` run exposed a
concurrent ProcessIdentity-injection omission in the handshake test; the live Host
assembly was corrected before this stable rerun. Source boundary remains FAIL
and make exits nonzero; no sandbox/browser/north-star success gain is inferred.

2026-11-09 P0 supervisor identity-failure ownership:
`.local/test-ci/dev-9r6bhmgd/unit-test_process_environment/execution.log`
reproduced retained live socket peers after missing identity, PermissionError and
CancelledError: all three branches failed the immediate peer-EOF oracle.
Identity lookup now runs inside the existing channel-ownership cleanup guard,
without fallback or exception translation. Two regressions retain real socket
objects so garbage collection cannot conceal missing explicit close; provider
exceptions retain their identity and cancellation remains cancellation.
`make test MODULE="harness lifecycle control" KEEP_GOING=1 FORMAT=json` observed
291 tests across 19 unit suites, all passing, no skips/failures/errors.
Stable-input evidence: `.local/test-ci/dev-isbgs6nd/report.json`.
Storage/Profile/native-owner cases were not selected in this run. Source boundary
remains FAIL/nonzero; no browser-task, sandbox or north-star gain is inferred.

2026-11-09 P0 lifecycle exit identity:
`.local/test-ci/dev-pc3bl1lm/unit-test_native_lifecycle/execution.log`
reproduced boolean/float launch identities marking live children exited and
removing their endpoint. Lifecycle replies now require an exact integer identity;
invalid replies close the control channel without recording exit or releasing
the domain. Real subprocess regressions check child liveness, socket presence
and domain reservation for both coercions; normal exact exit cleanup remains
covered. `make test MODULE="harness lifecycle control" KEEP_GOING=1 FORMAT=json`
first passed 294 tests in `.local/test-ci/dev-vz9i21fg/report.json`, but concurrent
input changes made that run ERROR, not stable-input qualification. Stable rerun
`.local/test-ci/dev-ntrk09ma/report.json` observed 294 passing tests across 19
unit suites, no skips/failures/errors. All 51 lifecycle cases passed, including
the live-child coercion regression and normal exact-exit cleanup. Storage/Profile/
native-owner cases were not selected. Source boundary remains FAIL/nonzero;
no browser-task, sandbox or north-star gain is inferred.

2026-11-09 P0 application-ready identity and ownership:
`.local/test-ci/dev-k7ia2z97/unit-test_profile_service/execution.log`
reproduced four accepted boolean/float launch/PID/UID receipts and a live unclaimed
socket after missing identity authority. Application-ready validation now requires
exact integer identities; its socket stays in the existing cleanup list until
successful transfer. Real Profile subprocess/socket regressions cover all four
coercions, missing authority, PermissionError, CancelledError and usable exact
identity delivery. Received socket objects are retained so garbage collection
cannot conceal missing explicit close; existing Profile domains remain usable.
`make test MODULE="harness lifecycle storage control" KEEP_GOING=1 FORMAT=json`
in `.local/test-ci/dev-lfues3vm/report.json` observed 325 tests across 23 unit suites:
321 passed, four unchanged rebuilt-native-cookie-owner skips, zero failures/errors.
All 54 lifecycle cases passed. The intermediate `dev-_4itfxn0` run passed the new
Profile regressions but exposed concurrent navigation regression/implementation
changes; the final rerun covers their updated implementation. Profile remains
INCOMPLETE for the four skips; source boundary remains FAIL/nonzero.
No browser-task, native-owner, sandbox or north-star success gain is inferred.

2026-11-09 P0 owner-started confirmation identity:
`.local/test-ci/dev-609z6c0z/unit-test_profile_service/execution.log` reproduced
boolean cookie and float IndexedDB launch identities passing readiness confirmation
and reaching application admission. `owner_started.launch_id` now requires an exact
integer match before ready acknowledgement. Real Profile subprocess/socket tests
cover both rejections, exact confirmation of both owners, failed-channel closure
and continued existing-domain work. Failed confirmation does not undo Profile-owned
registrations or infer child exit. Core enrollment callers explicitly pass the
public message service; navigation fixtures supply the injected monotonic clock.
`make test MODULE="harness lifecycle storage control" KEEP_GOING=1 FORMAT=json`
in `.local/test-ci/dev-3yelzmv_/report.json` observed 335 tests across 23 unit suites:
331 passed, four unchanged rebuilt-native-cookie-owner skips, zero failures/errors.
All three new Profile cases and 56 lifecycle cases passed. Earlier passing Profile
runs had input drift or concurrent integration failures and are not qualification;
the stable rerun includes the concurrent URL-rematch fix without claiming ownership.
Profile remains INCOMPLETE for the four skips; source boundary remains FAIL/nonzero.
No real native-owner, browser-task, sandbox or north-star gain is inferred.

2026-11-09 P0 navigation completion after final-URL commit:
`.local/test-ci/dev-9g92p1mg/unit-test_native_lifecycle/execution.log` reproduced
premature goto/reload/go_back completion and missing timeout when a new main-frame
commit overtook the final URL query. The shared completion path now repeats the
load wait and URL read under the original deadline; stable navigation adds no
remote calls and commit-only navigation still needs no JavaScript context.
Real socket/WebSocket/CDP regressions gate the second document's readiness,
check the final response/URL, and require timeout if that document remains loading.
They exercise the real Core client against a scripted peer, not a native browser.
The old subcase that accepted return after perpetual commits was removed; lost-
target rejection remains covered. `make test MODULE="harness lifecycle control network"
KEEP_GOING=1 FORMAT=json` in `.local/test-ci/dev-9oh3msxc/report.json` observed
315 passing tests across 20 unit suites, no skips/failures/errors; all 60 lifecycle
cases passed. `dev-tjb_a6lo` passed its cases but had concurrent input drift and is
not stable qualification. Storage/native-owner cases were not selected.
Source boundary remains FAIL/nonzero; no browser-task or north-star gain is inferred.

2026-11-09 navigation observation read boundary:
`navigation-observation-before/unit-test_native_lifecycle/execution.log` records
three real socket/CDP failures: old-document title success was published, and
title/observer-allocation failures overtaken by context invalidation remained
fatal protocol errors. Page/child-frame handle allocation and title reads now
reject that overtaken observation as `stale_reference`, without replaying it.
Stable protocol errors and ordinary user evaluations remain unchanged. The new
context supplies a title before any load-completion event; delayed resources
must not prevent post-commit observation. `navigation-observation-verified/report.json`
records 402 passing assertions in 23 unit suites and 10 native cases, no skips
or drift errors, with source-bound engine `zero-opacity-build/receipt.json`.
OS-source boundary remains FAIL/nonzero; this is not a navigation latency benchmark.

Progressive HTML qualification uses `web/progressive-html-stream` through the
existing network/lifecycle/control plan. The socket fixture withholds all body
bytes until `goto(commit)` has returned final headers, then withholds EOF until
the prefix's DOM, scripts and native pixels have been observed with
`readyState=loading`. It checks split UTF-8/UTF-16 input, tags/entities, legacy
meta decoding, BOM precedence, headerless MIME, document.write, exactly-once
load events, stop and replacement; split-declaration XML remains buffered.
Response-stage patterns and per-request `interceptResponse` overrides must hold
the entire original body without exposing DOM/scripts until explicitly released;
the override also checks buffered Windows-1252 text and exactly-once execution.
Window `nav-loading` separately checks actual prefix pixels at 800 ms, completed
pixels after the delayed tail, and replacement stability beyond the old tail's
deadline. These are ordering/checkpoint tests, not first-paint latency samples.

2026-11-09 source qualification: `.local/test-ci/progressive-html-verified/report.json`
passed 412 assertions and 21 native cases, including the gated stream and both
response-interception variants, with no skips or input drift. Engine receipt:
`runtime/.local/progressive-html-build/receipt.json`, SHA-256
`d2acebc132a3428e65ec21df2305f4fa1d2058d4becc34cd92a651ffc874c585`.
The earlier candidate-2 timeout exposed header delivery waiting for first body
bytes; the final socket gate passes without releasing those bytes prematurely.
Final native-window `/tmp/mioa_it_24663` passed 17 nav-loading steps, including
prefix pixels, completed tail and replacement beyond the abandoned tail deadline;
screenshots were inspected. Desktop unit/protocol build passed 35/35 steps.
OS-source remains FAIL/nonzero; full desktop smoke retains style/new-tab failures.
This is source-backend qualification, not a daily-App update or scanout benchmark.

2026-11-09 independent auxiliary-page qualification:
`web/independent-noopener-pages` exercises a guarded `_blank` link plus document
`window.open` with noopener/noreferrer against independently gated HTTP headers
and body. Each child is adopted and executable before headers, survives launcher
closure, paints and accepts native input, then navigates/reloads and script-closes
without orphaning its CDP target. Named opener-bearing references remain stable;
their events cannot replace the launcher identity. Closing followed immediately
by navigation must still retire the child. Before-fix evidence:
`.local/test-ci/noopener-close-before/report.json` and
`.local/test-ci/noopener-launcher-before/report.json`.
Final `.local/test-ci/noopener-pages-verified/report.json` passed 414 assertions
and 22 native cases with no skips/drift and verified source binding to
`runtime/.local/noopener-pages-final-build/receipt.json`, engine SHA-256
`badab4b506ec11b065b97dad9291d15698ea7ed9520a9a7a3ab3f9663dbc8354`.
Only OS-source remains FAIL/nonzero in that Core run. This does not qualify
cross-origin WindowProxy, sessionStorage cloning, popup blocking or managed domains.
Final source-window `/tmp/mioa_it_67862` passed all 20 slow-new-tab steps and nine
captured checkpoints: the background tab exists at the 800-ms checkpoint before
any response bytes, explicit Ctrl+Tab selects its loading document, the original
accepts Apply while the response remains gated, and the same child survives
original-tab closure and accepts input. Screenshots were inspected. Fast
`open-new-tab` also passes; full Desktop smoke now fails only the retained
Chrome computed-style differential, not tab count. Unit/protocol build passed
35/35 steps. This is an ordering checkpoint, not measured first-feedback latency,
packaging/daily-App qualification or a physical-screen benchmark.



2026-11-09 standalone child-process capability migration:
`ChildProcesses` now owns direct exec; Core retains argv/environment policy,
borrowed lease selection and graceful-then-forced shutdown. Headless installs
`LocalChildProcesses` only for standalone launch; supervised adoption is unchanged.
Real-child regressions verify input snapshots, explicit environment and FD
inheritance, unchanged parent leases, separate output, held parent-EOF stdin,
cancelled waits, raw exit status and exec errors. Repeated start cancellation
with both output pipes backpressured waits for kill, drainage and actual reaping;
the borrowed channel remains usable. The Core startup-failure smoke executes
a real child through the new service and retains credential redaction.
`make test MODULE="harness lifecycle control network" KEEP_GOING=1 FORMAT=json`
in `.local/test-ci/dev-b6hb3j95/report.json` recorded 323 passing tests across
20 unit suites, no skips/failures/errors or input-drift error. All 17
`test_process_environment` cases passed. Source boundary remains FAIL/nonzero.
Storage/native-owner and real-browser task qualification were not selected;
no sandbox, process-tree, durable-flush or north-star improvement is established.

2026-11-09 P0 cancelled CDP close:
the real socket regression in
`.local/test-ci/dev-lch9fcnx/unit-test_native_lifecycle/execution.log` reproduced
missing peer EOF after close cancellation while the send lock was held.
The close-frame timeout path already passed; timeout is an OSError and was
already suppressed. WebSocket transport closure and CDP reader/event cleanup
now run in finally blocks while preserving cancellation. The two real
socket/WebSocket/CDP cases check peer EOF, rejection of the pending and next
request, and event-listener cleanup for both cancellation and timeout.
`make test MODULE="harness lifecycle control network dom" KEEP_GOING=1 FORMAT=json`
in `.local/test-ci/dev-cdamazya/report.json` observed 336 passing tests across
21 unit suites, no skips/failures/errors or input-drift error; all 66 lifecycle
cases passed. Initial `dev-vnn3i7z7` had concurrent input drift and is not stable
qualification. Concurrent locator changes were preserved, not part of this fix.
Source boundary remains FAIL/nonzero. No storage/native-owner, actual-browser,
sandbox or north-star task-success qualification is inferred.

2026-11-09 P0 CDP response identity:
`.local/test-ci/dev-gs0aqjua/unit-test_native_lifecycle/execution.log`
reproduced boolean/float response IDs completing integer request 1. Core now
checks strict integer identity before lookup, fails pending requests on malformed
IDs and rejects further commands without replay. The three real socket/CDP
cases cover both coercions and valid delivery after a late unmatched response.
`make test LEVEL=2 MODULE="harness lifecycle control network dom input"
BINARY=runtime/.local/northstar-build/engine/zig-out/bin/lightpanda
KEEP_GOING=1 FORMAT=json` in
`.local/test-ci/diagnose-_yzgat5a/report.json` observed 357 passing tests across
22 unit suites (69 lifecycle), no skips/failures/errors, and 13 passing native
web cases including search submit/back and form history. The controller returned
ERROR for concurrent source/test input drift, as did `diagnose-fri72o29`; neither
is stable qualification. Source boundary remains FAIL/nonzero. Native artifact
SHA-256 `1ce78e90fb7c4eab7431ca581fed4da9f8f7a0ff58ed7f54b3b5305a09d35eec`
was exercised without a current-source build receipt. No native rebuild,
storage/native-owner, sandbox or weighted target-task success gain is inferred.

2026-11-09 P0 missing legacy Profile lock:
`.local/test-ci/dev-ywsoetcc/unit-test_os_leases/execution.log` reproduced
an older owner acquiring `.lock` while the OS held an imported Profile's lease.
`LocalProfileLeases` now creates missing engine/spawn compatibility locks in
existing Profile directories before granting the lease. The real flock
regression verifies exclusion during ownership and acquisition after release;
it is registered in the existing harness, without a new make target.
Stable-input evidence: `.local/test-ci/diagnose-als_7y85/report.json`, from
`make test LEVEL=2 MODULE="harness lifecycle storage control input"
BINARY=runtime/.local/northstar-build/engine/zig-out/bin/lightpanda
KEEP_GOING=1 FORMAT=json`: 378 passing tests across 25 unit suites, no skips,
failures or errors; eight native web cases passed, including search submission,
return and form history. `diagnose-jlsz7c_m` had input drift and is not stable
qualification. Concurrent Profile API/caller migration and import restoration
are not this fix. Global source boundary remains FAIL/nonzero. The existing
native artifact was not rebuilt or bound to current runtime sources; no sandbox
or weighted target-task success improvement is established.

2026-11-09 storage task qualification:
The storage plan previously omitted its existing native browser cases. LEVEL=2
now selects local/session storage CRUD, Cookie Store CRUD/permissions and
IndexedDB open/upgrade, plus `storage-draft-navigation`. The latter uses a fresh
Profile and one tab: write a structured Unicode draft and session marker,
navigate/reload, visit a second loopback origin, write the same keys there,
then verify both origins retain only their own values. Its task card records
goal, initial state, actions and limits; the report retains actual values.
Stable-input evidence: `.local/test-ci/diagnose-xgtfwdig/report.json`, from
`make test LEVEL=2 MODULE="harness storage"
BINARY=runtime/.local/northstar-build/engine/zig-out/bin/lightpanda
KEEP_GOING=1 FORMAT=json`: all four native cases and 201 tests across 21 unit
suites passed, without skips/failures/errors or input drift. The first draft
execution `diagnose-44l3g8ij` failed on a harness assertion-helper argument error,
not a product defect; the corrected workflow passed in `diagnose-qay7otik`.
No production Core/OS storage fix was needed. This qualifies single-tab
navigation/reload and origin partitioning, not restart durability, independent
tab/Profile isolation, IndexedDB CRUD or authenticated/live-site workflows.
The native artifact was not rebuilt or bound to current runtime sources.
Global source boundary remains FAIL/nonzero; no weighted north-star task-success
gain or sandbox qualification is established.

2026-11-09 independent-tab draft task:
`storage-independent-tabs` extends the existing native driver and storage plan.
In one fresh standalone Profile, independent same-origin tabs with no opener
share the structured localStorage draft but not sessionStorage markers. Editing
in B and reloading A/B must preserve the new shared content and distinct markers;
after B closes, A must still write/read its draft and retain its own marker.
The manifest records the goal, start, actions and limits; reports retain all four
observed states. This is native task evidence, not a mocked storage test.
Stable-input evidence: `.local/test-ci/diagnose-reogsegs/report.json`, from
`make test LEVEL=2 MODULE="harness storage lifecycle"
BINARY=runtime/.local/northstar-build/engine/zig-out/bin/lightpanda
KEEP_GOING=1 FORMAT=json`: 274 tests across 22 unit suites and six native cases
passed, without skips/failures/errors or input drift. The targeted first run
`diagnose-kgv4kv6y` also passed the new task; no production defect was reproduced
and no Core/OS implementation was changed.
This covers independent tabs in the standalone domain, not popup/opener session
cloning, managed application domains, independent Profiles, browser restart or
authenticated/live-site workflows. Artifact SHA-256
`1ce78e90fb7c4eab7431ca581fed4da9f8f7a0ff58ed7f54b3b5305a09d35eec`
was reused without a current-source build receipt. Global source boundary stays
FAIL/nonzero; no sandbox or weighted north-star success-rate gain is established.

2026-11-09 independent-Profile draft task:
`storage-independent-profiles` launches two real standalone native processes
with separate Profile paths/leases, identical environment seed and the same
loopback origin. B must initially see no A draft, session marker or script-visible
cookie. Both Profiles must retain their distinct same-key values across reload;
clearing B and closing its process must leave A unchanged and able to edit.
The task records six observed states, B's native receipt and `peer-native.log`;
B is closed before its lease is released, including on task failure/cancellation.
The targeted run `.local/test-ci/diagnose-szpwq131/report.json` passed all six
storage cases and five unit suites, with no input-drift error; the mandatory
source boundary remains FAIL/nonzero. The initial baseline `diagnose-0ll_yuec`
had concurrent input drift and is not stable qualification. No production
Core/OS defect was reproduced or implementation changed.
Scope is localStorage, sessionStorage and script-visible Cookie isolation in
standalone Profiles, not HttpOnly/HTTP credential transport, real login,
managed-domain isolation, browser restart durability or live-site compatibility.
The existing native artifact was not rebuilt or bound to current runtime
sources; no sandbox or weighted north-star success-rate gain is established.
Stable combination: `.local/test-ci/diagnose-hwtvh3ka/report.json`, from
`make test LEVEL=2 MODULE="harness storage lifecycle"
BINARY=runtime/.local/northstar-build/engine/zig-out/bin/lightpanda
KEEP_GOING=1 FORMAT=json`: 275 tests across 22 unit suites and seven native cases
passed, without skips/failures/errors or input drift. Only the mandatory source
boundary failed; the command remained nonzero. Concurrent lifecycle changes are
not part of this task addition.

2026-11-09 HTTP authenticated-session task:
`http-authenticated-session` is selected by both network and storage at LEVEL=2.
The existing loopback fixture server optionally owns random in-memory session
tokens. The native browser first receives no private content, enters fixture
credentials through pointer/keyboard input, submits an HTTP form, follows a 303
with HttpOnly Set-Cookie, displays the server-protected draft and retains access
after reload. Page script must not see the session Cookie. Logout revokes the
server session and expires the Cookie; another private navigation must be denied.
The report retains seven observations plus `authenticated.png`/`signed-out.png`.
A real socket harness regression checks wrong-password rejection and replays the
original Cookie after logout to prove server-side revocation, independently of
browser Cookie deletion. No production Core/OS defect was reproduced or changed.
Stable focused evidence: `.local/test-ci/diagnose-w1q5xrh7/report.json`, from
`make test LEVEL=2 MODULE=network
BINARY=runtime/.local/northstar-build/engine/zig-out/bin/lightpanda
KEEP_GOING=1 FORMAT=json`: the HTTP native task and four network unit tests passed,
without skips/failures/errors or input drift; only the mandatory source boundary
failed, retaining nonzero exit. Rendered private content was inspected in the
native frame. The earlier stable combination `diagnose-tqtm2ri1` passed 206 tests
across 22 unit suites plus all seven native cases, before the visible-draft
assertion was added. Final combined runs `diagnose-_cagrs04` and `diagnose-e9999wep`
observed the same passing counts, including the visible draft, but returned ERROR
for concurrent input drift; they are not stable combined qualification.
Scope is local HTTP, one standalone Profile and fixture credentials: no TLS,
Secure-cookie, cross-site identity provider, restart, managed-domain or live-site
authentication claim. The existing native artifact was not rebuilt or bound to
current runtime sources. No sandbox or weighted north-star success gain follows.

`fetch-basic` runs under `LEVEL=2 MODULE=network`: same-origin response body,
HTTP 404 resolution and unsupported-scheme rejection. It is a bounded local
fixture, not Fetch conformance or a replacement for the engine's Fetch suite.

2026-11-09 P0 CDP error-response ownership:
`.local/test-ci/dev-a8mdc_uf/report.json` reproduced the matching command remaining
unfinished after an `error: null` response terminated the real socket/CDP reader;
the other 79 lifecycle tests passed. `_CDP._read` now retains a request in pending
until response decoding and delivery complete, so failure cleanup still owns the
offending caller. No extra validation, retries, API or OS capability was added.
Two real socket regressions cover malformed-error completion of both pending
callers and rejection of later work, plus normal protocol-error isolation from
another successful pending command.
Stable focused evidence: `.local/test-ci/dev-zvcz0277/report.json` records all 80
lifecycle tests passing. Stable native combination:
`.local/test-ci/diagnose-eu3fza0z/report.json`, from
`make test LEVEL=2 MODULE="lifecycle network input control"
BINARY=runtime/.local/northstar-build/engine/zig-out/bin/lightpanda
KEEP_GOING=1 FORMAT=json`: 195 tests across six unit suites and nine native cases
passed without skips/failures/errors or input drift. Cases include HTTP login,
search submission/back and form history. The first combination `diagnose-xajxuwh6`
observed the same passes but returned ERROR for concurrent input drift and is not
stable qualification. Global OS source boundary remains FAIL/nonzero. The
existing native artifact was not rebuilt or bound to current runtime sources;
no storage/native-owner, sandbox or weighted north-star success gain is inferred.

2026-11-09 P0 child-frame pointer preparation:
`.local/test-ci/dev-7lv7oyny/report.json` observed eight real socket/CDP subcase
failures: child navigation/detachment during geometry evaluation or temporary-owner
release still dispatched old coordinates for move/click. Live scaled coordinates
passed. That run had concurrent input drift and is not stable qualification.
`_FrameMouse._point` now checks frame validity after its final awaited cleanup,
before returning coordinates; no extra RPC, retry or OS capability was added.
Three regressions cover both invalidation stages and live scaling, with explicit
temporary-object release. This is not atomic multi-event or guarded-input authority.
Final combination `.local/test-ci/diagnose-4u_kycfb/report.json`, from
`make test LEVEL=2 MODULE="lifecycle network input control"
BINARY=runtime/.local/northstar-build/engine/zig-out/bin/lightpanda
KEEP_GOING=1 FORMAT=json`, observed 203 tests in six suites and nine native cases
passing without skips/failures/errors. Focused follow-up
`.local/test-ci/dev-36njbosl/report.json` observed all 86 lifecycle tests passing.
Both returned ERROR for concurrent source/test drift; neither is stable acceptance.
The earlier combination `diagnose-4jx7mie5` additionally had two shutdown fixture
errors during the concurrent Profile persistence migration, corrected outside this
slice. Source boundary remains FAIL/nonzero. The native artifact was reused, not
rebuilt or source-qualified. No latency or weighted north-star gain is established.

2026-11-09 perceived hover latency (native Desktop/Graphics, not CDP timing):
the user's physical-pointer lag report redirects priority to hover, typing and
scrolling. Desktop keeps the newest unsent legacy page-motion position while the
worker is busy; frame/owner/document/viewport changes and discrete-input barriers
discard it. Governed input is unchanged. Interactive drawing retains dirty state
on unavailable nonblocking acquisition, retries while visible, and sleeps when
settled/hidden. No new make target, click replay, per-motion allocation or queue.

Existing `make -C ../mioa-browser test` completed its unit stage but the native
launch in `/tmp/mioa_it_55364` returned StartupRejected; no hover acceptance is
claimed for that run. Follow-up existing `make -C ../mioa-browser test SUITE=smoke`
produced `/tmp/mioa_it_59342`: alpha, dedicated no-click hover burst, same-tab
navigation and new-tab click runs all exited zero without diagnostic/capture
rejects. The burst's unique `mioa-hover` receipt records `hover_enter` and
`pointer_move` on nav-next, with no `link_clicked`; screenshot
`hover_burst_hover.png` shows the pointer on the link. This oracle cannot be
satisfied by a click-generated mouse move. The earlier pre-coalescing burst
in `/tmp/mioa_it_22160` failed to deliver its final link position, but used the
older click-containing scenario; it is not an identical failing-before oracle.

`/tmp/mioa_it_59342/probe.json.result.json` records 48 SDL motions/48 submissions:
event-to-GPU-submit total 388387664ns, max 9454625ns (mean 8.09ms);
swapchain acquisition total 373124327ns (mean 7.77ms). Baseline
`/tmp/mioa_it_51012` used the same 48-motion probe and recorded total 401393251ns
(mean 8.36ms). These runs do not establish a meaningful average speedup;
nonblocking acquisition still incurs driver-internal waits. Idle check:
zero draws/uploads, 460000ns CPU over 206960917ns elapsed. The timing excludes
screen scan-out, and page mouseenter-to-rAF/worker RTT are separate metrics.
The full smoke remains FAIL/nonzero for the pre-existing Chrome computed-style
drift and target=_blank page-count finding. Newly built Desktop used existing
packaged backend resources; no full runtime/bundle or weighted north-star
qualification. Physical-pointer feel still requires user acceptance. Next
performance investigation: swapchain/present pacing and the page-action/capture
serial path, not test-count growth or unmeasured broad optimization.

2026-11-09 presentation queue experiment:
existing `make -C ../mioa-browser test` exercised one allowed GPU frame in flight
instead of SDL's default two. `/tmp/mioa_it_74445/probe.json.result.json` recorded
48 motions/submissions, total 393273961ns (mean 8.19ms), max 9663167ns, and 47
deferred attempts. Idle draws/uploads remained zero (356000ns CPU over
208567167ns elapsed). This did not improve event-to-submit latency versus
`mioa_it_59342`; the single-frame change was removed. VSYNC remains enabled;
IMMEDIATE permits tearing, and upstream Metal does not support MAILBOX.
SDL source reference:
https://github.com/libsdl-org/SDL/blob/main/src/gpu/metal/SDL_gpu_metal.m
(`METAL_SupportsPresentMode`, `METAL_INTERNAL_AcquireSwapchainTexture`).
The upstream source describes a possible driver wait in `nextDrawable` even on
the nonblocking path; it is not a version-matched local-driver qualification.
Both existing smoke findings remained; no performance gain is claimed.

2026-11-09 native invalidation leading-edge delivery:
`mioa-headless/mioa_headless/native_service.py` now emits the first pending batch
before its 8ms burst cooldown, rather than delaying first feedback. The single
task survives through that finite cooldown; page identities still deduplicate,
changes received during emit/cooldown remain pending, and settled bursts exit.
No new capture loop, input replay, OS capability or protocol fields.
The real Unix socket regression holds the cooldown unresolved: the first
length-prefixed invalidation must already arrive; 200 subsequent p/q changes
must become exactly one packet per page, then EOF without surplus notifications.
This is causal ordering, not a tight wall-clock threshold. Before the change,
`.local/test-ci/dev-ef458ghc/report.json` failed that read with TimeoutError;
the other 51 native-service tests and the service/frame-ring suites passed.
The global source boundary remains independent of this performance slice.
After the change, existing
`make test LEVEL=2 MODULE="control lifecycle input"
BINARY=runtime/.local/northstar-build/engine/zig-out/bin/lightpanda
KEEP_GOING=1 FORMAT=json` produced
`.local/test-ci/diagnose-9dghb0t9/report.json`: 198 unit/resource tests and eight
native cases passed without skips/failures/errors or input-drift error. The
previous first-packet timeout now passes, as do burst coalescing and invalidation
during emit. Overall status remains FAIL/nonzero for the OS source boundary.

Following removal of the one-frame experiment, existing
`make -C ../mioa-browser test SUITE=smoke` produced `/tmp/mioa_it_97382`:
all four native runs exited zero without diagnostic/capture rejects, including
the no-click hover burst. The 48-motion probe recorded total 389476544ns
(mean 8.11ms), max 9119583ns, zero deferred attempts, and zero idle draws/uploads;
idle CPU was 453000ns over 200116958ns. Its screenshot was inspected. Full smoke
still fails the same Chrome-style and new-tab-count checks. These Desktop runs
use existing packaged Headless resources and therefore do not measure the new
source notifier or prove reduced motion-to-photon latency. The source-level
first-notification delay is removed; no screen-latency or packaged-app claim.

2026-11-09 action acknowledgement disk latency:
`Session.action` no longer rewrites unchanged Profile state before returning.
It still reads the latest disk state; changed URLs use the original atomic
file+directory fsync path, preserving other fields and error propagation.
No cache, debounce, input-guard relaxation, replay or new OS API.
The real-file unchanged-hover regression failed before the fix with injected
fsync OSError in `.local/test-ci/dev-3z4p1pid/report.json`. The two changed-URL
cases already passed: input-driven URL changes preserve the latest metadata,
and a durable-write failure remains visible with the input basis consumed.
After the fix, existing `make test LEVEL=2 MODULE="control lifecycle input"
BINARY=runtime/.local/northstar-build/engine/zig-out/bin/lightpanda
KEEP_GOING=1 FORMAT=json` produced
`.local/test-ci/diagnose-o24ff3mx/report.json`: 203 tests and eight native cases
passed with no skips/failures/errors or input drift. Overall FAIL is the existing
OS source boundary, not waived.

A temporary source-mode smoke in the existing web runner attached a real Engine
and Headless Session to the native frame-contract process. Existing
`make test LEVEL=2 MODULE="control lifecycle"
BINARY=runtime/.local/northstar-build/engine/zig-out/bin/lightpanda
KEEP_GOING=1 FORMAT=json` produced
`.local/test-ci/diagnose-ekkbdb9k/report.json`: 24 observed native mousemove
events through fresh guarded frame bases, zero fsyncs during actions, preserved
Profile state, and a verified blue CSS :hover pixel. The inspected screenshot is
`web-native-frame-contract/artifacts/native-frame-contract/hover-action.png`
under that report directory. Action entry-to-return mean was 2259510ns,
max 3051166ns, including source Session validation/native dispatch/cleanup;
capture, GPU submission and scanout are excluded. The hook was removed after
the smoke; its report applies to that recorded temporary input. These are
post-change samples, not an A/B speedup or motion-to-photon measurement. Existing
native binary reused; daily packaged App unchanged.
After removing the temporary hook, the same control/lifecycle/input make command
produced `.local/test-ci/diagnose-6s5n1p55/report.json`: 203 tests and eight native
cases passed, no skips/errors/failures or input drift; only OS source boundary FAIL.

2026-11-09 viewport validation/dimensions RPC:
`RefStore.viewport` obtains root dimensions from its post-pixel validation,
not a second remote query. Document/view/mutation checks, recursive child
validation, final binding checks and action authority consumption are unchanged.
The boundary regression covers root versus child dimensions and rejects a
child viewport change during both raw and PNG captures; existing lease-race
tests now suspend the combined validation/dimensions call.

The same temporary source-mode 24-hover probe and existing control/lifecycle
Level 2 make command recorded baseline
`.local/test-ci/diagnose-u8day0nm/report.json`: mean capture 1804988ns,
mean action 1970016ns, 24 standalone dimensions calls. After the change,
`.local/test-ci/diagnose-2_jw0tzc/report.json` recorded mean capture 1671911ns,
mean action 1978057ns and zero standalone dimensions calls. Both native cases
passed with 24 received mousemoves and a blue CSS :hover pixel; the screenshots
`web-native-frame-contract/artifacts/native-frame-contract/hover-profile.png`
were inspected. Each report had stable input during execution, but intervening
OS owner-service changes prevent attributing all timing differences solely to
this edit. The latter also contains two preparation-test errors from missing
new `owners` arguments, subsequently migrated; it is not full-suite acceptance.
The drifted `diagnose-dwztcc7b` and `diagnose-5eee1rvq` runs are diagnostic only.
The temporary probe was removed. These small source capture timings exclude
Desktop queuing, GPU submission and scanout; packaged App unchanged.
Final files without the probe were exercised with existing
`make test LEVEL=2 MODULE="control lifecycle input dom"
BINARY=runtime/.local/northstar-build/engine/zig-out/bin/lightpanda
KEEP_GOING=1 FORMAT=json`. Both `diagnose-fcezll8o` and `diagnose-sssaf_vx`
observed 216 unit/resource tests and 13 native cases passing with no skips,
but returned input-drift ERROR; neither is stable combined acceptance.
The same final module set at Level 1 produced
`.local/test-ci/dev-ay1u73gv/report.json`: all 216 tests in six suites passed,
no skips/errors/failures or input drift. Overall FAIL/nonzero remains the
OS source boundary. This separates stable unit evidence from the unqualified
combined native run rather than suppressing drift or weakening checks.


The default console is a bounded capability checklist: one guidance sentence,
candidate/review counts, at most three API names and two source locations per
group. This replaces the first-100-reference dump. No payloads or source bodies
are printed. API and location lists prioritize calls over broad guard matches.

```sh
make test
make test CHECKLIST=1 PRIORITY=P0
```

The controller retains the complete boundary result under its output directory.
Classification and checking live in the single `tests/leak_direct_os_api.py`.

The internal checker's detailed view expands console selection; JSON retains all
findings, source hashes, capability groups and exact occurrence locations.
`inventory.occurrences` records `reference`, `path`, `line`, `kind`, `capability`
and `confidence`. `candidate` means a syntax-recognized host call, not proven
scope/type resolution or a migration requirement. `review` includes imports,
types, bare C names and unresolved matches. Neither classification waives the
guard: existing FAIL/ERROR exit codes and CI release blocking are unchanged.
The inventory does not infer instance-method receiver types, transitive effects
or complete test/production scope. Inspect review entries rather than manually
re-scanning the repository; retain platform fast paths inside providers.


The `input` plan checks form-data collection and a local search journey: native
text entry, button or Enter submission, GET navigation, encoded query, result
content and returning to the search form. History scenarios also cover back
and forward restoration of matching light-DOM controls, excluding password
and file fields. Restoration is best effort, not a back-forward cache: child
frames and controls created after parsing are not restored. It does not
establish representative form, IME or live-site compatibility.

## Legacy regression entry, explicit layers

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
`web/results.json`. Its default layers cannot succeed without a native binary.
`--layer unit` intentionally makes the smaller claim only. This legacy `all`
does not execute all Zig/Rust modules or standalone quality campaigns and is
not the three-mode release gate.

Both entries add `mioa-core/`, `mioa-headless/`, `mioa-os/api/system/` and `mioa-os/`
to the source import path for public contracts and Host-owned implementations.
This is source mode, not installed-package
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
selects `assertion`, `reftest`, `native` or `wpt`. An empty selection is an error.
`--output` is a new or empty directory; the default is a newly created system
temporary directory. Evidence may be stored outside source or under ignored
`core/.local`, never alongside tracked tests. Existing artifacts are not
silently overwritten. No test launches user windows or uses an existing profile.

## Isolated Entities/PNG quality closure

```sh
python tests/quality_native.py --output /tmp/mioa-native-quality
python -m unittest discover -s tests -p test_quality_native.py
```

The native entry resolves the installed `zig`, `cc`, and `pkg-config libpng`
(optional `--zig`/`--cc` select existing executables). It compiles standalone
probes and the maintained `src/native_tests.zig` tests with explicit Zig
providers, without building the browser, Rust backend, or V8. No toolchain is
downloaded. Output must be new/empty and outside Core or under `.local`; source
mutations are made only in private output copies, never production files.

`report.json` schema 1 records source, executable, compiler and libpng library
hashes; exact commands and stdout/stderr logs; pinned corpus revisions/licenses;
baseline checks; and each mutant's patched-source/executable identity and
intended semantic test. The last stdout line is `{"status": "...", "report":
"<absolute path>"}`. Missing tools produce `NOT_RUN` and nonzero exit, not a
success or unsupported-capability exemption.

The hashed `tests/quality/native_contracts.json` pins thirteen required executed
test identities, including both import-root entries. The gate matches those
fixed harness IDs against actual successful test records; removing a native
test cannot silently lower the required inventory. The same configuration pins
explicit production/test-helper boundaries for source mutations, so helpers
before the first `test` declaration are protected too.

The fixed full WHATWG table, all prefix-only names and separate dead ends are
checked through actual native lookup. Native and community table counts must
both equal the pinned 2231 entries, rejecting native-only additions as well as
missing or incorrect values. Official libpng PNGSuite `basn6a08.png`
is decoded by installed libpng without gamma conversion: its checked-in README
and `pngtest.c` specify RGBA8 and 160 zero **RGB** samples (alpha is excluded).
Generated straight-alpha and multi-window fixtures, plus that reference image,
are encoded by the real Zig encoder and independently decoded to exact RGBA.
Vendored primary sources, full licenses and hashes are in
`tests/quality/assets/manifest.json`. This is not whole-PNG-suite, HTML
tokenizer, or browser conformance.

Only after every baseline passes, thirteen source mutations exercise entity
scalar/status/case/legacy/output contracts and PNG channel/alpha/dimension/
callback/allocation ownership contracts. A mutation is `KILLED` only after
successful compilation and failure of its named maintained test with the
expected semantic assertion or allocation leak evidence. Build errors, timeouts,
crashes and unrelated assertions are `INVALID` and fail the gate.

Reusable executables appear under output `bin/`: `entity_probe names.json`
returns lookup `{status, codepoints}` records; `entity_probe --table-count`
returns `{"entries": N}` without changing the normal names-file ABI;
`png_probe WIDTH HEIGHT input.rgba output.png` encodes RGBA8;
`libpng_decoder input.png output.rgba`
returns `{"width": W, "height": H, "format": "RGBA8"}` and exits nonzero on
decode failure. Their absolute paths are also in the report's `artifacts`.

## Bounded fuzz, reduction and confirmed regression

`quality_fuzz.py --decoder /absolute/libpng_decoder --output /new/private/output`
runs deterministic CRC-valid PNG chunk-alphabet mutations against the existing
Python export oracle and actual libpng. `--seed` and `--iterations` fix the
campaign; `--oracle-source` explicitly selects trusted local code, including an
immutable historical source for before/fixed-after reproduction. This is not an
AFL++/Atheris coverage-guided campaign or broad PNG conformance.

The decoder distinguishes library rejection (exit 3), adapter/resource/I/O
failure (1), usage error (2), and success (0). Crashes/timeouts/infrastructure
failures never become semantic findings. ddmin preserves the exact failure
signature and certifies only its declared one-step reduction relation/budget.

`promote --record ... --decoder ... --output ... --confirm-spec ...
--confirm-oracle-sha256 ...` requires explicit confirmation, matching independent
decoder identity and a passing fixed replay. It writes a new private directory,
never source expectations. `replay --record ... --decoder ...` rechecks a promoted
record. Only reviewed portable data is retained publicly under
`quality_regressions/`; original source/observations remain private.

The first confirmed regression is a real oracle defect: CRC-valid non-letter
chunk names were accepted. The fixed oracle rejects them; a valid unknown
private ancillary chunk remains accepted. See the pinned W3C PNG chunk-alphabet
contract and portable `quality_regressions/png-invalid-chunk-type.json`.

Product orchestration is outside Core: private `quality/run.py` composes these
CLIs with the browser differential and G0–G4 evidence. It does not turn a component
or explicitly historical browser result into current-runtime/release acceptance.


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
  captures; stale native input rejects without dispatch after paint, failed
  capture, focus, Canvas and scroll changes, without an intervening recapture;
  a current click reaches the same painted/hit-tested target; focused native
  document/element identity and bounded caret geometry; navigation replaces
  document observation and focused rendering identity. Fingerprint reuse must
  pass these same actual-engine rejection oracles, not just generation tests.
- **Scaled overflow capture:** an isolated 1.5× DPR Profile captures before/after
  container scrolling. Exact LPFR pixels verify the final inside device pixel,
  outside/bottom bounds, nested rounded intersections, shaped-text containment
  and generated-content clipping; artifacts retain both PNGs and frame metadata.
  The previous CSS-size raster/Lanczos path fails the sharp-edge regression.
  Source-bound verification: `scroll-scale-verified/report.json` records 308
  assertions in 22 unit suites and 16 native cases PASS, without skips or input
  drift errors. Only the mandatory OS-source gate remains FAIL. The candidate
  receipt is `runtime/.local/scroll-scale-build/receipt.json`; its final SHA-256
  is `96ccc1a9f7373bb38f329f7f6273aee83f05ebe71a998276e09cca53ea65a83d`.
  The broad harness also required staging its real snapshot `config.json`
  fixture for the current archive contract; production archive behavior was
  unchanged. This is not an installed-App or sandbox acceptance.
  Outset-shadow follow-up (2026-11-09): the same fractional-DPR fixture now
  includes crisp offsets, a transparent border-box hole, ancestor rounded
  clipping and translucent rounded blur. Adding one shadow previously forced
  the whole page through the CSS-size/Lanczos fallback and failed the existing
  sharp-edge assertion (`shadow-scale-before/report.json`). Device-scale shadow
  geometry fixes that without changing CSS blur layers or input authority.
  `shadow-scale-verified/report.json` binds the rebuilt engine at
  `runtime/.local/shadow-scale-build/receipt.json`: 308 unit assertions in
  22 suites and 16 native cases PASS, no skips or input drift; the OS-source
  gate remains FAIL.
  Inset shadows and other unsupported effects keep their existing fallback.
  Pure-translation follow-up (2026-11-09): the fixture also composes percentage
  transform-list and individual translations, scrolls their rounded clip, and
  verifies a translated child cannot move its ancestor clip. Fully outside
  fractional bottom-edge probes round up; partially covered pixels are not
  asserted white. The old engine fails the retained sharp-edge check
  (`translate-scale-before/report.json`). `translate-scale-verified/report.json`
  binds `runtime/.local/translate-scale-build/receipt.json`: 308 assertions in
  22 unit suites and 16 native cases PASS, no skips or input drift; OS-source
  gate still FAIL. Only element translations gain eligibility; pseudo
  transforms, matrices, rotation/scale/skew and other effects retain fallback.
  Zero-opacity follow-up: an initially hidden group must not degrade sharp edges
  elsewhere. Captures then reveal two overlapping opaque children at group
  opacity .5, 1 and 0. Both overlap and nonoverlap must be half-gray, black and
  white respectively, with opaque output alpha; PNGs and actual samples retain
  each transition. Fractional groups keep their isolated-layer fallback.
  2026-11-09: `zero-opacity-before/report.json` fails the retained sharp edge
  (`ff2121ff` instead of `ff0000ff`). `zero-opacity-verified/report.json` binds
  `runtime/.local/zero-opacity-build/receipt.json`: 308 assertions in 22 unit
  suites and 16 native cases PASS, no skips or input drift; only OS-source FAIL.
  Transition samples are `808080ff`, `000000ff`, `ffffffff` at all three sites;
  the three PNGs were inspected. No fractional-opacity performance claim.

The incremental native suite also exercises complete named-entity lookup through
actual Blob-loaded WebVTT/VTTCue conversion, and Canvas/OffscreenCanvas PNG
exports (straight alpha, rows, resize/empty behavior, compression and high-entropy
output). Run it against each explicitly compiled provider configuration.

`tests/run_png_roundtrip.py --binary <engine> --output <new-directory>` reuses
the fixture server and native lifecycle helpers, saves the actual exported PNGs,
then independently verifies their chunks/CRC, Python-zlib stream, PNG filters,
dimensions and hardcoded RGBA content. `test_png_exports.py` proves that this
oracle rejects corrupt/truncated/oversized streams and accepts valid filter and
split-IDAT variants. The engine `native-tests` step separately tests Zig lookup,
PNG arithmetic/allocation failures and callback rejection; it does not replace
the actual-engine tests. Neither test entry selects a provider at runtime.

Each web case gets a fresh private temporary profile and owned native process.
Core `create_native_spec`, committed configuration, engine lease,
`launch_native` and receipt verification remain authoritative. The screen is
320x320 (the schema minimum), with a 320x240 CSS viewport. DPR defaults to 1;
`scaled-overflow-capture` pins 1.5 in both configuration and native emulation.
Evidence reports actual per-case DPR rather than a hardcoded suite value.
The server binds only a
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

The direct runner defaults to TOON and writes:

- `results.toon`: Agent-facing outcomes, failures, skipped checks and artifact identity;
- `results.json`: the CI validator's machine input, including source/binary hashes,
  native receipts and complete measured results; never the default reading entry;
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
4. Extend `native` driver cases for bounded user tasks or engine-only frame,
   identity, invalidation and lifecycle contracts. Keep assertions attached to
   observed native state and actual pixels where relevant. Extend unit
   regressions for harness/parser/protocol failures, not as substitutes for
   native behavior tests.

Further cases can cover navigation/network, storage, accessibility, subframes,
lifecycle/crashtests and additional CSS/Canvas features. Keep the server and
driver small; add origin/resource features only for a concrete test.
The `wpt` kind runs selected unmodified official DOM HTML tests with upstream
`testharness.js` and a completion-only reporter adapter. Inputs, revision and
licenses are pinned in `wpt-manifest.json` and `web/wpt/`. It is not a full
wptrunner/testdriver implementation; ordinary `assertion` fixtures remain local.

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

## Optional finite browser differential adapter

`quality_browser.py` is a separate, explicit observation adapter, not part of the
default test dependency path and not a WPT runner. It reuses `run_web.py` helpers
and the same isolated `mioa_core.native.launch_native` lifecycle. For one engine:

```sh
python tests/quality_browser.py --engine mioa --binary /absolute/native/binary --png-decoder /absolute/libpng-decoder --output /tmp/mioa-quality-native
python tests/quality_browser.py --engine chromium --png-decoder /absolute/libpng-decoder --output /tmp/mioa-quality-chromium
python tests/quality_browser.py --engine firefox --png-decoder /absolute/libpng-decoder --output /tmp/mioa-quality-firefox
```

Reference execution requires Playwright **1.56.0** and its pinned Chromium
revision **1194** / Firefox revision **1495**. Install references into an isolated
environment, then acquire them with `python -m playwright install chromium
firefox`; set `PLAYWRIGHT_BROWSERS_PATH` consistently during acquisition and use.
`--executable` explicitly selects another Playwright-compatible reference binary,
whose actual version/path/SHA256 are recorded. No runner attaches to an existing
browser/profile. The adapter never installs dependencies or builds a binary.
`--binary-label historical` can label a deliberate historical native executable;
its report does not establish current-source build provenance.

The identical `quality_browser/cases.js` and `payload.json` run in each engine.
`quality_browser/manifest.json` pins all 80 `html5lib-tests` `entities.test` cases
at revision `c5495c539ca8a79c3c5107f5f14010bcff9a35d5`, MIT license and exact
SHA256, plus five explicit WHATWG named/numeric-reference cases. These observe
DOM text/attribute results, **not** tokenizer parse-error events or primitive
entity-table lookup. Corpus error metadata remains in the retained payload.
Unicode and RGBA compare exactly: no trimming, Unicode normalization, hidden-RGB
adjustment, expected-failure rewriting or majority-reference oracle.

Canvas observes actual `getImageData` pixels and saves the actual `toDataURL`
PNG. `--png-decoder` uses the independent libpng decoder CLI
`DECODER input.png output.rgba` with JSON stdout
`{"width":3,"height":2,"format":"RGBA8"}`; decoded bytes must equal the source
pixels. Missing decoder/browser prerequisites are `NOT_RUN`, not success.
Exceptions, crashes, timeouts and semantic mismatches are failures; the process
returns nonzero for either `FAIL` or `NOT_RUN`. A final stdout JSON object locates
`report.json`; observations, payloads, PNG/RGBA and logs remain in the output.
Do not compare compressed PNG bytes across browsers: only decoded pixels are
the contract. The selected Canvas input uses transparent black rather than
claiming that nonzero RGB at alpha zero survives Canvas premultiplication.

Official WPT tooling may be invoked separately from an official checkout. This
adapter implements neither a private WPT runner nor a Mioa WebDriver product;
without that capability, Mioa WPT execution is explicitly `NOT_RUN`. Selected
reference-browser WPT execution cannot establish Mioa WPT conformance.
Domato, Fuzzilli and AFL++ remain optional P1 campaigns, not executions claimed
by this deterministic suite. See the manifest for exact gaps and oracle URLs.

## Executable Web standards checklist

See [`STANDARDS.md`](STANDARDS.md) for the pinned source, honest status semantics,
per-standard TDD process and decision schema:

```sh
make test CHECKLIST=1
make test CHECKLIST=1 SPEC=html
make test CHECKLIST=1 MODULE=layout REPORT=/absolute/path/to/report.toon
```

The default checklist includes all 831 pinned W3C browser-specs entries, not only
the browser category. Official titles/links, publication metadata, available WPT
paths, status, gaps and next actions remain per standard. Versions, overlapping
documents, guidance and non-browser entries are retained; completeness is
relative to that exact source, not the entire RFC universe or an obligation to
implement every document. The command reads local metadata/evidence; it does not
execute WPT, fetch a catalog, or claim whole-standard support from local smoke
fixtures. Empty compatibility decisions initially leave evidence TODO; unknown
coverage is not production implementation absence, and nothing is automatically
marked 放弃兼容. Measured failures remain visible.

Follow task value -> normative clauses/WPT -> failing consumer-visible test ->
owner fix -> selected module tests -> diagnose -> scoped deploy evidence.
Broader Zig/Rust suites are not yet comprehensively integrated into the runner;
their omitted execution remains a gap. Acceptance exceptions never waive
`native-frame-contract`, unit failures, errors, crashes, timeouts or safety
floors, and never turn missing or failed evidence into PASS.
