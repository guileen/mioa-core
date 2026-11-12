async () => {
  await check('window exposes WebSocket constructor and standard readyState constants', () => {
    assert(typeof window.WebSocket === 'function', 'WebSocket constructor exists');
    assert(WebSocket.CONNECTING === 0, 'CONNECTING is 0');
    assert(WebSocket.OPEN === 1, 'OPEN is 1');
    assert(WebSocket.CLOSING === 2, 'CLOSING is 2');
    assert(WebSocket.CLOSED === 3, 'CLOSED is 3');
  });
  await check('websocket instance initializes with CONNECTING readyState and normalized url', () => {
    const ws = new WebSocket('ws://127.0.0.1:1/socket-test');
    assert(ws.readyState === WebSocket.CONNECTING, 'initial readyState is CONNECTING');
    assert(ws.url.startsWith('ws://127.0.0.1:1/'), 'normalized url includes target path');
    assert(ws.binaryType === 'blob', 'default binaryType is blob');
    assert(ws.protocol === '', 'default protocol is empty');
    ws.close();
  });
  await check('websocket binaryType supports arraybuffer assignment', () => {
    const ws = new WebSocket('ws://127.0.0.1:1/socket-test');
    ws.binaryType = 'arraybuffer';
    assert(ws.binaryType === 'arraybuffer', 'binaryType mutated to arraybuffer');
    ws.close();
  });
}
