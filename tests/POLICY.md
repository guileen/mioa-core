# Testing policy: compatibility benefit, not test volume

This records the testing priorities and release-claim boundaries. Executable
selection and enforcement live in `tests/ci.py`, its manifest and the root
`Makefile`; use `make test HELP=1` and `make test PLAN=1` for the current contract.
[README.md](README.md) documents usage; [AUDIT.md](AUDIT.md) records source-based
coverage and gaps. Documentation is neither an execution nor release approval.

## North star

Core should complete enough **target users' real tasks** reliably. We do not aim
to make every website work, reach 100% WPT, maximize assertion counts, or perfect
one export format at the expense of navigation and interaction. Prioritize work
by affected target tasks, frequency, failure severity and compatibility benefit.
A thousand entity-table assertions are not a thousand independently working tasks.

A task case must identify its user goal, starting state, interactions, observable
completion criteria, environment and known limitations. Examples of task families
are reading/extracting a document, following navigation, searching/filtering,
submitting a form, and retaining an authenticated workflow. These are priorities,
**not claims that a representative task corpus exists or currently passes**.
Pinned/replayable fixtures give repeatable regressions; explicit live-site runs
provide complementary observations, not an unconditional compatibility promise.

## Current optimization focus: task performance

Subject to the risk floors below, prioritize measured latency in frequent task
flows over additional rare-edge coverage. Perceived latency is the first priority:
pointer-to-hover feedback, typing, scrolling, then navigation and return-to-task.
Trace input queue age, main-thread/GPU waits, protocol round trips, full-frame work,
DOM/layout reads and serialization to their owning hot path. First feedback must
not wait for a burst timer; continuous motion may coalesce to the latest position,
but press/release order and displayed-frame authority must remain intact.
Compare the same task, inputs, artifact and configuration before/after; separate
cold/warm runs and retain sample counts, durations, completion results and relevant
call counts. Report tail latency only with adequate samples, not a single run.
Do not claim speedups from shorter timeouts, reduced correctness/coverage or skipped
work. Caches require explicit invalidation; concurrency requires independent work.
Fixture timing establishes a local baseline, not representative user performance.
Name timing endpoints: page mouseenter-to-rAF, frame-request RTT and
pointer-to-GPU-submission are not motion-to-visible-pixels. Use native-window
evidence for desktop UI, and retain idle CPU/draw checks when changing pacing.

## Risk floors and module priorities

Compatibility benefit does not waive these release floors:

- **Authority and isolation:** no stale/wrong-document input, cross-profile or
  cross-origin data leak, unauthorized capability use, or secret-bearing errors.
- **Integrity and ownership:** no known data loss/corruption, unsafe lifetime,
  double release, unbounded leak or silent partial publication on required paths.
- **Operational safety:** required tasks must terminate within declared bounds;
  crashes, hangs, startup/shutdown failure and missing native evidence are not PASS.
- **Evidence integrity:** record exactly what ran and against which artifact;
  deleting a case, skipping a prerequisite or changing an oracle cannot improve
  a release verdict without explicit scope review.
- **OS dependency boundary:** `leak-direct-os-api` is mandatory and non-waivable.
  Core package and engine sources may obtain host capabilities only through the
  public API, not platform imports, direct OS calls or private Graphics modules.

This table classifies compatibility/risk coverage, not the optimization queue.
Select performance work by demonstrated high-frequency task bottlenecks.

| Priority | Module / behavior | Useful evidence |
| --- | --- | --- |
| P0 | Lifecycle, process/transport ownership, control leases, stale input, profile/storage isolation | Cheap deterministic regressions plus real native boundary checks |
| P0 | Navigation/network/loading, DOM/JavaScript execution and ordinary forms/input | Task completion and focused engine tests, not API-presence probes |
| P1 | CSS/layout/text/fonts, scrolling/hit testing, frames, accessibility observations | Geometry/semantic assertions and independent rendering references tied to tasks |
| P1 | Cookies/storage, fetch/resources and other APIs required by selected tasks | Persistence/origin/failure-path checks with the actual native owner |
| P2 | Specialized exports and deep primitive closure, including Entities/PNG mutation/fuzz campaigns | Retained regression oracles; expand when a real task or risk justifies it |

P2 is not permission to break existing contracts. Keep useful Entities/PNG tests;
PNG is regression coverage, not the product's compatibility north star. Prefer
covering an untested high-value task boundary to adding more low-risk PNG cases.

## Single quality controller

`make test` is the sole execution entry; `LEVEL` controls depth, not a choice of
independent tools. Default LEVEL=1 runs source boundary checks, unit regressions
and coverage/gap reporting. LEVEL=2 adds real runtime cases. LEVEL=3 requires the
current candidate binary and source receipt and cannot narrow modules.

```sh
make test
make test MODULE='network storage'
make test LEVEL=2 BINARY=/absolute/path/to/lightpanda
make test LEVEL=3 BINARY=/absolute/path/to/lightpanda RECEIPT=/absolute/path/to/receipt.json
make test PLAN=1
```

The OS boundary is mandatory at every level, including a single selected module.
Its failure stops later suites by default; KEEP_GOING collects diagnostics without
waiving the boundary or promoting its verdict.
HELP=1, PLAN=1 and CHECKLIST=1 are read-only discovery options on the same target,
not quality passes. No implicit build, download or retry is performed.
Explicit `NATIVE_INPUTS=...` at LEVEL=2/3 opts into the pinned offline native
builder before checks; `NATIVE_OUTPUT` selects its isolated output. It derives
BINARY/RECEIPT and retains source binding and all failures. Build failure stops
the invocation. HELP/PLAN/CHECKLIST do not build; no arbitrary commands are accepted.

Scope: every supported source under `mioa-core/mioa_core` and
`runtime/engine/src`, including conditional branches and local helpers.
Python imports/calls are inspected with AST; native import/include and OS
references are inspected lexically. Public `mioa-os/api` bindings, separate
`mioa-os/` implementations and domain-external build/test orchestration are not
restricted application code. Core importing the private OS implementation is
still forbidden. Cache/build directories are pruned; missing/empty roots,
unreadable or invalid Python
source, and source symlinks fail closed. Findings carry path, line and rule;
CI retains the source digests and complete findings in its boundary result.
Existing violations fail rather than being grandfathered; fix them through the
owning Core/API migration, not ignores, platform conditionals or release waivers.

This check catches known direct dependency patterns; it is not a complete
transitive/dynamic/FFI dependency proof, a compiler, or a runtime sandbox.
Native lexical checks do not resolve arbitrary aliases or preprocessor macros.
Actual direct host access must still be denied by the execution boundary.

Add `ACCEPTANCE=/absolute/path/to/acceptance.json` to deployment only for explicitly
reviewed exceptions. Registered modules are `network`, `dom`, `layout`, `input`,
`storage`, `media`, `lifecycle`, `control`, `security` and `harness`; gaps are
explicit per module, not filled by unrelated tests.

Every execution emits a structured `next_action` and retains complete evidence.
Source leaks require ownership review, public capability contracts, missing OS
implementation in `mioa-os/`, then Core/caller migration. The gate blocks acceptance,
not implementation; absent services are work to implement. Default execution stops
at source failure. `KEEP_GOING=1` observes all selected checks for repair diagnostics,
including after source failure; it preserves the failure and nonzero exit.
An explicit no-execution instruction leaves verification pending while file
edits continue; no status may be promoted on that basis.
`test_os_files` explicitly compiles a temporary file-service library, not an
engine build. It uses real file oracles for rights, ranges, revocation and
generation reuse; this does not establish process isolation.
Unit failures/skips require correction or missing evidence, not promotion.
After basic checks pass, the next action requests runtime evidence. Runtime
observation precedes candidate-bound delivery; remaining compatibility gaps
remain explicit and only eligible risks can receive reviewed acceptance.

At levels 1/2, MODULE selects changed modules and affected neighbors; omission
selects all registered modules and unregistered discovered suites. Level 1
excludes web/runtime cases and does not consume a browser artifact. Level 2
records absent runtime prerequisites as NOT_RUN, not PASS. Level 3 always covers
all modules and binds evidence to BINARY/RECEIPT. The internal dev/diagnose/deploy
verdict modes remain report semantics, not separate Makefile commands.

Reuse existing tests, add consumer-visible regressions at the owning boundary,
and widen only for affected contracts. Do not run full runtime quality closure
after every local edit. Evidence reuse requires matching sources, artifact,
configuration and dependencies. Mocks never replace native boundary execution.

`tests/run.py` remains the **legacy explicit-layer regression harness**. Its
`all` means its Python discovery suites plus its local web manifest, not every
Zig/Rust test, quality campaign, target task or WPT. `--layer unit` makes a smaller
claim. Neither legacy success nor a mode name establishes product readiness.

## Acceptance: baseline versus absolute product evidence

**Baseline acceptance** is a verdict on a declared finite plan for an identified
candidate, not on the whole product. Without exceptions, every mandatory item must
run and pass. Retain source identity, binary and receipt identity, configuration,
case inventory, commands, measured results, artifacts and omissions. Native
self-reported receipt checks and release build provenance are distinct evidence.
No new baseline regression is necessary but insufficient for usefulness: an
already broken task can remain broken without regressing.

The deploy acceptance JSON is a list of `{id, reason, user_impact, expires}`
records. Only explicit evidence gaps and semantic web `FAIL` outcomes may be
accepted; unit failures, infrastructure errors, crashes and timeouts cannot be
waived. Exceptions must be specific, reviewed and unexpired. They retain the gap
or failure in the report rather than converting it into PASS; scoped deployment
acceptance is not proof of absolute task sufficiency or a waiver of risk floors.

**Absolute release acceptance** additionally requires an explicitly reviewed
target-user task set, observable outcomes and acceptance thresholds, successful
candidate-bound task evidence, satisfaction of the risk floors, and a release
statement listing the supported scope and remaining failures. Choose thresholds
before observing candidate results. Known nonblocking gaps may ship only with
explicit scope/risk acceptance; they remain failures or missing evidence, not
rewritten PASS. A required task or risk-floor failure blocks the declared release.
There is no hidden pass through expected-failure metadata or broad waivers.

Current limitations must remain visible:

- The compact local web manifest is not a representative target-user task corpus.
  No task-success percentage or claim of sufficient product compatibility follows
  from its pass rate. Until task scope and thresholds exist, report absolute
  task acceptance as **NOT_ESTABLISHED**; baseline evidence can still be useful.
- Four pinned official DOM testharness pages can run through the native adapter.
  Full wptrunner, testdriver and cross-origin/reftest integration remain gaps.
  WPT is diagnostic input, not a mandatory 100% target for every feature.
- Source-mode imports do not prove installed-wheel correctness; isolated native
  probes do not prove browser integration; historical binaries do not prove the
  current source; a browser differential covers only its selected observations.

A report must distinguish PASS, failure/error/crash/timeout, skipped, unsupported,
NOT_RUN and NOT_ESTABLISHED, and preserve reasons. Empty selection, missing tools,
missing required evidence, unparsed/zero-test output and exit zero without matching
executed outcomes cannot establish a mandatory pass. Unittest skips must be shown
as gaps even if unittest itself exits zero. A release summary must state separately
what the baseline establishes, which exceptions were accepted, which user tasks
were exercised and what remains unknown. Publishing evidence is not approval to
deploy, and a scoped deploy verdict is not whole-product release readiness.

## Standards checklist and compatibility decisions

[`STANDARDS.md`](STANDARDS.md) defines the pinned complete-source boundary and
TDD workflow. `make test CHECKLIST=1` lists entries from the pinned W3C
browser-specs catalog; SPEC/MODULE/PRIORITY/REPORT filter discovery and evidence.
TOON is the default Agent view; FORMAT=json is the machine view.
This is a checklist, not an implicit
WPT run, browser build, or requirement to implement every catalog document.

Every standard retains its official name/link, metadata description, evidence
status, gap and next action. The four statuses are 支持 (only a declared tested
scope), TODO (insufficient evidence), 放弃兼容 (explicit reviewed product
decision), and 测试失败 (preserved measured failure). Missing evidence is not
proof that production lacks an implementation. Category, publication status,
discontinued standing and absent WPT mappings cannot automatically decline an
entry; a tiny fixture cannot establish whole-standard support. A declared
compatibility scope describes intent, not measured success.

Choose work by task value, read normative clauses/WPT, observe a failing
consumer-visible test, fix its owner, run selected module tests, diagnose, and
deploy within the actual evidence scope. Broader existing Zig/Rust suites are
not yet comprehensively included in the executable runner; record that omission.
Standards decisions and deployment acceptance exceptions are separate records.
**`native-frame-contract` is non-waivable**, including when the missing native
boundary is represented as an evidence gap. No acceptance file may override
that contract or the unit/error/crash/timeout and safety-floor restrictions.

On “improve the north star”, first run `make test`: audit standards with registered
tests and explicit compatibility scopes, then inspect P0 gaps. Reuse/register
existing tests before adding missing tests; only then fix measured implementation
failures. Registration is not sufficient coverage and unknown is not unsupported.
