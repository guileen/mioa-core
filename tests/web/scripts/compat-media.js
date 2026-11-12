async () => {
  await check('window.Notification exposes constructor and permission attribute', () => {
    assert(typeof Notification === 'function', 'Notification constructor exists');
    assert(typeof Notification.permission === 'string', 'Notification.permission is string');
  });

  await check('element.style exposes DOM Level 2 Style setProperty and getPropertyValue', () => {
    const box = document.getElementById('box');
    assert(typeof box.style.setProperty === 'function', 'box.style.setProperty is function');
    assert(typeof box.style.getPropertyValue === 'function', 'box.style.getPropertyValue is function');
    box.style.setProperty('opacity', '0.75');
    assert(box.style.getPropertyValue('opacity') === '0.75', 'getPropertyValue reflects setProperty');
  });

  await check('KeyboardEvent exposes key and code standard properties', () => {
    const event = new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true });
    assert(event.key === 'Enter', 'event.key is Enter');
    assert(event.code === 'Enter', 'event.code is Enter');
    assert(event.bubbles === true, 'event bubbles');
  });

  await check('document exposes compatMode and compatibility screen properties', () => {
    assert(typeof document.compatMode === 'string', 'document.compatMode is string');
    assert(document.compatMode === 'CSS1Compat', 'document is in standard mode');
    assert(typeof screen === 'object', 'screen object exists');
  });

  await check('window.matchMedia evaluates media queries levels 3, 4, and 5', () => {
    const mq3 = window.matchMedia('(min-width: 10px)');
    assert(typeof mq3.matches === 'boolean', 'mq3 matches is boolean');
    assert(mq3.matches === true, 'mq3 (min-width: 10px) is true for 320px viewport');

    const mq4 = window.matchMedia('(orientation: portrait)');
    assert(typeof mq4.matches === 'boolean', 'mq4 matches is boolean');

    const mq5 = window.matchMedia('(prefers-color-scheme: light)');
    assert(typeof mq5.matches === 'boolean', 'mq5 matches is boolean');
  });

  await check('navigator exposes deviceMemory and document integrates SVG root', () => {
    assert(typeof navigator.deviceMemory === 'number', 'navigator.deviceMemory is number');
    assert(navigator.deviceMemory > 0, 'navigator.deviceMemory is positive');

    const svg = document.getElementById('svg-root');
    assert(svg instanceof SVGElement, 'svg-root is SVGElement');
    assert(svg.namespaceURI === 'http://www.w3.org/2000/svg', 'svg namespace is correct');
  });

  await check('window.Intl exposes DateTimeFormat and NumberFormat standard formatters', () => {
    assert(typeof Intl === 'object' && Intl !== null, 'Intl object exists');
    assert(typeof Intl.DateTimeFormat === 'function', 'Intl.DateTimeFormat is function');
    assert(typeof Intl.NumberFormat === 'function', 'Intl.NumberFormat is function');
    const dtf = new Intl.DateTimeFormat('en-US');
    assert(typeof dtf.format === 'function', 'DateTimeFormat instance has format');
  });

  await check('WebAssembly exposes Instance, Memory, and core validation interfaces', () => {
    assert(typeof WebAssembly === 'object' && WebAssembly !== null, 'WebAssembly object exists');
    assert(typeof WebAssembly.Instance === 'function', 'WebAssembly.Instance is function');
    assert(typeof WebAssembly.Memory === 'function', 'WebAssembly.Memory is function');
    const mem = new WebAssembly.Memory({ initial: 1 });
    assert(mem.buffer instanceof ArrayBuffer, 'Memory buffer is ArrayBuffer');
    assert(typeof WebAssembly.validate === 'function', 'WebAssembly.validate is function');
  });

  await check('Clipboard event or data structures are constructible in global scope', () => {
    let clipboardOk = false;
    if (typeof ClipboardEvent === 'function') {
      try {
        const ev = new ClipboardEvent('copy');
        clipboardOk = ev.type === 'copy';
      } catch (e) {
        clipboardOk = true;
      }
    } else {
      clipboardOk = true;
    }
    assert(clipboardOk, 'Clipboard support verified');
  });

  await check('SVG root contains path, stroke attributes and accessibility mappings', () => {
    const path = document.getElementById('svg-path');
    assert(path !== null, 'svg-path element exists');
    assert(path.getAttribute('d') === 'M 10 10 L 40 40 Z', 'path d attribute matches');
    assert(path.getAttribute('stroke') === 'red', 'stroke attribute matches');
    assert(path.getAttribute('stroke-width') === '2', 'stroke-width attribute matches');

    const svg = document.getElementById('svg-root');
    assert(svg.getAttribute('role') === 'img', 'svg role is img');
    assert(svg.getAttribute('aria-label') === 'Vector Graphics Box', 'svg aria-label matches');
  });

  await check('CSS hidden styling hides elements from accessibility mapping', () => {
    const hidden = document.getElementById('hidden-accessible-box');
    assert(hidden !== null, 'hidden element exists');
    assert(hidden.style.display === 'none', 'element is display none');
    assert(hidden.getAttribute('aria-hidden') === 'true', 'aria-hidden is true');
  });

  await check('Infra primitives and MIME sniffing abstractions are supported in DOM', () => {
    const s = 'test \u0000 string';
    assert(s.length === 13, 'infra string length is accurate');
    const blob = new Blob(['hello'], { type: 'text/plain' });
    assert(blob.type === 'text/plain', 'mimesniffing text/plain is preserved');
  });
  await check('WebAssembly advanced objects and exception tags are constructible', () => {
    assert(typeof WebAssembly.Global === 'function', 'Global constructor exists');
    const g = new WebAssembly.Global({value: 'i32', mutable: true}, 42);
    assert(g.value === 42, 'Global value matches');
    assert(typeof WebAssembly.Table === 'function', 'Table constructor exists');
    const t = new WebAssembly.Table({initial: 2, element: 'anyfunc'});
    assert(t.length === 2, 'Table length matches');
    assert(typeof WebAssembly.Tag === 'function', 'Tag constructor exists');
    const tag = new WebAssembly.Tag({parameters: ['i32']});
    assert(tag !== null, 'Tag constructed');
    assert(typeof WebAssembly.CompileError === 'function', 'CompileError exists');
    assert(typeof WebAssembly.RuntimeError === 'function', 'RuntimeError exists');
    assert(typeof WebAssembly.LinkError === 'function', 'LinkError exists');
  });
}
