async () => {
  await check('search input is included in form data', () => {
    const form = document.getElementById('search');
    const data = new FormData(form);
    equal(data.get('q'), 'Core', 'typed search query');
    equal(data.get('scope'), 'all', 'hidden search scope');
    equal(data.getAll('filter').join(','), 'recent', 'only checked filters included');
    equal(data.has('ignored'), false, 'disabled control excluded');
  });
}
