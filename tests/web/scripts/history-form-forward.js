async () => {
  await check('forward restores its own form entry', () => {
    equal(document.getElementById('forward').value, 'Forward entry', 'text after forward');
  });
}
