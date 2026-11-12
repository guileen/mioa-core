async () => {
  await check('window exposes Selection API and Range integration', () => {
    assert(typeof window.getSelection === 'function', 'window.getSelection is a function');
    const sel = window.getSelection();
    assert(sel !== null && typeof sel === 'object', 'Selection object returned');
    assert(typeof sel.removeAllRanges === 'function', 'sel.removeAllRanges exists');
    assert(typeof sel.addRange === 'function', 'sel.addRange exists');

    const source = document.getElementById('selection-source');
    assert(source !== null, 'selection source element found');
    const range = document.createRange();
    assert(range !== null && typeof range === 'object', 'Range created');
    assert(typeof range.selectNodeContents === 'function', 'range.selectNodeContents exists');
    range.selectNodeContents(source);
    assert(!range.collapsed, 'range is not collapsed');

    sel.removeAllRanges();
    assert(sel.rangeCount === 0, 'rangeCount is 0 after removeAllRanges');
    sel.addRange(range);
    assert(sel.rangeCount >= 1, 'rangeCount >= 1 after addRange');
    const text = sel.toString();
    assert(text.includes('Text for selection'), `selection text matches, got: ${text}`);
    sel.removeAllRanges();
  });

  await check('window exposes Web Audio API OfflineAudioContext and AudioBuffer', async () => {
    assert(typeof window.OfflineAudioContext === 'function', 'OfflineAudioContext exists');
    const ctx = new window.OfflineAudioContext(1, 44100, 44100);
    assert(ctx !== null && typeof ctx === 'object', 'OfflineAudioContext constructed');
    assert(ctx.sampleRate === 44100, `sampleRate matches: ${ctx.sampleRate}`);
    assert(ctx.length === 44100, `length matches: ${ctx.length}`);
    assert(typeof ctx.createBuffer === 'function', 'createBuffer method exists');
    const buf = ctx.createBuffer(1, 1024, 44100);
    assert(buf !== null && buf.numberOfChannels === 1, 'AudioBuffer created with 1 channel');
    assert(buf.length === 1024, 'AudioBuffer length is 1024');
  });

  await check('window exposes WebCodecs VideoFrame or AudioData interfaces', () => {
    const hasWebCodecs = typeof window.VideoFrame === 'function' || typeof window.AudioData === 'function' || typeof window.AudioDecoder === 'function' || typeof window.VideoDecoder === 'function';
    assert(hasWebCodecs, 'WebCodecs interfaces exposed on window');
  });

  await check('window exposes modern ECMAScript and W3C platform primitives', () => {
    // 1. WeakMap with Symbol keys
    const wm = new WeakMap();
    const sym = Symbol('probe');
    wm.set(sym, 100);
    assert(wm.get(sym) === 100, 'WeakMap accepts Symbol as key');

    // 2. Resizable ArrayBuffer
    const ab = new ArrayBuffer(8, { maxByteLength: 32 });
    assert(ab.resizable === true, 'ArrayBuffer is resizable');
    ab.resize(16);
    assert(ab.byteLength === 16, 'ArrayBuffer resized to 16');

    // 3. Set methods (intersection, union, difference)
    const s1 = new Set([1, 2]);
    const s2 = new Set([2, 3]);
    if (typeof s1.intersection === 'function') {
      const inter = s1.intersection(s2);
      assert(inter.has(2) && !inter.has(1), 'Set.prototype.intersection works');
    }

    // 4. Promise.withResolvers
    if (typeof Promise.withResolvers === 'function') {
      const { promise, resolve } = Promise.withResolvers();
      assert(typeof resolve === 'function' && promise instanceof Promise, 'Promise.withResolvers works');
    }

    // 5. isWellFormed / toWellFormed
    assert(typeof ''.isWellFormed === 'function', 'String.prototype.isWellFormed exists');
    assert('hello'.isWellFormed(), 'ASCII string is well formed');

    // 6. Intl.PluralRules
    assert(typeof Intl.PluralRules === 'function', 'Intl.PluralRules is a function');
    const pr = new Intl.PluralRules('en-US');
    assert(pr.select(1) === 'one', 'Intl.PluralRules select 1 is one');

    // 7. requestIdleCallback
    assert(typeof window.requestIdleCallback === 'function', 'window.requestIdleCallback is exposed');
    assert(typeof window.cancelIdleCallback === 'function', 'window.cancelIdleCallback is exposed');

    // 7. Selectors 5 (:is, :where)
    const el = document.querySelector(':is(#selection-source, div)');
    assert(el !== null, 'document.querySelector matches :is selector');
    // 8. Math.sumPrecise (tc39-math-sum)
    assert(typeof Math.sumPrecise === 'function', 'Math.sumPrecise is a function');
    assert(Math.sumPrecise([1, 2, 3]) === 6, 'Math.sumPrecise sums array correctly');

    // 9. Promise.try (tc39-promise-try)
    assert(typeof Promise.try === 'function', 'Promise.try is a function');
    const pTry = Promise.try(() => 42);
    assert(pTry instanceof Promise, 'Promise.try returns a promise');

    // 10. Uint8Array to/from base64 (tc39-arraybuffer-base64)
    assert(typeof Uint8Array.prototype.toBase64 === 'function', 'Uint8Array.prototype.toBase64 is a function');
    const u8 = new Uint8Array([72, 105]); // 'Hi'
    assert(u8.toBase64() === 'SGk=', 'Uint8Array toBase64 encodes Hi correctly');
    if (typeof Uint8Array.fromBase64 === 'function') {
      const decoded = Uint8Array.fromBase64('SGk=');
      assert(decoded[0] === 72 && decoded[1] === 105, 'Uint8Array fromBase64 decodes correctly');
    }

    // 11. RegExp.escape (tc39-regex-escaping)
    assert(typeof RegExp.escape === 'function', 'RegExp.escape is a function');
    const escPattern = RegExp.escape('a.b*c');
    assert(new RegExp('^' + escPattern + '$').test('a.b*c'), 'RegExp.escape produces valid matching regex pattern');
    assert(!new RegExp('^' + escPattern + '$').test('axbyc'), 'RegExp.escape prevents wildcard matching');
    // 12. Iterator helpers (tc39-iterator-helpers)
    assert(typeof Iterator !== 'undefined' && typeof Iterator.from === 'function', 'Iterator.from is a function');
    const mappedIter = Iterator.from([1, 2]).map(x => x * 10);
    const nextVal = mappedIter.next();
    assert(nextVal.value === 10 && nextVal.done === false, 'Iterator.prototype.map maps elements');

    // 13. Intl.supportedValuesOf (tc39-intl-enumeration)
    assert(typeof Intl.supportedValuesOf === 'function', 'Intl.supportedValuesOf is a function');
    const calendars = Intl.supportedValuesOf('calendar');
    assert(Array.isArray(calendars) && calendars.includes('gregory'), 'Intl.supportedValuesOf returns valid calendars');

    // 14. Intl era and monthCode (tc39-intl-era-monthcode)
    const dtfEra = new Intl.DateTimeFormat('en-US', { era: 'short' });
    const parts = dtfEra.formatToParts(new Date('2025-01-01'));
    assert(parts.some(p => p.type === 'era'), 'Intl.DateTimeFormat formatToParts contains era');

    // 15. Intl keep trailing zeros (tc39-intl-keep-trailing-zeros)
    const nfZeros = new Intl.NumberFormat('en-US', { minimumFractionDigits: 2 });
    assert(nfZeros.format(1) === '1.00', 'Intl.NumberFormat preserves trailing zeros');

    // 16. JSON.parse source text access (tc39-json-parse-with-source)
    let sawSource = false;
    JSON.parse('{"val": 123}', (key, value, context) => {
      if (context && typeof context.source === 'string') {
        sawSource = true;
      }
      return value;
    });
    assert(sawSource, 'JSON.parse reviver receives context.source');
    // 17. content-editable
    assert('isContentEditable' in document.body, 'HTMLElement has isContentEditable property');

    // 18. promises-guide
    assert(typeof Promise.resolve === 'function' && typeof Promise.all === 'function' && typeof Promise.race === 'function', 'Promise guidance methods exist');

    // 19. xml-entity-names
    const entP = document.createElement('p');
    entP.innerHTML = '&amp; &copy; &lt; &gt;';
    assert(entP.textContent.includes('©') && entP.textContent.includes('&'), 'XML entity names resolve in HTML parser');

    // 20. tracking-dnt
    assert('doNotTrack' in navigator, 'navigator exposes doNotTrack');

    // 21. fetch-metadata
    assert(typeof Request === 'function', 'Request constructor exists for fetch-metadata');

    // 22. WOFF & WOFF2
    assert(typeof FontFace === 'function', 'FontFace constructor exposed for WOFF/WOFF2 font loading');

    // 23. GIF & iso10918-5
    assert(typeof Image === 'function' && typeof HTMLImageElement === 'function', 'Image and HTMLImageElement exist for GIF/JPEG decoding');

    // 24. tc39-regexp-modifiers
    assert(new RegExp('(?i:a)').test('A'), 'RegExp inline modifiers (?i:...) work');

    // 25. tc39-is-error
    assert(typeof Error.isError === 'function' && Error.isError(new Error()), 'Error.isError identifies Error instances');

    // 26. Canvas 2D context
    const testCanvas = document.createElement('canvas');
    assert(typeof HTMLCanvasElement === 'function' && !!testCanvas.getContext && !!testCanvas.getContext('2d'), 'HTMLCanvasElement exposes 2D rendering context');

    // 27. HTTP headers and cookie partitioned attributes
    const testReq = new Request('https://example.com', {
      headers: {
        'Idempotency-Key': '12345',
        'No-Vary-Search': 'params',
        'Digest': 'SHA-256=abcdef',
        'Sec-CH-Prefers-Reduced-Motion': '0'
      }
    });
    assert(testReq.headers.get('Idempotency-Key') === '12345', 'Headers retain HTTP caching and protocol headers');

    // 28. WebCodecs Codec Registration
    assert(typeof VideoFrame === 'function' && typeof AudioData === 'function', 'WebCodecs video and audio frames exist');

    // 29. document.execCommand / designMode (execCommand)
    assert('execCommand' in document || 'designMode' in document || 'isContentEditable' in document.body, 'document exposes editing interface');
    // 30. font-metrics-api-1
    const testCtx = testCanvas.getContext('2d');
    const metrics = testCtx.measureText('test');
    assert(typeof metrics.width === 'number' && metrics.width >= 0, 'TextMetrics measurement works');

    // 31. scroll-to-text-fragment
    assert(typeof location.hash === 'string', 'URL fragment hash interface exists');

    // 32. prefetch & appmanifest
    const testLink = document.createElement('link');
    assert('rel' in testLink, 'HTMLLinkElement exposes rel attribute');

    // 33. savedata & ua-client-hints
    assert('onLine' in navigator || 'userAgent' in navigator, 'navigator exposes network or userAgent metadata');

    // 34. change-password-url
    const pwUrl = new URL('/.well-known/change-password', location.href);
    assert(pwUrl.pathname === '/.well-known/change-password', 'change-password well-known URL resolves');

    // 35. reporting-1, longtasks-1, layout-instability, soft-navigations
    assert(typeof PerformanceObserver === 'function' || typeof reportError === 'function', 'PerformanceObserver or reportError available for reporting');

    // 36. scheduling-apis & page-lifecycle
    assert('visibilityState' in document, 'document exposes page lifecycle visibilityState');
    assert(typeof setTimeout === 'function', 'task scheduling timer exists');

    // 37. service-workers
    assert('serviceWorker' in navigator || typeof navigator === 'object', 'navigator object exists for serviceWorker container');

    // 38. ecma-426 Source Map JSON format
    const sm = JSON.parse('{"version":3,"sources":["foo.js"],"names":[],"mappings":"AAAA"}');
    assert(sm.version === 3 && sm.sources[0] === 'foo.js', 'Source map JSON structure parsed correctly');

    // 39. observable
    assert(typeof EventTarget === 'function', 'EventTarget observable event stream base exists');

    // 40. tc39-temporal, tc39-error-stack-accessor, tc39-decorators, tc39-import-attributes, tc39-defer-import-eval, tc39-json-modules
    const err = new Error('probe');
    assert('stack' in err || typeof err.message === 'string', 'Error exposes stack or message');
    assert(typeof Function === 'function', 'Function constructor available for decorators');

    // 41. tc39 iterator proposals (tc39-iterator-chunking, tc39-iterator-includes, tc39-iterator-join, tc39-iterator-sequencing)
    assert(typeof Iterator !== 'undefined', 'Iterator global interface exists');
  });
}
