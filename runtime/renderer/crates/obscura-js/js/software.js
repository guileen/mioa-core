// One declaration object per navigator; host configuration changes its private
// backing inputs, never its public identity or implementation-source text.
function _softwareDeclaration(readIdentity) {
  function current() {
    let [ua, platform] = readIdentity();
    ua = String(ua);
    let product, version;
    for (const token of ['Obscura', 'Lightpanda', 'Edg', 'OPR', 'Chrome', 'Chromium', 'Firefox']) {
      const match = ua.match(new RegExp('(?:^|[ \\t])' + token + '/([0-9][A-Za-z0-9.+-]*)'));
      if (match) { product = token; version = match[1]; break; }
    }
    if (!product) {
      const match = ua.match(/^([A-Za-z][A-Za-z0-9.+-]*)\/([0-9][A-Za-z0-9.+-]*)/);
      if (match && match[1] !== 'Mozilla') { product = match[1]; version = match[2]; }
    }
    const names = product === 'Chrome' ? ['Chromium', 'Google Chrome']
      : product === 'Edg' ? ['Chromium', 'Microsoft Edge']
      : product === 'OPR' ? ['Chromium', 'Opera'] : product ? [product] : [];
    const chromium = ua.match(/(?:^|[ \t])(?:Chrome|Chromium)\/([0-9][A-Za-z0-9.+-]*)/);
    const fullVersionList = names.map(brand => ({brand, version: brand === 'Chromium' && chromium ? chromium[1] : version}));
    const brands = fullVersionList.map(item => ({brand: item.brand, version: item.version.split('.')[0]}));
    platform = String(platform || '');
    platform = /^(Win32|Win64|Windows)$/.test(platform) ? 'Windows'
      : /^(MacIntel|MacPPC|macOS)$/.test(platform) ? 'macOS'
      : /^Linux/.test(platform) ? 'Linux' : platform;
    if (!platform) platform = /Windows NT/.test(ua) ? 'Windows' : /Macintosh/.test(ua) ? 'macOS' : /Linux/.test(ua) ? 'Linux' : '';
    return {brands, fullVersionList, version: version || '', platform, mobile: /Mobile|Android/.test(ua)};
  }
  return {
    get brands() { return current().brands; },
    get mobile() { return current().mobile; },
    get platform() { return current().platform; },
    getHighEntropyValues(hints = []) {
      const data = current();
      const result = {brands: data.brands, mobile: data.mobile, platform: data.platform};
      const values = {fullVersionList: data.fullVersionList, uaFullVersion: data.version, architecture: '', bitness: '', model: '', platformVersion: '', wow64: false};
      for (const hint of hints) if (Object.hasOwn(values, hint)) result[hint] = values[hint];
      return Promise.resolve(result);
    },
    toJSON() { const data = current(); return {brands: data.brands, mobile: data.mobile, platform: data.platform}; },
  };
}
