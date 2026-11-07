(function bootstrapDedicatedWorker() {
  "use strict";
  const __obscuraCore = globalThis.Deno.core;
  const init = globalThis.__workerInit;
  delete globalThis.__workerInit;
  delete globalThis.Deno;
  const _markNative = value => value;
  const _eventToString = String;
  const _eventTargetListeners = new WeakMap();
  const _nativeEvents = new WeakSet();
  const _nativeEventAdd = WeakSet.prototype.add.bind(_nativeEvents);
  const _nativeEventHas = WeakSet.prototype.has.bind(_nativeEvents);
  const _trustedEvents = new WeakSet();
  const _trustedEventAdd = WeakSet.prototype.add.bind(_trustedEvents);
  const _trustedEventDelete = WeakSet.prototype.delete.bind(_trustedEvents);
  const _trustedEventHas = WeakSet.prototype.has.bind(_trustedEvents);
  const _defineEventProperty = Object.defineProperty;
  function _eventIsTrusted() { _requireEvent(this); return _trustedEventHas(this); }
  function _requireEvent(event) {
    if (!_nativeEventHas(event)) throw new TypeError('Event required');
  }
  function _dispatchTrusted(target, event, deliver = _eventTargetDispatch) {
    _requireEvent(event);
    _trustedEventAdd(event);
    return deliver(target, event);
  }
  function _dispatchUntrusted(target, event, deliver = _eventTargetDispatch) {
    _requireEvent(event);
    _trustedEventDelete(event);
    return deliver(target, event);
  }
  function _createNativeEvent(kind, type, init) {
    switch (kind) {
      case 'Event': return new Event(type, init);
      case 'MessageEvent': return new MessageEvent(type, init);
      case 'ErrorEvent': return new ErrorEvent(type, init);
      case 'IDBVersionChangeEvent': return new IDBVersionChangeEvent(type, init);
      default: throw new TypeError('Unsupported native event kind');
    }
  }
  function _eventTargetAdd(target, type, callback, options) {
    if (callback == null) return;
    let types = _eventTargetListeners.get(target);
    if (!types) _eventTargetListeners.set(target, types = new Map());
    type = _eventToString(type);
    let list = types.get(type);
    if (!list) types.set(type, list = []);
    const capture = typeof options === 'boolean' ? options : !!options?.capture;
    if (!list.some(item => item.callback === callback && item.capture === capture)) list.push({ callback, capture, once: !!options?.once });
  }
  function _eventTargetRemove(target, type, callback, options) {
    const list = _eventTargetListeners.get(target)?.get(_eventToString(type));
    const capture = typeof options === 'boolean' ? options : !!options?.capture;
    if (!list) return;
    const i = list.findIndex(item => item.callback === callback && item.capture === capture);
    if (i !== -1) list.splice(i, 1);
  }
  class Event {
    constructor(type, options = {}) {
      this.type = _eventToString(type); this.bubbles = !!options.bubbles; this.cancelable = !!options.cancelable;
      this.defaultPrevented = false; this.target = null; this.currentTarget = null; this.eventPhase = 0;
      this.timeStamp = performance.now(); this._dispatching = false;
      this._propagationStopped = this._immediatePropagationStopped = false;
      _nativeEventAdd(this);
      _defineEventProperty(this, 'isTrusted', { get: _eventIsTrusted, enumerable: true });
    }
    preventDefault() { if (this.cancelable) this.defaultPrevented = true; }
    stopPropagation() { this._propagationStopped = true; }
    stopImmediatePropagation() { this._immediatePropagationStopped = this._propagationStopped = true; }
  }
  class MessageEvent extends Event {
    constructor(type, options = {}) { super(type, options); this.data = Object.hasOwn(options, 'data') ? options.data : null; this.origin = options.origin || ''; this.ports = []; this.source = null; }
  }
  class ErrorEvent extends Event {
    constructor(type, options = {}) {
      super(type, options);
      this.message = String(options.message || ''); this.filename = String(options.filename || '');
      this.lineno = Number(options.lineno || 0); this.colno = Number(options.colno || 0);
      this.error = options.error ?? null;
    }
  }
  function _eventTargetDispatch(target, event) {
    if (event.type === '') throw new DOMException('The event type was not specified', 'InvalidStateError');
    if (event._dispatching) throw new DOMException('The event is already being dispatched', 'InvalidStateError');
    event._dispatching = true;
    event._propagationStopped = event._immediatePropagationStopped = false;
    event.target = event.currentTarget = target; event.eventPhase = 2;
    try {
      const listeners = (_eventTargetListeners.get(target)?.get(event.type) || []).slice();
      for (const listener of listeners) {
        if (event._immediatePropagationStopped) break;
        if (!(_eventTargetListeners.get(target)?.get(event.type) || []).includes(listener)) continue;
        if (listener.once) _eventTargetRemove(target, event.type, listener.callback, listener.capture);
        try {
          if (typeof listener.callback === 'function') listener.callback.call(target, event);
          else listener.callback.handleEvent(event);
        } catch (error) { reportError(error); }
      }
      return !event.defaultPrevented;
    } finally {
      event.currentTarget = null; event.eventPhase = 0; event._dispatching = false;
    }
  }
  class EventTarget {
    addEventListener(type, callback, options) { _eventTargetAdd(this ?? globalThis, type, callback, options); }
    removeEventListener(type, callback, options) { _eventTargetRemove(this ?? globalThis, type, callback, options); }
    dispatchEvent(event) { return _dispatchUntrusted(this ?? globalThis, event); }
  }
  class DOMException extends Error { constructor(message = '', name = 'Error') { super(message); this.name = name; } }
  class WorkerGlobalScope extends EventTarget {}
  class DedicatedWorkerGlobalScope extends WorkerGlobalScope {}
  Object.setPrototypeOf(globalThis, DedicatedWorkerGlobalScope.prototype);
  Object.assign(globalThis, { Event, MessageEvent, ErrorEvent, EventTarget, DOMException, WorkerGlobalScope, DedicatedWorkerGlobalScope });
  Object.defineProperty(DedicatedWorkerGlobalScope.prototype, Symbol.toStringTag, { value: 'DedicatedWorkerGlobalScope' });
  globalThis.self = globalThis;
  globalThis.onmessage = null; globalThis.onmessageerror = null; globalThis.onerror = null;
  globalThis.reportError = error => __obscuraCore.ops.op_worker_error(String(error?.stack || error));
  const timeOrigin = Date.now();
  globalThis.performance = Object.freeze({ timeOrigin, now: () => __obscuraCore.ops.op_worker_now() });
  globalThis.console = Object.fromEntries(['log', 'info', 'warn', 'error', 'debug'].map(level => [level, (...args) => {
    __obscuraCore.print(args.map(value => typeof value === 'string' ? value : String(value)).join(' ') + '\n', level === 'error');
  }]));
  globalThis.queueMicrotask = callback => { if (typeof callback !== 'function') throw new TypeError('Callback required'); Promise.resolve().then(callback).catch(reportError); };
  const timers = new Map(); let nextTimer = 0;
  function timer(callback, delay, args, repeat) {
    if (typeof callback !== 'function') throw new TypeError('String timers are not supported in workers');
    const id = ++nextTimer;
    const run = () => {
      if (!timers.has(id)) return;
      if (!repeat) timers.delete(id);
      __obscuraCore.ops.op_begin_render_task();
      try { callback(...args); } catch (error) { reportError(error); }
      if (repeat && timers.has(id)) timers.set(id, __obscuraCore.createTimer(run, Math.max(1, Number(delay) || 0), undefined, false, true, false));
    };
    timers.set(id, __obscuraCore.createTimer(run, Math.max(0, Number(delay) || 0), undefined, false, true, false));
    return id;
  }
  globalThis.setTimeout = (callback, delay = 0, ...args) => timer(callback, delay, args, false);
  globalThis.setInterval = (callback, delay = 0, ...args) => timer(callback, delay, args, true);
  globalThis.clearTimeout = globalThis.clearInterval = id => { const native = timers.get(Number(id)); if (native !== undefined) __obscuraCore.cancelTimer(native); timers.delete(Number(id)); };
  const _schedulerPriorityRank = { 'user-visible': 1 };
  const _browserPostedTaskEnqueue = fn => setTimeout(fn, 0);
  class TextEncoder {
    get encoding() { return 'utf-8'; }
    encode(text = '') { return __obscuraCore.encode(String(text)); }
    encodeInto(text, destination) {
      let read = 0, written = 0;
      for (const char of String(text)) {
        const bytes = this.encode(char);
        if (written + bytes.length > destination.length) break;
        destination.set(bytes, written); written += bytes.length; read += char.length;
      }
      return { read, written };
    }
  }
  class TextDecoder {
    constructor(label = 'utf-8', options = {}) {
      const encoding = __obscuraCore.ops.op_encoding_for_label(String(label));
      if (!encoding) throw new RangeError('Unknown encoding');
      this.encoding = encoding; this.fatal = !!options.fatal; this.ignoreBOM = !!options.ignoreBOM;
    }
    decode(input = new Uint8Array(), options = {}) {
      if (options.stream) throw new DOMException('Streaming text decoding is not supported', 'NotSupportedError');
      const bytes = input instanceof ArrayBuffer ? new Uint8Array(input) : new Uint8Array(input.buffer, input.byteOffset, input.byteLength);
      const decoded = JSON.parse(__obscuraCore.ops.op_text_decode(this.encoding, bytes, this.fatal, this.ignoreBOM));
      if (!decoded.ok) throw new TypeError('The encoded data is not valid');
      return decoded.v;
    }
  }
  Object.assign(globalThis, { TextEncoder, TextDecoder });
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
  globalThis.btoa = text => {
    text = String(text); let out = '';
    for (let i = 0; i < text.length; i += 3) {
      const a = text.charCodeAt(i), b = text.charCodeAt(i + 1), c = text.charCodeAt(i + 2);
      if (a > 255 || b > 255 || c > 255) throw new DOMException('Invalid binary string', 'InvalidCharacterError');
      const bits = a << 16 | (b || 0) << 8 | (c || 0);
      out += alphabet[bits >>> 18] + alphabet[bits >>> 12 & 63] + (i + 1 < text.length ? alphabet[bits >>> 6 & 63] : '=') + (i + 2 < text.length ? alphabet[bits & 63] : '=');
    }
    return out;
  };
  globalThis.atob = text => {
    text = String(text).replace(/[\t\n\f\r ]/g, '');
    if (text.length % 4 === 0) text = text.replace(/={1,2}$/, '');
    if (text.length % 4 === 1 || /[^A-Za-z0-9+/]/.test(text)) throw new DOMException('Invalid base64', 'InvalidCharacterError');
    let bits = 0, count = 0, out = '';
    for (const char of text) { bits = bits << 6 | alphabet.indexOf(char); count += 6; if (count >= 8) { count -= 8; out += String.fromCharCode(bits >>> count & 255); } }
    return out;
  };
  const _bytesToBinaryString = bytes => { let out = ''; for (const byte of bytes) out += String.fromCharCode(byte); return out; };
  const _base64ToUint8Array = value => Uint8Array.from(atob(value), char => char.charCodeAt(0));
  class URL {
    constructor(input, base) {
      const value = JSON.parse(__obscuraCore.ops.op_url_parse(String(input), base === undefined ? '' : String(base)));
      if (!value.ok) throw new TypeError('Invalid URL');
      this._value = value;
    }
    toString() { return this.href; }
    toJSON() { return this.href; }
    static canParse(input, base) { try { new URL(input, base); return true; } catch (_) { return false; } }
    static parse(input, base) { try { return new URL(input, base); } catch (_) { return null; } }
  }
  for (const key of ['href','protocol','username','password','host','hostname','port','pathname','search','hash','origin']) {
    Object.defineProperty(URL.prototype, key, { enumerable: true, get() { return this._value[key]; },
      ...(key === 'origin' ? {} : { set(value) {
        const next = JSON.parse(key === 'href' ? __obscuraCore.ops.op_url_parse(String(value), '') : __obscuraCore.ops.op_url_set(this.href, key, String(value)));
        if (!next.ok) throw new TypeError('Invalid URL'); this._value = next;
      } }) });
  }
  globalThis.URL = URL;
  class WorkerLocation {
    constructor(url) { const parsed = new URL(url); for (const key of ['href','origin','protocol','host','hostname','port','pathname','search','hash']) Object.defineProperty(this, key, { value: parsed[key], enumerable: true }); }
    toString() { return this.href; }
  }
  // Use the same immutable schema3 object as the creator. Legacy embedders
  // without schema3 retain the creator's already-applied navigator values.
  /* SOFTWARE_DECLARATION */
  const environment = Object.freeze(init.identity.environment || {});
  const navigatorIdentity = { ...init.identity.navigator,
    userAgent: environment.user_agent ?? init.identity.navigator.userAgent,
    platform: environment.platform ?? init.identity.navigator.platform,
    language: environment.locale ?? init.identity.navigator.language,
    languages: environment.languages ?? init.identity.navigator.languages,
    hardwareConcurrency: environment.hardware_concurrency ?? init.identity.navigator.hardwareConcurrency,
    deviceMemory: environment.device_memory ?? init.identity.navigator.deviceMemory,
  };
  const softwareDeclaration = _softwareDeclaration(() => [navigatorIdentity.userAgent, navigatorIdentity.platform]);
  class WorkerNavigator {}
  for (const [key, value] of Object.entries(navigatorIdentity)) {
    if (key === 'userAgentData') continue;
    Object.defineProperty(WorkerNavigator.prototype, key, { get: () => typeof value === 'object' ? Object.freeze(value) : value, enumerable: true });
  }
  Object.defineProperty(WorkerNavigator.prototype, 'userAgentData', {
    get() { return softwareDeclaration; },
    enumerable: true,
  });
  Object.assign(globalThis, { WorkerLocation, WorkerNavigator });
  Object.defineProperties(globalThis, {
    location: { value: new WorkerLocation(init.url), enumerable: true },
    navigator: { value: new WorkerNavigator(), enumerable: true },
    name: { value: init.identity.name, enumerable: true },
    origin: { value: init.identity.origin, enumerable: true },
    isSecureContext: { value: init.identity.origin.startsWith('https://') || init.identity.origin.startsWith('http://localhost'), enumerable: true },
  });
  class Blob {
    constructor(parts = [], options = {}) {
      const chunks = Array.from(parts, part => part instanceof Blob ? part._bytes : part instanceof ArrayBuffer ? new Uint8Array(part) : ArrayBuffer.isView(part) ? new Uint8Array(part.buffer, part.byteOffset, part.byteLength) : new TextEncoder().encode(String(part)));
      this._bytes = new Uint8Array(chunks.reduce((n, bytes) => n + bytes.length, 0));
      let offset = 0; for (const chunk of chunks) { this._bytes.set(chunk, offset); offset += chunk.length; }
      this.type = String(options.type || '').toLowerCase();
    }
    get size() { return this._bytes.length; }
    async arrayBuffer() { return this._bytes.slice().buffer; }
    async text() { return new TextDecoder().decode(this._bytes); }
    slice(start = 0, end = this.size, type = '') { return new Blob([this._bytes.slice(start, end)], { type }); }
  }
  class File extends Blob { constructor(parts, name, options = {}) { super(parts, options); this.name = String(name).replace(/\//g, ':'); this.lastModified = options.lastModified === undefined ? Date.now() : Number(options.lastModified); } }
  Object.assign(globalThis, { Blob, File });
  class Headers {
    constructor(init = {}) { this._map = new Map(); for (const [name, value] of init instanceof Headers || Array.isArray(init) ? init : Object.entries(init)) this.append(name, value); }
    append(name, value) { const key = String(name).toLowerCase(); if (!/^[!#$%&'*+.^_`|~0-9a-z-]+$/.test(key) || /[\r\n\0]/.test(String(value))) throw new TypeError('Invalid header'); this._map.set(key, this.has(key) ? this.get(key) + ', ' + String(value).trim() : String(value).trim()); }
    set(name, value) { this.delete(name); this.append(name, value); }
    get(name) { return this._map.get(String(name).toLowerCase()) ?? null; }
    has(name) { return this._map.has(String(name).toLowerCase()); }
    delete(name) { this._map.delete(String(name).toLowerCase()); }
    entries() { return Array.from(this._map).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0)[Symbol.iterator](); }
    keys() { return Array.from(this.entries(), ([key]) => key)[Symbol.iterator](); }
    values() { return Array.from(this.entries(), ([, value]) => value)[Symbol.iterator](); }
    forEach(callback, thisArg) { for (const [name, value] of this) callback.call(thisArg, value, name, this); }
    [Symbol.iterator]() { return this.entries(); }
  }
  const bytesOf = value => value == null ? new Uint8Array() : value instanceof Blob ? value._bytes : value instanceof ArrayBuffer ? new Uint8Array(value) : ArrayBuffer.isView(value) ? new Uint8Array(value.buffer, value.byteOffset, value.byteLength) : new TextEncoder().encode(String(value));
  class Response {
    constructor(body = null, options = {}) {
      this._bytes = Promise.resolve(bytesOf(body)); this.bodyUsed = false;
      this.status = options.status === undefined ? 200 : options.status; this.statusText = options.statusText || '';
      this.headers = new Headers(options.headers); this.url = options.url || ''; this.redirected = !!options.redirected; this.type = options.type || 'default';
    }
    get ok() { return this.status >= 200 && this.status <= 299; }
    async arrayBuffer() { if (this.bodyUsed) throw new TypeError('Body already consumed'); this.bodyUsed = true; return (await this._bytes).slice().buffer; }
    async text() { return new TextDecoder().decode(await this.arrayBuffer()); }
    async json() { return JSON.parse(await this.text()); }
    async blob() { return new Blob([await this.arrayBuffer()], { type: this.headers.get('content-type') || '' }); }
    clone() { if (this.bodyUsed) throw new TypeError('Body already consumed'); const clone = new Response(null, this); clone._bytes = this._bytes; return clone; }
    static json(data, options = {}) { const headers = new Headers(options.headers); if (!headers.has('content-type')) headers.set('content-type', 'application/json'); return new Response(JSON.stringify(data), { ...options, headers }); }
  }
  class Request {
    constructor(input, options = {}) {
      const previous = input instanceof Request ? input : {};
      const redirect = options.redirect === undefined ? (previous.redirect ?? 'follow') : String(options.redirect);
      const cache = options.cache === undefined ? (previous.cache ?? 'default') : String(options.cache);
      const referrerPolicy = options.referrerPolicy === undefined ? (previous.referrerPolicy ?? '') : String(options.referrerPolicy);
      if (redirect !== 'follow' || !['default','no-store'].includes(cache) || referrerPolicy !== '') {
        throw new DOMException('This worker fetch backend supports follow redirects, default/no-store cache and default referrer policy only', 'NotSupportedError');
      }
      this.redirect = redirect; this.cache = cache; this.referrerPolicy = referrerPolicy;
      this.url = new URL(previous.url || String(input), location.href).href;
      this.method = String(options.method || previous.method || 'GET').toUpperCase();
      this.headers = new Headers(options.headers || previous.headers);
      this.mode = options.mode || previous.mode || 'cors'; this.credentials = options.credentials || previous.credentials || 'same-origin';
      this._body = options.body === undefined ? previous._body : options.body; this.signal = options.signal || previous.signal;
    }
  }
  class AbortSignal extends EventTarget {
    constructor() { super(); this.aborted = false; this.reason = undefined; }
    throwIfAborted() { if (this.aborted) throw this.reason; }
    static abort(reason) { const controller = new AbortController(); controller.abort(reason); return controller.signal; }
    static timeout(delay) { const controller = new AbortController(); setTimeout(() => controller.abort(new DOMException('Timed out', 'TimeoutError')), delay); return controller.signal; }
  }
  class AbortController {
    constructor() { this.signal = new AbortSignal(); }
    abort(reason = new DOMException('Aborted', 'AbortError')) { if (this.signal.aborted) return; this.signal.aborted = true; this.signal.reason = reason; this.signal.dispatchEvent(new Event('abort')); }
  }
  Object.assign(globalThis, { Headers, Response, Request, AbortSignal, AbortController });
  const workerScriptURL = init.url, ImportURL = URL, ImportException = DOMException;
  globalThis.importScripts = function importScripts(...urls) {
    // Resolve the complete argument list before executing anything. Each script
    // then loads and runs synchronously, in order, in this actual Worker realm.
    const resolved = urls.map(value => {
      if (typeof value === 'symbol') throw new TypeError('Script URL must be a string');
      const text = String(value);
      try { return new ImportURL(text, workerScriptURL).href; }
      catch (_) { throw new ImportException('Invalid script URL: ' + text, 'SyntaxError'); }
    });
    for (const url of resolved) {
      const failure = __obscuraCore.ops.op_worker_import_script(url);
      if (failure) throw new ImportException(failure, 'NetworkError');
    }
  };
  globalThis.fetch = async (input, options = {}) => {
    const request = new Request(input, options), signal = request.signal;
    signal?.throwIfAborted();
    const headers = {};
    for (const [name, value] of request.headers) {
      if (/^(sec-|proxy-)/.test(name) || ['cookie','cookie2','host','origin','referer','connection','content-length','accept-encoding','set-cookie'].includes(name)) continue;
      headers[name] = value;
    }
    const rid = signal ? __obscuraCore.ops.op_fetch_cancel_handle() : null;
    const cancel = () => { try { __obscuraCore.close(rid); } catch (_) {} };
    if (signal) signal.addEventListener('abort', cancel, { once: true });
    try {
      const parsed = JSON.parse(await __obscuraCore.ops.op_fetch_url(request.url, request.method, JSON.stringify(headers), bytesOf(request._body), origin,
        request.mode, request.credentials, { destination: 'Fetch', cache: request.cache, ...(signal ? { cancel_rid: rid } : {}) }));
      signal?.throwIfAborted();
      if (parsed.blocked || parsed.corsBlocked) throw new TypeError('Failed to fetch');
      const response = new Response(parsed.bodyBase64 ? _base64ToUint8Array(parsed.bodyBase64) : parsed.body || '', {
        status: parsed.status, headers: parsed.headers, url: parsed.opaque ? '' : parsed.url || request.url,
        redirected: parsed.redirected, type: parsed.opaque ? 'opaque' : 'basic' });
      if (typeof parsed.bodyRid === 'number') {
        response._bytes = __obscuraCore.ops.op_fetch_body(parsed.bodyRid).finally(() => {
          if (signal) { signal.removeEventListener('abort', cancel); cancel(); }
        });
        response._bytes.catch(() => {});
      } else if (signal) { signal.removeEventListener('abort', cancel); cancel(); }
      return response;
    } catch (error) { if (signal) { signal.removeEventListener('abort', cancel); cancel(); signal.throwIfAborted(); } throw error; }
  };
  globalThis.crypto = Object.freeze({
    getRandomValues(array) {
      if (!ArrayBuffer.isView(array) || array instanceof DataView || array instanceof Float32Array || array instanceof Float64Array) throw new DOMException('Integer typed array required', 'TypeMismatchError');
      if (array.byteLength > 65536) throw new DOMException('Random request exceeds 65536 bytes', 'QuotaExceededError');
      new Uint8Array(array.buffer, array.byteOffset, array.byteLength).set(__obscuraCore.ops.op_random_bytes(array.byteLength)); return array;
    },
    randomUUID() { const bytes = __obscuraCore.ops.op_random_bytes(16); bytes[6] = bytes[6] & 15 | 64; bytes[8] = bytes[8] & 63 | 128; const hex = Array.from(bytes, value => value.toString(16).padStart(2, '0')).join(''); return hex.slice(0,8) + '-' + hex.slice(8,12) + '-' + hex.slice(12,16) + '-' + hex.slice(16,20) + '-' + hex.slice(20); },
  });
  WebAssembly.instantiateStreaming = async (source, imports) => WebAssembly.instantiate(await (await source).arrayBuffer(), imports);
  WebAssembly.compileStreaming = async source => WebAssembly.compile(await (await source).arrayBuffer());
  globalThis.structuredClone = (value, options) => _workerDeserialize(_workerSerialize(value, options));
  /* INDEXED_DB */
  /* WORKER_CODEC */
  globalThis.close = () => { closed = true; __obscuraCore.ops.op_worker_close(); };
  let closed = false;
  globalThis.postMessage = (value, options) => {
    if (closed) return;
    __obscuraCore.ops.op_worker_outbox(_workerSerialize(value, options));
  };
  (async () => {
    while (!closed) {
      const bytes = await __obscuraCore.ops.op_worker_inbox();
      if (closed || bytes === null) break;
      await new Promise(resolve => setTimeout(() => {
        if (!closed) {
          let event;
          try { event = _createNativeEvent('MessageEvent', 'message', { data: _workerDeserialize(bytes) }); }
          catch (_) { event = _createNativeEvent('MessageEvent', 'messageerror'); }
          _dispatchTrusted(self, event);
          try { const handler = self['on' + event.type]; if (typeof handler === 'function') handler.call(self, event); }
          catch (error) { reportError(error); }
        }
        resolve();
      }, 0));
    }
  })().catch(reportError);
  return { pollIndexedDb: _idbPollDeliver };
})();
