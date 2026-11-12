async () => {
  await check('canvas exposes 2D and WebGL rendering contexts', () => {
    const canvas = document.getElementById('canvas-target');
    assert(canvas !== null, 'canvas target element found');
    const ctx2d = canvas.getContext('2d');
    assert(ctx2d !== null && typeof ctx2d === 'object', '2d context acquired');

    // Create a secondary canvas for WebGL to avoid context collision if any
    const webglCanvas = document.createElement('canvas');
    webglCanvas.width = 64;
    webglCanvas.height = 64;
    const gl = webglCanvas.getContext('webgl') || webglCanvas.getContext('experimental-webgl');
    assert(gl !== null && typeof gl === 'object', 'WebGL context acquired');
    assert(typeof gl.clearColor === 'function', 'gl.clearColor function exists');
    assert(typeof gl.createBuffer === 'function', 'gl.createBuffer function exists');
    assert(gl.canvas === webglCanvas, 'gl.canvas points to webgl canvas');
    const dbgExt = gl.getExtension('WEBGL_debug_renderer_info');
    assert(dbgExt !== null && typeof dbgExt === 'object', 'WEBGL_debug_renderer_info extension acquired');
    assert(dbgExt.UNMASKED_VENDOR_WEBGL === 0x9245, 'UNMASKED_VENDOR_WEBGL property matches 0x9245');
    assert(dbgExt.UNMASKED_RENDERER_WEBGL === 0x9246, 'UNMASKED_RENDERER_WEBGL property matches 0x9246');
  });

  await check('document renders SVG2 elements and exposes SVG DOM interfaces', () => {
    const svg = document.getElementById('svg-root');
    assert(svg !== null, 'svg element found');
    assert(svg instanceof SVGSVGElement, 'svg is SVGSVGElement');

    const rect = document.getElementById('svg-rect');
    assert(rect !== null && rect instanceof SVGRectElement, 'rect is SVGRectElement');

    const circle = document.getElementById('svg-circle');
    assert(circle !== null && circle instanceof SVGCircleElement, 'circle is SVGCircleElement');

    const path = document.getElementById('svg-path');
    assert(path !== null && path instanceof SVGPathElement, 'path is SVGPathElement');

    assert(typeof svg.createSVGPoint === 'function', 'svg.createSVGPoint method exists');
    const pt = svg.createSVGPoint();
    assert(pt !== null && typeof pt.x === 'number', 'SVGPoint created with numeric coordinate');
  });

  await check('DOMParser parses HTML and XML documents into DOM trees', () => {
    assert(typeof window.DOMParser === 'function', 'DOMParser constructor exists');
    const parser = new window.DOMParser();
    assert(parser !== null && typeof parser.parseFromString === 'function', 'parseFromString method exists');

    const parsed = parser.parseFromString('<div id="parsed-root"><span class="child">parsed text</span></div>', 'text/html');
    assert(parsed !== null, 'parsed document created');
    const elem = parsed.getElementById('parsed-root');
    assert(elem !== null, 'parsed element found by ID');
    const span = parsed.querySelector('.child');
    assert(span !== null && span.textContent === 'parsed text', 'parsed text content matches');
  });

  await check('navigator exposes Geolocation API interface', () => {
    assert(navigator.geolocation !== null && typeof navigator.geolocation === 'object', 'navigator.geolocation exists');
    assert(typeof navigator.geolocation.getCurrentPosition === 'function', 'getCurrentPosition function exists');
    assert(typeof navigator.geolocation.watchPosition === 'function', 'watchPosition function exists');
    assert(typeof navigator.geolocation.clearWatch === 'function', 'clearWatch function exists');
  });
}
