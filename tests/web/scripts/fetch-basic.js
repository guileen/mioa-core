async () => {
  await check('same-origin fetch returns the response body', async () => {
    const response = await fetch('/fixtures/empty.html');
    equal(response.status, 200, 'successful status');
    equal(response.ok, true, 'successful ok flag');
    equal(response.type, 'basic', 'same-origin response type');
    assert((await response.text()).includes('Harness negative control document'), 'response body');
    equal(response.bodyUsed, true, 'body consumed');
  });
  await check('HTTP errors resolve with an unsuccessful response', async () => {
    const response = await fetch('/fixtures/mioa-missing-fetch-resource.html');
    equal(response.status, 404, 'missing resource status');
    equal(response.ok, false, 'missing resource ok flag');
    equal(await response.text(), 'not found', 'missing resource body');
  });
  await check('unsupported schemes reject the fetch promise', async () => {
    let rejected = false;
    try {
      await fetch('mailto:nobody@example.invalid');
    } catch (error) {
      rejected = error instanceof TypeError;
    }
    assert(rejected, 'unsupported scheme rejects with TypeError');
  });
}
