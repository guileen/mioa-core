# Native runtime changes

## 2026-11-11 — Native HTML/encoding/render bridge

- `engine/src/c/html_bridge.cpp` builds a real HTML5 tree instead of returning
  without invoking the sink: tokenizer, implied html/head/body, void and
  raw-text/RCDATA elements, the common auto-closing rules (p, li, dd/dt,
  headings, table cells/rows/sections), comments, doctypes, character
  references and parse errors. XML parsing/validation and the incremental
  streaming parser use the same tree builder.
- The encoding API resolves WHATWG-style labels and transcodes UTF-8,
  windows-1252, ISO-8859-1 and UTF-16 in both directions; numeric and named
  character references resolve through the shared table.
- `render_bridge.cpp` rasterizes blocks with a built-in 5x7 bitmap font
  (ASCII plus accent-folded Latin-1, hollow box otherwise), emits PNG colour
  type 6 with CRC32/Adler32 and stored-block zlib, blits RGBA pixels in
  `lp_canvas_put`, and encodes `lp_canvas_png`.
- `tests/bridge_contract.cpp` asserts parse, PNG bytes, canvas pixels and
  encoding without V8, plus that every C symbol the Zig sink imports resolves
  at link time; `make bridge-contract` runs it and `make test` LEVEL=1 executes
  it before the remaining checks. All five checks pass.
- Known limits: no adoption agency algorithm, foster parenting, `<template>`
  content fragments or SVG name adjustment; no real deflate compression;
  `lp_layout_new` still exports an empty layout because its C structs do not
  yet match the Zig mirror; `make test` LEVEL=1 still stops at the pre-existing
  `source/leak-direct-os-api` boundary.

## 2026-11-09 — Device-scale text truncation markers

- `text-overflow: ellipsis` and `-webkit-line-clamp` ellipsis markers are now
  shaped and rasterized on the same device grid as their content glyphs. The
  marker had been drawn at 1x with its logical offset added to device pixels,
  so at 2x DPR it was both misplaced and blurry while the content text was
  already device-scale. The marker's logical position is now scaled explicitly
  before `LayoutGlyph::physical`, which applies its offset after scaling.
- The native device-scale capture predicate no longer rejects
  `text-overflow: ellipsis`; ellipsis truncation is pure shaped text plus a
  marker and needs no logical-pixel intermediate surface.
- `cargo test -p obscura-render --features paint --lib` passes the new
  `ellipsis_markers_rasterize_on_the_device_grid_at_two_x` and the existing
  truncation/2x tests; the full lib suite reports two failures unrelated to
  this change (`native_button_intrinsic_width...` with the paint feature and
  the font-directory symlink test only in full-suite order, which passes in
  isolation). `make test` LEVEL=1 still stops at the pre-existing
  `source/leak-direct-os-api` boundary.

## 2026-11-09 — Runtime data-directory error

- Keep the data-directory path alive while formatting a directory-creation error.
  Native build and normal control scenarios passed; the creation-failure path
  remains unverified.
- Release the allocated data-directory path if later `App.init` steps fail.
  Native build is blocked by unrelated `StyleManager.zig` errors; failure-path
  and successful runtime verification remain pending.

## 2026-11-08 — Task-boundary repairs (execution NOT_RUN)

- History state URLs are resolved against the owning document and authorized
  before mutation; disallowed rewrites raise `SecurityError`. Fragment assignment
  pushes history rather than replacing it.
- Empty form/submitter actions use the document URL, independent of `<base>`,
  for both GET and POST.
- Locator/element keyboard input retains the original node and document. Native
  dispatch checks document, attachment and focus before each event; stale input
  is rejected without retargeting or replay. Key-up may follow a same-document
  focus change, but not a different focused frame.
- Added History authorization, hash traversal, empty-action submission and native
  element/focus/navigation/adoption regressions. These changes and tests have not
  been built or executed: this repair session permits file operations only.
- IndexedDB acceptance now rejects errors, blocked opens and timeouts, and requires
  upgrade/store creation before success. Offline-audio assertions cannot pass by
  skipping the offline context. Standards mappings/scopes no longer borrow
  unrelated fixture PASS results; missing evidence remains TODO.

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
