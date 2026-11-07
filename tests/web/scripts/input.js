async () => {
  await check('native pointer and text input update focused control', () => {
    const field = document.getElementById('field');
    equal(document.activeElement, field, 'native click focused input');
    equal(field.value, 'Core', 'native text insertion changed value');
    equal(window.inputEvents, 1, 'one insertion emitted one input event');
    equal(document.elementFromPoint(40, 35), field, 'painted input is pointer target');
  });
  await check('focus transfer follows the active element', () => {
    const second = document.getElementById('second');
    second.focus();
    equal(document.activeElement, second, 'focus moved to second input');
    equal(window.lastFocus, 'second', 'focus event identifies destination');
    equal(document.getElementById('field').value, 'Core', 'focus change preserves first value');
  });
}
