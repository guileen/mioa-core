async () => {
  await check('window exposes PointerEvent constructor and pointer state attributes', () => {
    assert(typeof window.PointerEvent === 'function', 'PointerEvent constructor exists');
    const pe = new PointerEvent('pointerdown', {
      pointerId: 42,
      pointerType: 'mouse',
      width: 10.5,
      height: 20.5,
      pressure: 0.75,
      clientX: 100,
      clientY: 200,
      isPrimary: true
    });
    assert(pe.pointerId === 42, 'pointerId is 42');
    assert(pe.pointerType === 'mouse', 'pointerType is mouse');
    assert(pe.clientX === 100 && pe.clientY === 200, 'clientX and clientY match');
    assert(pe.width === 10.5 && pe.height === 20.5, 'width and height match');
    assert(pe.isPrimary === true, 'isPrimary is true');

    const touchPe = new PointerEvent('pointermove', {
      pointerId: 7,
      pointerType: 'touch',
      width: 2.0,
      height: 2.0
    });
    assert(touchPe.pointerId === 7, 'touchPe pointerId is 7');
    assert(touchPe.pointerType === 'touch', 'touchPe pointerType is touch');
    assert(touchPe.width === 2.0 && touchPe.height === 2.0, 'touchPe dimensions match');
  });

  await check('window exposes Touch and TouchEvent constructors', () => {
    assert(typeof window.TouchEvent === 'function', 'TouchEvent constructor exists');
    const te = new TouchEvent('touchstart', {
      altKey: false,
      ctrlKey: false,
      metaKey: false,
      shiftKey: false
    });
    assert(te !== null && te.type === 'touchstart', 'TouchEvent initialized with type touchstart');
    assert(te instanceof TouchEvent && te instanceof UIEvent, 'te is instance of TouchEvent and UIEvent');
    assert(te.touches !== null && typeof te.touches === 'object', 'touches is TouchList');
    assert(te.changedTouches !== null && typeof te.changedTouches === 'object', 'changedTouches is TouchList');
    assert(te.targetTouches !== null && typeof te.targetTouches === 'object', 'targetTouches is TouchList');
  });

  await check('window exposes Web Cryptography API and subtle crypto interface', () => {
    assert(window.crypto !== null && typeof window.crypto === 'object', 'window.crypto exists');
    assert(typeof window.crypto.getRandomValues === 'function', 'crypto.getRandomValues function exists');
    const buf = new Uint8Array(16);
    window.crypto.getRandomValues(buf);
    assert(buf.some(b => b !== 0), 'getRandomValues generated non-zero entropy');

    assert(typeof window.crypto.randomUUID === 'function', 'crypto.randomUUID function exists');
    const uuid = window.crypto.randomUUID();
    assert(typeof uuid === 'string' && uuid.length === 36, `randomUUID returned valid RFC4122 length: ${uuid}`);
    assert(uuid.charAt(14) === '4', `UUID indicates version 4: ${uuid}`);

    assert(window.crypto.subtle !== null && typeof window.crypto.subtle === 'object', 'crypto.subtle object exists');
  });
}
