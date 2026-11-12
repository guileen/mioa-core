async () => {
  const track = document.getElementById('entities-track');
  await check('WebVTT named entities decode complete Unicode text', () => {
    equal(window.entitiesTrackEvent, 'load', 'WebVTT resource loaded successfully');
    equal(track.readyState, 2, 'track reached LOADED');
    equal(track.track.cues.length, 3, 'all three actual WebVTT cues parsed');
    const cue = track.track.cues[0];
    equal(cue.id, 'unicode', 'WebVTT cue identifier survives parsing');
    const fragment = cue.getCueAsHTML();
    equal(fragment.nodeType, 11, 'cue HTML is a document fragment');
    equal(fragment.firstChild.localName, 'b', 'cue markup creates the expected element');
    equal(fragment.textContent, '&&<>"\'|\u00C1\u00E1|\u{1D504}\u2242\u0338fj|\t\n',
      'case aliases, astral scalars, two-scalar entities and control entities decode');
    equal(cue.text.includes('&NotEqualTilde;'), true, 'conversion leaves original cue text unchanged');
  });
  await check('WebVTT longest names and malformed references preserve suffixes', () => {
    const cue = track.track.cues[1];
    equal(cue.id, 'longest', 'longest-match cue loaded');
    equal(cue.getCueAsHTML().textContent,
      '\u00ACit;|\u2209|\u00ACin|\u00ACinX|&ersand;|\u00ACAName;|\u2233|&CounterClockwiseContourIntegralX;|&zzzz;|&Amp;',
      'short legacy match survives dead ends; only complete longer references win');
  });
  await check('VTTCue annotations decode entities without re-tokenizing text', () => {
    const cue = track.track.cues[2];
    const fragment = cue.getCueAsHTML();
    equal(fragment.firstChild.localName, 'span', 'voice annotation produces a span');
    equal(fragment.firstChild.getAttribute('title'), 'Voice\u2242\u0338',
      'two-codepoint entity decoded inside voice annotation');
    equal(fragment.textContent, '<b>&notin;', 'decoded angle brackets and ampersands stay literal text');
    equal(fragment.firstChild.childNodes.length, 1, 'decoded markup creates no nested element');
  });
  await check('VTTCue direct conversion shares legacy and malformed entity behavior', () => {
    const cue = new VTTCue(0, 1, '&apos|&apos;|&Aacute|&aacute;|&AMP|&amp;|&notin;&notit;|&Afr;&fjlig;');
    equal(cue.getCueAsHTML().textContent,
      '&apos|\'|\u00C1|\u00E1|&|&|\u2209\u00ACit;|\u{1D504}fj',
      'constructor path uses the same case-sensitive semicolon and longest-match rules');
  });
}
