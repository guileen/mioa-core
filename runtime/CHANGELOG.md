# Native runtime changes

## 2026-11-12 — Zig renderer replaces Obscura

- Rendering (DOM layout, inline text, paint, PNG) now runs through the
  Desktop-owned Zig module `src/render_kernel`: `document.zig` consumes the
  engine's DOM projection, `block.zig` serves the block model/PDF path, and
  `painter.zig`/`png.zig` rasterize. The engine imports the module directly:
  there is no renderer selector and no `extern` render entry point; the ABI
  test compares the kernel's `abi.zig` mirrors against the engine's structs.
  `runtime/renderer` (Obscura), `engine/src/rust`, the `renderer` build target,
  the `-Drender_kernel` option, and the document/layout/paint/raster path in
  `engine/src/c/render_bridge.cpp` were removed. That file now only bridges
  Canvas2D/WebGL (still stubbed) and the canvas PNG encoder.
- `runtime/build.mjs` builds only the engine: no Cargo home, registry
  vendoring, or `rusty_v8` input. `inputs.lock.json` keeps the Rust rows as
  provenance and verifies only `engine/build.zig.zon`.
- Fixes found while wiring the renderer: fragment parsing dropped children
  (`htmlAsChildren`), `<li>` popped across `<ol>`/`<ul>`, declarative shadow
  roots ignored `allow_declarative`, and `Streaming.feed` panicked after
  teardown.
- The DOM projection carries one computed-style string per element, resolved by
  the engine's cascade (author rules, inline style, `:focus`/`:focus-visible`,
  inheritance), instead of the raw `style` attribute plus a serialized `<style>`
  sheet the kernel never read. The kernel applies that string and now models the
  box (width/height, padding, border widths, `box-sizing`) for block flow,
  reporting border boxes from `getBoundingClientRect`/metrics and padding boxes
  from `clientWidth`/`clientHeight`. There is still no second CSS engine.
- Fonts stay explicit: `--render-font`/environment assets first, then the
  checked-in Liberation Sans regular/bold fallback. No host font discovery.

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
