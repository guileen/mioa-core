async () => {
  await check('Canvas fill and clear change exact backing pixels', () => {
    const canvas = document.createElement('canvas'); canvas.width = 8; canvas.height = 8;
    const context = canvas.getContext('2d');
    context.fillStyle = '#ff0000'; context.fillRect(1, 1, 4, 4);
    const pixel = context.getImageData(2, 2, 1, 1).data;
    equal(pixel[0], 255, 'red channel'); equal(pixel[1], 0, 'green channel');
    equal(pixel[2], 0, 'blue channel'); equal(pixel[3], 255, 'opaque alpha');
    context.clearRect(2, 2, 1, 1);
    equal(context.getImageData(2, 2, 1, 1).data[3], 0, 'clear removes alpha');
    equal(context.getImageData(1, 1, 1, 1).data[3], 255, 'clear preserves neighboring pixel');
  });
  await check('Canvas state restore preserves the previous fill', () => {
    const canvas = document.createElement('canvas'); canvas.width = 4; canvas.height = 4;
    const context = canvas.getContext('2d');
    context.fillStyle = '#00ff00'; context.save(); context.fillStyle = '#0000ff'; context.restore();
    context.fillRect(0, 0, 4, 4);
    const pixel = context.getImageData(1, 1, 1, 1).data;
    equal(pixel[0], 0, 'restored red'); equal(pixel[1], 255, 'restored green');
    equal(pixel[2], 0, 'restored blue'); equal(pixel[3], 255, 'restored alpha');
  });
}
