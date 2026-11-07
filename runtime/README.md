# Core native Web runtime

Core owns `engine/` (the maintained Lightpanda Zig engine and Rust bridge) and
`renderer/` (the maintained Obscura Rust layout/paint implementation and its
internal path dependencies). These are source trees, not a binary wrapper or a
patch overlay. Desktop is a Host/consumer; independent headless callers use the
same native CDP executable. This migration does not claim broader Web standards
coverage than the imported implementations provide.

## Build

Supported input configuration: **macOS arm64 only**, Zig **0.17.0**, Rust/Cargo
**1.99.1**, Node.js **22 or newer**, and the macOS SDK whose `SDKSettings.json`
is pinned in `inputs.lock.json`. Other platforms need a separately reviewed
input lock and qualification; their upstream source support is not a Core build
support promise.

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

The default output root is `runtime/.local/build`. `--output-root /absolute/dir`
supports a consumer-owned directory without modifying a vendored Core checkout.
Generated outputs:

- engine: `<output-root>/engine/zig-out/bin/lightpanda`;
- standalone renderer: `<output-root>/renderer/target/release/obscura`;
- build receipt: `<output-root>/receipt.json`.

The builder materializes current owned sources into isolated `engine/` and
`renderer/` output trees, verifies each copied source, verifies/copies configured
native dependency inputs, and uses a fresh isolated Cargo home with a locally
extracted, checksum-verified registry source directory. Both native targets pass
that Cargo configuration explicitly, overriding ancestor registry rewrites.
The builder never generates a new Cargo lockfile, exports/applies patches,
searches a sibling checkout, or accepts an already-built sibling browser.
Engine and standalone Rust compilation use `--frozen`; lockfiles remain authoritative.
Old output executables are removed before compiling their target. Only the
builder-owned standalone renderer target cache is retained when rematerializing
that tree; Zig caches are under the output root, never in maintained sources.

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
