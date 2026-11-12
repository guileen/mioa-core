(payload) => {
  'use strict';
  const results = [];
  for (const test of payload.cases) {
    try {
      if (test.context === 'canvas-rgba-export') {
        const canvas = document.createElement('canvas');
        canvas.width = 3;
        canvas.height = 2;
        const context = canvas.getContext('2d');
        if (!context) throw new Error('2D canvas context unavailable');
        const image = context.createImageData(3, 2);
        for (let i = 0; i < test.rgba.length; i++) image.data[i] = test.rgba[i];
        context.putImageData(image, 0, 0);
        const pixels = Array.from(context.getImageData(0, 0, 3, 2).data);
        const png = canvas.toDataURL('image/png');
        if (typeof png !== 'string' || !png.startsWith('data:image/png;base64,')) {
          throw new Error('canvas PNG export absent or wrong MIME type');
        }
        results.push({id: test.id, value: pixels, png: png});
      } else {
        const container = document.createElement('div');
        container.innerHTML = test.input;
        const value = test.context === 'dom-attribute' ? container.firstElementChild.getAttribute('a') : container.textContent;
        results.push({id: test.id, value: value});
      }
    } catch (error) {
      results.push({id: test.id, error: String(error && error.stack || error)});
    }
  }
  return {schema: 1, complete: true, results: results};
}
