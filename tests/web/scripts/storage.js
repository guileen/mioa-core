async () => {
  await check('localStorage persists, retrieves, removes and clears key-value pairs', () => {
    localStorage.clear();
    equal(localStorage.length, 0, 'empty initial localStorage length');
    localStorage.setItem('user', 'core-tester');
    localStorage.setItem('theme', 'dark');
    equal(localStorage.length, 2, 'populated localStorage length');
    equal(localStorage.getItem('user'), 'core-tester', 'retrieved stored value');
    localStorage.removeItem('theme');
    equal(localStorage.length, 1, 'length after item removal');
    equal(localStorage.getItem('theme'), null, 'removed item returns null');
    localStorage.clear();
    equal(localStorage.length, 0, 'length after clear');
  });
  await check('sessionStorage maintains separate session-isolated key-value entries', () => {
    sessionStorage.clear();
    equal(sessionStorage.length, 0, 'empty initial sessionStorage length');
    sessionStorage.setItem('tab_token', 'token_xyz');
    equal(sessionStorage.getItem('tab_token'), 'token_xyz', 'sessionStorage item value');
    equal(sessionStorage.length, 1, 'sessionStorage length');
    sessionStorage.removeItem('tab_token');
    equal(sessionStorage.getItem('tab_token'), null, 'removed sessionStorage item returns null');
  });
}
