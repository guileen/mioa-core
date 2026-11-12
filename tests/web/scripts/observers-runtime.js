async () => {
  await check('navigator exposes standard webdriver automation property', () => {
    assert('webdriver' in navigator, 'webdriver property exists in navigator');
    assert(typeof navigator.webdriver === 'boolean', 'navigator.webdriver is boolean');
  });
  await check('document and element expose standard fullscreen api properties and methods', () => {
    assert('fullscreenEnabled' in document, 'fullscreenEnabled exists in document');
    assert('fullscreenElement' in document, 'fullscreenElement exists in document');
    assert(typeof document.exitFullscreen === 'function', 'document.exitFullscreen exists');
    assert(typeof Element.prototype.requestFullscreen === 'function', 'Element.prototype.requestFullscreen exists');
  });
  await check('window exposes ResizeObserver constructor and observe/disconnect methods', () => {
    assert(typeof window.ResizeObserver === 'function', 'ResizeObserver exists');
    const target = document.getElementById('observe-target');
    let callbackTriggered = false;
    const ro = new ResizeObserver((entries) => {
      callbackTriggered = true;
    });
    ro.observe(target);
    ro.unobserve(target);
    ro.disconnect();
    assert(typeof ro.observe === 'function' && typeof ro.disconnect === 'function', 'observe and disconnect methods execute cleanly');
  });
  await check('window exposes IntersectionObserver constructor and observe/disconnect methods', () => {
    assert(typeof window.IntersectionObserver === 'function', 'IntersectionObserver exists');
    const target = document.getElementById('observe-target');
    const io = new IntersectionObserver((entries) => {});
    io.observe(target);
    io.unobserve(target);
    io.disconnect();
    assert(typeof io.observe === 'function' && typeof io.disconnect === 'function', 'observe and disconnect methods execute cleanly');
  });
}
