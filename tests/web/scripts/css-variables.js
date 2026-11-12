async () => {
  await check('css supports api recognizes var custom property values', () => {
    assert(CSS.supports('color', 'var(--custom-color)'), 'supports var() as property value');
    assert(CSS.supports('--custom-var', '10px'), 'supports custom property declaration');
  });
  await check('element style setProperty stores custom css property and getPropertyValue retrieves it', () => {
    const card = document.getElementById('target-card');
    assert(card !== null, 'target card exists');
    card.style.setProperty('--card-pad', '24px');
    const val = card.style.getPropertyValue('--card-pad');
    assert(val.trim() === '24px', 'getPropertyValue retrieves set custom property');
  });
  await check('element style removeProperty deletes custom property and updates computed value', () => {
    const card = document.getElementById('target-card');
    card.style.removeProperty('--card-pad');
    const val = card.style.getPropertyValue('--card-pad');
    assert(val.trim() === '', 'custom property is empty after removeProperty');
  });
}
