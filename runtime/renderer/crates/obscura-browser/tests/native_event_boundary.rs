use std::io::{Read, Write};
use std::sync::Arc;

use obscura_browser::{BrowserContext, Page};

// This is the first author script in an actual HTTP response. No controller
// evaluation installs the probe or its listeners before navigation.
const AUTHOR_PAGE: &str = r#"<!doctype html><html><body><button id="target">dispatch target</button>
<script>
  globalThis.boundary = { nativeCalls: 0, hijacked: 0, trace: [] };
  const originalEvent = Event;
  const originalDispatch = Element.prototype.dispatchEvent;
  const target = document.getElementById('target');
  globalThis.boundaryTarget = target;
  const event = new originalEvent('boundary', {bubbles: true, cancelable: true});
  boundary.before = event.isTrusted;
  boundary.marker = [typeof globalThis.__obscura_markTrusted,
    Function('return typeof __obscura_markTrusted')()];
  if (typeof __obscura_markTrusted === 'function') __obscura_markTrusted(event);
  boundary.after = event.isTrusted;
  boundary.privateNames = ['_dispatchTrusted', '_dispatchUntrusted', '_createNativeEvent',
    '_trustedEvents', '__obscura_activateLabel', '__obscura_setInputFiles',
    '__obscura_fireBeforeUnload', '__obscura_deliverMessage', '__obscura_poll_indexeddb',
    'createNativeEvent', 'dispatchTrustedEvent', 'activateLabel', 'nativeDocument'].map(name =>
      [typeof globalThis[name], Function('return typeof ' + name)()]);
  boundary.shadowBlocked = false;
  try { Object.defineProperty(event, 'isTrusted', {value: true}); }
  catch (_) { boundary.shadowBlocked = true; }
  function observe(delivered) {
    boundary.trace.push(delivered.isTrusted);
    if (delivered.isTrusted) {
      boundary.nativeCalls++;
      boundary.nativeEvent = delivered;
      boundary.nativeConstructor = delivered instanceof originalEvent;
      try { boundary.callerHidden = observe.caller === null; }
      catch (_) { boundary.callerHidden = true; }
      delivered.preventDefault();
    }
  }
  target.addEventListener('boundary', observe);
  originalDispatch.call(target, event);
  boundary.redispatch = () => originalDispatch.call(target, boundary.nativeEvent);
  function authorReplacement() { boundary.hijacked++; throw Error('author replacement called'); }
  target.dispatchEvent = authorReplacement;
  window.dispatchEvent = authorReplacement;
  globalThis.Event = authorReplacement;
  globalThis.createNativeEvent = authorReplacement;
  globalThis.dispatchTrustedEvent = authorReplacement;
  globalThis.activateLabel = authorReplacement;
  globalThis.nativeDocument = authorReplacement;
  boundary.firstAuthorFinished = true;
</script></body></html>"#;

fn serve_author_page() -> (String, std::thread::JoinHandle<()>) {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    let server = std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        stream.set_read_timeout(Some(std::time::Duration::from_secs(10))).unwrap();
        let mut request = [0_u8; 2_048];
        let _ = stream.read(&mut request).unwrap();
        write!(stream,
            "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
            AUTHOR_PAGE.len(), AUTHOR_PAGE).unwrap();
    });
    (format!("http://{address}/first-author"), server)
}

#[tokio::test(flavor = "current_thread")]
async fn first_http_author_cannot_forge_trust_or_capture_native_dispatch_capabilities() {
    let context = Arc::new(BrowserContext::with_storage_and_network(
        "native-event-boundary".to_owned(), None, false, None, None, true,
    ));
    let mut page = Page::new("native-event-boundary-page".to_owned(), context);
    let (url, server) = serve_author_page();
    page.navigate(&url).await.unwrap();
    server.join().unwrap();

    assert_eq!(page.evaluate("[boundary.firstAuthorFinished,boundary.before,boundary.marker,boundary.after,boundary.shadowBlocked]"),
        serde_json::json!([true, false, ["undefined", "undefined"], false, true]));
    assert_eq!(page.evaluate("boundary.privateNames"),
        serde_json::json!(vec![["undefined", "undefined"]; 13]));
    assert_eq!(page.evaluate("boundary.trace"), serde_json::json!([false]));

    // Private constructors and dispatch are native-held parameters in a strict
    // host scope. The page replaced every identically named global above.
    assert_eq!(page.evaluate_native_script(
        "dispatchTrustedEvent(boundaryTarget,createNativeEvent('Event','boundary',{bubbles:true,cancelable:true}))"
    ).unwrap(), serde_json::json!(false));
    assert_eq!(page.evaluate("[boundary.hijacked,boundary.nativeCalls,boundary.nativeConstructor,boundary.callerHidden,boundary.trace,boundary.nativeEvent.defaultPrevented]"),
        serde_json::json!([0, 1, true, true, [false, true], true]));

    page.evaluate("boundary.redispatch()");
    assert_eq!(page.evaluate("[boundary.trace,boundary.nativeEvent.isTrusted,boundary.nativeCalls,boundary.hijacked]"),
        serde_json::json!([[false, true, false], false, 1, 0]));
}
