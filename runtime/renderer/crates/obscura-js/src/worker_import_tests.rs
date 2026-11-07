// Actual loopback requests exercise the same Worker native loader and HTTP
// policies as production; no evaluator/fetch hooks or substitute script bytes.
async fn authenticity_worker_import_server() -> (
    String, std::sync::Arc<std::sync::Mutex<Vec<String>>>, tokio::task::JoinHandle<()>,
) {
    use tokio::io::{AsyncReadExt as _, AsyncWriteExt as _};
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    let receipts = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let captured = receipts.clone();
    let server = tokio::spawn(async move {
        loop {
            let Ok((mut stream, _)) = listener.accept().await else { break };
            let captured = captured.clone();
            tokio::spawn(async move {
                let mut request = Vec::new();
                let mut chunk = [0_u8; 2048];
                while request.len() < 16_384 && !request.windows(4).any(|bytes| bytes == b"\r\n\r\n") {
                    let count = stream.read(&mut chunk).await.unwrap_or(0);
                    if count == 0 { break; }
                    request.extend_from_slice(&chunk[..count]);
                }
                let request = String::from_utf8_lossy(&request).into_owned();
                let path = request.lines().next().and_then(|line| line.split_whitespace().nth(1)).unwrap_or("/").to_owned();
                captured.lock().unwrap().push(request);
                let (status, mime, body, extra) = match path.as_str() {
                    "/start-worker.js" => ("302 Found", "text/javascript", "", "Location: /scripts/main.js\r\n"),
                    "/scripts/main.js" => ("200 OK", "application/javascript", r#"
                        globalThis.importOrder=[];
                        importScripts('sub/one.js','two.js');
                        const failures=[];
                        for (const url of ['missing.js','wrong.js','forbidden.js','blocked.js','syntax.js','throws.js']) {
                            try { importScripts(url); failures.push('none'); } catch (error) { failures.push(error.name); }
                        }
                        try { importScripts('never.js','http://['); failures.push('none'); }
                        catch (error) { failures.push(error.name); }
                        postMessage({order:importOrder,value:importedValue+5,failures,
                            sameObject:navigator.userAgentData===navigator.userAgentData,
                            software:navigator.userAgentData.toJSON(),scriptPath:location.pathname});
                    "#, ""),
                    "/scripts/sub/one.js" => ("200 OK", "text/javascript; charset=utf-8",
                        "let importedValue=37; importOrder.push('one'); importScripts('nested.js'); importOrder.push('one-end');", ""),
                    "/scripts/nested.js" => ("200 OK", "text/javascript", "importOrder.push('nested');", ""),
                    "/scripts/two.js" => ("200 OK", "text/javascript", "importOrder.push('two');", ""),
                    "/scripts/missing.js" => ("404 Not Found", "text/javascript", "importOrder.push('bad-status');", ""),
                    "/scripts/wrong.js" => ("200 OK", "text/plain", "importOrder.push('bad-mime');", ""),
                    "/scripts/forbidden.js" => ("302 Found", "text/javascript", "", "Location: file:///etc/passwd\r\n"),
                    "/scripts/syntax.js" => ("200 OK", "text/javascript", "let = ;", ""),
                    "/scripts/throws.js" => ("200 OK", "text/javascript", "throw new RangeError('imported failure');", ""),
                    "/scripts/never.js" => ("200 OK", "text/javascript", "importOrder.push('bad-url-order');", ""),
                    "/scripts/cancel.js" => ("200 OK", "text/javascript",
                        "postMessage('started'); importScripts('slow.js'); postMessage('after-import');", ""),
                    "/scripts/slow.js" => {
                        tokio::time::sleep(std::time::Duration::from_millis(500)).await;
                        ("200 OK", "text/javascript", "postMessage('slow-executed'); importScripts('after-termination.js');", "")
                    },
                    "/scripts/after-termination.js" => ("200 OK", "text/javascript", "postMessage('after-termination');", ""),
                    _ => ("200 OK", "text/plain", "native response", ""),
                };
                let response = format!("HTTP/1.1 {status}\r\nContent-Type: {mime}\r\nContent-Length: {}\r\n{extra}Connection: close\r\n\r\n{body}", body.len());
                let _ = stream.write_all(response.as_bytes()).await;
            });
        }
    });
    (base, receipts, server)
}

#[tokio::test(flavor = "current_thread")]
async fn authenticity_worker_imports_scripts_in_order_over_configured_transport() {
    let (base, receipts, server) = authenticity_worker_import_server().await;
    let jar = std::sync::Arc::new(obscura_net::CookieJar::new());
    jar.set_cookie("native_session=keep; Path=/", &url::Url::parse(&base).unwrap());
    let client = std::sync::Arc::new(obscura_net::ObscuraHttpClient::with_full_options(jar, None, true));
    client.set_user_agent("Obscura/0.1.0").await;
    client.set_accept_language("en-US,en").await;
    *client.client_hint_platform.write().await = Some("MacIntel".into());
    // Navigation and script/fetch observations are independent server receipts.
    client.fetch(&url::Url::parse(&format!("{base}/navigation")).unwrap()).await.unwrap();
    let mut rt = setup_runtime("<!doctype html><body></body>");
    rt.set_url(&format!("{base}/page"));
    rt.set_cookie_jar(client.cookie_jar.clone());
    rt.set_http_client(client);
    rt.set_user_agent("Obscura/0.1.0");
    rt.set_platform("MacIntel", "macOS", "");
    rt.state.borrow_mut().blocked_urls.push("blocked.js".into());
    rt.execute_script("authenticity-native-imports", r#"
        globalThis.__imports=null; globalThis.__echo=null;
        const worker=new Worker('/start-worker.js');
        worker.onerror=event=>{__imports={error:event.message};worker.terminate();};
        worker.onmessage=event=>{__imports=event.data;worker.terminate();};
        fetch('/echo').then(response=>response.text()).then(text=>__echo=text);
    "#).unwrap();
    rt.run_event_loop_bounded(2_000).await.unwrap();
    assert_eq!(rt.evaluate("__imports").unwrap(), serde_json::json!({
        "order":["one","nested","one-end","two"],"value":42,
        "failures":["NetworkError","NetworkError","NetworkError","NetworkError","SyntaxError","RangeError","SyntaxError"],
        "sameObject":true,"software":{"brands":[{"brand":"Obscura","version":"0"}],"mobile":false,"platform":"macOS"},
        "scriptPath":"/scripts/main.js"
    }));
    assert_eq!(rt.evaluate("__echo").unwrap(), serde_json::json!("native response"));
    let observed = receipts.lock().unwrap();
    for path in ["/navigation","/echo","/start-worker.js","/scripts/main.js","/scripts/sub/one.js","/scripts/nested.js","/scripts/two.js"] {
        let request = observed.iter().find(|request| request.starts_with(&format!("GET {path} HTTP/1.1\r\n")))
            .unwrap_or_else(|| panic!("missing actual request for {path}: {observed:?}"));
        for expected in ["user-agent: Obscura/0.1.0","sec-ch-ua: \"Obscura\";v=\"0\"",
            "sec-ch-ua-platform: \"macOS\"","sec-ch-ua-mobile: ?0","accept-language: en-US,en"] {
            assert!(request.lines().any(|line| line.eq_ignore_ascii_case(expected)), "{path}: {request}");
        }
        assert!(request.lines().any(|line| line.to_ascii_lowercase().starts_with("cookie:") && line.contains("native_session=keep")), "{request}");
    }
    assert!(!observed.iter().any(|request| request.starts_with("GET /scripts/never.js ") || request.starts_with("GET /scripts/blocked.js ")));
    server.abort();
}

#[tokio::test(flavor = "current_thread")]
async fn authenticity_worker_termination_cancels_pending_classic_import() {
    let (base, receipts, server) = authenticity_worker_import_server().await;
    let mut rt = setup_runtime("<!doctype html><body></body>");
    rt.set_url(&format!("{base}/page"));
    rt.set_http_client(std::sync::Arc::new(obscura_net::ObscuraHttpClient::with_full_options(
        std::sync::Arc::new(obscura_net::CookieJar::new()), None, true,
    )));
    rt.execute_script("authenticity-import-cancel", r#"
        globalThis.__importMessages=[];
        globalThis.__importWorker=new Worker('/scripts/cancel.js');
        __importWorker.onmessage=event=>__importMessages.push(event.data);
        __importWorker.onerror=event=>__importMessages.push('error:'+event.message);
    "#).unwrap();
    let mut loading = false;
    for _ in 0..30 {
        rt.run_event_loop_bounded(10).await.unwrap();
        loading = receipts.lock().unwrap().iter().any(|request| request.starts_with("GET /scripts/slow.js "))
            && rt.evaluate("__importMessages.includes('started')").unwrap() == serde_json::json!(true);
        if loading { break; }
    }
    assert!(loading, "the real import request must begin before termination");
    rt.evaluate("__importWorker.terminate();").unwrap();
    // Let the actual delayed HTTP response arrive after termination, then
    // deliver any remaining page tasks before checking observable side effects.
    tokio::time::sleep(std::time::Duration::from_millis(600)).await;
    rt.run_event_loop_bounded(50).await.unwrap();
    assert_eq!(rt.evaluate("__importMessages").unwrap(), serde_json::json!(["started"]));
    assert!(!receipts.lock().unwrap().iter().any(|request| request.starts_with("GET /scripts/after-termination.js ")));
    server.abort();
}
