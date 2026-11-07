// Reuse the context's IndexedDB platform codecs for Blob/File alongside V8's
// native clone format. The memo preserves cycles, shared references, typed-array
// backing buffers, dates, maps, sets, bigint and errors across isolates.
function _workerSerialize(value, options) {
  const transfer = Array.isArray(options) ? options : options?.transfer;
  if (transfer?.length) throw new DOMException('Worker transfer lists are not supported', 'DataCloneError');
  try {
    const platform = [];
    const prepared = _idbPrepare(value, new Map(), platform);
    return __obscuraCore.serialize({ value: prepared, platform }, { forStorage: true }, () => {
      throw new DOMException('Value cannot be cloned', 'DataCloneError');
    });
  } catch (error) { throw new DOMException(error.message, 'DataCloneError'); }
}
function _workerDeserialize(bytes) {
  const decoded = __obscuraCore.deserialize(bytes, { forStorage: true });
  if (!decoded.platform.length) return decoded.value;
  const replacements = new Map(decoded.platform.map(([marker, name, data]) => {
    const codec = _idbPlatformCodecs.find(codec => codec.name === name);
    if (!codec) throw new DOMException('Unsupported platform clone', 'DataCloneError');
    return [marker, codec.decode(data)];
  }));
  return _idbRevive(decoded.value, replacements);
}
