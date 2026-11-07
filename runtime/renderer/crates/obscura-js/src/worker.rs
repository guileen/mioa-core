//! Dedicated workers: each owns a thread, reactor and fresh V8 isolate.
//! The page snapshot is deliberately not used: DOM globals never exist here.
use std::cell::RefCell;
use std::collections::HashMap;
use std::rc::Rc;
use std::sync::{Arc, Mutex, atomic::{AtomicBool, Ordering}};
use deno_core::{op2, v8, JsBuffer, JsRuntime, OpDecl, OpState, RuntimeOptions};
use tokio::sync::{mpsc, Notify};
use crate::ops::{ObscuraState, RealmStates, SharedState};

type EventReceiver = Rc<tokio::sync::Mutex<mpsc::UnboundedReceiver<WorkerEvent>>>;
#[derive(serde::Serialize)]
#[serde(tag = "kind", rename_all = "lowercase")]
enum WorkerEvent {
    Message { data: deno_core::ToJsBuffer },
    Error { message: String },
    Closed,
}
#[derive(Default)]
struct Control {
    stopped: AtomicBool,
    handle: Mutex<Option<deno_core::v8::IsolateHandle>>,
    stop: Notify,
}
impl Control {
    fn terminate(&self) {
        self.stopped.store(true, Ordering::Release);
        if let Some(handle) = self.handle.lock().unwrap_or_else(|p| p.into_inner()).as_ref() {
            handle.terminate_execution();
        }
        self.stop.notify_one();
        self.stop.notify_waiters();
    }
}
struct Worker {
    input: mpsc::UnboundedSender<Vec<u8>>,
    output: EventReceiver,
    control: Arc<Control>,
}
impl Drop for Worker {
    fn drop(&mut self) { self.control.terminate(); }
}
#[derive(Default)]
struct Workers { next: u32, entries: HashMap<u32, Worker> }
struct Endpoint {
    input: Rc<tokio::sync::Mutex<mpsc::UnboundedReceiver<Vec<u8>>>>,
    output: mpsc::UnboundedSender<WorkerEvent>,
    control: Arc<Control>,
    origin: std::time::Instant,
    import_io: tokio::runtime::Handle,
}
fn workers(state: &mut OpState) -> &mut Workers {
    if state.try_borrow::<Workers>().is_none() { state.put(Workers::default()); }
    state.borrow_mut::<Workers>()
}

#[op2(fast)]
fn op_worker_create(
    state: &mut OpState,
    #[string] url: String,
    #[string] source: String,
    #[string] identity: String,
    frame_id: u32,
) -> Result<u32, deno_error::JsErrorBox> {
    // Only thread-safe context resources cross this boundary; never Rc, DOM,
    // v8::Global, callbacks into the page isolate, or its evaluator.
    let parent = crate::ops::frame_state(state, frame_id);
    let parent = parent.borrow();
    let http = parent.http_client.clone();
    let cookies = parent.cookie_jar.clone();
    let indexed_db = parent.indexed_db.clone();
    let local_storage = parent.local_storage.clone();
    let session_storage = parent.session_storage.clone();
    let callbacks = parent.callbacks.clone();
    let in_flight = parent.page_in_flight.clone();
    let blocked = parent.blocked_urls.clone();
    let intercept_tx = parent.intercept_tx.clone();
    let intercept_enabled = parent.intercept_enabled;
    let intercept_patterns = parent.intercept_url_patterns.clone();
    let page_id = parent.intercept_page_id.clone();
    let environment = parent.environment_identity.clone();
    let creator_origin = if parent.sandbox_opaque_origin {
        "null".to_owned()
    } else {
        parent.inherited_origin.clone().unwrap_or_else(|| {
            url::Url::parse(&parent.url).map(|url| url.origin().ascii_serialization())
                .unwrap_or_else(|_| "null".to_owned())
        })
    };
    #[cfg(feature = "stealth")]
    let stealth = parent.stealth_client.clone();
    drop(parent);
    let mut identity: serde_json::Value = serde_json::from_str(&identity)
        .map_err(|e| deno_error::JsErrorBox::type_error(e.to_string()))?;
    let identity_object = identity.as_object_mut()
        .ok_or_else(|| deno_error::JsErrorBox::type_error("Worker identity must be an object"))?;
    identity_object.insert("origin".to_owned(), creator_origin.into());
    if let Some(resolved) = &environment {
        identity_object.insert("environment".to_owned(), resolved.clone());
        let navigator = identity_object.entry("navigator").or_insert_with(|| serde_json::json!({}));
        if !navigator.is_object() { *navigator = serde_json::json!({}); }
        for (target, source) in [
            ("userAgent", "user_agent"), ("platform", "platform"),
            ("language", "locale"), ("languages", "languages"),
            ("hardwareConcurrency", "hardware_concurrency"), ("deviceMemory", "device_memory"),
        ] {
            navigator[target] = resolved[source].clone();
        }
        if let Some(user_agent) = resolved["user_agent"].as_str() {
            navigator["appVersion"] = user_agent.strip_prefix("Mozilla/").unwrap_or(user_agent).into();
        }
    }
    let (input_tx, input_rx) = mpsc::unbounded_channel();
    let (output_tx, output_rx) = mpsc::unbounded_channel();
    let control = Arc::new(Control::default());
    let thread_control = control.clone();
    std::thread::Builder::new().name("obscura-worker".into()).spawn(move || {
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| -> Result<(), String> {
            // Deno async ops require a current-thread reactor. Synchronous
            // imports poll their non-Send fetch future on this owning thread,
            // while a separate I/O reactor drives sockets during the wait.
            let import_io = tokio::runtime::Builder::new_multi_thread().worker_threads(1).enable_all().build().map_err(|e| e.to_string())?;
            let reactor = tokio::runtime::Builder::new_current_thread().enable_all().build().map_err(|e| e.to_string())?;
            reactor.block_on(async {
                let mut worker_state = ObscuraState::new();
                worker_state.url = url.clone();
                worker_state.environment_identity = environment;
                if url.starts_with("blob:") || url.starts_with("data:") {
                    worker_state.inherited_origin = identity["origin"].as_str().map(str::to_owned);
                }
                worker_state.http_client = http;
                worker_state.cookie_jar = cookies;
                worker_state.indexed_db = indexed_db;
                worker_state.local_storage = local_storage;
                worker_state.session_storage = session_storage;
                worker_state.callbacks = callbacks;
                worker_state.page_in_flight = in_flight;
                worker_state.blocked_urls = blocked;
                worker_state.intercept_tx = intercept_tx;
                worker_state.intercept_enabled = intercept_enabled;
                worker_state.intercept_url_patterns = intercept_patterns;
                worker_state.intercept_page_id = page_id;
                #[cfg(feature = "stealth")]
                { worker_state.stealth_client = stealth; }
                let shared: SharedState = Rc::new(RefCell::new(worker_state));
                let mut runtime = JsRuntime::new(RuntimeOptions {
                    extensions: vec![crate::ops::build_extension()],
                    create_params: Some(deno_core::v8::CreateParams::default().heap_limits(0, 256 * 1024 * 1024)),
                    ..Default::default()
                });
                {
                    let mut handle = thread_control.handle.lock().unwrap_or_else(|p| p.into_inner());
                    *handle = Some(runtime.v8_isolate().thread_safe_handle());
                    if thread_control.stopped.load(Ordering::Acquire) { return Ok(()); }
                }
                let worker_stop = thread_control.clone();
                runtime.add_near_heap_limit_callback(move |limit, _| {
                    worker_stop.terminate();
                    limit.saturating_add(16 * 1024 * 1024)
                });
                {
                    let context = runtime.main_context();
                    let op_state = runtime.op_state();
                    let mut op_state = op_state.borrow_mut();
                    op_state.put(shared.clone());
                    let mut realms = RealmStates::default();
                    realms.register_main(context, shared.clone());
                    op_state.put(Rc::new(RefCell::new(realms)));
                    op_state.put(Endpoint { input: Rc::new(tokio::sync::Mutex::new(input_rx)), output: output_tx.clone(), control: thread_control.clone(), origin: std::time::Instant::now(), import_io: import_io.handle().clone() });
                }
                let init = format!("globalThis.__workerInit = {{url:{},identity:{}}};", serde_json::json!(url), identity);
                runtime.execute_script("<worker:init>", init).map_err(|e| e.to_string())?;
                let bootstrap = include_str!("../js/worker_bootstrap.js")
                    .replace("/* SOFTWARE_DECLARATION */", include_str!("../js/software.js"))
                    .replace("/* INDEXED_DB */", include_str!("../js/indexed_db.js"))
                    .replace("/* WORKER_CODEC */", include_str!("../js/worker_codec.js"));
                let bridge = runtime.execute_script("<worker:bootstrap>", bootstrap).map_err(|e| e.to_string())?;
                // Capture the bootstrap return before author code runs. No
                // global or global-lexical callback transports native notices.
                let poll_indexed_db = {
                    let context = runtime.main_context();
                    v8::scope_with_context!(scope, runtime.v8_isolate(), context);
                    let bridge = v8::Local::<v8::Object>::try_from(v8::Local::new(scope, bridge))
                        .map_err(|_| "Worker private bridge is invalid")?;
                    let key = v8::String::new(scope, "pollIndexedDb")
                        .ok_or("Worker private bridge key allocation failed")?;
                    let callback = bridge.get(scope, key.into())
                        .and_then(|value| v8::Local::<v8::Function>::try_from(value).ok())
                        .ok_or("Worker private IndexedDB callback is unavailable")?;
                    v8::Global::new(scope, callback)
                };
                runtime.execute_script(url, source).map_err(|e| e.to_string())?;
                loop {
                    // Also wake on IDB notices from another isolate. Messages
                    // and timers run on this worker's own reactor/task queue.
                    let turn = tokio::select! {
                        biased;
                        _ = thread_control.stop.notified() => break,
                        result = runtime.run_event_loop(Default::default()) => Some(result),
                        _ = tokio::time::sleep(std::time::Duration::from_millis(10)) => None,
                    };
                    if let Some(Err(error)) = turn {
                        if thread_control.stopped.load(Ordering::Acquire) { break; }
                        let _ = output_tx.send(WorkerEvent::Error { message: error.to_string() });
                    } else if let Some(Ok(())) = turn { break; }
                    let notices = shared.borrow().indexed_db_owner.as_ref().map(|owner| owner.take_notices()).unwrap_or_default();
                    if !notices.is_empty() {
                        let context = runtime.main_context();
                        v8::scope_with_context!(scope, runtime.v8_isolate(), context);
                        v8::tc_scope!(let scope, scope);
                        let callback = v8::Local::new(scope, &poll_indexed_db);
                        let notices = deno_core::serde_v8::to_v8(scope, &notices).map_err(|error| error.to_string())?;
                        let receiver = v8::undefined(scope).into();
                        callback.call(scope, receiver, &[notices]).ok_or_else(|| {
                            scope.exception().map(|error| error.to_rust_string_lossy(scope))
                                .unwrap_or_else(|| "Worker IndexedDB notice delivery failed".to_owned())
                        })?;
                    }
                }
                Ok(())
            })
        }));
        match result {
            Ok(Err(message)) => { let _ = output_tx.send(WorkerEvent::Error { message }); }
            Err(_) => { let _ = output_tx.send(WorkerEvent::Error { message: "Worker host panicked".into() }); }
            _ => {}
        }
        *thread_control.handle.lock().unwrap_or_else(|p| p.into_inner()) = None;
        let _ = output_tx.send(WorkerEvent::Closed);
    }).map_err(|e| deno_error::JsErrorBox::generic(e.to_string()))?;
    let registry = workers(state);
    registry.next = registry.next.checked_add(1).ok_or_else(|| deno_error::JsErrorBox::generic("Worker id exhausted"))?;
    let id = registry.next;
    registry.entries.insert(id, Worker { input: input_tx, output: Rc::new(tokio::sync::Mutex::new(output_rx)), control });
    Ok(id)
}

/// Fetch and execute one classic script in this Worker, not in a page evaluator.
/// A nonempty result is a network/MIME error for the private JS boundary to
/// expose as DOMException. V8 compile/runtime exceptions propagate unchanged.
#[op2(reentrant)]
#[string]
fn op_worker_import_script(
    scope: &mut v8::PinScope,
    state: Rc<RefCell<OpState>>,
    #[string] url: String,
) -> Result<String, deno_error::JsErrorBox> {
    let control = state.borrow().try_borrow::<Endpoint>()
        .ok_or_else(|| deno_error::JsErrorBox::type_error("importScripts requires a Worker"))?.control.clone();
    if control.stopped.load(Ordering::Acquire) { return Ok("Worker terminated".into()); }
    let origin = {
        let shared = state.borrow().borrow::<SharedState>().clone();
        let worker = shared.borrow();
        worker.inherited_origin.clone().unwrap_or_else(||
            url::Url::parse(&worker.url).map(|url| url.origin().ascii_serialization()).unwrap_or_else(|_| "null".into()))
    };
    let response = {
        let import_io = state.borrow().borrow::<Endpoint>().import_io.clone();
        let _entered = import_io.enter();
        deno_core::futures::executor::block_on(async {
            tokio::select! {
                biased;
                _ = control.stop.notified() => Err("Worker terminated".to_owned()),
                response = crate::ops::fetch_worker_classic_script(state.clone(), url.clone(), origin) =>
                    response.map_err(|error| error.to_string()),
            }
        })
    };
    let response = match response {
        Ok(response) => response,
        Err(error) => return Ok(error),
    };
    if control.stopped.load(Ordering::Acquire) { return Ok("Worker terminated".into()); }
    let response: serde_json::Value = serde_json::from_str(&response)
        .map_err(|error| deno_error::JsErrorBox::generic(error.to_string()))?;
    if response["blocked"].as_bool() == Some(true) || response["corsBlocked"].as_bool() == Some(true)
        || !response["status"].as_u64().is_some_and(|status| (200..300).contains(&status)) {
        return Ok(response["error"].as_str().unwrap_or("Worker import request failed").to_owned());
    }
    let mime = response["headers"].as_object()
        .and_then(|headers| headers.iter().find(|(name, _)| name.eq_ignore_ascii_case("content-type")))
        .and_then(|(_, value)| value.as_str()).unwrap_or("")
        .split(';').next().unwrap_or("").trim().to_ascii_lowercase();
    if !matches!(mime.as_str(), "text/javascript" | "application/javascript"
        | "text/ecmascript" | "application/ecmascript" | "application/x-javascript"
        | "application/x-ecmascript" | "text/javascript1.0" | "text/javascript1.1"
        | "text/javascript1.2" | "text/javascript1.3" | "text/javascript1.4"
        | "text/javascript1.5" | "text/jscript" | "text/livescript" | "text/x-javascript"
        | "text/x-ecmascript") {
        return Ok(format!("Worker import requires a JavaScript MIME type, received {mime:?}"));
    }
    let source_bytes = if let Some(encoded) = response["bodyBase64"].as_str().filter(|value| !value.is_empty()) {
        use base64::Engine as _;
        match base64::engine::general_purpose::STANDARD.decode(encoded) {
            Ok(bytes) => Some(bytes),
            Err(_) => return Ok("Invalid encoded Worker script response".into()),
        }
    } else { None };
    let source = source_bytes.as_deref().map(String::from_utf8_lossy)
        .unwrap_or_else(|| std::borrow::Cow::Borrowed(response["body"].as_str().unwrap_or("")));
    let source = v8::String::new(scope, &source)
        .ok_or_else(|| deno_error::JsErrorBox::generic("Worker import source allocation failed"))?;
    let final_url = response["url"].as_str().unwrap_or(&url);
    let name = v8::String::new(scope, final_url)
        .ok_or_else(|| deno_error::JsErrorBox::generic("Worker import URL allocation failed"))?;
    let origin = v8::ScriptOrigin::new(
        scope, name.into(), 0, 0, false, 0, None, false, false, false, None,
    );
    v8::tc_scope!(let scope, scope);
    if let Some(script) = v8::Script::compile(scope, source, Some(&origin)) {
        if !control.stopped.load(Ordering::Acquire) { script.run(scope); }
    }
    if scope.has_caught() { scope.rethrow(); }
    Ok(String::new())
}

#[op2]
fn op_worker_send(state: &mut OpState, id: u32, #[buffer] data: JsBuffer) {
    if let Some(worker) = workers(state).entries.get(&id) {
        let _ = worker.input.send(data.to_vec());
    }
}
#[op2]
#[serde]
async fn op_worker_receive(state: Rc<RefCell<OpState>>, id: u32) -> Option<WorkerEvent> {
    let receiver = workers(&mut state.borrow_mut()).entries.get(&id)?.output.clone();
    let result = receiver.lock().await.recv().await;
    result
}
#[op2(fast)]
fn op_worker_terminate(state: &mut OpState, id: u32) { workers(state).entries.remove(&id); }
#[op2]
#[buffer]
async fn op_worker_inbox(state: Rc<RefCell<OpState>>) -> Option<Vec<u8>> {
    let receiver = state.borrow().borrow::<Endpoint>().input.clone();
    let result = receiver.lock().await.recv().await;
    result
}
#[op2]
fn op_worker_outbox(state: &OpState, #[buffer] data: JsBuffer) {
    let endpoint = state.borrow::<Endpoint>();
    if !endpoint.control.stopped.load(Ordering::Acquire) {
        let _ = endpoint.output.send(WorkerEvent::Message { data: data.to_vec().into() });
    }
}
#[op2(fast)]
fn op_worker_close(state: &OpState) {
    let endpoint = state.borrow::<Endpoint>();
    endpoint.control.stopped.store(true, Ordering::Release);
    endpoint.control.stop.notify_one();
}
#[op2(fast)]
fn op_worker_error(state: &OpState, #[string] message: String) {
    let _ = state.borrow::<Endpoint>().output.send(WorkerEvent::Error { message });
}
#[op2(fast)]
fn op_worker_now(state: &OpState) -> f64 {
    state.borrow::<Endpoint>().origin.elapsed().as_secs_f64() * 1000.0
}
pub(crate) fn ops() -> Vec<OpDecl> {
    vec![op_worker_create(), op_worker_send(), op_worker_receive(), op_worker_terminate(),
        op_worker_inbox(), op_worker_outbox(), op_worker_close(), op_worker_error(), op_worker_now(), op_worker_import_script()]
}

#[cfg(test)]
mod tests {
    use crate::runtime::ObscuraJsRuntime;

    #[tokio::test(flavor = "current_thread")]
    async fn first_worker_author_cannot_mint_trusted_events() {
        let mut rt = ObscuraJsRuntime::new();
        rt.set_dom(obscura_dom::parse_html("<!doctype html><body></body>"));
        rt.set_url("https://worker-trust.test/");
        rt.run_page_init();
        let source = serde_json::to_string(r#"
            const result = {
                hidden: ['__obscura_markTrusted', '_dispatchTrusted', '_dispatchUntrusted',
                    '_createNativeEvent', '_trustedEvents', '__obscuraCore', '__obscura_poll_indexeddb'].map(name =>
                        [typeof globalThis[name], Function('return typeof ' + name)()]),
            };
            const NativeEvent = Event, NativeMessageEvent = MessageEvent;
            const NativeVersionEvent = IDBVersionChangeEvent;
            const forged = new NativeEvent('author', { cancelable: true });
            try { __obscura_markTrusted(forged); }
            catch (error) { result.bareMarkerCall = error.name; }
            try { globalThis.__obscura_markTrusted(forged); }
            catch (error) { result.globalMarkerCall = error.name; }
            forged.isTrusted = true;
            try { Object.defineProperty(forged, 'isTrusted', { value: true }); }
            catch (error) { result.redefineTrust = error.name; }
            result.createdTrusted = forged.isTrusted;
            const authorTarget = new EventTarget();
            authorTarget.addEventListener('author', event => {
                result.authorDispatchedTrusted = event.isTrusted;
                event.preventDefault();
            });
            result.authorDispatchCanceled = !authorTarget.dispatchEvent(forged);
            function privateCaller(callback) {
                try { return callback.caller === null; }
                catch (error) { return error instanceof TypeError; }
            }
            addEventListener('message', function nativeListener(event) {
                result.nativeListenerTrusted = event.isTrusted;
                result.listenerCallerPrivate = privateCaller(nativeListener);
            }, { once: true });
            // Native delivery must not consult author-replaced constructors.
            globalThis.Event = globalThis.MessageEvent = globalThis.IDBVersionChangeEvent =
                function ReplacedConstructor() { throw new Error('Author constructor invoked'); };
            onmessage = async function nativeMessage(event) {
                result.nativeMessageTrusted = event.isTrusted;
                result.nativeMessageClass = event instanceof NativeMessageEvent;
                result.handlerCallerPrivate = privateCaller(nativeMessage);
                await Promise.resolve();
                const target = new EventTarget();
                target.addEventListener('message', redispatched => {
                    result.redispatchedMessageTrusted = redispatched.isTrusted;
                });
                target.dispatchEvent(event);
                result.retainedMessageTrusted = event.isTrusted;
                const request = indexedDB.open('private-worker-event-trust', 1);
                request.onupgradeneeded = function upgrade(event) {
                    result.upgradeTrusted = event.isTrusted;
                    result.upgradeClass = event instanceof NativeVersionEvent;
                    result.idbCallerPrivate = privateCaller(upgrade);
                    request.result.createObjectStore('items');
                };
                request.onerror = () => postMessage({ error: request.error.name });
                request.onsuccess = event => {
                    result.openTrusted = event.isTrusted;
                    result.successClass = event instanceof NativeEvent && !(event instanceof NativeVersionEvent);
                    // Redispatch only after the original IDB dispatch has ended.
                    setTimeout(() => {
                        request.onsuccess = null;
                        request.addEventListener('success', redispatched => {
                            result.redispatchedIdbTrusted = redispatched.isTrusted;
                        }, { once: true });
                        request.dispatchEvent(event);
                        const db = request.result;
                        const tx = db.transaction('items', 'readwrite');
                        const store = tx.objectStore('items');
                        store.add('first', 1).onsuccess = event => { result.requestTrusted = event.isTrusted; };
                        const duplicate = store.add('duplicate', 1);
                        duplicate.onerror = event => {
                            result.errorTrusted = event.isTrusted;
                            result.errorCancelable = event.cancelable;
                            result.errorClass = event instanceof NativeEvent && !(event instanceof NativeVersionEvent);
                            event.preventDefault();
                        };
                        tx.addEventListener('error', event => {
                            result.errorBubbled = event.target === duplicate && event.currentTarget === tx &&
                                event.eventPhase === 3 && event.isTrusted && event.defaultPrevented;
                        });
                        tx.onabort = () => postMessage({ error: 'Unexpected transaction abort' });
                        tx.oncomplete = event => {
                            result.completeTrusted = event.isTrusted;
                            db.close();
                            postMessage(result);
                        };
                    }, 0);
                };
            };
        "#).unwrap();
        let result = rt.call_function_on_for_cdp(&format!(r#"async () => {{
            const NativeEvent = Event, NativeMessageEvent = MessageEvent, NativeErrorEvent = ErrorEvent;
            const source = {source};
            const url = URL.createObjectURL(new Blob([source], {{ type: 'application/javascript' }}));
            const errorUrl = URL.createObjectURL(new Blob(['throw new Error("native-worker-error")'], {{
                type: 'application/javascript',
            }}));
            const worker = new Worker(url), broken = new Worker(errorUrl);
            const page = {{ hiddenMethods: [typeof worker._error, typeof worker._receive] }};
            try {{
                globalThis.Event = globalThis.MessageEvent = globalThis.ErrorEvent =
                    function ReplacedConstructor() {{ throw new Error('Author constructor invoked'); }};
                const workerResult = new Promise((resolve, reject) => {{
                    worker.onerror = event => reject(new Error(event.message));
                    worker.onmessage = async event => {{
                        page.nativeMessageTrusted = event.isTrusted;
                        page.nativeMessageClass = event instanceof NativeMessageEvent;
                        await Promise.resolve();
                        const target = new EventTarget();
                        target.addEventListener('message', event => {{ page.redispatchedTrusted = event.isTrusted; }});
                        target.dispatchEvent(event);
                        resolve(event.data);
                    }};
                    worker.postMessage(null);
                }});
                const errorResult = new Promise(resolve => {{
                    broken.onerror = event => {{
                        page.nativeErrorTrusted = event.isTrusted;
                        page.nativeErrorClass = event instanceof NativeErrorEvent;
                        resolve();
                    }};
                }});
                const [workerValue] = await Promise.all([workerResult, errorResult]);
                return {{ worker: workerValue, page }};
            }} finally {{
                globalThis.Event = NativeEvent;
                globalThis.MessageEvent = NativeMessageEvent;
                globalThis.ErrorEvent = NativeErrorEvent;
                worker.terminate(); broken.terminate();
                URL.revokeObjectURL(url); URL.revokeObjectURL(errorUrl);
            }}
        }}"#), None, &[], true, true).await.unwrap();
        assert_eq!(result.value, Some(serde_json::json!({
            "worker": {
                "hidden": vec![["undefined", "undefined"]; 7],
                "bareMarkerCall": "ReferenceError", "globalMarkerCall": "TypeError",
                "redefineTrust": "TypeError", "createdTrusted": false,
                "authorDispatchedTrusted": false, "authorDispatchCanceled": true,
                "nativeListenerTrusted": true, "listenerCallerPrivate": true,
                "nativeMessageTrusted": true, "nativeMessageClass": true, "handlerCallerPrivate": true,
                "redispatchedMessageTrusted": false, "retainedMessageTrusted": false,
                "upgradeTrusted": true, "upgradeClass": true, "idbCallerPrivate": true,
                "openTrusted": true, "successClass": true, "redispatchedIdbTrusted": false,
                "requestTrusted": true, "errorTrusted": true, "errorCancelable": true,
                "errorClass": true, "errorBubbled": true, "completeTrusted": true,
            },
            "page": {
                "hiddenMethods": ["undefined", "undefined"],
                "nativeMessageTrusted": true, "nativeMessageClass": true,
                "redispatchedTrusted": false, "nativeErrorTrusted": true, "nativeErrorClass": true,
            },
        })), "{result:?}");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn worker_receives_trusted_cross_isolate_idb_notices_without_global_bridge() {
        let mut rt = ObscuraJsRuntime::new();
        rt.set_dom(obscura_dom::parse_html("<!doctype html><body></body>"));
        rt.set_url("https://worker-trust.test/");
        rt.run_page_init();
        let result = rt.call_function_on_for_cdp(r#"async () => {
            const source = `
                const request = indexedDB.open('private-worker-versionchange', 1);
                request.onupgradeneeded = () => request.result.createObjectStore('items');
                request.onerror = () => postMessage({ error: request.error.name });
                request.onsuccess = () => {
                    const db = request.result;
                    db.onversionchange = event => {
                        postMessage({
                            trusted: event.isTrusted,
                            versionEvent: event instanceof IDBVersionChangeEvent,
                            oldVersion: event.oldVersion, newVersion: event.newVersion,
                            hiddenBridge: typeof globalThis.__obscura_poll_indexeddb,
                        });
                        db.close();
                    };
                    postMessage('ready');
                };
            `;
            const url = URL.createObjectURL(new Blob([source], { type: 'application/javascript' }));
            const worker = new Worker(url);
            try {
                let resolveNotice;
                const notice = new Promise(resolve => { resolveNotice = resolve; });
                const received = new Promise((resolve, reject) => {
                    worker.onerror = event => reject(new Error(event.message));
                    worker.onmessage = event => {
                        if (event.data === 'ready') {
                            const request = indexedDB.open('private-worker-versionchange', 2);
                            request.onerror = () => reject(new Error(request.error.name));
                            request.onsuccess = event => {
                                const version = request.result.version;
                                request.result.close();
                                resolve({ version, pageSuccessTrusted: event.isTrusted });
                            };
                        } else if (event.data.error) reject(new Error(event.data.error));
                        else resolveNotice(event.data);
                    };
                });
                const [noticeValue, upgraded] = await Promise.all([notice, received]);
                return { notice: noticeValue, upgraded };
            } finally {
                worker.terminate();
                URL.revokeObjectURL(url);
            }
        }"#, None, &[], true, true).await.unwrap();
        assert_eq!(result.value, Some(serde_json::json!({
            "notice": {
                "trusted": true, "versionEvent": true, "oldVersion": 1, "newVersion": 2,
                "hiddenBridge": "undefined",
            },
            "upgraded": { "version": 2, "pageSuccessTrusted": true },
        })));
    }
}
