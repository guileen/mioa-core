#!/usr/bin/env node
// Core-owned native source build. No patch replay or external executable fallback.
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { createReadStream } from 'node:fs';
import { chmod, copyFile, cp, lstat, mkdir, open, readFile, readdir, readlink, realpath, rm, stat, writeFile } from 'node:fs/promises';
import { dirname, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const runtime = dirname(fileURLToPath(import.meta.url));
const core = dirname(runtime);
const usage = 'Usage: node runtime/build.mjs build --inputs <machine-config.json> [--output-root <directory>]';
const args = process.argv.slice(2);
if (args.shift() !== 'build') throw new Error(usage);
const options = {};
while (args.length) {
  const key = args.shift();
  if (!['--inputs', '--output-root', '--target'].includes(key) || !args.length || options[key]) throw new Error(usage);
  options[key] = args.shift();
}
if (!options['--inputs']) throw new Error(usage);
if (options['--target']) throw new Error(usage);
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
async function sources() {
  const files = [];
  async function visit(directory) {
    for (const name of (await readdir(directory)).sort(byteOrder)) {
      if (name === '.local') continue;
      const path = join(directory, name);
      const metadata = await lstat(path);
      if (metadata.isDirectory()) await visit(path);
      else if (metadata.isFile()) files.push({ path: relative(core, path).split(sep).join('/'), ...await fileInfo(path), executable: metadata.mode & 0o111 });
      else throw new Error(`Source must not contain symlinks or special files: ${path}`);
    }
  }
  await visit(runtime);
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
  const destination = join(output, name);
  await mkdir(destination, { recursive: true });
  for (const entry of await readdir(destination)) {
    await rm(join(destination, entry), { recursive: true, force: true });
  }
  for (const entry of snapshot.files) {
    const prefix = `runtime/${name}/`;
    if (!entry.path.startsWith(prefix)) continue;
    const to = join(destination, entry.path.slice(prefix.length));
    await mkdir(dirname(to), { recursive: true });
    await copyFile(join(core, entry.path), to);
    await chmod(to, 0o644 | entry.executable);
    await verifyFile(to, entry);
  }
}

try {
  if (process.platform !== 'darwin' || process.arch !== 'arm64') throw new Error('Only macOS arm64 is configured and input-pinned by this builder. Other platforms require a separately reviewed input lock.');
  const configPath = resolve(options['--inputs']);
  const config = JSON.parse(await readFile(configPath, 'utf8'));
  const allowed = new Set(['schema', 'zig', 'cargo', 'rustc', 'sdk', 'v8_archive', 'rusty_v8_archive', 'zig_packages', 'cargo_registry_cache', 'jobs']);
  if (config.schema !== 1 || Object.keys(config).some(key => !allowed.has(key))) throw new Error('Unsupported input config schema or key. See runtime/README.md.');
  const required = ['sdk', 'zig', 'v8_archive', 'zig_packages'];
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
  receipt.sources = await sources();
  try { receipt.core_manifest_sha256 = (await fileInfo(join(core, 'MANIFEST.sha256'))).sha256; }
  catch (error) { if (error.code !== 'ENOENT') throw error; receipt.core_manifest_sha256 = null; }
  const env = {
    PATH: [...new Set(['zig'].map(key => dirname(config[key]))), '/usr/bin', '/bin', '/usr/sbin', '/sbin'].join(':'),
    HOME: process.env.HOME ?? '', LANG: 'C', LC_ALL: 'C',
    ...(process.env.TMPDIR ? { TMPDIR: process.env.TMPDIR } : {}),
    SDKROOT: config.sdk,
    ZIG_GLOBAL_CACHE_DIR: join(output, 'zig-global-cache'),
    ZIG_LOCAL_CACHE_DIR: join(output, 'zig-local-cache'),
    ZIG_LOCAL_PKG_DIR: join(output, 'zig-pkg'),
  };
  receipt.tools = { node: { version: process.version, path: process.execPath, ...await fileInfo(await realpath(process.execPath)) } };
  for (const name of ['zig']) {
    const version = execute(config[name], [name === 'zig' ? 'version' : '--version'], output, env, true);
    if (version !== lock.tools[name]) throw new Error(`Pinned ${name} version mismatch: ${version}`);
    receipt.tools[name] = { path: config[name], version, ...await fileInfo(await realpath(config[name])) };
  }
  receipt.sdk = { path: config.sdk, settings: await verifyFile(join(config.sdk, lock.sdk_settings.path), lock.sdk_settings) };
  await materialize('engine', receipt.sources);
  const inputDirectory = join(output, 'inputs');
  await mkdir(inputDirectory, { recursive: true });
  {
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
  receipt.cargo_crates = [];
  receipt.artifacts = {};
  {
    const binary = join(output, 'engine/zig-out/bin/lightpanda');
    await rm(binary, { force: true });
    execute(config.zig, ['build', '-Doptimize=fast', '-Ddev_fast=false', '-Dshared_v8=false', `-Dprebuilt_v8_path=${receipt.v8_archive.path}`, `-j${jobs}`, '--cache-dir', env.ZIG_LOCAL_CACHE_DIR], join(output, 'engine'), env);
    receipt.artifacts.engine = { path: binary, ...await fileInfo(binary) };
    receipt.binary = receipt.artifacts.engine;
  }
  const after = await sources();
  if (after.sha256 !== receipt.sources.sha256) throw new Error('Core source inputs changed during compilation; no completed receipt is issued.');
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
