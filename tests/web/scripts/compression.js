async () => {
  await check('CompressionStream constructor initializes readable and writable gzip streams', () => {
    assert(typeof window.CompressionStream === 'function', 'CompressionStream constructor exists');
    const cs = new CompressionStream('gzip');
    assert(cs.readable instanceof ReadableStream, 'readable is a ReadableStream');
    assert(cs.writable instanceof WritableStream, 'writable is a WritableStream');
  });
  await check('DecompressionStream constructor initializes readable and writable decompression streams', () => {
    assert(typeof window.DecompressionStream === 'function', 'DecompressionStream constructor exists');
    const ds = new DecompressionStream('gzip');
    assert(ds.readable instanceof ReadableStream, 'decompression readable is a ReadableStream');
    assert(ds.writable instanceof WritableStream, 'decompression writable is a WritableStream');
  });
  await check('CompressionStream supports piping text chunks through compression transform', async () => {
    const cs = new CompressionStream('gzip');
    const writer = cs.writable.getWriter();
    const reader = cs.readable.getReader();
    const enc = new TextEncoder();
    writer.write(enc.encode('lightpanda-stream-chunk'));
    await writer.close();
    const {value, done} = await reader.read();
    assert(value instanceof Uint8Array, 'compressed chunk is a Uint8Array');
    assert(value.length > 0, 'compressed chunk has non-zero bytes');
  });
}
