async () => {
  await check('relative units map to viewport and font geometry', () => {
    equal(innerWidth, 320, 'CSS viewport width'); equal(innerHeight, 240, 'CSS viewport height');
    near(document.querySelector('.vh-margin').getBoundingClientRect().top, 36, '15vh top');
    near(document.querySelector('.vw-margin').getBoundingClientRect().left, 32, '10vw left');
    near(document.querySelector('.em-inner').getBoundingClientRect().left, 40, '2em padding');
    near(document.querySelector('.rem-inner').getBoundingClientRect().left, 36, '2rem padding');
    near(document.querySelector('.vmin-margin').getBoundingClientRect().left, 24, '10vmin left');
    near(document.querySelector('.vmax-margin').getBoundingClientRect().left, 32, '10vmax left');
  });
  await check('style mutation recomputes geometry and hit testing', () => {
    const box = document.querySelector('.vh-margin');
    box.style.width = '140px'; box.style.marginLeft = '20px';
    const bounds = box.getBoundingClientRect();
    near(bounds.left, 20, 'mutated left'); near(bounds.width, 140, 'mutated width');
    equal(document.elementFromPoint(25, 40), box, 'hit test inside moved box');
    assert(document.elementFromPoint(5, 40) !== box, 'old position no longer hits box');
  });
}
