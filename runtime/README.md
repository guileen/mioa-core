# Core native Web runtime

Core owns `engine/` (the maintained Lightpanda Zig engine plus the native
C-ABI bridges in `engine/src/c/`) and `renderer/` (the maintained Obscura Rust
layout/paint implementation and its internal path dependencies). These are
source trees, not a binary wrapper or a patch overlay. Desktop is a
Host/consumer; independent headless callers use the
same native CDP executable. This migration does not claim broader Web standards
coverage than the imported implementations provide.

Legacy raw captures retain at most one pixel fingerprint per Frame, not pixel
buffers. Reuse requires an unchanged exact document/tree/viewport/focus/input/
resource/Canvas state before and after reprojecting all document semantics.
Capture failure clears the cache; unstable or evicted guard records fall back
to full raster/hash. Expected-fingerprint comparison at input dispatch is
unchanged. This internal optimization grants no displayed-frame authority.

Raw pixel SHA-256 is supplied synchronously by the Zig bridge for both capture
and uncached atomic-input validation. The hash covers the same full RGBA bytes;
the scope hash and equality check are unchanged. This uses Zig's target-selected
crypto backend instead of the pinned Rust sha2 default's software ARM backend,
without an additional dependency, buffer copy or persistent pixel cache.
The Rust renderer retains exclusive pixel ownership until the callback returns.

Device-scale rasterization supports rectangular/nested rounded overflow clips,
outset box shadows, element-only pure translations and shaped
`text-overflow: ellipsis` / `-webkit-line-clamp` truncation markers for the
existing eligible-content subset. Both the content glyphs and the separately
shaped marker are rasterized on the device grid, so truncation neither falls
back to CSS-size rasterization nor paints a 1x marker at a logical offset.
Transform-list translations and individual `translate`
compose in CSS space through the existing offset painter, without a layer or
resampling. Clip paths, text/pseudo-element masks and shadow geometry share the
surface raster scale.
Shadow radii resolve in CSS space before scaling; CSS blur thresholds, layer
counts and alpha remain unchanged. Temporary shadow surfaces are culled and
clamped to the visible device-pixel bounds. The per-surface mask cache remains
bounded to two entries. Eligible captures avoid CSS-size rasterization followed
by full-frame Lanczos resizing. Inset shadows, pseudo-element transforms,
rotation/scale/skew/matrix transforms and existing unsupported effects/content
retain that fallback. Zero-opacity groups, already skipped before layer
allocation, do not force it; fractional-opacity groups still do. Reveal/hide
transitions re-evaluate eligibility. Exact input guards and full-pixel hashes
are unchanged.
No persistent pixel cache or new input authority is introduced.

Network HTML is parsed progressively by one navigation-owned tokenizer. Final
HTTP headers commit the active document even before body bytes arrive; redirect
hops do not commit. Auth challenges, CORS validation and CDP response-stage
interception retain completed-response gates. History commits before the first
parser script. Each received chunk updates the same DOM without reparsing prior
bytes. EOF finishes the parser once and retains existing script/load-event ordering.
Stop preserves the parsed prefix; replacement destroys the old parser/decoder.
Explicit charsets need BOM lookahead; undecided HTML retains at most the
1024-byte meta prescan window. Stateful decoding preserves split multibyte input,
with BOM precedence; complete UTF-8 chunks avoid a decoding copy. XML and other
non-HTML documents retain buffered parsing. A visible prefix remains loading:
this is not an early load-success signal or an input-authority relaxation.

Default `target=_blank` links/forms and `window.open` with `noopener` or
`noreferrer` create independently owned Session Pages and discoverable CDP targets
before destination response bytes arrive. Closing the creator cannot close them.
Their script-close permission and close intent survive document replacement and
reload; Session retires only that target after JS unwinds, before queued navigation.
Noopener non-special names do not reuse opener-bearing named windows.
Opener-bearing windows retain stable references and creator-bound lifetime; their
top-level frame events cannot replace the creator target's document identity.
Cross-origin WindowProxy lifetime separation is not implemented by this change.
Target discovery does not implicitly select a new tab or transfer input authority.

## Build

Supported input configuration: **macOS arm64 only**, Zig **0.17.0**, Rust/Cargo
**1.99.1**, Node.js **22 or newer**, and the macOS SDK whose `SDKSettings.json`
is pinned in `inputs.lock.json`. Other platforms need a separately reviewed
input lock and qualification; their upstream source support is not a Core build
support promise.

For a source-bound engine build followed by checks through the existing target:

```sh
make test LEVEL=2 MODULE="control input" NATIVE_INPUTS=runtime/.local/inputs.json \
  NATIVE_OUTPUT=runtime/.local/test-build KEEP_GOING=1 FIX_ZIG_FORMAT=1
```

`FIX_ZIG_FORMAT=1` formats the reviewed `StyleManager.zig` with the pinned Zig
before the source-bound build; the formatted source is included in its digest.
Omit it when the source is already formatted.

`NATIVE_INPUTS` explicitly runs the pinned offline builder, derives BINARY/RECEIPT
and retains the mandatory source gate and nonzero failures. HELP/PLAN/CHECKLIST do
not build. Without `NATIVE_INPUTS`, the controller remains artifact-consuming only.

Create a private machine configuration outside tracked sources, for example
`runtime/.local/inputs.json`:

```json
{
  "schema": 1,
  "zig": "/toolchains/zig/bin/zig",
  "cargo": "/toolchains/rust/bin/cargo",
  "rustc": "/toolchains/rust/bin/rustc",
  "sdk": "/toolchains/MacOSX.sdk",
  "v8_archive": "/native-inputs/libc_v8_15.5.35.13_macos_aarch64.a",
  "zig_packages": "/native-inputs/zig-pkg",
  "cargo_registry_cache": "/native-inputs/crate-archives",
  "rusty_v8_archive": "/native-inputs/librusty_v8.a",
  "jobs": 4
}
```

Paths may be absolute or relative to the configuration file. Executable fields
are explicit paths, not PATH lookup names. `rusty_v8_archive` is required only
for the standalone renderer target; `zig`, `v8_archive`, and `zig_packages` are
required only for the engine. The other fields are required; `jobs` defaults to
4. Unknown configuration keys fail. Configurations and receipts may contain
machine paths and must stay private and outside the source manifest.

From the Core root:

```sh
node runtime/build.mjs build --inputs runtime/.local/inputs.json
# Optional standalone Obscura CDP runtime, retaining the imported render feature:
node runtime/build.mjs build --inputs runtime/.local/inputs.json --target renderer
# Both native runtimes:
node runtime/build.mjs build --inputs runtime/.local/inputs.json --target all
```

The engine has no Rust static library: `engine/src/rust/` is gone and
`engine/src/c/` supplies the C ABI that the Zig sink calls. Two primitives
remain selectable at compile time:

```sh
# Defaults: Zig named-entity lookup and shared RGBA8-to-PNG export.
node runtime/build.mjs build --inputs runtime/.local/inputs.json \
  --entities-provider zig --png-provider zig
```

`native_providers` in the build receipt records these choices. The HTML/XML
tree builder, the document encoding API, `lp_render_png` and the Canvas2D
pixel/PNG path live in `src/c/{html,render}_bridge.cpp`. The standalone
`renderer/` target is still Rust and is built separately.

The native bridge is a documented subset: tokenization, implied
html/head/body, void and raw-text elements, common auto-closing, comments,
doctypes, character references, XML parsing/validation, incremental streaming
feeds, WHATWG-style encoding labels with UTF-8/windows-1252/ISO-8859-1/UTF-16
transcoding, a built-in 5x7 bitmap font (ASCII plus folded Latin-1) and PNG
colour type 6 emission with stored-block zlib. It does not implement the
adoption agency algorithm, foster parenting, `<template>` content fragments,
SVG name adjustment or real deflate compression, and `lp_layout_new` still
reports an empty layout because its C structs do not yet match the Zig mirror.
A bridge success return is not proof of those paths.

`make bridge-contract` compiles the two bridges with the Zig C++ driver and
asserts the four capabilities without V8, plus the complete exported symbol
surface the engine imports; `make test` runs it at LEVEL=1.
`tests/bridge_contract.cpp` is the acceptance surface for the parser, PNG,
Canvas blit, encoding transcoder and ABI exports.

The engine's `native-tests` Zig build step exercises isolated Zig primitive
behavior without V8/browser startup. It uses the same pinned build inputs and
Zig options setup; actual consumer behavior is separately exercised by
`tests/run.py` against each selected engine binary.

External V8 adapters must use the exact Chromium libc++ ABI v2 headers bundled
with the pinned V8 build, not the platform's system libc++. `--v8-headers`
accepts the extracted V8 `include/` directory. Before passing it to the builder,
prepare its private C++ ABI inputs using the Core-owned policy:

```sh
node --input-type=module -e \
  'import {prepareV8CppAbi} from "./runtime/v8_abi.mjs"; await prepareV8CppAbi(process.argv[1], process.argv[2])' \
  /absolute/v8-headers /absolute/private-v8-abi-cache
node runtime/build.mjs build --inputs runtime/.local/inputs.json \
  --v8-headers /absolute/v8-headers
```

`prepareV8CppAbi(root, cache?)` verifies the pinned V8 include Git tree, downloads
the exact libc++/libc++abi include trees and Chromium support files, verifies
their pinned Git tree/blob identities, and atomically stages
`root/cpp-abi/config`, `root/cpp-abi/libcxx/include`, and
`root/cpp-abi/libcxxabi/include`. Links and archive traversal are forbidden.
The optional cache defaults to `.v8-cpp-abi-cache` beside `root`; it must be
an owned private directory (mode 0700). Verified cached inputs support offline
preparation; existing mismatches fail rather than falling back or being silently
replaced. Preparation does not modify global toolchains or dependency versions.
`verifyV8CppAbi(root)` performs the same identity checks without network access.
`V8_CPP_ABI_POLICY` and `V8_CPP_ABI_POLICY_SHA256` expose the canonical provenance.
The native builder validates these inputs before compilation, records their
exact identities in `receipt.v8_headers.cpp_abi`, and includes them in the
full header-root fingerprint checked after compilation. Desktop candidate
preparation imports this same Core helper and stages before its final header
inventory; `--describe` reports the policy without downloading it.

Adapters compile as isolated C++ objects with the pinned `__Cr` ABI v2
configuration, both smart-pointer trivial ABIs, pointer compression enabled and
sandbox disabled. Their header paths precede C headers; Zig's automatic system
libc++ definitions must not enter these objects. Matching V8 version/include
bytes alone does not establish C++ calling-ABI compatibility.

The default output root is `runtime/.local/build`. `--output-root /absolute/dir`
supports a consumer-owned directory without modifying a vendored Core checkout.
Generated outputs:

- engine: `<output-root>/runtime/engine/zig-out/bin/lightpanda`;
- standalone renderer: `<output-root>/runtime/renderer/target/release/obscura`;
- build receipt: `<output-root>/receipt.json`.

The builder materializes current owned sources into isolated
`runtime/engine/` and `runtime/renderer/` output trees, preserving their
relative build paths, verifies each copied source, verifies/copies configured
native dependency inputs, and uses a fresh isolated Cargo home with a locally
extracted, checksum-verified registry source directory. Both native targets pass
that Cargo configuration explicitly, overriding ancestor registry rewrites.
The builder never generates a new Cargo lockfile, exports/applies patches,
searches a sibling checkout, or accepts an already-built sibling browser.
Engine and standalone Rust compilation use `--frozen`; lockfiles remain authoritative.
Old output executables are removed before compiling their target. Only the
builder-owned standalone renderer target cache is retained when rematerializing
that tree; Zig caches are under the output root, never in maintained sources.
Source inventory and materialization omit generated `.local`, `.zig-cache`,
`zig-out`, `target`, `zig-pkg`, and `.lp-cache` trees, matching the runtime
output exclusions. Existing compiler outputs must not become source inputs.

`receipt.json` schema 1 has `status: "completed"` only after the compiler
succeeds, its expected output exists, and the Core source fingerprint and root
manifest remain unchanged. It contains `binary` and `artifacts` (absolute output
paths, byte counts and SHA-256), `sources` (Core-relative file inventory and
combined SHA-256), `inputs_lock_sha256`, `core_manifest_sha256` (null if the root
manifest is absent), tool versions/executable hashes, SDK settings identity,
verified dependencies, and executed commands. A failed attempt writes a failed
receipt, not a successful reference to an old executable. A receipt is
compilation evidence, **not** functional, performance, release, or bit-identical
reproducibility acceptance. Consumers must compare its source file hashes and
root manifest identity to their own pinned Core manifest.

## Acquiring or reusing inputs

`inputs.lock.json` is the committed, fixed expected-input inventory, not a
machine configuration and not a generated receipt. Do not rewrite it merely to
accept a failing cache. Pin changes require source/dependency review.

- **Engine V8:** download the exact `v8_archive.url` in the input lock. It is an
  uncompressed static archive. The builder checks its SHA-256 and length before
  compilation. A cache copy is acceptable only if the bytes match.
- **Zig packages:** provide directories named by the 15 `zig_packages[].id`
  values. Root acquisition URLs/hashes are in `engine/build.zig.zon`; transitive
  URLs/hashes are in each package's `build.zig.zon`. `zig fetch --pkg-dir
  <directory> <pinned-url-or-archive>` is the upstream acquisition operation;
  verify its printed Zig package ID. The Core builder additionally verifies the
  complete normalized tree digest, including executable bits and symlink
  targets, before using each copied package tree. This also permits reuse of
  an existing exact cache without trusting its location or directory name.
  The input lock's absent optional SLJIT package is lazy and unused by the
  supported JIT-disabled PCRE2 build. No JIT-enabled variant is offered here.
- **Cargo:** put `<name>-<version>.crate` archives directly in
  `cargo_registry_cache`. A preexisting Cargo cache's registry-specific
  `registry/cache/<registry-id>` directory is suitable. The expected names and
  SHA-256 checksums are in `inputs.lock.json` and the two Cargo lockfiles.
  Archives may also be acquired from
  `https://static.crates.io/crates/<name>/<name>-<version>.crate`. The builder
  does not need or trust a registry index or existing extracted crate source.
  Engine compilation needs 245 registry packages; standalone renderer needs
  478; the union is 570. Each target selects only its locked package set.
- **Standalone Rust V8:** download `rusty_v8_archive.url`, decompress its gzip
  stream to a raw `.a`, and configure that raw archive. The committed SHA-256
  pins the **decompressed** bytes. The V8 crate's generated source bindings are
  included in its checksummed crate archive; no live binding download is used.
- **SDK/toolchains:** install the pinned versions and configure their explicit
  paths. The SDK settings file is content-pinned; tool versions are checked and
  executable digests are recorded. Full compiler-distribution and SDK-header
  inventories are **not** supplied, so this is reproducible-input source and
  dependency binding, not a fully hermetic or bit-reproducible toolchain claim.
  Engine builds pass the selected path as `-Dmacos_sdk`; native C compilation
  uses that SDK as its sysroot and resolves framework/header search paths inside
  it, rather than falling back to headers under `/System/Library`.

The builder copies verified V8/Zig inputs into its output directory. Cargo
archives are verified before extraction and protected by Cargo directory-source
checksums. No dependency cache, native executable, generated V8 snapshot,
compiler target directory, profile, or credential is checked into Core.
Required compile fonts are intentionally checked in: four engine DejaVu fonts,
17 renderer fonts, and three vendored text-engine test fonts. The CJK runtime
font is `assets/fonts/NotoSansCJKsc-Regular.otf`, with its adjacent license.
Runtime fonts are source inputs, not arbitrary installed system-font fallbacks.

Tree digests in the input lock use UTF-8 byte-sorted relative paths and SHA-256
over one compact JSON row plus LF per entry. Regular-file rows are
`[path,"file",sha256,byte_count,executable_mode_bits]`; symlink rows are
`[path,"symlink",target]`. File timestamps and non-executable permission bits
are not inputs.

## Provenance and rights

`source-origin.json` records the original imported file SHA-256 inventory,
upstream identities, current-source status, historical patch/dependency metadata
SHA-256 values, and added font-notice sources. Original digests describe the
import before Core-local build path changes; build receipts describe current
maintained sources. Historical patch hashes are evidence only, not active
build inputs. No private preservation directory is required to build.

- Lightpanda attribution and AGPL text remain in `engine/LICENSE`, source
  headers and `engine/LICENSING.md`. The latter declares AGPL-3.0-only while
  source headers and the Rust render manifest declare AGPL-3.0-or-later.
  This discrepancy is retained, not resolved or relicensed by migration.
- Obscura's Apache-2.0 text remains in `renderer/LICENSE`; vendored
  `cosmic-text`, `taffy`, and `wreq-util` retain their own license files.
- DejaVu/Bitstream/Arev terms remain with the engine fonts and are additionally
  included as `renderer/crates/obscura-render/assets/LICENSE-DEJAVU.txt`.
- The bundled Liberation files identify **1.07.4** in their name records. They
  use the original Liberation license (GPLv2 plus font exceptions), **not**
  the SIL OFL applicable to modern Liberation 2.x. Their original license and
  GPLv2 text are preserved in that renderer assets directory; the original
  license authority is the Fedora URL recorded in `source-origin.json`.
- Noto Color Emoji and the CJK font retain their SIL OFL notices. Vendored
  Noto test-font notices remain in `renderer/vendor/cosmic-text/fonts/`.

No licensing compatibility determination, public binary distribution,
publication, signing, or application-profile migration is performed here.

## Migration status

The maintained source import, locked builder and compact Core test system are
implemented. The integrating run has compiled the migrated engine and exercised
real webpage assertions, exact pixel references, native frame/input identities
and deliberately failing controls. Current source-bound receipts remain local;
this is not full Web conformance, Desktop acceptance, a published release or
proof of two bit-identical clean builds. Run the commands above and the Core
`tests/run.py` entry against the exact source/artifact tuple being qualified.

## Governed native capture

The private macOS broker producer is a qualified `trusted-core-cpu-v1` executor,
not isolation from a compromised native provider. Its synchronous IOSurface
borrow completes only after unlocking and dropping its local surface/right
aliases. Release proof is distinct from pixel qualification: a clean
`borrow-released` result with `capture_stable=false` must be disposed without
publication or input binding, not treated as an unreleased writer.

The borrowed target is premultiplied RGBA8 with physical extent
`ceil(viewport * DPR)`. Its native row stride is a multiple of four, at least
`physical_width * 4`; the exact writable span is `stride * physical_height`
(checked for overflow), not the tight pixel byte count. Broker metadata reports
`stride`, `byte_length` (that span), and `allocation_bytes` (the IOSurface
capacity, which may include an aligned tail). Painting writes directly into
that span, initializes row padding to zero, and never accesses the allocation
tail. The resource owner clears the complete native allocation before its first
export and reuse, so neither padding nor tail can expose previous contents.
Publication/import must compare the actual metadata to the authorized backing.
Resize/DPR changes invalidate capture identity without invalidating old reader
holds; a disconnected/unknown writer stays charged until qualified terminal
evidence. CPU exit does not prove GPU completion. Device loss rejects old-device
admission and keeps outstanding GPU uses until their actual terminal callback;
recovery requires a fresh owner/device generation, not replay of unknown work.

Governed capture/input uses `native-exact-v1`: retained, exact model identities
and saturating visual/canvas epochs bookend painting and authorize one displayed
input batch. Pending observed GPU work settles before the first snapshot;
in-flight or raced writes fail qualification. Guard validation does no paint,
canvas readback or pixel hashing. The legacy raw-frame/fingerprint API and the
resource-v1 ABI are unchanged.

## Governed native heap

Managed native startup binds the application, Rust, Canvas, V8 page/ArrayBuffer,
curl, SQLite and pinned compression/HTTP2 backing hooks to the owner's MGR1
account. The ArrayBuffer maximum is bounded by the admitted owner ceiling.
Replacement allocation is admitted before old backing is freed; failed realloc
retains its original bytes and owner. Brotli uses cleanup-on-OOM, not exit-on-OOM.
Custom compression allocators retain their upstream ownership and cleanup.

Trusted pinned native launches can return whole growth blocks only after actual
backing disposal, using one atomic used/credit state; at most8MiB unused local
credit remains. Baseline still requires authenticated closure and exact process
exit. Default/unqualified endpoints retain exit-only refund. Separate Cookie/
IndexedDB storage grants do not cover those processes' heaps. C++/framework
heaps, stacks and direct mappings remain unqualified; this is not a whole-process
cap.
