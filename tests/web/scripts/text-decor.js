async () => {
  await check('css supports api recognizes text-decoration properties', () => {
    assert(CSS.supports('text-decoration-line', 'underline'), 'supports text-decoration-line');
    assert(CSS.supports('text-decoration-color', 'red'), 'supports text-decoration-color');
    assert(CSS.supports('text-decoration-style', 'solid'), 'supports text-decoration-style');
  });
  await check('inline and computed text-decoration line and color styles are retained', () => {
    const el = document.getElementById('target-dynamic');
    assert(el !== null, 'dynamic element exists');
    el.style.textDecorationLine = 'underline';
    el.style.textDecorationColor = 'rgb(0, 128, 0)';
    el.style.textDecorationStyle = 'dashed';
    assert(el.style.textDecorationLine === 'underline', 'textDecorationLine reflects inline style');
    assert(el.style.textDecorationColor === 'rgb(0, 128, 0)', 'textDecorationColor reflects inline style');
    assert(el.style.textDecorationStyle === 'dashed', 'textDecorationStyle reflects inline style');
  });
  await check('text-decoration shorthand sets line decoration', () => {
    const el = document.getElementById('target-dynamic');
    el.style.textDecoration = 'line-through';
    assert(el.style.textDecoration === 'line-through' || el.style.textDecorationLine === 'line-through', 'shorthand updates text decoration');
  });
}
