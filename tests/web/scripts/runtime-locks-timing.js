async () => {
  await check('navigator.locks grants lock and executes callback asynchronously', async () => {
    assert(!!(navigator.locks && typeof navigator.locks.request === 'function'), 'navigator.locks.request exists');
    let lockExecuted = false;
    await navigator.locks.request('test-resource', async (lock) => {
      assert(!!(lock && lock.name === 'test-resource'), 'lock object has name test-resource');
      lockExecuted = true;
    });
    assert(lockExecuted === true, 'lock callback executed');
  });

  await check('PerformanceObserver and performance.getEntries record timing entries', async () => {
    assert(typeof PerformanceObserver === 'function', 'PerformanceObserver constructor exists');
    assert(typeof performance.getEntries === 'function', 'performance.getEntries exists');
    const entries = performance.getEntries();
    assert(Array.isArray(entries), 'performance.getEntries returns an array');
  });

  await check('performance.getEntriesByType returns resource timing records', () => {
    assert(typeof performance.getEntriesByType === 'function', 'getEntriesByType function exists');
    const resources = performance.getEntriesByType('resource');
    assert(Array.isArray(resources), 'resource timing entries is an array');
  });

  await check('performance.getEntriesByType returns navigation timing record', () => {
    const navEntries = performance.getEntriesByType('navigation');
    assert(Array.isArray(navEntries), 'navigation entries is an array');
    if (navEntries.length > 0) {
      assert(typeof navEntries[0].startTime === 'number', 'navigation entry has numeric startTime');
    } else {
      assert(typeof performance.timeOrigin === 'number', 'performance.timeOrigin is number fallback');
    }
  });

  await check('screen.orientation exposes type and angle with change event handler', () => {
    assert(!!(screen && screen.orientation), 'screen.orientation exists');
    assert(typeof screen.orientation.type === 'string', 'screen.orientation.type is string');
    assert(typeof screen.orientation.angle === 'number', 'screen.orientation.angle is number');
    assert(!!('onchange' in screen.orientation || typeof screen.orientation.addEventListener === 'function'), 'orientation supports change events');
  });
}
