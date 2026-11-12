# Web standards: pinned, executable checklist

Use the executable checklist to choose compatibility work and retain its evidence.
It is not a declaration that Core implements every document named by a standards
organization. [POLICY.md](POLICY.md) defines task priorities and release boundaries;
[AUDIT.md](AUDIT.md) records the limits of the existing test inventories.

## Complete relative to an explicit source

[`standards/catalog.json`](standards/catalog.json) contains **all 831 entries** in
[W3C browser-specs index.json](https://w3c.github.io/browser-specs/index.json),
retrieved at **2026-11-08T17:37:46Z**. The SHA256 of the **exact downloaded bytes**,
not the transformed local JSON, is:

```text
3f2f142f6e4e064070b5eb72ba1bfe032d73ded6e2dde06566ec40e1facf9a28
```

This is a pinned catalog, not a live query. Checklist execution does not download
standards, WPT, browsers or dependencies. Refreshing the catalog is an explicit
reviewed data change: retrieve the full source, record new UTC time and raw-byte
SHA256, transform every entry, and review additions, removals, metadata changes
and existing decisions. Do not filter entries to obtain a smaller denominator.

The source has 715 entries with category `browser` and 116 with an empty category
list; **both groups are retained**. Entries include different levels, editions,
snapshots, overlapping specifications, proposals, registries, guidance, process
documents and non-browser technologies. There are 753 `good`, 71 `discontinued`
and seven `pending` standing values in this snapshot. These are publisher/catalog
metadata, not our compatibility decisions. A discontinued document is not
implicitly waived, and a `browser` category is not a requirement that every
product implement every feature. The catalog is not the universe of every RFC,
ISO document, historical specification or future Web standard. “Complete” here
means every entry in this exact source snapshot, without collapsing versions or
filtering by category, standing or perceived usefulness.

Each catalog record has `id` (the official `shortname`), official `title` and
`url`, `description`, `categories`, `standing`, `wpt_paths`, and `module`.
Descriptions reproduce the official short title plus publication-status and
standing metadata; they are not invented summaries of normative requirements.
`wpt_paths` copies the source's `tests.testPaths`, or is empty when no paths are
listed. Available paths are linked to the official
[web-platform-tests repository](https://github.com/web-platform-tests/wpt).
No listed path means “no mapping in this catalog”, not “no tests exist”. Paths
are discovery aids, not a pinned WPT checkout or executed WPT evidence.

`module` is a primary work-routing label derived from official names/metadata:
`network`, `dom`, `layout`, `input`, `storage`, `media`, `lifecycle`, `control`,
`security`, or `other`. It is not an implementation claim or exclusive dependency
boundary: HTML, CSS, security and many other documents span multiple modules.
`other` preserves entries outside the current runner's module vocabulary. It
must not disappear from the default unfiltered checklist.

## Commands and output

From the checkout root:

```sh
make test CHECKLIST=1
make test CHECKLIST=1 SPEC=html
make test CHECKLIST=1 MODULE=layout
make test CHECKLIST=1 SPEC=html REPORT=/absolute/path/to/report.toon
```

`SPEC` is an exact official shortname, not a guessed alias: for example
`cssom-view-1` and `webvtt1`. The direct CLI supports repeatable `--spec` and
`--module` filters. `REPORT` / `--report` reads an existing CI evidence report;
it does not run the referenced checks or acquire candidate evidence. Use
`make test HELP=1` for the current executable interface. Selecting one standard or
module limits the output, not the catalog's declared total.
`report.toon` is the Agent-facing entry. Its machine record binds the TOON digest;
the validator rejects modified evidence and rechecks source/artifact identity. The
Agent must not read the JSON intermediate.

Default TOON is the structured ledger an Agent reads; `--format markdown` gives
one line per standard, and `--format json` is the machine export. Full links,
source metadata, rationale and next TDD actions remain in JSON (`FORMAT=json`).
Aggregate counts do not replace the per-standard rows.
JSON has `schema`, `source`, `catalog_count`,
`selected_count`, `status_counts`, `evidence`, `decision_schema`, and `specs`.
Each `specs` row includes its catalog metadata together with `status`,
`status_label`, `scope`, `required_checks`, `evidence`, `gap`, `next_action`, and
`decision`. A printed checklist is evidence discovery, not a standards execution
or release approval.

## Four statuses, with honest evidence boundaries

| Status | Meaning and required interpretation |
| --- | --- |
| **支持** | Fresh passing required checks establish only an explicitly declared finite behavioral scope. Display that scope; never promote one fixture, API-presence probe or module test into whole-standard conformance. |
| **TODO** | The standard has no sufficient current scoped evidence/decision. It may be implemented, partially implemented, untested, or outside a selected task; the ledger cannot infer which from source names. Missing evidence is not a measured failure. |
| **放弃兼容** | A reviewed, explicit per-standard product decision records the reason and affected user tasks. No automatic decline by category, age, `standing`, absent WPT path or missing implementation evidence. A measured failure remains visible rather than being erased by this decision. |
| **测试失败** | Associated executed evidence records a failure/error/crash/timeout. Preserve the underlying outcome and finite scope; this is not proof that every clause in the document fails. Missing/unexecuted evidence must remain distinguishable. |

[`standards/decisions.json`](standards/decisions.json) records intended compatibility
scopes or explicit non-support, not automatic PASS verdicts. DOM currently declares
a bounded node-operation scope tied to four pinned official WPT files. Tests still
must run successfully against a verified build before that scope becomes 支持.
No other standard is implicitly declined or claimed supported.

Decisions are keyed by exact catalog ID. The executable report's
`decision_schema` is the authoritative field contract. A scoped support decision
requires `status: "支持"`, a precise `scope`, `required_checks`, and `reason`;
a decline requires `status: "放弃兼容"`, `reason`, and `user_impact`. A support
decision alone is not a pass: required checks must have fresh matching successful
evidence. Do not add broad `html`/`dom` support declarations merely because one
smoke fixture passes. Deliberate product non-support and unknown test coverage
are separate facts.

`RELATED_SPECS` must follow actual assertions, not API names, dependencies or
features inherited from another specification. A refused WebSocket connection
does not test RFC6455 handshake/framing; gzip does not test Brotli; a local
platform-interface fixture does not test HSTS, SRI, Document Policy or other
response policies. Such associations and unsupported scope decisions have been
removed. Missing decisions remain TODO, not automatic compatibility declines.
Interface-presence and `CSS.supports` scopes must say exactly that; they do not
establish construction, rendering, delivery, isolation or cryptographic strength.

The 2026-11-08 source-only repair strengthens IndexedDB acceptance: error,
blocked open and timeout reject; a new database must upgrade, create its store
and return that store on success. Offline-audio acceptance now requires an actual
OfflineAudioContext, not an AudioContext-only skip. Both changed cases are
**NOT_RUN** for this repair. Previous PASS receipts and prior support counts do
not validate modified assertions, mappings or the current source.

## Task-value-driven TDD loop

1. **Choose a target user task and standard.** Prioritize navigation, ordinary
   forms/input, useful DOM execution, reading/layout, storage/authentication and
   required safety boundaries by task value and failure severity, not by how
   cheaply a standard's count can be improved. Use the per-standard gap/action
   and module filter to discover the owning area and affected neighbors.
2. **Read the official normative clauses and applicable WPT.** Follow the
   catalog's official URL and linked WPT paths; select the edition/level relevant
   to the task. Identify observable requirements, prerequisites, exceptions,
   security/origin rules and an independent oracle. For a document without a WPT
   mapping, locate its authoritative conformance tests or derive a minimal test
   from the exact clauses. Do not invent a normative summary from the title.
3. **Write and observe a failing consumer-visible test.** State the task goal,
   starting state, interactions, expected completion and normative clause. Exercise
   the actual public/native owner where required; a mock, symbol presence or
   source scan cannot substitute for the browser behavior. Distinguish semantic
   failure from infrastructure failure and missing prerequisites.
4. **Fix the owning implementation.** Add the smallest correct fix that closes
   that contract and its affected failure paths; preserve regressions and risk
   floors. Do not relabel the test, suppress errors or waive behavior to turn a
   red result green.
5. **Run selected module tests, then diagnose.** Use
   `make test MODULE='...'` for the owner and affected neighbors, followed by
   `make test LEVEL=2 BINARY=...` at integration milestones. Retain actual candidate-bound
   outputs and gaps, then regenerate the checklist with `REPORT=...`. Broader
   Zig/Rust suites are not yet incorporated comprehensively in this runner;
   their existing tests remain useful but their omitted execution is an explicit
   gap, not an automatic pass or proof those tests do not exist.
6. **Deploy only within reviewed evidence scope.** Use `make test LEVEL=3` with
   the identified candidate and receipt. Specific, reviewed, unexpired acceptance
   exceptions can preserve eligible evidence gaps/semantic failures without
   rewriting them to PASS. **`native-frame-contract` is never waivable**, nor are
   unit failures, infrastructure errors, crashes, timeouts or required safety
   boundaries. A standards support/decline decision is not a deploy exception;
   a deploy exception is not a standards conformance decision. A scoped deploy
   verdict still does not establish sufficient target-task coverage or absolute
   product readiness.

## TODO priorities

`standards/priorities.json` controls work ordering independently of compatibility
decisions. Every row carries `priority`, `priority_reason` and a visible label such
as **TODO P0**. These are initial product judgments, not measured usage weights:

- **P0:** core loading/script/DOM/input foundations and required safety boundaries.
- **P1:** common layout, text, state and control capabilities.
- **P2:** browser-targeted capabilities awaiting task-value evidence.
- **P3:** peripheral catalog entries; keep visible and promote when a consumer needs them.

Defaults apply by catalog category; per-standard overrides require a reason.
Priority never means support, never silently declines compatibility, and never
changes a failed check into PASS. A P0 standard means prioritize the required
behavioral slice, not implement every clause before shipping.

```sh
make test CHECKLIST=1 PRIORITY=P0
make test CHECKLIST=1 MODULE=layout PRIORITY=P1 FORMAT=toon
make test CHECKLIST=1 MODULE=layout PRIORITY=P1 FORMAT=json
```

TOON lists P0 first. Primary evidence is `report.toon`, `coverage.txt`,
`standards.toon` and `standards.md`; machine-only intermediates serve validators.
JSON ledger/coverage exports are opt-in (`FORMAT=json`). Priority filtering limits
display, not the catalog denominator or deployment test selection.

## First command: audit the tests, not product pass rates

`make test` reports total standards, standards with registered executable tests,
unregistered standards, and explicit compatibility-scope decisions, broken down
by P0–P3. It runs no browser and cannot approve a release. `FORMAT=toon` exposes
the complete per-standard test/decision inventory for an Agent; `FORMAT=json` is
the machine export.

The registry counts only tests linked in `RELATED_SPECS` and present in the runnable
plan. Existing unregistered Zig/Rust tests remain an audit gap, not proof of absence.
Multiple tests do not inflate the standard count; registered tests do not establish
clause completeness. Scope decisions remain distinct from execution results.

After inspecting P0, reuse/register existing tests and define the relevant behavior
scope. Add tests for genuine gaps, observe failures, then fix the implementation.
`make test LEVEL=2 MODULE=dom BINARY=... KEEP_GOING=1` observes selected node WPTs;
the same invocation emits the standards verdicts from its own evidence.
