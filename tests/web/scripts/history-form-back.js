async () => {
  await check('back restores text and textarea edits', () => {
    equal(document.getElementById('query').value, 'Café & tea', 'text after back');
    equal(document.getElementById('notes').value, 'First line\nSecond line', 'textarea after back');
  });
  await check('back restores checkbox radio and select state', () => {
    equal(document.getElementById('remember').checked, true, 'checkbox after back');
    equal(document.getElementById('weekly').checked, false, 'previous radio after back');
    equal(document.getElementById('daily').checked, true, 'selected radio after back');
    equal(document.getElementById('category').value, 'books', 'select after back');
  });
  await check('back excludes password and file data', () => {
    equal(document.getElementById('secret').value, '', 'password after back');
    equal(document.getElementById('upload').files.length, 0, 'file selection after back');
  });
  await check('removed control does not receive another control value', () => {
    equal(document.getElementById('removed').value, 'original', 'reappearing control after back');
  });
}
