// Dedicated workers run in a fresh native V8 isolate, never in a page closure.
(function installWorkerClient() {
  "use strict";
  const slots = new WeakMap();
  /* WORKER_CODEC */
  function _workerError(worker, message) {
    const event = _createNativeEvent('ErrorEvent', 'error', { cancelable: true, message: String(message) });
    _dispatchTrusted(worker, event);
    try { if (typeof worker.onerror === 'function') worker.onerror(event); } catch (error) { reportError(error); }
  }
  async function _workerReceive(worker) {
    const state = slots.get(worker);
    while (!state.terminated) {
      let result;
      try { result = await __obscuraCore.ops.op_worker_receive(state.id); }
      catch (error) { if (!state.terminated) _workerError(worker, error.message); break; }
      if (state.terminated || !result || result.kind === 'closed') break;
      if (result.kind === 'error') { _workerError(worker, result.message); continue; }
      let data;
      try { data = _workerDeserialize(result.data); }
      catch (_) {
        const event = _createNativeEvent('MessageEvent', 'messageerror');
        _dispatchTrusted(worker, event);
        try { if (typeof worker.onmessageerror === 'function') worker.onmessageerror(event); } catch (error) { reportError(error); }
        continue;
      }
      // Each cross-isolate delivery enters the creator's task queue.
      await new Promise(resolve => setTimeout(() => {
        try {
          if (!state.terminated) {
            const event = _createNativeEvent('MessageEvent', 'message', { data });
            _dispatchTrusted(worker, event);
            if (typeof worker.onmessage === 'function') worker.onmessage(event);
          }
        } catch (error) { reportError(error); }
        finally { resolve(); }
      }, 0));
    }
    if (state.id !== null) __obscuraCore.ops.op_worker_terminate(state.id);
    state.terminated = true;
    state.pending.length = 0;
  }
  globalThis.Worker = class Worker extends EventTarget {
    constructor(scriptURL, options = {}) {
      super();
      if (options.type !== undefined && options.type !== 'classic') throw new DOMException('Module workers are not supported', 'NotSupportedError');
      const url = new URL(String(scriptURL), location.href);
      if (url.protocol !== 'data:' && url.origin !== location.origin) throw new DOMException('Worker script must be same-origin', 'SecurityError');
      this.onmessage = null;
      this.onmessageerror = null;
      this.onerror = null;
      const state = { id: null, terminated: false, pending: [] };
      slots.set(this, state);
      const blobSource = globalThis.__blobStore?.[url.href];
      let loadedURL = url.href;
      const load = blobSource !== undefined ? Promise.resolve(blobSource) : (async () => {
        if (url.protocol === 'data:') {
          const serialized = url.href.split('#', 1)[0];
          const comma = serialized.indexOf(',');
          if (comma === -1) throw new TypeError('Invalid data URL');
          const payload = serialized.slice(comma + 1).replace(/%([0-9a-f]{2})/gi, (_, hex) => String.fromCharCode(parseInt(hex, 16)));
          const bytes = /;base64$/i.test(serialized.slice(0, comma))
            ? _base64ToUint8Array(payload) : Uint8Array.from(payload, char => char.charCodeAt(0));
          return new TextDecoder().decode(bytes);
        }
        const response = await _fetch(url.href, { mode: 'same-origin', credentials: options.credentials || 'same-origin' }, 'Script');
        if (!response.ok) throw new Error('Worker script fetch failed: ' + response.status);
        loadedURL = response.url || url.href;
        if (new URL(loadedURL).origin !== location.origin) throw new DOMException('Worker redirect must remain same-origin', 'SecurityError');
        return response.text();
      })();
      load.then(source => {
        if (state.terminated) return;
        const identity = {
          environment: globalThis.__obscura_environment || {},
          navigator: { userAgent: navigator.userAgent, appVersion: navigator.appVersion, platform: navigator.platform,
            language: navigator.language, languages: Array.from(navigator.languages), hardwareConcurrency: navigator.hardwareConcurrency,
            deviceMemory: navigator.deviceMemory, userAgentData: navigator.userAgentData ? {
              brands: navigator.userAgentData.brands, mobile: navigator.userAgentData.mobile, platform: navigator.userAgentData.platform } : undefined },
          name: String(options.name || ''), origin: location.origin,
        };
        state.id = __obscuraCore.ops.op_worker_create(loadedURL, String(source), JSON.stringify(identity), _realmFrameId || 0);
        for (const bytes of state.pending) __obscuraCore.ops.op_worker_send(state.id, bytes);
        state.pending.length = 0;
        _workerReceive(this);
      }).catch(error => { if (!state.terminated) _workerError(this, error.message); });
    }
    postMessage(value, options) {
      const state = slots.get(this);
      if (!state) throw new TypeError('Illegal invocation');
      if (state.terminated) return;
      const bytes = _workerSerialize(value, options);
      if (state.id === null) state.pending.push(bytes);
      else __obscuraCore.ops.op_worker_send(state.id, bytes);
    }
    terminate() {
      const state = slots.get(this);
      if (!state) throw new TypeError('Illegal invocation');
      state.terminated = true;
      state.pending.length = 0;
      if (state.id !== null) __obscuraCore.ops.op_worker_terminate(state.id);
    }
  };
})();
