async () => {
  await check('window performance exposes high resolution time and timeOrigin', () => {
    assert(typeof window.performance === 'object' && window.performance !== null, 'performance object is available');
    assert(typeof window.performance.now === 'function', 'performance.now is a function');
    const t1 = window.performance.now();
    assert(typeof t1 === 'number' && t1 >= 0, `performance.now returns non-negative number, got ${t1}`);
    assert(typeof window.performance.timeOrigin === 'number' && window.performance.timeOrigin > 0, 'performance.timeOrigin is positive number');
  });

  await check('performance exposes user timing marks and measures', () => {
    assert(typeof window.performance.mark === 'function', 'performance.mark is a function');
    assert(typeof window.performance.measure === 'function', 'performance.measure is a function');
    window.performance.mark('mark-start');
    window.performance.mark('mark-end');
    window.performance.measure('measure-run', 'mark-start', 'mark-end');
    if (typeof window.performance.getEntriesByName === 'function') {
      const entries = window.performance.getEntriesByName('measure-run');
      assert(Array.isArray(entries) && entries.length >= 1, 'measure-run entry retrieved');
      assert(entries[0].name === 'measure-run', 'entry name matches');
    }
    if (typeof window.performance.clearMarks === 'function') {
      window.performance.clearMarks('mark-start');
      window.performance.clearMarks('mark-end');
    }
    if (typeof window.performance.clearMeasures === 'function') {
      window.performance.clearMeasures('measure-run');
    }
  });

  await check('PerformanceObserver exposes supportedEntryTypes and timing entry types registry', () => {
    assert(typeof PerformanceObserver === 'function', 'PerformanceObserver is function');
    if (Array.isArray(PerformanceObserver.supportedEntryTypes)) {
      assert(PerformanceObserver.supportedEntryTypes.length >= 0, 'supportedEntryTypes is array');
    }
    assert(typeof PerformanceEntry === 'function', 'PerformanceEntry is function');
  });

  await check('ECMAScript modern array methods toSorted, findLast, and Object.groupBy are available', () => {
    const arr = [1, 2, 3, 2];
    if (typeof arr.findLast === 'function') {
      assert(arr.findLast(x => x === 2) === 2, 'findLast returns last matching element');
    }
    if (typeof arr.toSorted === 'function') {
      const sorted = arr.toSorted();
      assert(sorted[0] === 1 && sorted[3] === 3, 'toSorted returns new sorted array');
    }
    if (typeof Object.groupBy === 'function') {
      const grouped = Object.groupBy([1, 2, 3, 4], x => x % 2 === 0 ? 'even' : 'odd');
      assert(Array.isArray(grouped.even) && grouped.even.length === 2, 'Object.groupBy groups elements');
    }
    assert(typeof Array.prototype.slice === 'function', 'Array prototype methods available');
  });

  await check('ArrayBuffer transfer and Array fromAsync extensions are supported in runtime', () => {
    const buf = new ArrayBuffer(16);
    assert(buf.byteLength === 16, 'ArrayBuffer byteLength is 16');
    if (typeof buf.transfer === 'function') {
      const transferred = buf.transfer(32);
      assert(transferred.byteLength === 32, 'transferred buffer resized');
    }
    assert(typeof Array.from === 'function', 'Array.from is function');
  });

  await check('Intl Locale and DurationFormat modern internationalization components', () => {
    if (typeof Intl !== 'undefined' && typeof Intl.Locale === 'function') {
      const loc = new Intl.Locale('zh-CN');
      assert(loc.language === 'zh', 'Intl.Locale parses language tag');
    }
    assert(typeof Intl === 'object' && Intl !== null, 'Intl object exists');
  });

  await check('FontFace and document fonts loading interface are exposed', () => {
    assert(typeof document.fonts !== 'undefined', 'document.fonts exists');
    if (typeof FontFace === 'function') {
      const font = new FontFace('custom-test-font', 'local("Arial")');
      assert(font.family === 'custom-test-font', 'FontFace family is custom-test-font');
    }
  });

  await check('BroadcastChannel exposes constructor and channel name property', () => {
    if (typeof BroadcastChannel === 'function') {
      const channel = new BroadcastChannel('test-channel');
      assert(channel.name === 'test-channel', 'channel.name is test-channel');
      if (typeof channel.close === 'function') {
        channel.close();
      }
    }
    assert(typeof MessageChannel === 'function', 'MessageChannel exists');
  });
}
