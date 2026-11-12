async () => {
  await check('URLPattern constructor parses pattern and executes executive matching', () => {
    assert(typeof window.URLPattern === 'function', 'URLPattern constructor exists');
    const pattern = new URLPattern({ pathname: '/items/:id' });
    assert(pattern.test('https://example.com/items/99'), 'pattern.test matches valid pathname');
    const result = pattern.exec('https://example.com/items/99');
    assert(result !== null, 'pattern.exec returns non-null result');
    assert(result.pathname.groups.id === '99', `extracted item id is 99, got ${result.pathname.groups.id}`);
  });

  await check('navigator exposes sendBeacon method for lightweight data dispatch', () => {
    assert(typeof navigator.sendBeacon === 'function', 'sendBeacon method exists on navigator');
    const queued = navigator.sendBeacon('http://127.0.0.1:9/beacon', 'ping-payload');
    assert(typeof queued === 'boolean', 'sendBeacon returns boolean queuing status');
  });

  await check('Element exposes animate method to initialize Web Animations player', () => {
    assert(typeof Element.prototype.animate === 'function', 'Element.prototype.animate exists');
    const target = document.getElementById('anim-target');
    const player = target.animate([
      { opacity: 1, transform: 'scale(1)' },
      { opacity: 0.2, transform: 'scale(1.5)' }
    ], { duration: 500, fill: 'forwards' });
    assert(typeof player.pause === 'function' && typeof player.cancel === 'function', 'player exposes standard lifecycle methods');
    player.cancel();
  });
}
