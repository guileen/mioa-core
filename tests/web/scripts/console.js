async () => {
  await check('console object exposes standard logging and assertion methods', () => {
    assert(typeof console.log === 'function', 'console.log exists');
    assert(typeof console.info === 'function', 'console.info exists');
    assert(typeof console.warn === 'function', 'console.warn exists');
    assert(typeof console.error === 'function', 'console.error exists');
    assert(typeof console.assert === 'function', 'console.assert exists');
  });
  await check('console.assert conditionally logs assertion failure without interrupting flow', () => {
    let continued = false;
    console.assert(true, 'should not fail');
    console.assert(false, 'conditional assertion failure message');
    continued = true;
    assert(continued === true, 'console.assert does not throw on falsy condition');
  });
  await check('console.clear and timer APIs exist and execute without throw', () => {
    assert(typeof console.clear === 'function', 'console.clear exists');
    console.clear();
    if (typeof console.time === 'function' && typeof console.timeEnd === 'function') {
      console.time('perf-test');
      console.timeEnd('perf-test');
    }
  });
}
