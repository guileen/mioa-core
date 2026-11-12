async () => {
  await check('window exposes Blob and File constructors with text reading', async () => {
    assert(typeof window.Blob === 'function', 'Blob constructor exists');
    assert(typeof window.File === 'function', 'File constructor exists');
    assert(typeof window.FileReader === 'function', 'FileReader constructor exists');

    const blob = new Blob(['mioa-file-api-payload'], { type: 'text/plain;charset=utf-8' });
    assert(blob.size === 21, `Blob size is 21, got ${blob.size}`);
    assert(blob.type === 'text/plain;charset=utf-8', `Blob type matches, got ${blob.type}`);

    const text = await blob.text();
    assert(text === 'mioa-file-api-payload', `blob.text() returns content, got ${text}`);

    const file = new File(['header-bytes'], 'report.txt', { type: 'text/plain', lastModified: 1700000000000 });
    assert(file.name === 'report.txt', `File name is report.txt, got ${file.name}`);
    assert(file.size === 12, `File size is 12, got ${file.size}`);
    assert(file.lastModified === 1700000000000, `File lastModified matches, got ${file.lastModified}`);
  });
}
