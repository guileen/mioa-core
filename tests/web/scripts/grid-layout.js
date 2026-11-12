async () => {
  await check('grid container distributes initial track columns according to grid-template-columns', () => {
    const itemA = document.getElementById('item-a');
    const itemB = document.getElementById('item-b');
    assert(itemA !== null && itemB !== null, 'grid item elements exist');
    const rectA = itemA.getBoundingClientRect();
    const rectB = itemB.getBoundingClientRect();
    near(rectA.width, 80, 'first column initial width');
    near(rectB.width, 120, 'second column initial width');
    near(rectA.left, 0, 'first column initial left');
    near(rectB.left, 80, 'second column initial left');
  });
  await check('dynamic mutation of grid-template-columns updates grid child geometry', () => {
    const wrap = document.querySelector('.grid-wrap');
    assert(wrap !== null, 'grid container exists');
    wrap.style.gridTemplateColumns = '50px 150px';
    const itemA = document.getElementById('item-a');
    const itemB = document.getElementById('item-b');
    const rectA = itemA.getBoundingClientRect();
    const rectB = itemB.getBoundingClientRect();
    near(rectA.width, 50, 'first column mutated width');
    near(rectB.width, 150, 'second column mutated width');
    near(rectA.left, 0, 'first column mutated left');
    near(rectB.left, 50, 'second column mutated left');
  });
  await check('grid item placement with grid-row and grid-column positions items into tracks', () => {
    const wrap = document.querySelector('.grid-wrap');
    const itemA = document.getElementById('item-a');
    itemA.style.gridRow = '1 / 2';
    itemA.style.gridColumn = '2 / 3';
    const rectA = itemA.getBoundingClientRect();
    near(rectA.left, 50, 'placed item-a starts at column 2 left');
    near(rectA.width, 150, 'placed item-a spans column 2 width');
    near(rectA.top, 0, 'placed item-a starts at row 1 top');
  });
}
