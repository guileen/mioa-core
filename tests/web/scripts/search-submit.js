async () => {
  await check('search submission reaches results with selected controls', () => {
    const params = new URLSearchParams(location.search);
    equal(params.get('q'), 'C++ café & tea', 'submitted query');
    equal(params.get('scope'), 'all', 'submitted scope');
    equal(params.getAll('filter').join(','), 'recent', 'submitted filters');
    equal(params.has('ignored'), false, 'disabled control excluded');
    equal(document.getElementById('result').textContent, 'C++ café & tea', 'results reflect the query');
    assert(document.referrer.includes('fixtures/search-form.html'), 'document.referrer reflects the submission source');
  });
}
