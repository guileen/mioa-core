use obscura_browser::lifecycle::WaitUntil;
use obscura_browser::page::PendingNavigationOutcome;
use obscura_js::runtime::RemoteObjectInfo;
use serde_json::{json, Value};

use crate::dispatch::CdpContext;

pub(crate) struct DeferredEvaluation {
    pub(crate) page_id: String,
    pending: obscura_js::runtime::PendingCdpEvaluation,
    return_by_value: bool,
    pub(crate) deadline: tokio::time::Instant,
    command_deadline: Option<tokio::time::Instant>,
    timeout_ms: u64,
    method: String,
}

impl DeferredEvaluation {
    pub(crate) fn ready(&self, ctx: &mut CdpContext) -> bool {
        if tokio::time::Instant::now() >= self.deadline { return true; }
        let Some(js) = ctx.get_page_mut(&self.page_id).and_then(|page| page.js.as_mut()) else { return true; };
        !js.has_cdp_evaluation(&self.pending) || js.cdp_evaluation_ready(&self.pending)
    }

    pub(crate) async fn finish(self, ctx: &mut CdpContext, session_id: &Option<String>) -> Result<Value, String> {
        let js = ctx.get_page_mut(&self.page_id).and_then(|page| page.js.as_mut())
            .ok_or("Execution context was destroyed")?;
        if !js.has_cdp_evaluation(&self.pending) {
            return Err("Execution context was destroyed".into());
        }
        let remaining = self.command_deadline.map(|deadline| deadline.saturating_duration_since(tokio::time::Instant::now()));
        if remaining.is_some_and(|budget| budget.is_zero()) {
            js.cancel_cdp_evaluation(self.pending);
            return Err("Runtime evaluation exceeded its command budget".into());
        }
        if !js.cdp_evaluation_ready(&self.pending) {
            js.cancel_cdp_evaluation(self.pending);
            return Err(format!("Runtime.{} promise did not settle within {}ms", self.method, self.timeout_ms));
        }
        let watchdog = remaining.map(|budget| obscura_js::runtime::spawn_watchdog(js.isolate_handle(), budget));
        let result = match js.finish_cdp_evaluation(self.pending, self.return_by_value) {
            Ok(info) => emit_post_eval_nav(ctx, session_id).await.map(|_| evaluation_reply(&info)),
            Err(error) => Err(error),
        };
        if watchdog.is_some_and(|watchdog| watchdog.stop()) {
            if let Some(js) = ctx.get_page_mut(&self.page_id).and_then(|page| page.js.as_mut()) {
                js.cancel_termination();
            }
            return Err("Runtime result serialization exceeded its task budget".into());
        }
        result
    }
}

pub(crate) async fn start_awaited_evaluation(
    method: &str,
    params: &Value,
    ctx: &mut CdpContext,
    session_id: &Option<String>,
    command_budget_ms: u64,
) -> Result<Option<DeferredEvaluation>, String> {
    let field = if method == "evaluate" { "contextId" } else { "executionContextId" };
    let native_frame_id = validate_context(params, field, ctx, session_id, method)?;
    let return_by_value = params.get("returnByValue").and_then(Value::as_bool).unwrap_or(false);
    let timeout_ms = params.get("timeout").and_then(Value::as_u64).unwrap_or(30_000);
    let deadline = tokio::time::Instant::now() + std::time::Duration::from_millis(timeout_ms);
    let command_deadline = (command_budget_ms != 0).then(|| tokio::time::Instant::now() + std::time::Duration::from_millis(command_budget_ms));
    let deadline = command_deadline.map_or(deadline, |command| deadline.min(command));
    let page = ctx.get_session_page_mut(session_id).ok_or("No page")?;
    let Some(js) = page.js.as_mut() else { return Ok(None); };
    let evaluation = if method == "evaluate" {
        js.start_cdp_evaluation_in_frame(native_frame_id, params.get("expression").and_then(Value::as_str).ok_or("expression required")?, return_by_value, true)?
    } else {
        js.start_cdp_call_in_frame(
            native_frame_id,
            params.get("functionDeclaration").and_then(Value::as_str).unwrap_or("() => undefined"),
            params.get("objectId").and_then(Value::as_str),
            params.get("arguments").and_then(Value::as_array).map(Vec::as_slice).unwrap_or(&[]),
            return_by_value, true,
        )?
    };
    let obscura_js::runtime::CdpEvaluation::Pending(pending) = evaluation else {
        return Err("Awaited evaluation did not create a pending record".into());
    };
    // Finish this task's promise reactions before a script-initiated navigation.
    js.resolve_promises_until(|js| js.cdp_evaluation_ready(&pending), 0).await;
    Ok(Some(DeferredEvaluation {
        page_id: page.id.clone(), pending, return_by_value, deadline, command_deadline, timeout_ms, method: method.into(),
    }))
}

async fn with_live_peers<'a, F, Fut>(
    ctx: &'a mut CdpContext,
    session_id: &Option<String>,
    command: F,
) -> Result<RemoteObjectInfo, String>
where
    F: FnOnce(&'a mut obscura_browser::Page) -> Fut,
    Fut: std::future::Future<Output = Result<RemoteObjectInfo, String>>,
{
    if ctx.pages.len() == 1 {
        return command(ctx.get_session_page_mut(session_id).ok_or("No page")?).await;
    }
    let id = ctx.get_session_page_mut(session_id).ok_or("No page")?.id.clone();
    let index = ctx.pages.iter().position(|page| page.id == id).ok_or("No page")?;
    let (before, remaining) = ctx.pages.split_at_mut(index);
    let (page, after) = remaining.split_first_mut().ok_or("No page")?;
    let command = command(page);
    let peers = async {
        let mut idle = std::collections::HashSet::new();
        loop {
            let all_idle = match crate::server::pump_page_event_loops(before.iter_mut().chain(after.iter_mut()), &mut idle).await {
                Ok(idle) => idle,
                Err(error) => return error,
            };
            if all_idle {
                // Foreground and peer futures share this task's waker. Park
                // idle peers until it wakes again, without a polling timer.
                let mut parked = false;
                std::future::poll_fn(|_| {
                    if parked { std::task::Poll::Ready(()) }
                    else { parked = true; std::task::Poll::Pending }
                }).await;
            } else {
                tokio::task::yield_now().await;
            }
        }
    };
    tokio::select! {
        biased;
        result = command => result,
        error = peers => Err(error),
    }
}

pub(crate) fn execution_context_created_event(
    context: &crate::dispatch::ExecutionContextRecord,
    session_id: Option<String>,
) -> crate::types::CdpEvent {
    crate::types::CdpEvent {
        method: "Runtime.executionContextCreated".to_string(),
        params: json!({
            "context": {
                "id": context.id,
                "origin": context.origin,
                "name": context.world_name,
                "uniqueId": context.unique_id,
                "auxData": {
                    "isDefault": context.is_default,
                    "type": if context.is_default { "default" } else { "isolated" },
                    "frameId": context.frame_id,
                }
            }
        }),
        session_id,
    }
}

/// Whether a binding name is a plain JS identifier and therefore safe to
/// interpolate into the generated shim / teardown scripts. Chromium bindings
/// are identifiers; anything else (quotes, brackets, spaces, operators) could
/// break out of the surrounding string literal and inject arbitrary JS into the
/// page. `Runtime.addBinding` always enforced this, but `Runtime.removeBinding`
/// did not, so a crafted name escaped `delete globalThis['{name}']` and ran in
/// the page context. Both handlers now share this guard.
fn is_valid_binding_name(name: &str) -> bool {
    !name.is_empty()
        && name.chars().all(|c| c.is_alphanumeric() || c == '_' || c == '$')
        && !name.chars().next().unwrap_or('0').is_ascii_digit()
}

/// Drain pending JS-initiated navigation (form.submit, location.assign, etc),
/// then emit the same CDP nav-event sequence Page.navigate emits so
/// Puppeteer's waitForNavigation / Playwright's wait_for_url resolves.
/// Without this, in-page navigations look like Runtime.evaluate finishing
/// to clients and they hang waiting for a frameNavigated that never fires.
async fn emit_post_eval_nav(
    ctx: &mut CdpContext,
    session_id: &Option<String>,
) -> Result<(), String> {
    let server_managed_navigation = ctx.intercept_tx.is_some();
    let page = ctx
        .get_session_page_mut(session_id)
        .ok_or("No page")?;
    // The connection processor owns the Fetch reply channel. Let it perform
    // document navigation outside dispatch so destination requests can resume.
    if server_managed_navigation && page.has_pending_navigation() {
        return Ok(());
    }
    let navigation = page
        .process_pending_navigation_outcome()
        .await
        .map_err(|e| e.to_string())?;
    if navigation == PendingNavigationOutcome::None {
        return Ok(());
    }
    let (frame_id, page_url, page_id, network_events, reached_idle) = {
        let p = ctx.get_session_page_mut(session_id).ok_or("No page")?;
        (
            p.frame_id.clone(),
            p.url_string(),
            p.id.clone(),
            p.network_events.drain(..).collect::<Vec<_>>(),
            p.lifecycle.is_network_idle(),
        )
    };
    if navigation == PendingNavigationOutcome::SameDocument {
        super::page::emit_same_document_navigation(ctx, session_id, &frame_id, &page_url);
        return Ok(());
    }
    let loader_id = format!("loader-{}", uuid::Uuid::new_v4());
    super::page::emit_navigation_events(
        ctx,
        session_id,
        &frame_id,
        &loader_id,
        &page_url,
        &page_id,
        &network_events,
        WaitUntil::Load,
        reached_idle,
    );
    Ok(())
}

pub async fn handle(
    method: &str,
    params: &Value,
    ctx: &mut CdpContext,
    session_id: &Option<String>,
) -> Result<Value, String> {
    match method {
        "getHeapUsage" => {
            let page = ctx.get_session_page_mut(session_id).ok_or("No page")?;
            let js = page.js.as_mut().ok_or("No JavaScript runtime")?;
            let stats = js.heap_statistics();
            Ok(json!({
                "usedSize": stats.used_heap_size(),
                "totalSize": stats.total_heap_size(),
                // Rust-owned DOM storage is not a V8 cppgc heap.
                "embedderHeapUsedSize": 0,
                "backingStorageSize": stats.external_memory(),
            }))
        }
        "enable" => {
            // puppeteer-extra's FrameManager.initialize calls Runtime.enable on
            // the browser-level connection BEFORE any page target exists. Real
            // Chrome replies with `{}` and emits executionContextCreated when
            // a context appears. Returning "No page" here breaks the standard
            // puppeteer connect/newPage flow. If there's no session, succeed
            // silently — the next Target.attachToTarget will set things up.
            if let Some(page_id) = session_id.as_ref()
                .and_then(|session| ctx.sessions.get(session)).cloned()
            {
                ctx.ensure_default_context(&page_id);
                let frames = ctx.ensure_page_frame_contexts(&page_id);
                for subscriber in ctx.runtime_sessions_for_page(&page_id) {
                    for context in &frames {
                        ctx.pending_events.push(execution_context_created_event(context, Some(subscriber.clone())));
                    }
                }
                let newly_enabled = session_id.as_ref().is_some_and(|session| {
                    ctx.runtime_enabled_sessions.insert(session.clone())
                });
                ctx.refresh_runtime_event_collection(&page_id);
                if newly_enabled {
                    let events = ctx.contexts_for_page(&page_id)
                        .map(|context| execution_context_created_event(
                            context, session_id.clone(),
                        ))
                        .collect::<Vec<_>>();
                    ctx.pending_events.extend(events);
                }
            }
            Ok(json!({}))
        }
        "disable" => {
            if let Some(session_id) = session_id {
                let page_id = ctx.sessions.get(session_id).cloned();
                ctx.runtime_enabled_sessions.remove(session_id);
                if let Some(page_id) = page_id {
                    ctx.refresh_runtime_event_collection(&page_id);
                }
            }
            Ok(json!({}))
        }
        "evaluate" => {
            let expression = params
                .get("expression")
                .and_then(|v| v.as_str())
                .ok_or("expression required")?;
            let return_by_value = params
                .get("returnByValue")
                .and_then(|v| v.as_bool())
                .unwrap_or(false);

            let native_frame_id = validate_context(params, "contextId", ctx, session_id, "evaluate")?;

            let await_promise = params
                .get("awaitPromise")
                .and_then(|v| v.as_bool())
                .unwrap_or(false);

            // CDP `timeout` field (milliseconds). Default to Chrome's
            // protocolTimeout (30s) so long evaluations don't pin the V8 lock
            // indefinitely and starve every other CDP command on the same
            // session.
            let timeout_ms = params
                .get("timeout")
                .and_then(|v| v.as_u64())
                .unwrap_or(30_000);

            let info = match tokio::time::timeout(
                std::time::Duration::from_millis(timeout_ms),
                with_live_peers(ctx, session_id, |page| async move {
                    let evaluation = page.js.as_mut().ok_or("JavaScript runtime unavailable")?
                        .start_cdp_evaluation_in_frame(native_frame_id, expression, return_by_value, await_promise)?;
                    page.await_cdp_evaluation(evaluation, return_by_value, timeout_ms, "Runtime.evaluate").await
                }),
            )
            .await
            {
                Ok(Ok(info)) => info,
                Ok(Err(error)) => return Err(error),
                Err(_) => {
                    return Err(format!(
                        "Runtime.evaluate exceeded {timeout_ms}ms timeout"
                    ));
                }
            };
            emit_post_eval_nav(ctx, session_id).await?;

            Ok(evaluation_reply(&info))
        }
        "callFunctionOn" => {
            let function_declaration = params
                .get("functionDeclaration")
                .and_then(|v| v.as_str())
                .unwrap_or("() => undefined");
            let return_by_value = params
                .get("returnByValue")
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
            let await_promise = params
                .get("awaitPromise")
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
            let object_id = params.get("objectId").and_then(|v| v.as_str());
            let arguments = params
                .get("arguments")
                .and_then(|v| v.as_array())
                .map(|a| a.to_vec())
                .unwrap_or_default();

            // An object-only call keeps its original creation realm. An
            // explicit context selects a native frame and must match that
            // object's realm; neither path falls back to the main document.
            let native_frame_id = validate_context(params, "executionContextId", ctx, session_id, "callFunctionOn")?;

            // Keep awaitPromise alive for the same command budget as evaluate.
            // Playwright implements waits with callFunctionOn on some utility
            // paths, so a shorter hidden cap makes the client return before the
            // requested browser timer fires.
            let timeout_ms = params
                .get("timeout")
                .and_then(|v| v.as_u64())
                .unwrap_or(30_000);

            let info = match tokio::time::timeout(
                std::time::Duration::from_millis(timeout_ms),
                with_live_peers(ctx, session_id, |page| async move {
                    let evaluation = page.js.as_mut().ok_or("JavaScript runtime unavailable")?
                        .start_cdp_call_in_frame(native_frame_id, function_declaration, object_id, &arguments, return_by_value, await_promise)?;
                    page.await_cdp_evaluation(evaluation, return_by_value, timeout_ms, "Runtime.callFunctionOn").await
                }),
            )
            .await
            {
                Ok(Ok(info)) => info,
                Ok(Err(error)) => return Err(error),
                Err(_) => {
                    return Err(format!(
                        "Runtime.callFunctionOn exceeded {timeout_ms}ms timeout"
                    ));
                }
            };
            emit_post_eval_nav(ctx, session_id).await?;

            Ok(evaluation_reply(&info))
        }
        "getProperties" => {
            let object_id = params.get("objectId").and_then(Value::as_str)
                .ok_or("objectId required")?;
            let own_properties = params.get("ownProperties").and_then(Value::as_bool).unwrap_or(false);
            let accessor_only = params.get("accessorPropertiesOnly").and_then(Value::as_bool).unwrap_or(false);
            let page = ctx.get_session_page_mut(session_id).ok_or("No page")?;
            page.resume_js();
            let js = page.js.as_mut().ok_or("No JavaScript runtime")?;
            js.get_properties_for_cdp(object_id, own_properties, accessor_only)
        }
        "releaseObject" => {
            if let Some(oid) = params.get("objectId").and_then(|v| v.as_str()) {
                if let Some(page) = ctx.get_session_page_mut(session_id) {
                    page.release_object(oid);
                }
            }
            Ok(json!({}))
        }
        "releaseObjectGroup" => {
            if let Some(page) = ctx.get_session_page_mut(session_id) {
                page.release_object_group();
            }
            Ok(json!({}))
        }
        "addBinding" => {
            let name = params.get("name").and_then(|v| v.as_str()).unwrap_or("");
            if is_valid_binding_name(name) {
                // The shim forwards every call back to Rust through
                // op_binding_called; the CDP dispatcher then drains the
                // queue and emits Runtime.bindingCalled events the same
                // way Chromium does. Chromium's V8InspectorImpl rejects
                // calls without exactly one argument and ToString-coerces
                // that argument before emitting it as the payload — we
                // match the coercion (`String(arg)`) and silently drop
                // calls with wrong arity, which is what Chrome does.
                let shim = format!(
                    "globalThis['{name}'] = function (arg) {{\
                        if (arguments.length !== 1) return;\
                        try {{\
                            const payload = typeof arg === 'string' ? arg : String(arg);\
                            globalThis.__obscura_binding_called('{name}', payload);\
                        }} catch (e) {{ /* swallow: binding must not throw into page */ }}\
                    }};",
                    name = name,
                );
                // Re-install on every navigation: globalThis is wiped on
                // each new document, and puppeteer registers bindings
                // once-per-page rather than once-per-document.
                let key = format!("__obscura_binding__{}", name);
                ctx.preload_scripts.retain(|(k, _)| k != &key);
                ctx.preload_scripts.push((key, shim.clone()));
                // Remember who subscribed, so the call goes back to this
                // session rather than to whichever session of the page a
                // HashMap happens to yield first. A client discards an event
                // addressed to a session it does not hold, and the session
                // Target.createTarget leaves behind is not the one a client
                // ends up using.
                if let Some(session_id) = session_id {
                    let owners = ctx.binding_sessions.entry(name.to_string()).or_default();
                    if !owners.contains(session_id) {
                        owners.push(session_id.clone());
                    }
                }
                // Install on the current page so the binding is usable
                // immediately, without waiting for the next navigation.
                if let Some(page) = ctx.get_session_page_mut(session_id) {
                    page.evaluate(&shim);
                }
            }
            Ok(json!({}))
        }
        "removeBinding" => {
            let name = params.get("name").and_then(|v| v.as_str()).unwrap_or("");
            if is_valid_binding_name(name) {
                let key = format!("__obscura_binding__{}", name);
                ctx.preload_scripts.retain(|(k, _)| k != &key);
                if let Some(session_id) = session_id {
                    if let Some(owners) = ctx.binding_sessions.get_mut(name) {
                        owners.retain(|owner| owner != session_id);
                        if owners.is_empty() {
                            ctx.binding_sessions.remove(name);
                        }
                    }
                }
                if let Some(page) = ctx.get_session_page_mut(session_id) {
                    page.evaluate(&format!("delete globalThis['{}'];", name));
                }
            }
            Ok(json!({}))
        }
        "runIfWaitingForDebugger" => Ok(json!({})),
        "getExceptionDetails" => Ok(json!({ "exceptionDetails": null })),
        "discardConsoleEntries" => Ok(json!({})),
        _ => Err(format!("Unknown Runtime method: {}", method)),
    }
}

/// Reject `Runtime.{evaluate,callFunctionOn}` calls that target an execution
/// context Obscura has not advertised for the attached page. An absent identity
/// uses the page's default context. Direct embedders retain the compatibility
/// path for ids reserved through `next_isolated_context`.
fn validate_context(
    params: &Value,
    field: &str,
    ctx: &crate::dispatch::CdpContext,
    session_id: &Option<String>,
    method: &str,
) -> Result<Option<u32>, String> {
    let id = params.get(field).and_then(|value| value.as_i64());
    let unique_id = params.get("uniqueContextId").and_then(|value| value.as_str());
    if id.is_some() && unique_id.is_some() {
        return Err(format!(
            "Runtime.{method} cannot specify both {field} and uniqueContextId"
        ));
    }
    if id.is_none() && unique_id.is_none() {
        return Ok(None);
    }
    let record = id.and_then(|id| ctx.context_by_id(id))
        .or_else(|| unique_id.and_then(|id| ctx.context_by_unique_id(id)));
    if let Some(record) = record {
        let owner = session_id.as_ref().and_then(|session| ctx.sessions.get(session));
        if owner == Some(&record.page_id) {
            // Some(0) distinguishes an explicit main context from an omitted
            // context on a call against an original child-realm object.
            return Ok(Some(record.native_frame_id.unwrap_or(0)));
        }
    } else if session_id.is_none() && unique_id.is_none()
        && id.is_some_and(|id| ctx.valid_context_ids.contains(&id))
    {
        // Direct embedders can still reserve an id through the existing public
        // next_isolated_context API. Attached sessions require page ownership.
        return Ok(None);
    }
    let identity = id.map(|id| id.to_string())
        .or_else(|| unique_id.map(str::to_string))
        .unwrap_or_default();
    if record.is_none() || session_id.is_some() {
        return Err(format!(
            "Cannot find context with specified id: {}",
            identity
        ));
    }
    Ok(None)
}

/// Shape one `Runtime.evaluate` or `Runtime.callFunctionOn` reply.
///
/// A thrown value, or the value a promise rejected with, is not a protocol
/// failure: the command succeeds and reports it through `exceptionDetails`,
/// which is what a client rebuilds the page error from. The value is repeated
/// in `result` the way Chrome does, so a client that reads only `result` sees
/// the error object rather than a success that never happened.
///
/// `exceptionId` is a fixed 1 because nothing here correlates exceptions
/// across commands; clients key off the presence of the field, not its id.
fn evaluation_reply(info: &RemoteObjectInfo) -> Value {
    let remote = remote_object_from_info(info);
    if !info.thrown {
        return json!({ "result": remote });
    }
    json!({
        "result": remote.clone(),
        "exceptionDetails": {
            "exceptionId": 1,
            "text": "Uncaught",
            "lineNumber": 0,
            "columnNumber": 0,
            "exception": remote,
        }
    })
}

fn remote_object_from_info(info: &RemoteObjectInfo) -> Value {
    let mut obj = json!({ "type": info.js_type });

    if let Some(ref subtype) = info.subtype {
        obj["subtype"] = json!(subtype);
    }

    if !info.class_name.is_empty() {
        obj["className"] = json!(info.class_name);
    }

    if !info.description.is_empty() {
        obj["description"] = json!(info.description);
    }

    if let Some(ref oid) = info.object_id {
        obj["objectId"] = json!(oid);
    }

    if let Some(ref value) = info.value {
        obj["value"] = value.clone();
    }

    if let Some(ref value) = info.unserializable_value {
        obj["unserializableValue"] = json!(value);
    }

    obj
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::dispatch::CdpContext;

    #[tokio::test(flavor = "current_thread")]
    async fn child_context_commands_execute_in_the_actual_document_and_never_fall_back() {
        let mut ctx = CdpContext::new();
        let page_id = ctx.create_page();
        let session = Some(format!("{page_id}-session"));
        ctx.sessions.insert(session.clone().unwrap(), page_id.clone());
        ctx.get_session_page_mut(&session).unwrap().navigate_blank();
        handle("enable", &json!({}), &mut ctx, &session).await.unwrap();
        handle("evaluate", &json!({
            "expression":r#"(() => {
                globalThis.marker = 'top';
                const frame = document.createElement('iframe');
                frame.id = 'child';
                document.body.appendChild(frame);
                frame.contentWindow.marker = 'child';
                frame.contentDocument.body.textContent = 'actual child document';
                return true;
            })()"#,"returnByValue":true,
        }), &mut ctx, &session).await.unwrap();
        ctx.get_session_page_mut(&session).unwrap().adopt_ready_frame_documents().await;
        handle("enable", &json!({}), &mut ctx, &session).await.unwrap();
        let child = ctx.contexts_for_page(&page_id).find(|context|
            context.is_default && context.native_frame_id.is_some()).unwrap().clone();
        let main = ctx.contexts_for_page(&page_id).find(|context|
            context.is_default && context.native_frame_id.is_none()).unwrap().clone();
        let child_value = handle("evaluate", &json!({
            "contextId":child.id,"expression":"[marker,document.body.textContent]",
            "returnByValue":true,
        }), &mut ctx, &session).await.unwrap();
        assert_eq!(child_value["result"]["value"], json!(["child","actual child document"]));
        let call = handle("callFunctionOn", &json!({
            "executionContextId":child.id,
            "functionDeclaration":"function(array){return [marker,this===globalThis,array instanceof Array];}",
            "arguments":[{"value":[]}],"returnByValue":true,
        }), &mut ctx, &session).await.unwrap();
        assert_eq!(call["result"]["value"], json!(["child",true,true]));
        let body = handle("evaluate", &json!({
            "contextId":child.id,"expression":"document.body","returnByValue":false,
        }), &mut ctx, &session).await.unwrap();
        let body = body["result"]["objectId"].as_str().unwrap();
        let original = handle("callFunctionOn", &json!({
            "objectId":body,"functionDeclaration":"function(){return this===document.body && marker==='child';}",
            "returnByValue":true,
        }), &mut ctx, &session).await.unwrap();
        assert_eq!(original["result"]["value"], true);
        assert!(handle("callFunctionOn", &json!({
            "executionContextId":main.id,"objectId":body,
            "functionDeclaration":"function(){return marker;}","returnByValue":true,
        }), &mut ctx, &session).await.is_err());
        let awaited = handle("evaluate", &json!({
            "contextId":child.id,
            "expression":"new Promise(resolve=>setTimeout(()=>resolve([marker,document.body.textContent]),1))",
            "awaitPromise":true,"returnByValue":true,
        }), &mut ctx, &session).await.unwrap();
        assert_eq!(awaited["result"]["value"], json!(["child","actual child document"]));
        let pending = start_awaited_evaluation("evaluate", &json!({
            "contextId":child.id,"expression":"Promise.resolve(marker)",
            "awaitPromise":true,"returnByValue":true,
        }), &mut ctx, &session, 1_000).await.unwrap().unwrap();
        let reply = pending.finish(&mut ctx, &session).await.unwrap();
        assert_eq!(reply["result"]["value"], "child");
        handle("evaluate", &json!({
            "expression":"document.getElementById('child').remove();true","returnByValue":true,
        }), &mut ctx, &session).await.unwrap();
        assert!(handle("evaluate", &json!({
            "contextId":child.id,"expression":"marker","returnByValue":true,
        }), &mut ctx, &session).await.is_err());
        let root = handle("evaluate", &json!({
            "contextId":main.id,"expression":"marker","returnByValue":true,
        }), &mut ctx, &session).await.unwrap();
        assert_eq!(root["result"]["value"], "top");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn enabling_another_session_publishes_new_native_child_contexts_to_existing_subscribers() {
        let mut ctx = CdpContext::new();
        let page_id = ctx.create_page();
        let first = Some("frame-context-first".to_string());
        let second = Some("frame-context-second".to_string());
        ctx.sessions.insert(first.clone().unwrap(), page_id.clone());
        ctx.sessions.insert(second.clone().unwrap(), page_id.clone());
        ctx.get_session_page_mut(&first).unwrap().navigate_blank();
        handle("enable", &json!({}), &mut ctx, &first).await.unwrap();
        handle("evaluate", &json!({
            "expression":"document.body.appendChild(document.createElement('iframe'));true",
            "returnByValue":true,
        }), &mut ctx, &first).await.unwrap();
        ctx.get_session_page_mut(&first).unwrap().adopt_ready_frame_documents().await;
        ctx.pending_events.clear();
        handle("enable", &json!({}), &mut ctx, &second).await.unwrap();
        let child = ctx.contexts_for_page(&page_id).find(|context|
            context.is_default && context.native_frame_id.is_some()).unwrap();
        for subscriber in [&first, &second] {
            assert_eq!(ctx.pending_events.iter().filter(|event|
                event.method == "Runtime.executionContextCreated"
                    && &event.session_id == subscriber
                    && event.params["context"]["id"] == child.id
                    && event.params["context"]["auxData"]["frameId"] == child.frame_id
                    && event.params["context"]["auxData"]["isDefault"] == true
            ).count(), 1);
        }
    }

    #[tokio::test(flavor = "current_thread")]
    async fn indexeddb_awaited_commands_deliver_peer_versionchange_and_delayed_close() {
        for method in ["evaluate", "callFunctionOn"] {
            let mut ctx = CdpContext::new();
            let mut sessions = Vec::new();
            for _ in 0..2 {
                let id = ctx.create_page();
                let session = Some(format!("{id}-session"));
                ctx.sessions.insert(session.clone().unwrap(),id);
                crate::domains::page::handle("navigate",&json!({"url":"data:text/html,<body>idb</body>"}),&mut ctx,&session).await.unwrap();
                ctx.get_session_page_mut(&session).unwrap().js.as_mut().unwrap().set_url("https://idb.test/");
                sessions.push(session);
            }
            let first = ctx.get_session_page_mut(&sessions[0]).unwrap();
            first.evaluate(r#"(() => {
                const r = indexedDB.open('awaited-peer',1);
                r.onsuccess = () => {globalThis.peer = r.result;
                    peer.onversionchange = () => setTimeout(() => peer.close(),10);};
            })()"#);
            first.js.as_mut().unwrap().run_event_loop_bounded(100).await.unwrap();
            let body = "return new Promise((resolve,reject)=>{const r=indexedDB.open('awaited-peer',2);r.onsuccess=()=>{r.result.close();resolve(r.result.version)};r.onerror=()=>reject(r.error)})";
            let mut params = json!({"awaitPromise":true,"returnByValue":true,"timeout":1000});
            if method == "evaluate" { params["expression"] = json!(format!("(()=>{{{body}}})()")); }
            else { params["functionDeclaration"] = json!(format!("function(){{{body}}}")); }
            let result = handle(method,&params,&mut ctx,&sessions[1]).await.unwrap();
            assert_eq!(result["result"]["value"].as_f64(),Some(2.0),"{method}: {result}");
        }
    }

    #[tokio::test(flavor = "current_thread")]
    async fn heap_usage_reports_live_v8_allocations_without_running_page_tasks() {
        let mut ctx = CdpContext::new();
        let page_id = ctx.create_page();
        let session = Some("heap-usage".to_string());
        ctx.sessions.insert(session.clone().unwrap(), page_id);
        let before = handle("getHeapUsage", &json!({}), &mut ctx, &session).await.unwrap();
        for field in ["usedSize", "totalSize", "embedderHeapUsedSize", "backingStorageSize"] {
            assert!(before[field].as_u64().is_some(), "missing byte count: {before}");
        }
        assert!(before["usedSize"].as_u64().unwrap() > 0);
        assert!(before["totalSize"].as_u64().unwrap() >= before["usedSize"].as_u64().unwrap());
        ctx.get_session_page_mut(&session).unwrap().js.as_mut().unwrap().execute_script("heap-allocations",
            "globalThis.heapObjects = Array.from({length: 100000}, (_, i) => ({i}));\
             globalThis.heapBuffer = new ArrayBuffer(8 * 1024 * 1024);\
             globalThis.heapTimerRan = false;\
             setTimeout(() => { globalThis.heapTimerRan = true; }, 0);",
        ).unwrap();
        let after = handle("getHeapUsage", &json!({}), &mut ctx, &session).await.unwrap();
        assert!(after["usedSize"].as_u64().unwrap() > before["usedSize"].as_u64().unwrap());
        assert!(after["backingStorageSize"].as_u64().unwrap()
            >= before["backingStorageSize"].as_u64().unwrap() + 8 * 1024 * 1024);
        assert_eq!(ctx.get_session_page_mut(&session).unwrap()
            .evaluate("heapTimerRan"), json!(false));
    }

    // Issue #51 — Runtime.evaluate / callFunctionOn must read and validate
    // contextId. Pre-fix the parameter was silently dropped, so Playwright's
    // locator (which targets the utility world created by
    // Page.createIsolatedWorld) ran in the wrong context and timed out.
    //
    // Phase 5.5 (RED-then-GREEN) verification:
    //   - Without the prod fix, `valid_context_ids` does not exist on
    //     CdpContext → these tests fail to compile.
    //   - With the prod fix, all four tests pass.

    #[tokio::test]
    async fn evaluate_rejects_unknown_context_id() {
        let mut ctx = CdpContext::new();
        let err = handle(
            "evaluate",
            &json!({ "expression": "1 + 1", "contextId": 9999 }),
            &mut ctx,
            &None,
        )
        .await
        .expect_err("unknown contextId must error per CDP spec");
        assert!(
            err.contains("Cannot find context with specified id"),
            "error must match real Chrome's wording: {err}"
        );
        assert!(err.contains("9999"), "error must include the bad id: {err}");
    }

    #[tokio::test]
    async fn call_function_on_rejects_unknown_execution_context_id() {
        let mut ctx = CdpContext::new();
        let err = handle(
            "callFunctionOn",
            &json!({
                "functionDeclaration": "() => 42",
                "executionContextId": 9999,
            }),
            &mut ctx,
            &None,
        )
        .await
        .expect_err("unknown executionContextId must error per CDP spec");
        assert!(
            err.contains("Cannot find context with specified id"),
            "error must match Chrome wording: {err}"
        );
    }

    #[tokio::test]
    async fn evaluate_rejects_unadvertised_compatibility_context_ids() {
        for context_id in [1, 2] {
            let mut ctx = CdpContext::new();
            let error = handle(
                "evaluate",
                &json!({ "expression": "1 + 1", "contextId": context_id }),
                &mut ctx,
                &None,
            )
            .await
            .expect_err("an unadvertised compatibility id must not route");
            assert!(error.contains("Cannot find context"));
        }
    }

    #[tokio::test(flavor = "current_thread")]
    async fn evaluate_reports_a_rejection_through_exception_details() {
        let mut ctx = CdpContext::new();
        let page_id = ctx.create_page();
        let session_id = "evaluate-rejection".to_string();
        ctx.sessions.insert(session_id.clone(), page_id);

        let reply = handle(
            "evaluate",
            &json!({
                "expression": "Promise.reject(new Error('boom'))",
                "returnByValue": true,
                "awaitPromise": true,
                "timeout": 3000,
            }),
            &mut ctx,
            &Some(session_id),
        )
        .await
        .expect("a rejection is answered, not failed at the protocol level");

        let details = reply
            .get("exceptionDetails")
            .expect("a thrown value is reported through exceptionDetails");
        assert_eq!(details["text"], json!("Uncaught"));
        assert_eq!(details["exception"]["subtype"], json!("error"));
        assert_eq!(details["exception"]["className"], json!("Error"));
        assert!(
            details["exception"]["description"]
                .as_str()
                .unwrap_or_default()
                .contains("boom"),
            "the description carries the page message: {details}"
        );
        // Chrome repeats the exception in `result`, so a client that reads
        // only that field still sees the error rather than a stale success.
        assert_eq!(reply["result"], details["exception"]);
    }

    #[tokio::test(flavor = "current_thread")]
    async fn call_function_on_reports_a_rejection_through_exception_details() {
        // This is the path Puppeteer's page.evaluate(fn) takes. It used to
        // answer successfully with `{}` for a rejected Error, so the caller
        // could not tell a failure from an empty object.
        let mut ctx = CdpContext::new();
        let page_id = ctx.create_page();
        let session_id = "call-rejection".to_string();
        ctx.sessions.insert(session_id.clone(), page_id);

        let reply = handle(
            "callFunctionOn",
            &json!({
                "functionDeclaration": "() => Promise.reject(new Error('boom'))",
                "returnByValue": true,
                "awaitPromise": true,
                "timeout": 3000,
            }),
            &mut ctx,
            &Some(session_id),
        )
        .await
        .expect("a rejection is answered, not failed at the protocol level");

        let details = reply
            .get("exceptionDetails")
            .expect("a rejected call is reported through exceptionDetails");
        assert_eq!(details["exception"]["subtype"], json!("error"));
        assert!(reply["result"]["value"].is_null(), "the error must not be serialized by value: {reply}");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn a_resolved_evaluation_carries_no_exception_details() {
        let mut ctx = CdpContext::new();
        let page_id = ctx.create_page();
        let session_id = "evaluate-resolved".to_string();
        ctx.sessions.insert(session_id.clone(), page_id);

        let reply = handle(
            "evaluate",
            &json!({
                "expression": "Promise.resolve(2)",
                "returnByValue": true,
                "awaitPromise": true,
                "timeout": 3000,
            }),
            &mut ctx,
            &Some(session_id),
        )
        .await
        .unwrap();
        assert_eq!(reply["result"]["value"], json!(2.0));
        assert!(reply.get("exceptionDetails").is_none());
    }
    #[tokio::test(flavor = "current_thread")]
    async fn evaluate_await_promise_reports_the_requested_timeout() {
        let mut ctx = CdpContext::new();
        let page_id = ctx.create_page();
        let session_id = "await-timeout-session".to_string();
        ctx.sessions.insert(session_id.clone(), page_id);

        let error = handle(
            "evaluate",
            &json!({
                "expression": "new Promise(() => {})",
                "returnByValue": true,
                "awaitPromise": true,
                "timeout": 25,
            }),
            &mut ctx,
            &Some(session_id),
        )
        .await
        .expect_err("an unsettled promise must not return stale result metadata");
        assert!(
            error.contains("25ms timeout") || error.contains("within 25ms"),
            "unexpected timeout error: {error}"
        );
    }

    #[tokio::test]
    async fn create_isolated_world_registers_id_for_evaluate() {
        // Round-trip: Page.createIsolatedWorld returns contextId N, and a
        // subsequent Runtime.evaluate targeting that contextId must NOT be
        // rejected.
        let mut ctx = CdpContext::new();
        // Bypass the page-attached path of createIsolatedWorld by direct
        // insert — mirrors the same effect as calling the page handler with
        // a real session.
        ctx.valid_context_ids.insert(100);

        let result = handle(
            "evaluate",
            &json!({ "expression": "1 + 1", "contextId": 100 }),
            &mut ctx,
            &None,
        )
        .await;
        if let Err(e) = result {
            assert!(
                !e.contains("Cannot find context"),
                "registered isolated-world contextId=100 must be accepted, got: {e}"
            );
        }
    }

    /// Regression for #122 item 7: puppeteer-extra's FrameManager.initialize
    /// fires Runtime.enable on the browser-level WebSocket BEFORE any page
    /// target exists. Real Chrome replies with `{}`; before the fix Obscura
    /// returned `{"error":{"code":-32601,"message":"No page"}}` and the
    /// puppeteer connect flow died.
    #[tokio::test]
    async fn enable_succeeds_when_no_session_attached() {
        let mut ctx = CdpContext::new();
        let result = handle("enable", &json!({}), &mut ctx, &None)
            .await
            .expect("Runtime.enable must succeed even with no session");
        assert_eq!(result, json!({}));
    }

    /// SEC-002 / #578 — Runtime.removeBinding must validate the binding name the
    /// same way addBinding does. Before the fix the name was interpolated
    /// straight into `delete globalThis['{name}']`, so a CDP client could break
    /// out of the string delimiter and run arbitrary JS in the page. This drives
    /// the real handler against a live page and asserts the injected statement
    /// never executes.
    #[tokio::test(flavor = "current_thread")]
    async fn remove_binding_rejects_injection_in_name() {
        let mut ctx = CdpContext::new();
        let page_id = ctx.create_page();
        let session = Some(format!("{page_id}-session"));
        ctx.sessions.insert(session.clone().unwrap(), page_id);

        crate::domains::page::handle(
            "navigate",
            &json!({ "url": "data:text/html,<p>hi</p>", "waitUntil": "load" }),
            &mut ctx,
            &session,
        )
        .await
        .expect("navigate should succeed");

        // Canary the injection would flip from 0 to 1.
        ctx.get_session_page_mut(&session)
            .unwrap()
            .evaluate("globalThis.__pwned = 0");

        // The generated code is `delete globalThis['{name}']`, which the runtime
        // wraps as `return ( ... )`. A comma-expression payload stays a single
        // valid expression through that wrapper and runs the assignment:
        //   delete globalThis['x'] , (globalThis.__pwned = 1) , globalThis['y']
        handle(
            "removeBinding",
            &json!({ "name": "x'] , (globalThis.__pwned = 1) , globalThis['y" }),
            &mut ctx,
            &session,
        )
        .await
        .expect("removeBinding must return Ok regardless of the name");

        let pwned = ctx
            .get_session_page_mut(&session)
            .unwrap()
            .evaluate("globalThis.__pwned");
        assert_ne!(
            pwned.as_f64(),
            Some(1.0),
            "removeBinding must not execute JS injected via the binding name (got {pwned:?})"
        );
    }
}
