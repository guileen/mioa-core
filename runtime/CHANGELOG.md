# Native runtime changes

## 2026-11-07 — Core source ownership

- Imported current maintained Lightpanda engine/Rust bridge and Obscura
  layout/paint source, complete workspace crates and internal vendored path
  dependencies. Upstream attribution and component licenses are retained.
- Imported required compile fonts, native source-test fixtures and the runtime
  CJK font with notices; caches, `.git`, generated executables/snapshots,
  application profiles and private design/closure documents are excluded.
- Replaced mutable sibling renderer paths with Core-local paths. Engine V8 and
  Cargo inputs are explicit, Cargo builds are frozen, and version resolution
  no longer reads a surrounding Git repository.
- Added a macOS-arm64-only source materializer/builder with content-pinned
  native inputs, per-target locked Cargo archive vendoring, isolated caches,
  optional standalone renderer target, external output-root support, and
  source-bound success/failure receipts. No sibling executable fallback or
  patch-export/apply build step remains.
- Recorded original source digests and historical patch/dependency inventory
  identities separately from immutable non-source input pins and generated
  build receipts. Preserved AGPL declaration ambiguity and the original
  Liberation 1.07.4 font-license distinction without relicensing.
- Qualified the engine build command against Zig 0.17's supported cache
  interface and explicit Cargo configuration precedence; the host registry
  mirror cannot supersede the verified local directory source.
- Kept renderer-only materialization from deleting an existing engine output.
- Added Core-only service/harness regressions, real JS assertions, exact
  independent reftests, native paint/focus/input identities and failing controls.

Implementation and engine/Web-path smoke have been exercised in the integrating
workspace. Receipts bind their actual source and artifact identities; no broad
browser conformance, Desktop, performance or release qualification is implied.
