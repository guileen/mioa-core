#!/usr/bin/env node
// Core-owned native source build. No patch replay or external executable fallback.
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { createReadStream } from 'node:fs';
import { chmod, copyFile, cp, lstat, mkdir, open, readFile, readdir, readlink, realpath, rm, stat, writeFile } from 'node:fs/promises';
import { dirname, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { verifyV8CppAbi, V8_CPP_ABI_POLICY } from './v8_abi.mjs';

const runtime = dirname(fileURLToPath(import.meta.url));
const core = dirname(runtime);
const usage = 'Usage: node runtime/build.mjs build --inputs <machine-config.json> [--output-root <directory>] [--target engine|renderer|all] [--entities-provider zig|rust] [--png-provider zig|rust] [--v8-headers <directory>] | format --inputs <machine-config.json> --file runtime/engine/src/browser/StyleManager.zig';
const args = process.argv.slice(2);
const action = args.shift();
if (!['build', 'format'].includes(action)) throw new Error(usage);
const options = {};
const allowedOptions = action === 'format'
  ? ['--inputs', '--file']
  : ['--inputs', '--output-root', '--target', '--entities-provider', '--png-provider', '--v8-headers'];
while (args.length) {
  const key = args.shift();
  if (!allowedOptions.includes(key) || !args.length || options[key]) throw new Error(usage);
  options[key] = args.shift();
}
if (!options['--inputs']) throw new Error(usage);
if (action === 'format') {
  if (options['--file'] !== 'runtime/engine/src/browser/StyleManager.zig') throw new Error(usage);
  const configPath = resolve(options['--inputs']);
  const config = JSON.parse(await readFile(configPath, 'utf8'));
  if (config.schema !== 1 || typeof config.zig !== 'string' || !config.zig) throw new Error('Format requires a schema-1 input config with a pinned Zig path.');
  const lock = JSON.parse(await readFile(join(runtime, 'inputs.lock.json'), 'utf8'));
  const zig = resolve(dirname(configPath), config.zig);
  const version = spawnSync(zig, ['version'], { cwd: runtime, env: process.env, encoding: 'utf8' });
  if (version.error || version.status !== 0 || version.stdout.trim() !== lock.tools.zig) throw new Error('Pinned Zig version mismatch; formatter was not run.', { cause: version.error });
  const source = join(runtime, 'engine/src/browser/StyleManager.zig');
  if (await realpath(source) !== source) throw new Error('Formatter source must be a real file path.');
  const formatted = spawnSync(zig, ['fmt', source], { cwd: runtime, stdio: 'inherit' });
  if (formatted.error || formatted.status !== 0) throw new Error('Pinned Zig formatter failed.', { cause: formatted.error });
  console.log(JSON.stringify({ status: 'formatted', file: source, zig: version.stdout.trim() }));
  process.exit(0);
}
const target = options['--target'] ?? 'engine';
if (!['engine', 'renderer', 'all'].includes(target)) throw new Error(usage);
const targets = target === 'all' ? ['engine', 'renderer'] : [target];
const nativeProviders = { entities: options['--entities-provider'] ?? 'zig', png: options['--png-provider'] ?? 'zig' };
if (Object.values(nativeProviders).some(value => !['zig', 'rust'].includes(value))) throw new Error('Native providers must be zig or rust.');
if (!targets.includes('engine') && (options['--entities-provider'] || options['--png-provider'])) throw new Error('Native provider selection applies to the engine target only.');
const output = resolve(options['--output-root'] ?? join(runtime, '.local/build'));
const within = (parent, child) => child === parent || child.startsWith(parent + sep);
if (within(core, output) && !within(join(runtime, '.local'), output)) {
  throw new Error('Build output inside Core must be under runtime/.local; use an external --output-root otherwise.');
}
await mkdir(output, { recursive: true });
if (await realpath(output) !== output) throw new Error('Output root must be a real path, not a symlink alias.');
const guardPath = join(output, '.build-lock');
const guard = await open(guardPath, 'wx').catch(error => {
  throw new Error(`Cannot acquire ${guardPath}: another build may own it. After a terminated build, remove only this lock once its process has stopped.`, { cause: error });
});
const receipt = { schema: 1, status: 'started', target, started_utc: new Date().toISOString(), commands: [], qualification: 'Source-bound compilation only; functionality, release acceptance and bit-identical reproducibility are separate.' };
receipt.native_providers = {
  ...(targets.includes('engine') ? { engine: { ...nativeProviders, parser: 'cpp-bridge', rendering: 'cpp-bridge' } } : {}),
  ...(targets.includes('renderer') ? { renderer: 'rust' } : {}),
};
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const json = value => JSON.stringify(value, null, 2) + '\n';
const byteOrder = (a, b) => Buffer.compare(Buffer.from(a), Buffer.from(b));

async function fileInfo(path) {
  const metadata = await stat(path);
  if (!metadata.isFile()) throw new Error(`Expected regular file: ${path}`);
  const digest = createHash('sha256');
  for await (const bytes of createReadStream(path)) digest.update(bytes);
  return { sha256: digest.digest('hex'), bytes: metadata.size };
}
async function verifyFile(path, expected) {
  const actual = await fileInfo(path);
  if (actual.sha256 !== expected.sha256 || (expected.bytes !== undefined && actual.bytes !== expected.bytes)) {
    throw new Error(`Pinned input content mismatch: ${path}`);
  }
  return actual;
}
// Digest format: UTF-8 byte-sorted relative paths, one compact JSON row + LF.
// Files: [path,"file",sha256,size,executable-mode-bits]; links: [path,"symlink",target].
async function treeRows(base) {
  const rows = [];
  async function visit(directory) {
    for (const name of (await readdir(directory)).sort(byteOrder)) {
      const path = join(directory, name);
      const metadata = await lstat(path);
      const rel = relative(base, path).split(sep).join('/');
      if (metadata.isSymbolicLink()) rows.push([rel, 'symlink', await readlink(path)]);
      else if (metadata.isDirectory()) await visit(path);
      else if (metadata.isFile()) {
        const info = await fileInfo(path);
        rows.push([rel, 'file', info.sha256, info.bytes, metadata.mode & 0o111]);
      } else throw new Error(`Unsupported input kind: ${path}`);
    }
  }
  await visit(base);
  return rows.sort((a, b) => byteOrder(a[0], b[0]));
}
function treeDigest(rows) {
  const digest = createHash('sha256');
  for (const row of rows) digest.update(JSON.stringify(row) + '\n');
  return digest.digest('hex');
}
async function verifyTree(path, expected) {
  const rows = await treeRows(path);
  if (rows.length !== expected.files || treeDigest(rows) !== expected.sha256) throw new Error(`Pinned dependency tree mismatch: ${path}`);
  return rows;
}
const sourceOutputDirectories = new Set(['.local', '.zig-cache', 'zig-out', 'target', 'zig-pkg', '.lp-cache']);
async function sources(includeOs = false) {
  const files = [];
  async function visit(directory) {
    for (const name of (await readdir(directory)).sort(byteOrder)) {
      if (sourceOutputDirectories.has(name)) continue;
      const path = join(directory, name);
      const metadata = await lstat(path);
      if (metadata.isDirectory()) await visit(path);
      else if (metadata.isFile()) files.push({ path: relative(core, path).split(sep).join('/'), ...await fileInfo(path), executable: metadata.mode & 0o111 });
      else throw new Error(`Source must not contain symlinks or special files: ${path}`);
    }
  }
  await visit(runtime);
  if (includeOs) {
    for (const path of ['mioa-os/app_data.c', 'mioa-os/api/system/app_data_v1.h', 'mioa-os/environment.c', 'mioa-os/api/system/environment_v1.h', 'mioa-os/executable_path.c', 'mioa-os/api/system/executable_path_v1.h', 'mioa-os/process.zig', 'mioa-os/api/system/process_v1.h', 'mioa-os/terminal.c', 'mioa-os/api/system/terminal_v1.h', 'mioa-os/thread.zig', 'mioa-os/api/system/thread_v1.h', 'mioa-os/files.c', 'mioa-os/files.h', 'mioa-os/api/system/graphics_file_v1.h', 'mioa-os/broker.c', 'mioa-os/api/system/broker_v1.h', 'mioa-os/clock.c', 'mioa-os/api/system/clock_v1.h', 'mioa-os/random.c', 'mioa-os/api/system/random_v1.h']) {
      const fullPath = join(core, path);
      const metadata = await lstat(fullPath);
      if (!metadata.isFile()) throw new Error(`Source must be a regular file: ${fullPath}`);
      files.push({ path, ...await fileInfo(fullPath), executable: metadata.mode & 0o111 });
    }
  }
  files.sort((a, b) => byteOrder(a.path, b.path));
  return { sha256: hash(JSON.stringify(files)), files };
}
function execute(program, argv, cwd, env, capture = false) {
  const started = performance.now();
  const result = spawnSync(program, argv, { cwd, env, stdio: capture ? ['ignore', 'pipe', 'pipe'] : 'inherit', encoding: 'utf8', maxBuffer: 16 * 1024 * 1024 });
  receipt.commands.push({ program, args: argv, cwd, status: result.status, signal: result.signal, elapsed_ms: performance.now() - started });
  if (result.error || result.status !== 0) throw new Error(`Command failed: ${program} ${argv.join(' ')}${capture ? `\n${result.stderr ?? ''}` : ''}`, { cause: result.error });
  return result.stdout?.trim();
}
async function materialize(name, snapshot) {
  const destination = join(output, 'runtime', name);
  await mkdir(destination, { recursive: true });
  // Preserve only our standalone renderer's compiler target cache, never sources.
  for (const entry of await readdir(destination)) {
    if (name === 'renderer' && entry === 'target') continue;
    await rm(join(destination, entry), { recursive: true, force: true });
  }
  for (const entry of snapshot.files) {
    const prefix = `runtime/${name}/`;
    const localPath = entry.path.startsWith(prefix)
      ? entry.path.slice(prefix.length)
      : name === 'engine' && entry.path.startsWith('mioa-os/')
        ? entry.path
        : null;
    if (localPath === null) continue;
    const to = name === 'engine' && entry.path.startsWith('mioa-os/')
      ? join(output, entry.path)
      : join(destination, localPath);
    await mkdir(dirname(to), { recursive: true });
    await copyFile(join(core, entry.path), to);
    await chmod(to, 0o644 | entry.executable);
    await verifyFile(to, entry);
  }
}

try {
  if (options['--v8-headers']) {
    if (!targets.includes('engine')) throw new Error('V8 headers apply to the engine target only.');
    const path = resolve(options['--v8-headers']);
    if (await realpath(path) !== path) throw new Error('V8 headers must be a real directory path.');
    const cppAbi = await verifyV8CppAbi(path);
    const rows = await treeRows(path);
    if (!rows.some(row => row[0] === 'v8.h' && row[1] === 'file') || rows.some(row => row[1] !== 'file')) throw new Error('Invalid V8 header tree.');
    receipt.v8_headers = { path, commit: V8_CPP_ABI_POLICY.v8.revision, sha256: treeDigest(rows), files: rows.length, cpp_abi: cppAbi };
  }
  if (process.platform !== 'darwin' || process.arch !== 'arm64') throw new Error('Only macOS arm64 is configured and input-pinned by this builder. Other platforms require a separately reviewed input lock.');
  const configPath = resolve(options['--inputs']);
  const config = JSON.parse(await readFile(configPath, 'utf8'));
  const allowed = new Set(['schema', 'zig', 'cargo', 'rustc', 'sdk', 'v8_archive', 'rusty_v8_archive', 'zig_packages', 'cargo_registry_cache', 'jobs']);
  if (config.schema !== 1 || Object.keys(config).some(key => !allowed.has(key))) throw new Error('Unsupported input config schema or key. See runtime/README.md.');
  const required = ['cargo', 'rustc', 'sdk', 'cargo_registry_cache', ...(targets.includes('engine') ? ['zig', 'v8_archive', 'zig_packages'] : []), ...(targets.includes('renderer') ? ['rusty_v8_archive'] : [])];
  for (const key of required) {
    if (typeof config[key] !== 'string' || !config[key]) throw new Error(`Input config requires ${key}`);
    config[key] = resolve(dirname(configPath), config[key]);
  }
  const jobs = config.jobs ?? 4;
  if (!Number.isSafeInteger(jobs) || jobs < 1 || jobs > 64) throw new Error('jobs must be an integer from 1 to 64.');
  const lockBytes = await readFile(join(runtime, 'inputs.lock.json'));
  const lock = JSON.parse(lockBytes);
  if (lock.schema !== 1) throw new Error('Unsupported native input lock schema.');
  for (const file of lock.dependency_files) await verifyFile(join(runtime, file.path), file);
  if (Number(process.versions.node.split('.')[0]) < lock.tools.node_minimum_major) throw new Error('Node.js 22 or newer is required.');
  receipt.inputs_lock_sha256 = hash(lockBytes);
  receipt.sources = await sources(targets.includes('engine'));
  try { receipt.core_manifest_sha256 = (await fileInfo(join(core, 'MANIFEST.sha256'))).sha256; }
  catch (error) { if (error.code !== 'ENOENT') throw error; receipt.core_manifest_sha256 = null; }
  const cargoHome = join(output, 'cargo-home');
  // Separate credentials; the explicit --config also overrides ancestor registry rewrites.
  await rm(cargoHome, { recursive: true, force: true });
  await mkdir(cargoHome, { recursive: true });
  const env = {
    PATH: [...new Set(required.filter(key => ['zig', 'cargo', 'rustc'].includes(key)).map(key => dirname(config[key]))) , '/usr/bin', '/bin', '/usr/sbin', '/sbin'].join(':'),
    HOME: process.env.HOME ?? '', LANG: 'C', LC_ALL: 'C',
    ...(process.env.TMPDIR ? { TMPDIR: process.env.TMPDIR } : {}),
    SDKROOT: config.sdk, RUSTC: config.rustc, CARGO_HOME: cargoHome,
    CARGO_NET_OFFLINE: 'true', CARGO_INCREMENTAL: '0', CARGO_BUILD_JOBS: String(jobs),
    ZIG_GLOBAL_CACHE_DIR: join(output, 'zig-global-cache'),
    OBSCURA_VERSION: `0.1.0-mioa+${receipt.sources.sha256.slice(0, 12)}`,
    ZIG_LOCAL_CACHE_DIR: join(output, 'zig-local-cache'),
    ZIG_LOCAL_PKG_DIR: join(output, 'zig-pkg'),
  };
  receipt.tools = { node: { version: process.version, path: process.execPath, ...await fileInfo(await realpath(process.execPath)) } };
  for (const name of ['cargo', 'rustc', ...(targets.includes('engine') ? ['zig'] : [])]) {
    const version = execute(config[name], [name === 'zig' ? 'version' : '--version'], output, env, true);
    if (version !== lock.tools[name]) throw new Error(`Pinned ${name} version mismatch: ${version}`);
    receipt.tools[name] = { path: config[name], version, ...await fileInfo(await realpath(config[name])) };
  }
  receipt.sdk = { path: config.sdk, settings: await verifyFile(join(config.sdk, lock.sdk_settings.path), lock.sdk_settings) };
  if (targets.includes('engine')) await materialize('engine', receipt.sources);
  await materialize('renderer', receipt.sources);
  const inputDirectory = join(output, 'inputs');
  await mkdir(inputDirectory, { recursive: true });
  if (targets.includes('engine')) {
    // Copy then verify: compiler inputs cannot race mutations in an external cache.
    const archive = join(inputDirectory, 'libc_v8.a');
    await copyFile(config.v8_archive, archive);
    await verifyFile(archive, lock.v8_archive);
    receipt.v8_archive = { ...lock.v8_archive, path: archive };
    await rm(env.ZIG_LOCAL_PKG_DIR, { recursive: true, force: true });
    await mkdir(env.ZIG_LOCAL_PKG_DIR, { recursive: true });
    for (const pkg of lock.zig_packages) {
      const destination = join(env.ZIG_LOCAL_PKG_DIR, pkg.id);
      await cp(join(config.zig_packages, pkg.id), destination, { recursive: true, dereference: false, verbatimSymlinks: true });
      await verifyTree(destination, pkg);
    }
    receipt.zig_packages = lock.zig_packages;
  }
  if (targets.includes('renderer')) {
    const archive = join(inputDirectory, 'librusty_v8.a');
    await copyFile(config.rusty_v8_archive, archive);
    await verifyFile(archive, lock.rusty_v8_archive);
    env.RUSTY_V8_ARCHIVE = archive;
    receipt.rusty_v8_archive = { ...lock.rusty_v8_archive, path: archive };
  }
  // Cargo directory sources need no network or registry index. Each .crate is
  // checked against the committed Cargo.lock checksum before extraction.
  const vendor = join(output, 'cargo-vendor');
  await rm(vendor, { recursive: true, force: true });
  await mkdir(vendor, { recursive: true });
  receipt.cargo_crates = [];
  for (const pkg of lock.cargo_crates.filter(pkg => pkg.targets.some(name => targets.includes(name)))) {
    const name = `${pkg.name}-${pkg.version}`;
    const archive = join(inputDirectory, 'crate.tar.gz');
    await copyFile(join(config.cargo_registry_cache, `${name}.crate`), archive);
    await verifyFile(archive, pkg);
    execute('/usr/bin/tar', ['-xzf', archive, '-C', vendor], output, env);
    const directory = join(vendor, name);
    const checksums = {};
    for (const row of await treeRows(directory)) {
      if (row[1] !== 'file') throw new Error(`Registry package contains a non-regular file: ${name}/${row[0]}`);
      if (row[0] !== '.cargo-checksum.json') checksums[row[0]] = row[2];
    }
    await writeFile(join(directory, '.cargo-checksum.json'), json({ files: checksums, package: pkg.sha256 }));
    receipt.cargo_crates.push({ name: pkg.name, version: pkg.version, sha256: pkg.sha256 });
  }
  await rm(join(inputDirectory, 'crate.tar.gz'), { force: true });
  const cargoConfig = join(cargoHome, 'config.toml');
  await writeFile(cargoConfig, `[net]\noffline = true\n[source.crates-io]\nreplace-with = "core-verified-vendor"\n[source.core-verified-vendor]\ndirectory = ${JSON.stringify(vendor)}\n`);
  receipt.artifacts = {};
  if (targets.includes('engine')) {
    const binary = join(output, 'runtime/engine/zig-out/bin/lightpanda');
    await rm(binary, { force: true });
    execute(config.zig, ['build', '-Doptimize=fast', '-Ddev_fast=false', '-Dshared_v8=false', `-Dmacos_sdk=${config.sdk}`, `-Dprebuilt_v8_path=${receipt.v8_archive.path}`, `-Dentities_provider=${nativeProviders.entities}`, `-Dpng_provider=${nativeProviders.png}`, ...(receipt.v8_headers ? [`-Dv8-headers=${receipt.v8_headers.path}`] : []), `-j${jobs}`, '--cache-dir', env.ZIG_LOCAL_CACHE_DIR], join(output, 'runtime/engine'), env);
    if (receipt.v8_headers) await verifyTree(receipt.v8_headers.path, receipt.v8_headers);
    receipt.artifacts.engine = { path: binary, ...await fileInfo(binary) };
    receipt.binary = receipt.artifacts.engine;
  }
  if (targets.includes('renderer')) {
    const binary = join(output, 'runtime/renderer/target/release/obscura');
    await rm(binary, { force: true });
    execute(config.cargo, ['--config', cargoConfig, 'build', '--frozen', '--release', '-p', 'obscura-cli', '--features', 'render', '--bin', 'obscura', '--target-dir', join(output, 'runtime/renderer/target')], join(output, 'runtime/renderer'), env);
    receipt.artifacts.renderer = { path: binary, ...await fileInfo(binary) };
    if (!receipt.binary) receipt.binary = receipt.artifacts.renderer;
  }
  const after = await sources(targets.includes('engine'));
  if (after.sha256 !== receipt.sources.sha256) {
    const beforeRows = new Map(receipt.sources.files.map(row => [row.path, JSON.stringify(row)]));
    const afterRows = new Map(after.files.map(row => [row.path, JSON.stringify(row)]));
    const changed = [...new Set([...beforeRows.keys(), ...afterRows.keys()])]
      .filter(path => beforeRows.get(path) !== afterRows.get(path))
      .sort(byteOrder);
    throw new Error(`Core source inputs changed during compilation: ${changed.join(', ') || 'unknown paths'} (${receipt.sources.sha256} -> ${after.sha256}).`);
  }
  if (receipt.core_manifest_sha256 !== null && (await fileInfo(join(core, 'MANIFEST.sha256'))).sha256 !== receipt.core_manifest_sha256) throw new Error('Core MANIFEST.sha256 changed during compilation.');
  receipt.status = 'completed';
} catch (error) {
  receipt.status = 'failed';
  receipt.error = error.stack ?? String(error);
  process.exitCode = 1;
} finally {
  receipt.finished_utc = new Date().toISOString();
  try {
    await writeFile(join(output, 'receipt.json'), json(receipt));
    console.log(JSON.stringify({ status: receipt.status, receipt: join(output, 'receipt.json'), binary: receipt.binary ?? null }));
  } finally {
    await guard.close();
    await rm(guardPath, { force: true });
  }
}
