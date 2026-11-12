// Exact Chromium C++ headers used by the pinned V8 archive; never system libc++.
import { createHash } from 'node:crypto';
import { copyFile, lstat, mkdir, mkdtemp, readFile, readdir, realpath, rename, rm, writeFile, chmod } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { gunzipSync } from 'node:zlib';

const chromium = 'https://chromium.googlesource.com/';
const buildtools = '6f6a5dbf04b734214f3b1f386567d101ec9d607e';
const libcxx = '97b436da4c33663581d394f4ee0a5977fc38c2f4';
const libcxxabi = 'fc1897a2c12aa27e703c3ed48b62eba8abf4ce19';
function freeze(value) { for (const child of Object.values(value)) if (child && typeof child === 'object') freeze(child); return Object.freeze(value); }
export const V8_CPP_ABI_POLICY = freeze({
  schema: 1,
  v8: { version: '15.5.35.13', revision: 'cbd2e792be81ec6eec6a20408817c7cd62f4d9fa', include_tree: '747c7da600d8f7d15370a00e7995b5d0f9a8d9f3', archive_sha256: '99dfb9717dc0ee351f1c8ecd9abc86f6316ba523de1a7ad82323d63e95d330ba', source: `${chromium}v8/v8/+archive/cbd2e792be81ec6eec6a20408817c7cd62f4d9fa/include.tar.gz` },
  trees: [
    { path: 'libcxx/include', revision: libcxx, git_tree: 'f1dcb74c194cf37c2cf61f334c53919809c871ff', source: `${chromium}external/github.com/llvm/llvm-project/libcxx.git/+archive/${libcxx}/include.tar.gz` },
    { path: 'libcxxabi/include', revision: libcxxabi, git_tree: '8b77c5f63de4b8a8754627683db36ab4c5149613', source: `${chromium}external/github.com/llvm/llvm-project/libcxxabi.git/+archive/${libcxxabi}/include.tar.gz` },
  ],
  files: [
    { path: 'config/__config_site', revision: buildtools, git_blob: 'a9ab1614b67e2df7b379c1d8a4147265a15747db', source: `${chromium}chromium/src/buildtools/+/${buildtools}/third_party/libc++/__config_site?format=TEXT` },
    { path: 'config/__assertion_handler', revision: buildtools, git_blob: '0f60c0290a3135ac613103e99f77c7e66e24ba2e', source: `${chromium}chromium/src/buildtools/+/${buildtools}/third_party/libc++/__assertion_handler?format=TEXT` },
  ],
  compiler: { standard: 'c++23', namespace: '__Cr', abi_version: 2, defines: ['V8_COMPRESS_POINTERS', '_LIBCPP_DISABLE_VISIBILITY_ANNOTATIONS', '_LIBCXXABI_DISABLE_VISIBILITY_ANNOTATIONS', '_LIBCPP_INSTRUMENTED_WITH_ASAN=0', `CR_LIBCXX_REVISION=${libcxx}`, '_LIBCPP_HARDENING_MODE=_LIBCPP_HARDENING_MODE_EXTENSIVE'], flags: ['-nostdinc++', '-fno-rtti', '-fno-exceptions'] },
});
const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');
export const V8_CPP_ABI_POLICY_SHA256 = sha256(JSON.stringify(V8_CPP_ABI_POLICY));
const gitObject = (kind, bytes) => createHash('sha1').update(`${kind} ${bytes.length}\0`).update(bytes).digest('hex');
const compare = (a, b) => Buffer.compare(Buffer.from(a), Buffer.from(b));
const missing = error => error.code === 'ENOENT';
async function exists(path) { try { await lstat(path); return true; } catch (error) { if (missing(error)) return false; throw error; } }
async function directory(path) { if (!(await lstat(path)).isDirectory()) throw new Error(`Expected real directory, not a symlink: ${path}`); }
async function file(path) { const info = await lstat(path); if (!info.isFile()) throw new Error(`Expected regular file, not a symlink: ${path}`); return { info, bytes: await readFile(path) }; }

// Git tree hashing authenticates every name, byte and executable bit, not tar metadata.
async function tree(path, omitted = new Set()) {
  await directory(path);
  const rows = [];
  async function visit(base, prefix) {
    const entries = [];
    for (const name of await readdir(base)) {
      if (!prefix && omitted.has(name)) continue;
      const target = join(base, name), rel = prefix ? `${prefix}/${name}` : name;
      const info = await lstat(target);
      if (info.isDirectory()) entries.push({ name, mode: '40000', id: await visit(target, rel), sort: name + '/' });
      else if (info.isFile()) {
        const bytes = await readFile(target), mode = info.mode & 0o111 ? '100755' : '100644';
        entries.push({ name, mode, id: gitObject('blob', bytes), sort: name });
        rows.push([rel, sha256(bytes), bytes.length, mode]);
      } else throw new Error(`V8 ABI input must not contain links or special files: ${target}`);
    }
    entries.sort((a, b) => compare(a.sort, b.sort));
    return gitObject('tree', Buffer.concat(entries.map(entry => Buffer.concat([Buffer.from(`${entry.mode} ${entry.name}\0`), Buffer.from(entry.id, 'hex')]))));
  }
  const git_tree = await visit(path, '');
  rows.sort((a, b) => compare(a[0], b[0]));
  return { git_tree, content_sha256: sha256(JSON.stringify(rows)), files: rows.length };
}
async function expectTree(path, expected) {
  const actual = await tree(path);
  if (actual.git_tree !== expected) throw new Error(`V8 C++ ABI tree mismatch: ${path}; expected Git tree ${expected}, got ${actual.git_tree}. Remove only this owned derived input and prepare it again.`);
  return actual;
}
async function expectBlob(path, expected) {
  const { info, bytes } = await file(path), actual = gitObject('blob', bytes);
  if (actual !== expected || info.mode & 0o111) throw new Error(`V8 C++ ABI file mismatch: ${path}; expected non-executable Git blob ${expected}, got ${actual}.`);
  return { git_blob: actual, sha256: sha256(bytes), bytes: bytes.length };
}
async function verifyHeaders(root) {
  await directory(root);
  const actual = await tree(root, new Set(['cpp-abi']));
  if (actual.git_tree !== V8_CPP_ABI_POLICY.v8.include_tree) throw new Error(`V8 header root ${root} does not match ${V8_CPP_ABI_POLICY.v8.revision}: expected Git tree ${V8_CPP_ABI_POLICY.v8.include_tree}, got ${actual.git_tree}.`);
  return actual;
}
async function verifyAbi(base) {
  await directory(base);
  // Exact shape: no extra/unpinned inputs, including empty directories.
  const expected = new Map([['', ['config', 'libcxx', 'libcxxabi']], ['config', ['__assertion_handler', '__config_site']], ['libcxx', ['include']], ['libcxxabi', ['include']]]);
  for (const [rel, names] of expected) {
    const path = join(base, rel);
    await directory(path);
    if (JSON.stringify((await readdir(path)).sort(compare)) !== JSON.stringify(names.sort(compare))) throw new Error(`Unexpected V8 C++ ABI staging contents: ${path}`);
  }
  const trees = [], files = [];
  for (const input of V8_CPP_ABI_POLICY.trees) trees.push({ ...input, ...await expectTree(join(base, input.path), input.git_tree) });
  for (const input of V8_CPP_ABI_POLICY.files) files.push({ ...input, ...await expectBlob(join(base, input.path), input.git_blob) });
  return { policy_sha256: V8_CPP_ABI_POLICY_SHA256, policy: V8_CPP_ABI_POLICY, content_sha256: (await tree(base)).content_sha256, trees, files };
}
export async function verifyV8CppAbi(root) {
  root = resolve(root);
  try { const headers = await verifyHeaders(root); return { ...await verifyAbi(join(root, 'cpp-abi')), v8_headers: headers }; }
  catch (error) { throw new Error(`Cannot use V8 headers at ${root}: ${error.message} Prepare exact private inputs with prepareV8CppAbi(root, cache). No system C++ header fallback is allowed.`, { cause: error }); }
}

function tarString(bytes) { const end = bytes.indexOf(0); return bytes.subarray(0, end < 0 ? bytes.length : end).toString('utf8'); }
function tarNumber(bytes) { const text = tarString(bytes).trim(); if (!/^[0-7]+$/.test(text)) throw new Error('Invalid tar numeric field'); return parseInt(text, 8); }
function pax(bytes) {
  const fields = {};
  for (let offset = 0; offset < bytes.length;) {
    const space = bytes.indexOf(32, offset), lengthText = bytes.subarray(offset, space).toString();
    if (space < offset || !/^[1-9][0-9]*$/.test(lengthText)) throw new Error('Invalid tar PAX record');
    const length = Number(lengthText), end = offset + length;
    if (!Number.isSafeInteger(length) || end > bytes.length || end <= space + 1 || bytes[end - 1] !== 10) throw new Error('Invalid tar PAX length');
    const record = bytes.subarray(space + 1, end - 1).toString('utf8'), equals = record.indexOf('=');
    if (equals < 1) throw new Error('Invalid tar PAX field');
    const key = record.slice(0, equals);
    if (!['path', 'mtime', 'atime', 'ctime'].includes(key)) throw new Error(`Unsupported tar PAX field: ${key}`);
    fields[key] = record.slice(equals + 1); offset = end;
  }
  return fields;
}
// Extract only authenticated regular-file trees, never delegate archive paths to tar.
async function extract(bytes, destination) {
  const tar = gunzipSync(bytes, { maxOutputLength: 128 * 1024 * 1024 });
  let offset = 0, extended = null;
  const seen = new Set();
  while (offset + 512 <= tar.length) {
    const header = tar.subarray(offset, offset + 512);
    if (header.every(byte => byte === 0)) {
      if (extended || !tar.subarray(offset).every(byte => byte === 0)) throw new Error('Invalid tar terminator');
      return;
    }
    const checksum = tarNumber(header.subarray(148, 156));
    let sum = 0; for (let i = 0; i < 512; i++) sum += i >= 148 && i < 156 ? 32 : header[i];
    if (sum !== checksum) throw new Error('Tar checksum mismatch');
    const size = tarNumber(header.subarray(124, 136)), type = String.fromCharCode(header[156]);
    const start = offset + 512, end = start + size;
    if (!Number.isSafeInteger(size) || end > tar.length) throw new Error('Truncated tar entry');
    offset = start + Math.ceil(size / 512) * 512;
    if (type === 'x') { if (extended) throw new Error('Repeated tar PAX header'); extended = pax(tar.subarray(start, end)); continue; }
    const prefix = tarString(header.subarray(345, 500));
    let name = extended?.path ?? `${prefix ? prefix + '/' : ''}${tarString(header.subarray(0, 100))}`;
    extended = null;
    if (type === '5') name = name.replace(/\/$/, '');
    if (!name || name.includes('\0') || name.includes('\\') || name.split('/').some(part => !part || part === '.' || part === '..') || name.startsWith('/') || seen.has(name)) throw new Error(`Unsafe or duplicate tar path: ${name}`);
    seen.add(name);
    const target = join(destination, name);
    if (type === '5' && size === 0) await mkdir(target, { recursive: true, mode: 0o700 });
    else if (type === '0' || type === '\0') {
      await mkdir(dirname(target), { recursive: true, mode: 0o700 });
      await writeFile(target, tar.subarray(start, end), { flag: 'wx', mode: 0o644 | (tarNumber(header.subarray(100, 108)) & 0o111) });
    } else throw new Error(`Unsupported tar entry kind ${type}: ${name}; links are forbidden.`);
  }
  throw new Error('Missing tar terminator');
}
async function download(source) {
  const response = await fetch(source, { signal: AbortSignal.timeout(120000) });
  if (!response.ok) throw new Error(`HTTP ${response.status} downloading ${source}`);
  return Buffer.from(await response.arrayBuffer());
}
async function privateCache(path) {
  path = resolve(path);
  await mkdir(path, { recursive: true, mode: 0o700 });
  const actual = await realpath(path), info = await lstat(path);
  if (!info.isDirectory() || (process.getuid && info.uid !== process.getuid()) || info.mode & 0o077) throw new Error(`V8 ABI cache must be an owned private directory (mode 0700), not a symlink: ${path}`);
  return actual;
}
async function cached(input, cache) {
  const id = input.git_tree ?? input.git_blob, target = join(cache, id);
  const verify = path => input.git_tree ? expectTree(path, id) : expectBlob(path, id);
  if (await exists(target)) { await verify(target); return target; }
  const staging = await mkdtemp(join(cache, '.download-'));
  try {
    const output = join(staging, 'input'), bytes = await download(input.source);
    if (input.git_tree) { await mkdir(output); await extract(bytes, output); }
    else {
      const text = bytes.toString('ascii').trim();
      if (!/^[A-Za-z0-9+/]*={0,2}$/.test(text) || text.length % 4) throw new Error(`Invalid Gitiles base64: ${input.source}`);
      await writeFile(output, Buffer.from(text, 'base64'), { flag: 'wx', mode: 0o644 });
    }
    await verify(output);
    try { await rename(output, target); }
    catch (error) { if (!['EEXIST', 'ENOTEMPTY'].includes(error.code)) throw error; }
    await verify(target);
    return target;
  } catch (error) { throw new Error(`Cannot prepare pinned V8 C++ ABI input ${input.path} from ${input.source}: ${error.message}. Verified inputs in ${cache} can be reused offline.`, { cause: error }); }
  finally { await rm(staging, { recursive: true, force: true }); }
}
async function copyTree(source, destination) {
  await directory(source);
  await mkdir(destination, { mode: 0o700 });
  for (const name of await readdir(source)) {
    const from = join(source, name), to = join(destination, name), info = await lstat(from);
    if (info.isDirectory()) await copyTree(from, to);
    else if (info.isFile()) { await copyFile(from, to); await chmod(to, 0o644 | (info.mode & 0o111)); }
    else throw new Error(`Unsafe cached V8 ABI entry: ${from}`);
  }
}
export async function prepareV8CppAbi(root, cache) {
  root = resolve(root);
  await verifyHeaders(root);
  const destination = join(root, 'cpp-abi');
  if (await exists(destination)) return verifyV8CppAbi(root);
  cache = await privateCache(cache ?? join(dirname(root), '.v8-cpp-abi-cache'));
  const staging = await mkdtemp(join(cache, '.stage-'));
  try {
    const abi = join(staging, 'cpp-abi');
    await mkdir(abi);
    for (const input of [...V8_CPP_ABI_POLICY.trees, ...V8_CPP_ABI_POLICY.files]) {
      const source = await cached(input, cache), target = join(abi, input.path);
      await mkdir(dirname(target), { recursive: true });
      if (input.git_tree) await copyTree(source, target);
      else { await copyFile(source, target); await chmod(target, 0o644); }
    }
    await verifyAbi(abi);
    // Keep installation atomic even when the caller selects a cache on another volume.
    const local = await mkdtemp(join(root, '.cpp-abi-'));
    try {
      await copyTree(abi, join(local, 'cpp-abi'));
      await verifyAbi(join(local, 'cpp-abi'));
      try { await rename(join(local, 'cpp-abi'), destination); }
      catch (error) { if (!['EEXIST', 'ENOTEMPTY'].includes(error.code)) throw error; }
    } finally { await rm(local, { recursive: true, force: true }); }
    return await verifyV8CppAbi(root);
  } finally { await rm(staging, { recursive: true, force: true }); }
}
