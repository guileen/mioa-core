# Quality validation requirements

## ADDED Requirements

### Requirement: QV-IDENTITY — Scoped reproducible evidence
Each verdict MUST bind source/binary/toolchain/configuration/corpus identity and scope, preserving raw outcomes.
#### Scenario: Stale artifact or changed inputs
- WHEN a supplied binary differs from its receipt or source inputs change during a run
- THEN current qualification fails and the raw evidence remains; an explicitly historical artifact never qualifies current G3/G4.

### Requirement: QV-STATUS — Honest independent gates
G0–G4 MUST produce separate machine-readable status/reason/evidence. UNSUPPORTED, KNOWN_FAIL, NOT_RUN and FLAKY MUST NOT count as PASS.
#### Scenario: Missing execution
- WHEN a required tool/adapter is unavailable or a selected test is omitted
- THEN the report identifies NOT_RUN or execution failure and required gate admission is nonzero; no empty-set success occurs.

### Requirement: QV-ENT-DATA — Community entity contracts
The native lookup and HTML consumers MUST be distinguished and checked against pinned community inputs and independently stated expectations.
#### Scenario: Names and tokenizer contexts
- WHEN complete entity names, prefix ambiguity, case/legacy aliases, double scalars and text/attribute inputs are exercised
- THEN exact observable outputs are checked for each declared layer; lookup success does not imply HTML tokenizer correctness.

### Requirement: QV-PNG-ORACLE — Independent PNG checks
RGBA encoder output MUST be checked by independent libpng decoding plus applicable PNG structure/alpha/checksum contracts and fixed community samples.
#### Scenario: Correct pixels and malformed structure
- WHEN native probes/Canvas export PNG and corrupted chunks or streams are supplied
- THEN valid declared formats reconstruct expected pixels and required malformed structures are rejected; codec and Canvas scopes remain separate.

### Requirement: QV-MUTATION — Semantic mutation killing
At least ten directed contract mutants MUST compile successfully and be rejected by their associated semantic assertions after baseline success.
#### Scenario: Invalid mutant
- WHEN compilation fails, execution times out/crashes, or only an unrelated assertion fails
- THEN the mutant is not counted as killed and the gate cannot silently satisfy its kill requirement.

### Requirement: QV-DIFFERENTIAL — Reproducible real references
The same pinned inputs MUST be executed on isolated Mioa, Chromium and Firefox with raw and normalized observations retained.
#### Scenario: Reference disagreement
- WHEN observations differ
- THEN the report preserves the discrepancy and specification context; it does not auto-accept majority output or create KNOWN_FAIL exceptions.

### Requirement: QV-FUZZ — Confirmed shrink/replay loop
A deterministic target and seed corpus MUST support bounded fuzz, signature-preserving ddmin, explicit confirmation, and permanent regression replay.
#### Scenario: Real finding promoted
- WHEN an actual target failure is reproduced, minimized under the same signature and explicitly confirmed against a named contract
- THEN the original/minimized bytes, seed, target/oracle identities and expectation are retained and the fixed target passes regression replay; injected faults remain separately labeled.
#### Scenario: Unconfirmed or changed finding
- WHEN confirmation is absent, identity differs or reduction changes the failure class
- THEN no automatic regression promotion or expectation rewrite occurs.

### Requirement: QV-HARNESS — Test the verifier
Harness self-tests MUST detect assertion failure, timeout, script exception, omitted execution and stale artifacts; memory/resource fault checks MUST record actual support.
#### Scenario: Verifier faults
- WHEN a controlled fault is introduced
- THEN the raw expected failure and nonzero admission are observed; timeout/error is not normalized into PASS.
