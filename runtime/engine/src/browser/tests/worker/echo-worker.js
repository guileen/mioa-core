// Simple worker that echoes messages back with a prefix
onmessage = function(event) {
  if (event.data && event.data.canvasTransfer === 'draw') {
    const canvas = event.data.canvas;
    const context = canvas.getContext('2d');
    context.fillStyle = '#ff0000';
    context.fillRect(0, 0, canvas.width, canvas.height);
    postMessage({
      canvasTransfer: 'drawn',
      pixels: Array.from(context.getImageData(0, 0, 1, 1).data),
      sameReference: event.data.canvas === event.data.again,
      workerRealm: self instanceof DedicatedWorkerGlobalScope,
      documentType: typeof document,
    });
    return;
  }
  if (event.data && event.data.canvasTransfer === 'return') {
    const canvas = event.data.canvas;
    postMessage({ canvasTransfer: 'returned', canvas }, [canvas]);
    return;
  }
  if (event.data && event.data.canvasTransfer === 'create') {
    const canvas = new OffscreenCanvas(2, 2);
    postMessage({ canvasTransfer: 'created', canvas }, { transfer: [canvas] });
    return;
  }
  postMessage({ echo: event.data, from: 'worker' });
};
