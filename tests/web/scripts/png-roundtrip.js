async () => {
  const rgba = new Uint8ClampedArray([
    255, 0, 17, 255, 32, 200, 64, 128, 123, 45, 67, 0,
    1, 2, 3, 1, 255, 254, 253, 254, 9, 8, 7, 64,
  ]);
  // Retain real native output for tests/run_png_roundtrip.py's independent
  // Python zlib decoder, not merely the in-engine DecompressionStream check.
  window.pngRoundtripExports = [];
  const canvas = () => {
    const value = document.createElement('canvas');
    value.width = 3; value.height = 2;
    return value;
  };
  const paint = (value, pixels = rgba, width = 3, height = 2) => {
    const context = value.getContext('2d');
    const image = context.createImageData(width, height);
    image.data.set(pixels); context.putImageData(image, 0, 0);
  };
  const toBlob = value => new Promise(resolve => value.toBlob(resolve));
  const fromURL = url => {
    equal(url.slice(0, 22), 'data:image/png;base64,', 'PNG data URL MIME');
    return Uint8Array.from(atob(url.slice(22)), byte => byte.charCodeAt(0));
  };
  const crc32 = bytes => {
    let crc = 0xffffffff;
    for (const byte of bytes) {
      crc ^= byte;
      for (let bit = 0; bit < 8; bit++) crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0);
    }
    return (~crc) >>> 0;
  };
  const paeth = (a, b, c) => {
    const p = a + b - c, x = Math.abs(p - a), y = Math.abs(p - b), z = Math.abs(p - c);
    return x <= y && x <= z ? a : y <= z ? b : c;
  };
  async function verify(name, bytes, width, height, expected) {
    equal(Array.from(bytes.subarray(0, 8)).join(), '137,80,78,71,13,10,26,10', 'PNG signature');
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const chunks = []; let offset = 8, ihdr = 0, iend = 0, compressedLength = 0;
    while (offset < bytes.length) {
      assert(offset + 12 <= bytes.length, 'complete PNG chunk framing');
      const length = view.getUint32(offset);
      assert(offset + length + 12 <= bytes.length, 'PNG chunk stays within output');
      const kind = String.fromCharCode(...bytes.subarray(offset + 4, offset + 8));
      equal(crc32(bytes.subarray(offset + 4, offset + 8 + length)), view.getUint32(offset + 8 + length), kind + ' CRC');
      if (kind === 'IHDR') {
        equal(offset, 8, 'IHDR is first'); equal(length, 13, 'IHDR length'); ihdr++;
        equal(view.getUint32(offset + 8), width, 'PNG width');
        equal(view.getUint32(offset + 12), height, 'PNG height');
        equal(Array.from(bytes.subarray(offset + 16, offset + 21)).join(), '8,6,0,0,0', 'RGBA8 noninterlaced format');
      } else if (kind === 'IDAT') {
        chunks.push(bytes.subarray(offset + 8, offset + 8 + length)); compressedLength += length;
      } else if (kind === 'IEND') {
        equal(length, 0, 'IEND length'); equal(offset + 12, bytes.length, 'no trailing PNG data'); iend++;
      } else assert((bytes[offset + 4] & 32) !== 0, 'no unknown critical chunk');
      offset += length + 12;
    }
    equal(ihdr, 1, 'one IHDR'); equal(iend, 1, 'one IEND'); assert(chunks.length > 0, 'IDAT present');
    const compressed = new Uint8Array(compressedLength); let cursor = 0;
    for (const part of chunks) { compressed.set(part, cursor); cursor += part.length; }
    const stream = new Blob([compressed]).stream().pipeThrough(new DecompressionStream('deflate'));
    const reader = stream.getReader(), parts = []; let size = 0;
    for (;;) {
      const item = await reader.read(); if (item.done) break;
      parts.push(item.value); size += item.value.length;
    }
    equal(size, (width * 4 + 1) * height, 'exact inflated scanline size');
    const scanlines = new Uint8Array(size); cursor = 0;
    for (const part of parts) { scanlines.set(part, cursor); cursor += part.length; }
    const pixels = new Uint8Array(width * height * 4), stride = width * 4;
    for (let y = 0; y < height; y++) {
      const filter = scanlines[y * (stride + 1)]; assert(filter <= 4, 'standard PNG filter');
      for (let x = 0; x < stride; x++) {
        const i = y * stride + x, a = x >= 4 ? pixels[i - 4] : 0;
        const b = y ? pixels[i - stride] : 0, c = y && x >= 4 ? pixels[i - stride - 4] : 0;
        const prediction = [0, a, b, Math.floor((a + b) / 2), paeth(a, b, c)][filter];
        pixels[i] = scanlines[y * (stride + 1) + 1 + x] + prediction;
      }
    }
    equal(Array.from(pixels).join(), Array.from(expected).join(), 'all straight RGBA channels and rows roundtrip');
    window.pngRoundtripExports.push({name, width, height, pixels: Array.from(expected),
      base64: btoa(Array.from(bytes, byte => String.fromCharCode(byte)).join(''))});
    return bytes.length;
  }
  async function blobBytes(blob) {
    assert(blob instanceof Blob, 'PNG is a Blob'); equal(blob.type, 'image/png', 'Blob MIME');
    return new Uint8Array(await blob.arrayBuffer());
  }
  await check('Canvas PNG exports preserve exact straight RGBA rows', async () => {
    const value = canvas();
    await verify('no-context blank', fromURL(value.toDataURL()), 3, 2, new Uint8Array(24));
    await verify('no-context blank Blob', await blobBytes(await toBlob(value)), 3, 2, new Uint8Array(24));
    paint(value);
    await verify('toDataURL RGBA', fromURL(value.toDataURL()), 3, 2, rgba);
    await verify('toBlob RGBA', await blobBytes(await toBlob(value)), 3, 2, rgba);
  });
  await check('Canvas PNG resize and empty exports follow bitmap state', async () => {
    const value = canvas(); paint(value);
    const captured = toBlob(value);
    value.width = 2; value.height = 3;
    await verify('toBlob snapshot before resize', await blobBytes(await captured), 3, 2, rgba);
    await verify('resized data URL cleared', fromURL(value.toDataURL()), 2, 3, new Uint8Array(24));
    await verify('resized Blob cleared', await blobBytes(await toBlob(value)), 2, 3, new Uint8Array(24));
    value.width = 0;
    equal(value.toDataURL(), 'data:,', 'zero-width data URL'); equal(await toBlob(value), null, 'zero-width Blob');
    value.width = 2; value.height = 0;
    equal(value.toDataURL(), 'data:,', 'zero-height data URL'); equal(await toBlob(value), null, 'zero-height Blob');
  });
  await check('OffscreenCanvas PNG exports preserve pixels, resize and empty behavior', async () => {
    const value = new OffscreenCanvas(3, 2); paint(value);
    await verify('OffscreenCanvas RGBA', await blobBytes(await value.convertToBlob()), 3, 2, rgba);
    value.width = 2; value.height = 3;
    await verify('OffscreenCanvas resized cleared', await blobBytes(await value.convertToBlob()), 2, 3, new Uint8Array(24));
    value.width = 0;
    let failure;
    try { await value.convertToBlob(); } catch (error) { failure = error.name; }
    equal(failure, 'IndexSizeError', 'zero-width OffscreenCanvas rejects');
    value.width = 2; value.height = 0; failure = undefined;
    try { await value.convertToBlob(); } catch (error) { failure = error.name; }
    equal(failure, 'IndexSizeError', 'zero-height OffscreenCanvas rejects');
  });
  await check('WebGL Canvas PNG exports preserve actual GPU colors', async () => {
    const expected = new Uint8Array([255, 0, 0, 255, 255, 0, 0, 255, 255, 0, 0, 255, 255, 0, 0, 255]);
    const value = document.createElement('canvas'); value.width = 2; value.height = 2;
    const gl = value.getContext('webgl', {preserveDrawingBuffer: true});
    assert(gl !== null, 'native WebGL context');
    gl.clearColor(1, 0, 0, 1); gl.clear(gl.COLOR_BUFFER_BIT);
    await verify('WebGL Canvas RGBA', fromURL(value.toDataURL()), 2, 2, expected);
    const offscreen = new OffscreenCanvas(2, 2);
    const offscreenGL = offscreen.getContext('webgl', {preserveDrawingBuffer: true});
    assert(offscreenGL !== null, 'native OffscreenCanvas WebGL context');
    offscreenGL.clearColor(1, 0, 0, 1); offscreenGL.clear(offscreenGL.COLOR_BUFFER_BIT);
    await verify('OffscreenCanvas WebGL RGBA', await blobBytes(await offscreen.convertToBlob()), 2, 2, expected);
  });
  await check('Canvas PNG compression is effective across long images', async () => {
    const value = canvas(); value.width = 257; value.height = 67;
    const pixels = new Uint8ClampedArray(257 * 67 * 4);
    for (let i = 0; i < pixels.length; i++) pixels[i] = i % 251;
    paint(value, pixels, 257, 67);
    const length = await verify('multi-window compressed RGBA', fromURL(value.toDataURL()), 257, 67, pixels);
    assert(length < pixels.length / 4, 'PNG is compressed, not stored DEFLATE');
    const randomCanvas = canvas(); randomCanvas.width = 97; randomCanvas.height = 41;
    const randomPixels = new Uint8ClampedArray(97 * 41 * 4); let state = 0x504e47;
    for (let i = 0; i < randomPixels.length; i++) {
      state ^= state << 13; state ^= state >>> 17; state ^= state << 5;
      randomPixels[i] = state >>> 24;
    }
    paint(randomCanvas, randomPixels, 97, 41);
    await verify('high-entropy RGBA', fromURL(randomCanvas.toDataURL()), 97, 41, randomPixels);
  });
}
