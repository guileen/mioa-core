async () => {
  await check('cookieStore sets, gets, lists, and deletes scoped cookies', async () => {
    assert(typeof window.cookieStore !== 'undefined', 'window.cookieStore availability');
    await cookieStore.set({name: 'session_auth', value: 'auth_value_42'});
    const entry = await cookieStore.get('session_auth');
    assert(entry !== null, 'cookie entry retrieved');
    equal(entry.value, 'auth_value_42', 'retrieved cookie value matches');
    const all = await cookieStore.getAll();
    assert(Array.isArray(all) && all.length >= 1, 'cookieStore getAll returns list with at least 1 entry');
    // rfc6454 (The Web Origin Concept) & rfc6265bis (Cookies with sameSite)
    assert(typeof window.location.origin === 'string' && window.location.origin.length > 0, 'window.location.origin is defined');
    await cookieStore.set({name: 'samesite_pref', value: 'pref_1', sameSite: 'lax'});
    const sameSiteEntry = await cookieStore.get('samesite_pref');
    assert(sameSiteEntry !== null && sameSiteEntry.value === 'pref_1', 'sameSite cookie stored and retrieved');
    await cookieStore.delete('samesite_pref');
    await cookieStore.delete('session_auth');
    const after = await cookieStore.get('session_auth');
    equal(after, null, 'deleted cookie returns null');
  });
  await check('navigator.permissions queries queryable permission status objects', async () => {
    assert(typeof navigator.permissions !== 'undefined', 'navigator.permissions availability');
    const geoStatus = await navigator.permissions.query({name: 'geolocation'});
    assert(geoStatus instanceof PermissionStatus, 'query returns PermissionStatus instance');
    assert(typeof geoStatus.state === 'string', 'permission state is string');
    const notifStatus = await navigator.permissions.query({name: 'notifications'});
    assert(notifStatus instanceof PermissionStatus, 'notifications query returns PermissionStatus instance');
    assert(typeof notifStatus.state === 'string', 'notifications state is string');
  });
}
