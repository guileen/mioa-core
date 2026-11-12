async () => {
  await check('element reflection of role attribute exposes standard WAI-ARIA role property', () => {
    const el = document.getElementById('custom-button');
    assert(el !== null, 'custom button element exists');
    assert(el.role === 'button', 'role IDL property reflects HTML role attribute');
    assert(el.getAttribute('role') === 'button', 'getAttribute matches role');
    el.role = 'link';
    assert(el.getAttribute('role') === 'link', 'mutating role property reflects to HTML attribute');
  });
  await check('element reflection of ariaLabel and aria attributes exposes ARIA state', () => {
    const el = document.getElementById('custom-button');
    assert(el.ariaLabel === 'Perform Action', 'ariaLabel reflects initial aria-label attribute');
    assert(el.ariaRequired === 'true', 'ariaRequired reflects initial aria-required attribute');
    assert(el.ariaHidden === 'false', 'ariaHidden reflects initial aria-hidden attribute');
    el.ariaLabel = 'Updated Action Label';
    assert(el.getAttribute('aria-label') === 'Updated Action Label', 'mutating ariaLabel reflects to HTML attribute');
  });
  await check('native HTML semantic elements expose accessible name and required attributes', () => {
    const form = document.getElementById('contact-form');
    const input = document.getElementById('email-field');
    const btn = document.getElementById('submit-btn');
    assert(form !== null && input !== null && btn !== null, 'native form controls exist');
    assert(form.getAttribute('aria-label') === 'Customer Feedback', 'form element carries accessible label');
    assert(input.getAttribute('aria-label') === 'Email Address', 'input element carries accessible name');
    assert(input.required === true, 'native required attribute reflects boolean property');
    assert(btn.textContent === 'Submit Feedback', 'button element provides accessible text content');
  });
}
