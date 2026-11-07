use serde_json::{json, Value};

use crate::dispatch::CdpContext;

/// Embed a string as a JS string literal (double-quoted, with backslash,
/// quotes, and control characters escaped) for interpolation into generated
/// KeyboardEvent scripts. A plain `replace('\'', ...)` misses newline / NUL /
/// U+2028-29, which terminate the literal and silently drop the event.
fn js_str(s: &str) -> String {
    serde_json::to_string(s).unwrap_or_else(|_| "\"\"".to_string())
}

// Insert `text` at the caret, replacing any non-collapsed selection the way a
// real browser does when you type over selected text (for example after a
// triple-click select-all). selectionStart is null during ordinary typing, so
// the legacy append path is kept when no selection is tracked.
//
// The text is embedded as a JSON string literal rather than escaped by hand
// into single quotes. JSON string syntax is a subset of JavaScript's, so this
// covers the quote and the backslash of issue #433 and the control characters
// they left out: a newline inside a single-quoted literal is a syntax error,
// so the whole snippet was dropped and nothing was inserted. obscura-mcp
// already builds its typing snippet this way.
fn insert_text_js(text: &str, ime_commit: bool) -> String {
    let literal = serde_json::to_string(text).unwrap_or_else(|_| "\"\"".to_string());
    format!(
        "(function() {{\
            var t = nativeTarget;\
            if (!t || (t.localName !== 'input' && t.localName !== 'textarea')) return;\
            if (globalThis.__obscura_isDisabled(t)) return;\
            if (!{ime_commit} && t.hasAttribute('readonly')) return;\
            var ins = {text};\
            var commitStart = {ime_commit} ? t.selectionStart : null;\
            if ({ime_commit} && commitStart == null) commitStart = (t.value || '').length;\
            var before = createNativeEvent('InputEvent', 'beforeinput', {{bubbles:true,composed:true,cancelable:true,data:ins,inputType:'insertText'}});\
            if (!dispatchTrustedEvent(t, before)) return;\
            if (t.hasAttribute('readonly') || globalThis.__obscura_isDisabled(t)) return;\
            var v = t.value || '';\
            var s = t.selectionStart, e = t.selectionEnd;\
            if (s == null) {{\
                globalThis.__obscura_setFieldValue(t, 'value', v + ins);\
            }} else {{\
                s = Math.max(0, Math.min(s, v.length));\
                e = (e == null) ? s : Math.max(0, Math.min(e, v.length));\
                var lo = Math.min(s, e), hi = Math.max(s, e);\
                globalThis.__obscura_setFieldValue(t, 'value', v.slice(0, lo) + ins + v.slice(hi));\
                var caret = Math.max(0, Math.min((commitStart == null ? lo : commitStart) + ins.length, t.value.length));\
                t.setSelectionRange(caret, caret);\
            }}\
            dispatchTrustedEvent(t, createNativeEvent('InputEvent', 'input', {{bubbles:true,composed:true,data:ins,inputType:'insertText'}}));\
        }})()",
        text = literal,
    )
}

// Backspace deletes the selected range when there is one, so the common
// "triple-click to select-all, then Backspace to clear" pattern works. With a
// collapsed caret it removes the character before the caret, and with no
// selection tracked it falls back to trimming the last character (legacy).
const BACKSPACE_JS: &str = "(function() {\
    var t = nativeTarget;\
    if (!t || (t.localName !== 'input' && t.localName !== 'textarea')) return;\
    if (t.hasAttribute('readonly')) return;\
    var v = t.value || '';\
    var s = t.selectionStart, e = t.selectionEnd;\
    function backCount(str, p) {\
        if (p >= 2) {\
            var low = str.charCodeAt(p - 1), high = str.charCodeAt(p - 2);\
            if (low >= 0xDC00 && low <= 0xDFFF && high >= 0xD800 && high <= 0xDBFF) return 2;\
        }\
        return 1;\
    }\
    if (s == null) {\
        var n = backCount(v, v.length);\
        globalThis.__obscura_setFieldValue(t, 'value', v.slice(0, v.length - n));\
    } else {\
        s = Math.max(0, Math.min(s, v.length));\
        e = (e == null) ? s : Math.max(0, Math.min(e, v.length));\
        if (s !== e) {\
            var lo = Math.min(s, e), hi = Math.max(s, e);\
            globalThis.__obscura_setFieldValue(t, 'value', v.slice(0, lo) + v.slice(hi));\
            t.setSelectionRange(lo, lo);\
        } else if (s > 0) {\
            var d = backCount(v, s);\
            var hi = s;\
            if (hi < v.length && v.charCodeAt(hi - 1) >= 0xD800 && v.charCodeAt(hi - 1) <= 0xDBFF && v.charCodeAt(hi) >= 0xDC00 && v.charCodeAt(hi) <= 0xDFFF) hi++;\
            globalThis.__obscura_setFieldValue(t, 'value', v.slice(0, s - d) + v.slice(hi));\
            t.setSelectionRange(s - d, s - d);\
        }\
    }\
    dispatchTrustedEvent(t, createNativeEvent('Event', 'input', {bubbles:true}));\
})()";

const DELETE_JS: &str = "(function() {\
    var t = nativeTarget;\
    if (!t || (t.localName !== 'input' && t.localName !== 'textarea')) return;\
    if (t.hasAttribute('readonly')) return;\
    var v = t.value || '';\
    var s = t.selectionStart, e = t.selectionEnd;\
    if (s == null) return;\
    s = Math.max(0, Math.min(s, v.length));\
    e = (e == null) ? s : Math.max(0, Math.min(e, v.length));\
    var lo = Math.min(s, e), hi = Math.max(s, e);\
    if (lo === hi) {\
        hi = Math.min(v.length, hi + 1);\
        if (lo > 0 && v.charCodeAt(lo - 1) >= 0xD800 && v.charCodeAt(lo - 1) <= 0xDBFF && v.charCodeAt(lo) >= 0xDC00 && v.charCodeAt(lo) <= 0xDFFF) lo--;\
        if (hi < v.length && v.charCodeAt(hi - 1) >= 0xD800 && v.charCodeAt(hi - 1) <= 0xDBFF && v.charCodeAt(hi) >= 0xDC00 && v.charCodeAt(hi) <= 0xDFFF) hi++;\
    }\
    if (lo === hi) return;\
    globalThis.__obscura_setFieldValue(t, 'value', v.slice(0, lo) + v.slice(hi));\
    t.setSelectionRange(lo, lo);\
    dispatchTrustedEvent(t, createNativeEvent('Event', 'input', {bubbles:true}));\
})()";

fn mouse_button_code(button: &str) -> u8 {
    match button {
        "middle" => 1,
        "right" => 2,
        "back" => 3,
        "forward" => 4,
        _ => 0,
    }
}

fn mouse_button_mask(button: &str) -> u64 {
    match button {
        "right" => 2,
        "middle" => 4,
        "back" => 8,
        "forward" => 16,
        "none" => 0,
        _ => 1,
    }
}

fn modifier_flags(modifiers: u64) -> (bool, bool, bool, bool) {
    // CDP Input.Modifier: Alt=1, Ctrl=2, Meta=4, Shift=8.
    (
        modifiers & 1 != 0,
        modifiers & 2 != 0,
        modifiers & 4 != 0,
        modifiers & 8 != 0,
    )
}

pub async fn handle(
    method: &str,
    params: &Value,
    ctx: &mut CdpContext,
    session_id: &Option<String>,
) -> Result<Value, String> {
    match method {
        "dispatchMouseEvent" => {
            let event_type = params.get("type").and_then(|v| v.as_str()).unwrap_or("");
            let x = params.get("x").and_then(|v| v.as_f64()).unwrap_or(0.0);
            let y = params.get("y").and_then(|v| v.as_f64()).unwrap_or(0.0);
            let button = params.get("button").and_then(|v| v.as_str()).unwrap_or("left");
            let button_code = mouse_button_code(button);
            let buttons = params
                .get("buttons")
                .and_then(|v| v.as_u64())
                .unwrap_or_else(|| mouse_button_mask(button));
            let click_count = params.get("clickCount").and_then(|v| v.as_u64()).unwrap_or(1);
            let modifiers = params.get("modifiers").and_then(|v| v.as_u64()).unwrap_or(0);
            let (alt_key, ctrl_key, meta_key, shift_key) = modifier_flags(modifiers);
            let pointer_route = {
                let page = ctx.get_session_page_mut(session_id).ok_or("No page")?;
                let js = page.js.as_mut().ok_or("JavaScript runtime unavailable")?;
                let route = js.route_pointer_input(x, y)?;
                js.prepare_pointer_input(&route, event_type, x, y)?;
                route
            };
            let (x, y) = (pointer_route.x, pointer_route.y);

            if event_type == "mouseMoved" {
                if let Some(page) = ctx.get_session_page_mut(session_id) {
                    let code = format!(
                        "(function() {{\
                            var target = nativeTarget;\
                            if (!target) return;\
                            var old = globalThis.__obscura_hover_target || null;\
                            globalThis.__obscura_hover_target = target;\
                            globalThis.__obscura_setHovered(target);\
                            var init = {{bubbles:true,cancelable:true,view:globalThis,clientX:{x},clientY:{y},button:{button_code},buttons:{buttons},detail:0,altKey:{alt_key},ctrlKey:{ctrl_key},metaKey:{meta_key},shiftKey:{shift_key}}};\
                            if (old !== target) {{\
                                if (old) dispatchTrustedEvent(old, createNativeEvent('MouseEvent', 'mouseout', Object.assign({{}}, init, {{relatedTarget:target}})));\
                                dispatchTrustedEvent(target, createNativeEvent('MouseEvent', 'mouseover', Object.assign({{}}, init, {{relatedTarget:old}})));\
                                dispatchTrustedEvent(target, createNativeEvent('MouseEvent', 'mouseenter', Object.assign({{}}, init, {{bubbles:false,relatedTarget:old}})));\
                            }}\
                            dispatchTrustedEvent(target, createNativeEvent('MouseEvent', 'mousemove', init));\
                        }})()",
                        x = x,
                        y = y,
                        button_code = button_code,
                        buttons = buttons,
                        alt_key = alt_key,
                        ctrl_key = ctrl_key,
                        meta_key = meta_key,
                        shift_key = shift_key,
                    );
                    page.js.as_mut().ok_or("JavaScript runtime unavailable")?.evaluate_input_script(&pointer_route, &code)?;
                }
            } else if event_type == "mousePressed" {
                if let Some(page) = ctx.get_session_page_mut(session_id) {
                    let code = format!(
                        "(function() {{\
                            var target = nativeTarget;\
                            if (!target) return;\
                            globalThis.__obscura_click_target = target;\
                            globalThis.__obscura_mouse_down = {{target:target,button:{button_code},clickCount:{click_count}}};\
                            globalThis.__obscura_setHovered(target);\
                            globalThis.__obscura_setActive({button_code} === 0 ? target : null);\
                            var previousTarget = globalThis.__obscura_mouse_over_target || null;\
                            if (previousTarget !== target) {{\
                                function ancestry(node) {{ var path=[]; while (node) {{ path.push(node); node=node.parentNode || null; }} return path; }}\
                                function pointerEvent(node,type,bubbles,related) {{ dispatchTrustedEvent(node, createNativeEvent('PointerEvent', type, {{bubbles:bubbles,cancelable:bubbles,composed:bubbles,view:globalThis,clientX:{x},clientY:{y},button:{button_code},buttons:{buttons},detail:0,pointerId:1,pointerType:'mouse',isPrimary:true,pressure:{pointer_pressure},relatedTarget:related,altKey:{alt_key},ctrlKey:{ctrl_key},metaKey:{meta_key},shiftKey:{shift_key}}})); }}\
                                function mouseEvent(node,type,bubbles,related) {{ dispatchTrustedEvent(node, createNativeEvent('MouseEvent', type, {{bubbles:bubbles,cancelable:bubbles,composed:bubbles,view:globalThis,clientX:{x},clientY:{y},button:{button_code},buttons:{buttons},detail:0,relatedTarget:related,altKey:{alt_key},ctrlKey:{ctrl_key},metaKey:{meta_key},shiftKey:{shift_key}}})); }}\
                                var oldPath = previousTarget && previousTarget.isConnected ? ancestry(previousTarget) : [];\
                                var newPath = ancestry(target);\
                                var common = newPath.find(function(node) {{ return oldPath.includes(node); }}) || null;\
                                var exited = common ? oldPath.slice(0, oldPath.indexOf(common)) : oldPath;\
                                var entered = common ? newPath.slice(0, newPath.indexOf(common)) : newPath;\
                                if (previousTarget && previousTarget.isConnected) pointerEvent(previousTarget,'pointerout',true,target);\
                                for (var pi=0; pi<exited.length; pi++) pointerEvent(exited[pi],'pointerleave',false,target);\
                                pointerEvent(target,'pointerover',true,previousTarget);\
                                for (var pe=entered.length-1; pe>=0; pe--) pointerEvent(entered[pe],'pointerenter',false,previousTarget);\
                                if (previousTarget && previousTarget.isConnected) mouseEvent(previousTarget,'mouseout',true,target);\
                                for (var mi=0; mi<exited.length; mi++) mouseEvent(exited[mi],'mouseleave',false,target);\
                                mouseEvent(target,'mouseover',true,previousTarget);\
                                for (var me=entered.length-1; me>=0; me--) mouseEvent(entered[me],'mouseenter',false,previousTarget);\
                                globalThis.__obscura_mouse_over_target = target;\
                            }}\
                            var focusTarget = target.closest && target.closest('input,select,textarea,button,a[href],[tabindex],[contenteditable]');\
                            var pointer = createNativeEvent('PointerEvent', 'pointerdown', {{bubbles:true,cancelable:true,composed:true,view:globalThis,clientX:{x},clientY:{y},button:{button_code},buttons:{buttons},detail:0,pointerId:1,pointerType:'mouse',isPrimary:true,pressure:{pointer_pressure},altKey:{alt_key},ctrlKey:{ctrl_key},metaKey:{meta_key},shiftKey:{shift_key}}});\
                            dispatchTrustedEvent(target, pointer);\
                            var evt = createNativeEvent('MouseEvent', 'mousedown', {{bubbles:true,cancelable:true,composed:true,view:globalThis,clientX:{x},clientY:{y},button:{button_code},buttons:{buttons},detail:{click_count},altKey:{alt_key},ctrlKey:{ctrl_key},metaKey:{meta_key},shiftKey:{shift_key}}});\
                            dispatchTrustedEvent(target, evt);\
                            if (!evt.defaultPrevented) {{\
                                var focusTarget = globalThis.__obscura_interactiveHost(target) || (target.closest && target.closest('[tabindex]'));\
                                if (focusTarget && !globalThis.__obscura_isDisabled(focusTarget)) focusTarget.focus();\
                                else if (document.activeElement && document.activeElement !== document.body) document.activeElement.blur();\
                            }}\
                            return !evt.defaultPrevented;\
                        }})()",
                        x = x,
                        y = y,
                        button_code = button_code,
                        buttons = buttons,
                        click_count = click_count,
                        alt_key = alt_key,
                        ctrl_key = ctrl_key,
                        meta_key = meta_key,
                        shift_key = shift_key,
                        pointer_pressure = if buttons == 0 { 0.0 } else { 0.5 },
                    );
                    let js = page.js.as_mut().ok_or("JavaScript runtime unavailable")?;
                    if js.evaluate_input_script(&pointer_route, &code)?.as_bool() == Some(true) {
                        js.focus_pointer_owners(&pointer_route)?;
                    }
                }
            } else if event_type == "mouseReleased" {
                let moved_frame = if let Some(page) = ctx.get_session_page_mut(session_id) {
                    let code = format!(
                        "(function() {{\
                            var target = nativeTarget;\
                            var down = globalThis.__obscura_mouse_down;\
                            globalThis.__obscura_mouse_down = null;\
                            globalThis.__obscura_setActive(null);\
                            if (!target) return;\
                            var pointer = createNativeEvent('PointerEvent', 'pointerup', {{bubbles:true,cancelable:true,composed:true,view:globalThis,clientX:{x},clientY:{y},button:{button_code},buttons:0,detail:0,pointerId:1,pointerType:'mouse',isPrimary:true,pressure:0,altKey:{alt_key},ctrlKey:{ctrl_key},metaKey:{meta_key},shiftKey:{shift_key}}});\
                            dispatchTrustedEvent(target, pointer);\
                            var evt = createNativeEvent('MouseEvent', 'mouseup', {{bubbles:true,cancelable:true,composed:true,view:globalThis,clientX:{x},clientY:{y},button:{button_code},buttons:0,detail:{click_count},altKey:{alt_key},ctrlKey:{ctrl_key},metaKey:{meta_key},shiftKey:{shift_key}}});\
                            dispatchTrustedEvent(target, evt);\
                            if (!down || down.button !== {button_code} || {button_code} !== 0) return;\
                            var clickTarget = down.target;\
                            while (clickTarget && clickTarget !== target && !(clickTarget.contains && clickTarget.contains(target))) {{\
                                clickTarget = clickTarget.parentElement;\
                            }}\
                            if (!clickTarget) return;\
                            var tag = clickTarget.tagName;\
                            var type = (clickTarget.getAttribute && clickTarget.getAttribute('type') || '').toLowerCase();\
                            if (globalThis.__obscura_isDisabled(clickTarget)) return;\
                            var checkable = tag === 'INPUT' && (type === 'checkbox' || type === 'radio');\
                            var oldChecked = checkable ? globalThis.__obscura_inputChecked(clickTarget) : false;\
                            var oldIndeterminate = checkable ? globalThis.__obscura_inputIndeterminate(clickTarget) : false;\
                            var radioStates = null;\
                            if (checkable && type === 'radio') {{\
                                var radioName = clickTarget.getAttribute('name') || '';\
                                if (radioName) {{\
                                    var candidates = document.querySelectorAll('input');\
                                    radioStates = [];\
                                    for (var ri = 0; ri < candidates.length; ri++) {{\
                                        var radio = candidates[ri];\
                                        if ((radio.getAttribute('type') || '').toLowerCase() !== 'radio' || (radio.getAttribute('name') || '') !== radioName || radio.form !== clickTarget.form) continue;\
                                        radioStates.push([radio, globalThis.__obscura_inputChecked(radio)]);\
                                        if (radio !== clickTarget) globalThis.__obscura_setInputChecked(radio, false);\
                                    }}\
                                }}\
                                globalThis.__obscura_setInputChecked(clickTarget, true);\
                            }} else if (checkable) {{\
                                globalThis.__obscura_setInputChecked(clickTarget, !oldChecked);\
                                globalThis.__obscura_setInputIndeterminate(clickTarget, false);\
                            }}\
                            var click = createNativeEvent('MouseEvent', 'click', {{bubbles:true,cancelable:true,composed:true,view:globalThis,clientX:{x},clientY:{y},button:0,buttons:0,detail:{click_count},altKey:{alt_key},ctrlKey:{ctrl_key},metaKey:{meta_key},shiftKey:{shift_key}}});\
                            var cancelled = !dispatchTrustedEvent(clickTarget, click);\
                            if (cancelled) {{\
                                if (radioStates) {{\
                                    for (var rr = 0; rr < radioStates.length; rr++) globalThis.__obscura_setInputChecked(radioStates[rr][0], radioStates[rr][1]);\
                                }} else if (checkable) {{ globalThis.__obscura_setInputChecked(clickTarget, oldChecked); globalThis.__obscura_setInputIndeterminate(clickTarget, oldIndeterminate); }}\
                                return;\
                            }}\
                            if ({click_count} === 2) {{\
                                var doubleClick = createNativeEvent('MouseEvent', 'dblclick', {{bubbles:true,cancelable:true,view:globalThis,clientX:{x},clientY:{y},button:0,buttons:0,detail:2,altKey:{alt_key},ctrlKey:{ctrl_key},metaKey:{meta_key},shiftKey:{shift_key}}});\
                                dispatchTrustedEvent(clickTarget, doubleClick);\
                            }}\
                            if (checkable && globalThis.__obscura_inputChecked(clickTarget) !== oldChecked) {{\
                                try {{ dispatchTrustedEvent(clickTarget, createNativeEvent('Event', 'input', {{bubbles:true}})); }} catch(e) {{}}\
                                try {{ dispatchTrustedEvent(clickTarget, createNativeEvent('Event', 'change', {{bubbles:true}})); }} catch(e) {{}}\
                                return;\
                            }}\
                            var labelHost = tag === 'LABEL' ? clickTarget : (clickTarget.closest ? clickTarget.closest('label') : null);\
                            var interactiveHost = globalThis.__obscura_interactiveHost(clickTarget);\
                            if (labelHost && !(interactiveHost && labelHost.contains(interactiveHost))) {{\
                                var ctl = globalThis.__obscura_labeledControl(labelHost);\
                                if (ctl && ctl !== clickTarget && activateLabel(labelHost, ctl, true)) {{ return; }}\
                            }}\
                            if (globalThis.__obscura_activateSummary(clickTarget)) return;\
                            var link = clickTarget.closest ? clickTarget.closest('a[href]') : null;\
                            if (!link && tag === 'A' && clickTarget.getAttribute('href')) link = clickTarget;\
                            var submitButton = tag === 'BUTTON' ? clickTarget : (clickTarget.closest ? clickTarget.closest('button') : null);\
                            var submitButtonType = submitButton ? ((submitButton.getAttribute('type') || '').toLowerCase()) : '';\
                            if (link) {{\
                                var href = link.getAttribute('href');\
                                if (href && !href.startsWith('javascript:')) {{\
                                    if (href.startsWith('#')) history.pushState(null, '', href);\
                                    else location.assign(href);\
                                }}\
                            }} else if (submitButton && submitButtonType !== 'button' && submitButtonType !== 'reset' && !globalThis.__obscura_isDisabled(submitButton)) {{\
                                var form = submitButton.closest ? submitButton.closest('form') : null;\
                                if (form) {{ try {{ if (typeof form.requestSubmit === 'function') {{ form.requestSubmit(submitButton); }} else {{ form.submit(submitButton); }} }} catch(e) {{}} }}\
                            }} else if (tag === 'INPUT' && (type === 'submit' || type === 'image')) {{\
                                var form2 = clickTarget.closest ? clickTarget.closest('form') : null;\
                                if (form2) {{ try {{ if (typeof form2.requestSubmit === 'function') {{ form2.requestSubmit(clickTarget); }} else {{ form2.submit(clickTarget); }} }} catch(e) {{}} }}\
                            }} else if ({click_count} >= 3 && (tag === 'INPUT' || tag === 'TEXTAREA')) {{\
                                var len = clickTarget.value ? clickTarget.value.length : 0;\
                                if (clickTarget.setSelectionRange) clickTarget.setSelectionRange(0, len);\
                                else {{ clickTarget.selectionStart = 0; clickTarget.selectionEnd = len; }}\
                            }}\
                        }})()",
                        x = x,
                        y = y,
                        button_code = button_code,
                        click_count = click_count,
                        alt_key = alt_key,
                        ctrl_key = ctrl_key,
                        meta_key = meta_key,
                        shift_key = shift_key,
                    );
                    page.js.as_mut().ok_or("JavaScript runtime unavailable")?.evaluate_input_script(&pointer_route, &code)?;
                    // Document navigation runs after the input acknowledgement.
                    // Same-document routing still needs a frame notification.
                    if !page.has_pending_navigation() && page.sync_virtual_url() {
                        Some((page.frame_id.clone(), page.url_string()))
                    } else {
                        None
                    }
                } else {
                    None
                };
                if let Some((frame_id, url)) = moved_frame {
                    crate::domains::page::emit_same_document_navigation(ctx, session_id, &frame_id, &url);
                }
            } else if event_type == "mouseWheel" {
                let delta_x = params.get("deltaX").and_then(|v| v.as_f64()).unwrap_or(0.0);
                let delta_y = params.get("deltaY").and_then(|v| v.as_f64()).unwrap_or(0.0);
                if let Some(page) = ctx.get_session_page_mut(session_id) {
                    let code = format!(
                        "(function() {{\
                            var target = nativeTarget;\
                            if (!target) return;\
                            var wheel = createNativeEvent('WheelEvent', 'wheel', {{bubbles:true,cancelable:true,view:globalThis,clientX:{x},clientY:{y},deltaX:{delta_x},deltaY:{delta_y},deltaMode:0,altKey:{alt_key},ctrlKey:{ctrl_key},metaKey:{meta_key},shiftKey:{shift_key}}});\
                            if (!dispatchTrustedEvent(target, wheel)) return;\
                            var dx = {delta_x}, dy = {delta_y};\
                            var root = document.scrollingElement || document.documentElement || document.body;\
                            var scrollTarget = null;\
                            var el = target;\
                            while (el && el.nodeType === 1 && el !== root && el !== document.body && el !== document.documentElement) {{\
                                var maxX = Math.max(0, (el.scrollWidth || 0) - (el.clientWidth || 0));\
                                var maxY = Math.max(0, (el.scrollHeight || 0) - (el.clientHeight || 0));\
                                var style = null;\
                                try {{ style = getComputedStyle(el); }} catch (_e) {{}}\
                                var ox = style ? (style.overflowX || style.overflow || '') : '';\
                                var oy = style ? (style.overflowY || style.overflow || '') : '';\
                                var allowX = ox === 'auto' || ox === 'scroll' || ox === 'overlay';\
                                var allowY = oy === 'auto' || oy === 'scroll' || oy === 'overlay';\
                                var consumesX = allowX && ((dx > 0 && el.scrollLeft < maxX) || (dx < 0 && el.scrollLeft > 0));\
                                var consumesY = allowY && ((dy > 0 && el.scrollTop < maxY) || (dy < 0 && el.scrollTop > 0));\
                                if (consumesX || consumesY) {{ scrollTarget = el; break; }}\
                                el = el.parentElement;\
                            }}\
                            if (!scrollTarget) scrollTarget = root;\
                            if (scrollTarget === root && root && typeof root.scrollBy === 'function') {{\
                                var beforeX = root.scrollLeft, beforeY = root.scrollTop;\
                                root.scrollBy(dx, dy);\
                                if (root.scrollLeft !== beforeX || root.scrollTop !== beforeY) setTimeout(function() {{\
                                    try {{ dispatchTrustedEvent(document, createNativeEvent('Event', 'scroll', {{bubbles:false}})); }} catch (_e) {{}}\
                                    try {{ dispatchTrustedEvent(globalThis, createNativeEvent('Event', 'scroll', {{bubbles:false}})); }} catch (_e) {{}}\
                                }}, 0);\
                            }} else if (scrollTarget && typeof scrollTarget.scrollBy === 'function') scrollTarget.scrollBy(dx, dy);\
                        }})()",
                        x = x,
                        y = y,
                        delta_x = delta_x,
                        delta_y = delta_y,
                        alt_key = alt_key,
                        ctrl_key = ctrl_key,
                        meta_key = meta_key,
                        shift_key = shift_key,
                    );
                    page.js.as_mut().ok_or("JavaScript runtime unavailable")?.evaluate_input_script(&pointer_route, &code)?;
                }
            }

            Ok(json!({}))
        }
        // Chrome's Input.insertText: Playwright's fill() focuses the field in
        // page and then types the whole value through this one call (#577).
        "insertText" => {
            let text = params.get("text").and_then(|v| v.as_str()).unwrap_or("");
            if let Some(page) = ctx.get_session_page_mut(session_id) {
                // IME commits snapshot the final caret before beforeinput.
                // Keyboard insertion instead follows the handler's selection.
                page.js.as_mut().ok_or("JavaScript runtime unavailable")?.evaluate_focused_input_script(&insert_text_js(text, true))?;
            }
            Ok(json!({}))
        }
        "dispatchKeyEvent" => {
            let event_type = params.get("type").and_then(|v| v.as_str()).unwrap_or("");
            let modifiers = params.get("modifiers").and_then(|v| v.as_u64()).unwrap_or(0);
            let (alt_key, ctrl_key, meta_key, shift_key) = modifier_flags(modifiers);
            let key = params.get("key").and_then(|v| v.as_str()).unwrap_or("");
            let code = params.get("code").and_then(|v| v.as_str()).unwrap_or("");
            let text = params.get("text").and_then(|v| v.as_str()).unwrap_or("");
            let virtual_key_code = params
                .get("windowsVirtualKeyCode")
                .and_then(|v| v.as_u64())
                .unwrap_or(0);

            if let Some(page) = ctx.get_session_page_mut(session_id) {
                match event_type {
                    "keyDown" | "rawKeyDown" => {
                        let js = format!(
                            "(function() {{\
                                var target = nativeTarget || document.body;\
                                var evt = createNativeEvent('KeyboardEvent', 'keydown', {{bubbles:true,cancelable:true,key:{key},code:{code},keyCode:{virtual_key_code},which:{virtual_key_code},altKey:{alt_key},ctrlKey:{ctrl_key},metaKey:{meta_key},shiftKey:{shift_key}}});\
                                return dispatchTrustedEvent(target, evt);\
                            }})()",
                            // Escape backslash BEFORE single-quote (as the text
                            // path below does) so a key like "\" — Chrome's
                            // backslash key — doesn't escape the closing quote
                            // and produce a syntax error that drops the event.
                            key = js_str(key),
                            code = js_str(code),
                            virtual_key_code = virtual_key_code,
                        );
                        if page.js.as_mut().ok_or("JavaScript runtime unavailable")?.evaluate_focused_input_script(&js)?.as_bool() == Some(false) {
                            return Ok(json!({}));
                        }
                        let mac = page.js.as_ref().ok_or("JavaScript runtime unavailable")?.input_uses_meta_shortcuts();
                        let primary = !alt_key && if mac { meta_key && !ctrl_key } else { ctrl_key && !meta_key };
                        let select_all = primary && (key.eq_ignore_ascii_case("a") || code == "KeyA" || virtual_key_code == 65);
                        if select_all {
                            page.js.as_mut().ok_or("JavaScript runtime unavailable")?.evaluate_focused_input_script(
                                "(function(){var target=nativeTarget;if(!target||(target.localName!=='input'&&target.localName!=='textarea')||globalThis.__obscura_isDisabled(target))return;target.setSelectionRange(0,(target.value||'').length);})()"
                            )?;
                        }

                        if key == "Tab" && !alt_key && !ctrl_key && !meta_key {
                            let source = "(function(){\
                                var nodes=Array.from(nativeDocument.querySelectorAll.call(document,'input,button,a[href],area[href],select,textarea,iframe,[tabindex],[contenteditable]'));\
                                var order=nodes.map(function(node,index){return {node:node,index:index,tab:node.tabIndex};}).filter(function(entry){\
                                    var node=entry.node,style=getComputedStyle(node);\
                                    return entry.tab>=0&&!globalThis.__obscura_isDisabled(node)&&!node.closest('[inert]')&&!(node.localName==='input'&&node.type==='hidden')&&style.display!=='none'&&style.visibility!=='hidden'&&node.getClientRects().length;\
                                });\
                                order.sort(function(a,b){return (a.tab>0?0:1)-(b.tab>0?0:1)||(a.tab>0&&b.tab>0?a.tab-b.tab:0)||a.index-b.index;});\
                                if(!order.length)return;\
                                var active=nativeDocument.focusedNode(),index=order.findIndex(function(entry){return entry.node===active;});\
                                var next=$REVERSE?(index<0?order.length-1:(index+order.length-1)%order.length):(index+1)%order.length;\
                                nativeDocument.focusOwner(order[next].node);\
                            })()".replace("$REVERSE", if shift_key { "true" } else { "false" });
                            page.js.as_mut().ok_or("JavaScript runtime unavailable")?.evaluate_focused_input_script(&source)?;
                        }

                        if !select_all && !text.is_empty() && text != "\r" && text != "\n" {
                            page.js.as_mut().ok_or("JavaScript runtime unavailable")?.evaluate_focused_input_script(&insert_text_js(text, false))?;
                        }

                        if key == "Enter" {
                            // In a textarea Enter inserts a newline; in input fields
                            // it submits the containing form. Real Chrome distinguishes
                            // these two and we should too: previously every Enter tried
                            // to submit the nearest form even from a textarea.
                            let js = "(function() {\
                                var target = nativeTarget;\
                                if (!target) return;\
                                dispatchTrustedEvent(target, createNativeEvent('KeyboardEvent', 'keypress', {bubbles:true,key:'Enter',code:'Enter',keyCode:$VK,which:$VK,charCode:$VK,$MODIFIERS}));\
                                if (target.localName === 'textarea') {\
                                    if (target.hasAttribute('readonly')) return;\
                                    var value = target.value || '';\
                                    var start = target.selectionStart, end = target.selectionEnd;\
                                    start = start == null ? value.length : Math.max(0, Math.min(start, value.length));\
                                    end = end == null ? start : Math.max(0, Math.min(end, value.length));\
                                    var lower = Math.min(start, end), upper = Math.max(start, end);\
                                    globalThis.__obscura_setFieldValue(target, 'value', value.slice(0, lower) + '\\n' + value.slice(upper));\
                                    target.setSelectionRange(lower + 1, lower + 1);\
                                    dispatchTrustedEvent(target, createNativeEvent('Event', 'input', {bubbles:true}));\
                                } else {\
                                    var form = target.form || (target.closest && target.closest('form'));\
                                    if (form) {{ try {{ if (typeof form.requestSubmit === 'function') {{ form.requestSubmit(); }} else {{ form.submit(); }} }} catch(e) {{}} }}\
                                }\
                            })()".replace("$VK", &virtual_key_code.to_string())
                                .replace("$MODIFIERS", &format!("altKey:{alt_key},ctrlKey:{ctrl_key},metaKey:{meta_key},shiftKey:{shift_key}"));
                            page.js.as_mut().ok_or("JavaScript runtime unavailable")?.evaluate_focused_input_script(&js)?;
                        }

                        if key == "Backspace" {
                            page.js.as_mut().ok_or("JavaScript runtime unavailable")?.evaluate_focused_input_script(BACKSPACE_JS)?;
                        }

                        if key == "Delete" {
                            page.js.as_mut().ok_or("JavaScript runtime unavailable")?.evaluate_focused_input_script(DELETE_JS)?;
                        }
                    }
                    "keyUp" => {
                        let js = format!(
                            "(function() {{\
                                var target = nativeTarget || document.body;\
                                var evt = createNativeEvent('KeyboardEvent', 'keyup', {{bubbles:true,key:{key},code:{code},keyCode:{virtual_key_code},which:{virtual_key_code},altKey:{alt_key},ctrlKey:{ctrl_key},metaKey:{meta_key},shiftKey:{shift_key}}});\
                                dispatchTrustedEvent(target, evt);\
                            }})()",
                            key = js_str(key),
                            code = js_str(code),
                            virtual_key_code = virtual_key_code,
                        );
                        page.js.as_mut().ok_or("JavaScript runtime unavailable")?.evaluate_focused_input_script(&js)?;
                    }
                    "char" => {
                        if !text.is_empty() {
                            page.js.as_mut().ok_or("JavaScript runtime unavailable")?.evaluate_focused_input_script(&insert_text_js(text, false))?;
                            // Pump event loop so Angular change detection picks up the input
                            page.settle(50).await;
                        }
                    }
                    _ => {}
                }
            }

            Ok(json!({}))
        }
        "dispatchTouchEvent" => Ok(json!({})),
        "setIgnoreInputEvents" => Ok(json!({})),
        _ => Err(format!("Unknown Input method: {}", method)),
    }
}

#[cfg(test)]
mod tests {
    use super::js_str;

    #[cfg(feature = "render")]
    use super::{handle, CdpContext};
    #[cfg(feature = "render")]
    use serde_json::json;

    #[cfg(feature = "render")]
    fn native_page(html: &str) -> (CdpContext, Option<String>) {
        let mut ctx = CdpContext::new();
        let page = ctx.create_page();
        let session = Some(format!("{page}-input"));
        ctx.sessions.insert(session.clone().unwrap(), page);
        let page = ctx.get_session_page_mut(&session).unwrap();
        page.navigate_blank();
        page.set_viewport((640.0,480.0));
        page.resume_js();
        page.evaluate_script(&format!("document.body.innerHTML={};true",serde_json::to_string(html).unwrap())).unwrap();
        (ctx,session)
    }

    #[cfg(feature = "render")]
    async fn mouse(ctx: &mut CdpContext, session: &Option<String>, kind: &str, point: (f64,f64)) {
        handle("dispatchMouseEvent",&json!({
            "type":kind,"x":point.0,"y":point.1,"button":"left",
            "buttons":if kind=="mouseReleased" {0}else{1},"clickCount":1,
        }),ctx,session).await.unwrap();
    }

    #[cfg(feature = "render")]
    async fn key_a(ctx: &mut CdpContext, session: &Option<String>, modifiers: u64) {
        handle("dispatchKeyEvent",&json!({
            "type":"keyDown","key":"a","code":"KeyA","windowsVirtualKeyCode":65,"modifiers":modifiers,
        }),ctx,session).await.unwrap();
    }

    #[cfg(feature = "render")]
    fn child_point(quad: [f32;8], viewport: (f64,f64), point: (f64,f64)) -> (f64,f64) {
        let (u,v) = (point.0/viewport.0,point.1/viewport.1);
        (f64::from(quad[0])+u*f64::from(quad[2]-quad[0])+v*f64::from(quad[6]-quad[0]),
         f64::from(quad[1])+u*f64::from(quad[3]-quad[1])+v*f64::from(quad[7]-quad[1]))
    }

    #[cfg(feature = "render")]
    #[tokio::test(flavor = "current_thread")]
    async fn tab_default_uses_focus_order_reverse_and_cancelled_keydown() {
        let (mut ctx,session) = native_page(r#"<input id="first"><button id="disabled" disabled>Disabled</button>
            <input id="hidden" style="display:none"><button id="negative" tabindex="-1">Negative</button>
            <button id="late" tabindex="2">Late</button><button id="early" tabindex="1">Early</button>
            <button id="after">After</button>"#);
        ctx.get_session_page_mut(&session).unwrap().evaluate_script("document.getElementById('early').focus()").unwrap();
        for (modifiers,expected) in [(0,"late"),(8,"early")] {
            handle("dispatchKeyEvent",&json!({"type":"keyDown","key":"Tab","code":"Tab","modifiers":modifiers}),&mut ctx,&session).await.unwrap();
            assert_eq!(ctx.get_session_page_mut(&session).unwrap().evaluate_script("document.activeElement.id").unwrap(),expected);
        }
        ctx.get_session_page_mut(&session).unwrap().evaluate_script(
            "document.getElementById('first').focus();document.getElementById('first').addEventListener('keydown',event=>event.preventDefault(),{once:true})"
        ).unwrap();
        handle("dispatchKeyEvent",&json!({"type":"keyDown","key":"Tab","code":"Tab"}),&mut ctx,&session).await.unwrap();
        assert_eq!(ctx.get_session_page_mut(&session).unwrap().evaluate_script("document.activeElement.id").unwrap(),"first");
        handle("dispatchKeyEvent",&json!({"type":"keyDown","key":"Tab","code":"Tab"}),&mut ctx,&session).await.unwrap();
        assert_eq!(ctx.get_session_page_mut(&session).unwrap().evaluate_script("document.activeElement.id").unwrap(),"after");
    }

    #[cfg(feature = "render")]
    #[tokio::test(flavor = "current_thread")]
    async fn pointer_descends_transformed_native_frames_and_release_cannot_reuse_another_realm_down() {
        let (mut ctx,session) = native_page(r#"<style>body{margin:0}</style>
            <button id="root" style="position:absolute;left:0;top:0;width:40px;height:30px">Root</button>
            <iframe id="owner" style="position:absolute;left:60px;top:80px;width:180px;height:120px;border:3px solid;padding:7px;transform:matrix(1.5,.25,.2,1.2,20,10);transform-origin:0 0"></iframe>"#);
        ctx.get_session_page_mut(&session).unwrap().set_device_scale_factor(2.0);
        ctx.get_session_page_mut(&session).unwrap().evaluate_script(r#"(() => {
            globalThis.rootClicks=0;document.getElementById('root').onclick=()=>rootClicks++;
            const child=document.getElementById('owner').contentDocument;
            child.body.innerHTML='<button id="child" style="position:absolute;left:12px;top:14px;width:90px;height:30px">Child</button><input id="field" value="初始😀z" style="position:absolute;left:12px;top:60px;width:90px;height:25px">';
            child.defaultView.events=[];child.defaultView.count=0;
            for(const type of ['pointerdown','mousedown','pointerup','mouseup','click']) child.getElementById('child').addEventListener(type,e=>{
                child.defaultView.events.push([type,e.isTrusted,e.clientX,e.clientY]);
                if(type==='click')child.defaultView.count++;
            });
            return true;
        })()"#).unwrap();
        ctx.get_session_page_mut(&session).unwrap().adopt_ready_frame_documents().await;
        let (point,field_point,child_id) = {
            let page=ctx.get_session_page_mut(&session).unwrap();
            let owner=page.with_dom(|dom|dom.get_element_by_id("owner").unwrap()).unwrap();
            let quad=page.js.as_ref().unwrap().node_box_model(0,owner.index() as u32).unwrap().0[0];
            let point=child_point(quad,(180.0,120.0),(30.0,25.0));
            let field=child_point(quad,(180.0,120.0),(30.0,72.0));
            (point,field,page.frames.iter().find(|frame|frame.parent_frame_id()==0).unwrap().frame_id())
        };
        mouse(&mut ctx,&session,"mousePressed",point).await;
        mouse(&mut ctx,&session,"mouseReleased",point).await;
        let events=ctx.get_session_page_mut(&session).unwrap().evaluate_script(
            "document.getElementById('owner').contentWindow.events"
        ).unwrap();
        assert_eq!(events.as_array().unwrap().iter().map(|event|event[0].as_str().unwrap()).collect::<Vec<_>>(),
            vec!["pointerdown","mousedown","pointerup","mouseup","click"]);
        for event in events.as_array().unwrap() {
            assert_eq!(event[1],true);
            assert!((event[2].as_f64().unwrap()-30.0).abs()<0.01 && (event[3].as_f64().unwrap()-25.0).abs()<0.01);
        }
        assert_eq!(ctx.get_session_page_mut(&session).unwrap().evaluate_script(
            "document.activeElement===document.getElementById('owner')"
        ).unwrap(),true);
        mouse(&mut ctx,&session,"mousePressed",point).await;
        mouse(&mut ctx,&session,"mouseReleased",(10.0,10.0)).await;
        mouse(&mut ctx,&session,"mouseReleased",point).await;
        mouse(&mut ctx,&session,"mousePressed",(10.0,10.0)).await;
        mouse(&mut ctx,&session,"mouseReleased",point).await;
        mouse(&mut ctx,&session,"mouseReleased",(10.0,10.0)).await;
        assert_eq!(ctx.get_session_page_mut(&session).unwrap().evaluate_script(
            "[rootClicks,document.getElementById('owner').contentWindow.count]"
        ).unwrap(),json!([0,1]));
        mouse(&mut ctx,&session,"mousePressed",field_point).await;
        mouse(&mut ctx,&session,"mouseReleased",field_point).await;
        let page=ctx.get_session_page_mut(&session).unwrap();
        assert_eq!(page.js.as_mut().unwrap().focused_input_route().unwrap().frame_id,child_id);
        page.evaluate_script("document.getElementById('root').focus();true").unwrap();
        assert_eq!(page.js.as_mut().unwrap().focused_input_route().unwrap().frame_id,0,"keyboard focus is not the last pointer realm");
    }

    #[cfg(feature = "render")]
    #[tokio::test(flavor = "current_thread")]
    async fn native_meta_select_all_replaces_utf16_selection_and_respects_cancelled_keydown() {
        for (platform,primary,wrong) in [("MacIntel",4,2),("Linux x86_64",2,4)] {
            let (mut ctx,session)=native_page("<input id='field' value='初始😀z'>");
            let page=ctx.get_session_page_mut(&session).unwrap();
            page.js.as_mut().unwrap().set_environment_identity(&json!({"platform":platform,"locale":"en-US","languages":["en-US"]}));
            page.evaluate_script("document.getElementById('field').focus();true").unwrap();
            key_a(&mut ctx,&session,wrong).await;
            assert_eq!(ctx.get_session_page_mut(&session).unwrap().evaluate_script(
                "[field.selectionStart,field.selectionEnd]"
            ).unwrap(),json!([null,null]));
            key_a(&mut ctx,&session,primary).await;
            assert_eq!(ctx.get_session_page_mut(&session).unwrap().evaluate_script(
                "[field.selectionStart,field.selectionEnd]"
            ).unwrap(),json!([0,5]));
            handle("insertText",&json!({"text":"替换😀"}),&mut ctx,&session).await.unwrap();
            assert_eq!(ctx.get_session_page_mut(&session).unwrap().evaluate_script(
                "[field.value,field.selectionStart,field.selectionEnd]"
            ).unwrap(),json!(["替换😀",4,4]));
            ctx.get_session_page_mut(&session).unwrap().evaluate_script(
                "field.setSelectionRange(2,2);field.addEventListener('keydown',e=>e.preventDefault());true"
            ).unwrap();
            key_a(&mut ctx,&session,primary).await;
            assert_eq!(ctx.get_session_page_mut(&session).unwrap().evaluate_script(
                "[field.selectionStart,field.selectionEnd]"
            ).unwrap(),json!([2,2]));
        }
    }

    #[cfg(feature = "render")]
    #[tokio::test(flavor = "current_thread")]
    async fn native_collapsed_deletion_keeps_surrogate_pairs_whole_and_obeys_keydown_cancellation() {
        let (mut ctx,session)=native_page("<input id='field'>");
        ctx.get_session_page_mut(&session).unwrap().evaluate_script(
            "field.focus();globalThis.inputs=[];field.addEventListener('input',e=>inputs.push(e.isTrusted));true"
        ).unwrap();
        // Offsets are UTF-16, including author-positioned carets between the
        // two units of a valid surrogate pair.
        for (key,caret) in [("Backspace",3),("Backspace",2),("Delete",1),("Delete",2)] {
            ctx.get_session_page_mut(&session).unwrap().evaluate_script(
                &format!("field.value='A😀B';field.setSelectionRange({caret},{caret});true")
            ).unwrap();
            handle("dispatchKeyEvent",&json!({"type":"keyDown","key":key,"code":key}),&mut ctx,&session).await.unwrap();
            assert_eq!(ctx.get_session_page_mut(&session).unwrap().evaluate_script(
                "[field.value,field.selectionStart,field.selectionEnd]"
            ).unwrap(),json!(["AB",1,1]));
        }
        ctx.get_session_page_mut(&session).unwrap().evaluate_script(
            "field.addEventListener('keydown',e=>e.preventDefault());true"
        ).unwrap();
        for (key,caret) in [("Backspace",3),("Delete",1)] {
            ctx.get_session_page_mut(&session).unwrap().evaluate_script(
                &format!("field.value='A😀B';field.setSelectionRange({caret},{caret});true")
            ).unwrap();
            handle("dispatchKeyEvent",&json!({"type":"keyDown","key":key,"code":key}),&mut ctx,&session).await.unwrap();
            assert_eq!(ctx.get_session_page_mut(&session).unwrap().evaluate_script(
                "[field.value,field.selectionStart,field.selectionEnd]"
            ).unwrap(),json!(["A😀B",caret,caret]));
        }
        assert_eq!(ctx.get_session_page_mut(&session).unwrap().evaluate_script("inputs").unwrap(),
            json!([true,true,true,true]));
    }

    #[cfg(feature = "render")]
    #[tokio::test(flavor = "current_thread")]
    async fn native_input_reaches_nested_cross_origin_dom_and_keeps_host_event_helpers_private() {
        use tokio::io::{AsyncReadExt,AsyncWriteExt};
        let listener=tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base=format!("http://{}",listener.local_addr().unwrap());
        tokio::spawn(async move {
            while let Ok((mut socket,_))=listener.accept().await {
                tokio::spawn(async move {
                    let mut request=[0;2048];
                    let count=socket.read(&mut request).await.unwrap();
                    let request=String::from_utf8_lossy(&request[..count]);
                    let body=if request.starts_with("GET /outer ") {
                        r#"<html><body style="margin:0"><iframe id="nested" src="/grand" style="position:absolute;left:40px;top:30px;width:90px;height:70px;border:2px solid;padding:3px;transform:matrix(.9,.1,.15,1.1,5,2);transform-origin:0 0"></iframe></body></html>"#
                    } else {
                        r#"<html><body style="margin:0">
                        <button id="go" style="position:absolute;left:8px;top:8px;width:60px;height:25px">Child</button>
                        <input id="field" value="初始😀z" style="position:absolute;left:8px;top:42px;width:60px;height:18px">
                        <div style="position:absolute;left:0;top:200px;width:80px;height:200px"></div>
                        <script>
                        globalThis.count=0;globalThis.publicDispatchCalls=0;globalThis.seen=[];
                        const button=document.getElementById('go');
                        const publicDispatch=EventTarget.prototype.dispatchEvent.bind(button);
                        globalThis.redispatchLast=()=>publicDispatch(globalThis.lastNativeEvent);
                        button.addEventListener('click',function(event){
                            seen.push([event.isTrusted,event.clientX,event.clientY]);
                            if(event.isTrusted){count++;globalThis.lastNativeEvent=event;button.textContent='Clicked';}
                        });
                        document.addEventListener('wheel',event=>globalThis.wheelSeen=[event.isTrusted,event.clientX,event.clientY]);
                        button.dispatchEvent=()=>{publicDispatchCalls++;return true};
                        globalThis.Event=function(){throw Error('author Event constructor intercepted')};
                        globalThis.MouseEvent=function(){throw Error('author MouseEvent constructor intercepted')};
                        </script></body></html>"#
                    };
                    let response=format!("HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",body.len());
                    let _=socket.write_all(response.as_bytes()).await;
                });
            }
        });
        let context=std::sync::Arc::new(obscura_browser::BrowserContext::with_storage_and_network(
            "cross-origin-input".into(),None,false,None,None,true,
        ));
        let mut ctx=CdpContext::new_with_shared_context(context);
        let page_id=ctx.create_page();
        let session=Some(format!("{page_id}-input"));
        ctx.sessions.insert(session.clone().unwrap(),page_id);
        let page=ctx.get_session_page_mut(&session).unwrap();
        page.navigate_blank();
        page.set_viewport((640.0,480.0));
        page.set_device_scale_factor(2.0);
        page.resume_js();
        page.evaluate_script(&format!(
            "document.body.innerHTML={};true",
            serde_json::to_string(&format!("<style>body{{margin:0}}</style><iframe id='owner' src='{base}/outer' style='position:absolute;left:100px;top:80px;width:200px;height:160px;border:2px solid;padding:4px'></iframe>")).unwrap(),
        )).unwrap();
        page.settle(2_000).await;
        assert_eq!(page.evaluate_script("document.getElementById('owner').contentDocument===null").unwrap(),true);
        let outer=page.frames.iter().position(|frame|frame.url()==format!("{base}/outer")).unwrap();
        let grand=page.frames.iter().position(|frame|frame.url()==format!("{base}/grand")).unwrap();
        let outer_id=page.frames[outer].frame_id();
        let owner=page.with_dom(|dom|dom.get_element_by_id("owner").unwrap()).unwrap();
        let nested=page.js.as_ref().unwrap().with_frame_dom(outer_id,|dom|dom.get_element_by_id("nested").unwrap()).unwrap();
        let root_quad=page.js.as_ref().unwrap().node_box_model(0,owner.index() as u32).unwrap().0[0];
        let nested_quad=page.js.as_ref().unwrap().node_box_model(outer_id,nested.index() as u32).unwrap().0[0];
        let point=child_point(root_quad,(200.0,160.0),child_point(nested_quad,(90.0,70.0),(24.0,18.0)));
        mouse(&mut ctx,&session,"mousePressed",point).await;
        mouse(&mut ctx,&session,"mouseReleased",point).await;
        let page=ctx.get_session_page_mut(&session).unwrap();
        let observed=page.frames[grand].evaluate(page.js.as_mut().unwrap(),
            "[count,publicDispatchCalls,seen,typeof __obscura_markTrusted,typeof createNativeEvent,typeof dispatchTrustedEvent,typeof nativeDocument]"
        ).unwrap();
        assert_eq!(observed[0],1);
        assert_eq!(observed[1],0,"native dispatch must bypass author-replaced dispatchEvent");
        assert_eq!(observed[2][0][0],true);
        assert!((observed[2][0][1].as_f64().unwrap()-24.0).abs()<0.02);
        assert!((observed[2][0][2].as_f64().unwrap()-18.0).abs()<0.02);
        assert_eq!(observed.as_array().unwrap()[3..],json!(["undefined","undefined","undefined","undefined"]).as_array().unwrap()[..]);
        let replay=page.frames[grand].evaluate(page.js.as_mut().unwrap(),"(() => {redispatchLast();return [count,seen[seen.length-1][0]]})()").unwrap();
        assert_eq!(replay,json!([1,false]),"author redispatch cannot retain native trust");
        page.frames[grand].evaluate(page.js.as_mut().unwrap(),
            "(() => {document.getElementById('field').focus();return true})()"
        ).unwrap();
        let primary=if page.js.as_ref().unwrap().input_uses_meta_shortcuts(){4}else{2};
        key_a(&mut ctx,&session,primary).await;
        handle("insertText",&json!({"text":"交接😀"}),&mut ctx,&session).await.unwrap();
        let page=ctx.get_session_page_mut(&session).unwrap();
        assert_eq!(page.frames[grand].evaluate(page.js.as_mut().unwrap(),
            "[document.getElementById('field').value,document.getElementById('field').selectionStart]"
        ).unwrap(),json!(["交接😀",4]));
        handle("dispatchMouseEvent",&json!({
            "type":"mouseWheel","x":point.0,"y":point.1,"deltaY":40,"deltaX":0,
        }),&mut ctx,&session).await.unwrap();
        let page=ctx.get_session_page_mut(&session).unwrap();
        let wheel=page.frames[grand].evaluate(page.js.as_mut().unwrap(),"[scrollY,wheelSeen]").unwrap();
        assert!(wheel[0].as_f64().unwrap()>0.0);
        assert_eq!(wheel[1][0],true);
        assert_eq!(page.evaluate_script("scrollY===0").unwrap(),true);
        page.evaluate_script(&format!(r#"(() => {{
            globalThis.coverClicks=0;const cover=document.createElement('div');
            cover.id='cover';cover.style.cssText='position:absolute;left:{}px;top:{}px;width:20px;height:20px;z-index:100;background:white';
            cover.addEventListener('click',()=>coverClicks++);document.body.appendChild(cover);return true;
        }})()"#,point.0-5.0,point.1-5.0)).unwrap();
        mouse(&mut ctx,&session,"mousePressed",point).await;
        mouse(&mut ctx,&session,"mouseReleased",point).await;
        let page=ctx.get_session_page_mut(&session).unwrap();
        assert_eq!(page.evaluate_script("coverClicks===1").unwrap(),true);
        assert_eq!(page.frames[grand].evaluate(page.js.as_mut().unwrap(),"count===1").unwrap(),true);
        assert_eq!(page.js.as_mut().unwrap().focused_input_route().unwrap().frame_id,0,
            "a root background click must not keep routing keyboard input to the child");
    }

    // SEC-501 / #819 — key/code are embedded via js_str; it must escape control
    // characters (newline/CR/tab/NUL/U+2028-29), not just backslash and quote,
    // so a control char cannot terminate the literal and drop the event.
    #[test]
    fn js_str_escapes_control_characters() {
        let lit = js_str("a\nb\r\t'c\\d\"e");
        assert!(
            !lit.contains('\n') && !lit.contains('\r') && !lit.contains('\t'),
            "control characters must be escaped, not left raw: {lit:?}"
        );
        // The result must be a valid JS/JSON string literal that round-trips.
        let decoded: String =
            serde_json::from_str(&lit).expect("the literal must be valid JSON");
        assert_eq!(decoded, "a\nb\r\t'c\\d\"e");
    }
}
