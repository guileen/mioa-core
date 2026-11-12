async () => {
  await check('window exposes indexedDB factory with open method', () => {
    assert(typeof window.indexedDB === 'object' && window.indexedDB !== null, 'window.indexedDB is available');
    assert(typeof window.indexedDB.open === 'function', 'indexedDB.open is a function');
    assert(typeof window.IDBDatabase !== 'undefined' || typeof window.IDBRequest !== 'undefined', 'IDB interface exposed');
  });

  await check('indexedDB.open returns IDBOpenDBRequest and triggers events', async () => {
    const dbName = 'test-db-' + Date.now();
    const req = window.indexedDB.open(dbName, 1);
    assert(req !== null && typeof req === 'object', 'open request returned');
    assert(typeof req.addEventListener === 'function' || 'onsuccess' in req, 'request handles events');
    await new Promise((resolve, reject) => {
      let upgraded = false;
      const timer = setTimeout(() => reject(new Error('indexedDB.open timed out without success')), 2000);
      const fail = error => { clearTimeout(timer); reject(error); };
      req.onupgradeneeded = (e) => {
        try {
          const db = e.target.result;
          assert(db && typeof db.createObjectStore === 'function', 'upgrade exposes createObjectStore');
          db.createObjectStore('items', { keyPath: 'id' });
          upgraded = true;
        } catch (error) { fail(error); }
      };
      req.onsuccess = (e) => {
        const db = e.target.result;
        try {
          assert(upgraded, 'new database completed upgrade');
          assert(db !== null, 'database opened');
          assert(db.name === dbName, `database name matches: ${db.name}`);
          assert(db.objectStoreNames.contains('items'), 'upgraded object store exists');
          assert(typeof db.close === 'function', 'database can close');
          db.close();
          clearTimeout(timer);
          resolve();
        } catch (error) {
          if (db && typeof db.close === 'function') db.close();
          fail(error);
        }
      };
      req.onerror = () => fail(new Error('indexedDB.open failed: ' + (req.error?.name || 'unknown error')));
      req.onblocked = () => fail(new Error('indexedDB.open blocked'));
    });
  });
}
