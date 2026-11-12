async () => {
  await check('ReadableStream enqueues, reads sequential chunks, and signals completion', async () => {
    const stream = new ReadableStream({
      start(controller) {
        controller.enqueue('chunk-1');
        controller.enqueue('chunk-2');
        controller.close();
      }
    });
    const reader = stream.getReader();
    const r1 = await reader.read();
    assert(!r1.done, 'first chunk done flag');
    equal(r1.value, 'chunk-1', 'first chunk payload');
    const r2 = await reader.read();
    assert(!r2.done, 'second chunk done flag');
    equal(r2.value, 'chunk-2', 'second chunk payload');
    const r3 = await reader.read();
    assert(r3.done === true, 'terminal chunk done flag');
  });
  await check('TransformStream transforms chunks via piped readable and writable streams', async () => {
    const ts = new TransformStream({
      transform(chunk, controller) {
        controller.enqueue(chunk.toUpperCase());
      }
    });
    const rs = new ReadableStream({
      start(controller) {
        controller.enqueue('hello-stream');
        controller.close();
      }
    });
    const transformed = rs.pipeThrough(ts);
    const reader = transformed.getReader();
    const r = await reader.read();
    assert(!r.done, 'transformed chunk done flag');
    equal(r.value, 'HELLO-STREAM', 'transformed uppercase chunk payload');
    const terminal = await reader.read();
    assert(terminal.done === true, 'transformed stream terminal done flag');
  });
}
