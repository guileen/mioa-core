async () => {
  await check('nested scrollIntoView exposes target', () => {
    const target = document.getElementById('normal-target');
    const port = target.parentElement;
    target.scrollIntoView({block:'nearest', inline:'nearest'});
    const box = target.getBoundingClientRect(), viewport = port.getBoundingClientRect();
    assert(port.scrollTop > 0, 'nested scroll offset changed');
    near(box.width, 100, 'target width'); near(box.height, 40, 'target height');
    assert(box.top >= viewport.top + 5 && box.bottom <= viewport.bottom - 5, 'target lies inside bordered port');
    near(box.left, viewport.left + 5, 'target starts at scrollport content edge');
  });
  await check('hit testing follows scrolled geometry', () => {
    const target = document.getElementById('normal-target');
    const port = target.parentElement;
    let box = target.getBoundingClientRect();
    equal(document.elementFromPoint(box.left + 10, box.top + 10), target, 'visible target is hit');
    port.scrollTop = 0;
    assert(document.elementFromPoint(box.left + 10, box.top + 10) !== target, 'scrolled-out target is not hit');
    box = target.getBoundingClientRect();
    near(box.top, port.getBoundingClientRect().top + 605, 'target geometry reflects reset scroll');
  });
}
