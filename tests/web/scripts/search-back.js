async () => {
  await check('returning to search retains the entered query', () => {
    equal(document.getElementById('field').value, 'C++ café & tea', 'query after returning from results');
  });
}
