use deno_core::v8;
use obscura_dom::NodeId;

use super::ObscuraJsRuntime;
use crate::ops::SharedState;
#[cfg(feature = "render")]
use crate::ops::{ObscuraState, RealmStates, ensure_resolved_scroll};

#[derive(Clone, Copy)]
struct InputOwner {
    frame_id: u32,
    generation: u64,
    node: u32,
}

/// A target in one actual native document, expressed only in CSS viewport units.
/// Its generation prevents an input acknowledgement from crossing navigation.
pub struct InputRoute {
    pub frame_id: u32,
    pub x: f64,
    pub y: f64,
    generation: u64,
    node: Option<u32>,
    owners: Vec<InputOwner>,
}

fn member<'s, 'i>(
    scope: &mut v8::PinScope<'s, 'i>,
    name: &str,
) -> Result<(v8::Local<'s, v8::Object>, v8::Local<'s, v8::Function>), String> {
    let members = scope.get_current_context()
        .get_embedder_data(scope, crate::ops::DOCUMENT_MEMBERS_SLOT)
        .and_then(|value| value.to_object(scope))
        .ok_or("Native document input bridge is unavailable")?;
    let key = v8::String::new(scope, name).ok_or("Native input member allocation failed")?;
    let function = members.get(scope, key.into())
        .and_then(|value| v8::Local::<v8::Function>::try_from(value).ok())
        .ok_or_else(|| format!("Native document member {name} is unavailable"))?;
    Ok((members, function))
}

fn original_target<'s, 'i>(
    scope: &mut v8::PinnedRef<'s, v8::TryCatch<'_, '_, v8::HandleScope<'i>>>,
    node: Option<u32>,
) -> Result<v8::Local<'s, v8::Value>, String> {
    let Some(node) = node else { return Ok(v8::null(scope).into()); };
    let (members, wrap) = member(scope, "wrapNode")?;
    let node = v8::Integer::new_from_unsigned(scope, node);
    wrap.call(scope, members.into(), &[node.into()])
        .ok_or_else(|| super::exception_text(scope))
}

#[cfg(feature = "render")]
fn content_geometry(
    state: &mut ObscuraState,
    node: NodeId,
) -> Option<([f32; 8], (f32, f32))> {
    ensure_resolved_scroll(state)?;
    let prepared = state.prepared_render.as_ref()?;
    let (_, scroll) = state.resolved_scroll.as_ref()?;
    let rect = prepared.layout().rects.get(&node)?;
    let style = prepared.layout().styles.get(&node)?;
    let width = rect.width - style.border.left - style.border.right - style.padding.left - style.padding.right;
    let height = rect.height - style.border.top - style.border.bottom - style.padding.top - style.padding.bottom;
    (width > 0.0 && height > 0.0).then_some((prepared.box_quads_with_scroll(node, scroll)?[0], (width, height)))
}

#[cfg(feature = "render")]
fn inverse_content_point(quad: [f32; 8], size: (f32, f32), x: f64, y: f64) -> Option<(f64, f64)> {
    let ax = f64::from(quad[2] - quad[0]);
    let ay = f64::from(quad[3] - quad[1]);
    let bx = f64::from(quad[6] - quad[0]);
    let by = f64::from(quad[7] - quad[1]);
    let det = ax * by - ay * bx;
    if !det.is_finite() || det.abs() <= f64::EPSILON { return None; }
    let dx = x - f64::from(quad[0]);
    let dy = y - f64::from(quad[1]);
    let u = (dx * by - dy * bx) / det;
    let v = (ax * dy - ay * dx) / det;
    let point = (u * f64::from(size.0), v * f64::from(size.1));
    (point.0.is_finite() && point.1.is_finite()).then_some(point)
}

#[cfg(feature = "render")]
fn pointer_route(
    shared: &SharedState,
    registry: &RealmStates,
    x: f64,
    y: f64,
    mut owners: Vec<InputOwner>,
    depth: usize,
) -> Result<InputRoute, String> {
    if depth > registry.live_count() { return Err("Invalid native frame owner chain".into()); }
    let mut state = shared.borrow_mut();
    let (frame_id, generation, viewport) = (state.frame_id, state.document_generation, state.viewport);
    let node = super::with_sync_render_loading_disabled(&mut state, |state| {
        ensure_resolved_scroll(state)?;
        let dom = state.dom.as_ref()?;
        let prepared = state.prepared_render.as_ref()?;
        let (_, scroll) = state.resolved_scroll.as_ref()?;
        if x < 0.0 || y < 0.0 || x >= f64::from(viewport.0) || y >= f64::from(viewport.1) {
            return None;
        }
        prepared.hit_test(dom, scroll, x as f32, y as f32)
            .or_else(|| dom.query_selector("body").ok().flatten())
    });
    if let Some(node) = node {
        let child = registry.owned_children(shared).find(|(owner, owner_generation, _)|
            *owner == node && *owner_generation == generation).map(|(_, _, child)| child.clone());
        if let Some(child) = child {
            if let Some((quad, size)) = super::with_sync_render_loading_disabled(&mut state, |state| content_geometry(state, node)) {
                if let Some((local_x, local_y)) = inverse_content_point(quad, size, x, y) {
                    if local_x >= 0.0 && local_y >= 0.0 && local_x < f64::from(size.0) && local_y < f64::from(size.1) {
                        owners.push(InputOwner { frame_id, generation, node: node.index() as u32 });
                        drop(state);
                        child.borrow_mut().set_viewport(f64::from(size.0), f64::from(size.1));
                        return pointer_route(&child, registry, local_x, local_y, owners, depth + 1);
                    }
                }
            }
        }
    }
    Ok(InputRoute { frame_id, generation, node: node.map(|node| node.index() as u32), x, y, owners })
}

#[cfg(feature = "render")]
fn point_in_frame(
    shared: &SharedState,
    registry: &RealmStates,
    target_frame: u32,
    x: f64,
    y: f64,
    depth: usize,
) -> Option<(f64, f64)> {
    if depth > registry.live_count() { return None; }
    let mut state = shared.borrow_mut();
    if state.frame_id == target_frame { return Some((x, y)); }
    let generation = state.document_generation;
    for (owner, owner_generation, child) in registry.owned_children(shared) {
        if owner_generation != generation || !state.dom.as_ref()?.is_connected(owner) { continue; }
        let Some((quad, size)) = super::with_sync_render_loading_disabled(&mut state, |state| content_geometry(state, owner)) else { continue; };
        let Some((x, y)) = inverse_content_point(quad, size, x, y) else { continue; };
        child.borrow_mut().set_viewport(f64::from(size.0), f64::from(size.1));
        if let Some(point) = point_in_frame(child, registry, target_frame, x, y, depth + 1) { return Some(point); }
    }
    None
}

impl ObscuraJsRuntime {
    fn input_state(&self, frame_id: u32) -> Result<SharedState, String> {
        if frame_id == 0 { return Ok(self.state.clone()); }
        let registry = self.realm_states();
        let registry = registry.borrow();
        if registry.frame_timers_active(frame_id) != Some(true) {
            return Err("stale_reference: input frame is detached".into());
        }
        registry.by_frame_id(frame_id).ok_or_else(|| "stale_reference: input frame is unavailable".into())
    }

    fn validate_input_route(&self, route: &InputRoute) -> Result<(), String> {
        let state = self.input_state(route.frame_id)?;
        let state = state.borrow();
        if state.document_generation != route.generation
            || route.node.is_some_and(|node| !state.dom.as_ref().is_some_and(|dom| dom.is_connected(NodeId::new(node))))
        {
            return Err("stale_reference: input document or original node was replaced".into());
        }
        Ok(())
    }

    #[doc(hidden)]
    #[cfg(feature = "render")]
    pub fn route_pointer_input(&self, x: f64, y: f64) -> Result<InputRoute, String> {
        if !x.is_finite() || !y.is_finite() { return Err("Input coordinates must be finite".into()); }
        pointer_route(&self.state, &self.realm_states().borrow(), x, y, Vec::new(), 0)
    }

    #[doc(hidden)]
    #[cfg(not(feature = "render"))]
    pub fn route_pointer_input(&self, _x: f64, _y: f64) -> Result<InputRoute, String> {
        Err("capability_unavailable: native pointer hit testing is not compiled".into())
    }

    /// Execute the existing event/default-action pipeline in the selected actual
    /// realm, with its original native target supplied as an argument.
    #[doc(hidden)]
    pub fn evaluate_input_script(&mut self, route: &InputRoute, code: &str) -> Result<serde_json::Value, String> {
        self.evaluate_input_script_in_task(route, code, true)
    }

    fn evaluate_input_script_in_task(&mut self, route: &InputRoute, code: &str, poll_notifications: bool) -> Result<serde_json::Value, String> {
        if poll_notifications {
            self.begin_javascript_task();
        } else {
            // IndexedDB delivery is itself called by begin_javascript_task.
            // Keep its native task epoch without recursively polling notices.
            self.recover_heap_limit();
            crate::ops::begin_animation_task(&mut self.state.borrow_mut());
        }
        self.validate_input_route(route)?;
        let context = self.native_cdp_context(Some(route.frame_id))?;
        self.begin_selected_cdp_task(&context);
        let source = format!("(function(nativeTarget,createNativeEvent,dispatchTrustedEvent,activateLabel,nativeDocument){{'use strict';return {code};}})");
        let result = (|| {
            let mut entered = self.runtime();
            let isolate = entered.v8_isolate();
            v8::scope_with_context!(scope, isolate, context.clone());
            v8::tc_scope!(let scope, scope);
            let target = original_target(scope, route.node)?;
            let (members, create) = member(scope, "createEvent")?;
            let (_, dispatch) = member(scope, "dispatchTrusted")?;
            let (_, label) = member(scope, "activateLabel")?;
            let source = v8::String::new(scope, &source).ok_or("Native input source allocation failed")?;
            let function = v8::Script::compile(scope, source, None).and_then(|script| script.run(scope))
                .and_then(|value| v8::Local::<v8::Function>::try_from(value).ok())
                .ok_or_else(|| super::exception_text(scope))?;
            let receiver = scope.get_current_context().global(scope);
            let result = function.call(scope, receiver.into(), &[target, create.into(), dispatch.into(), label.into(), members.into()])
                .ok_or_else(|| super::exception_text(scope))?;
            Ok(v8::Global::new(scope, result))
        })();
        let result = self.finish_heap_checked(result)?;
        self.v8_to_json_in_context(result, Some(context))
    }

    /// Evaluate a host-owned expression/IIFE with strictly scoped native event
    /// helpers. Author code receives neither a marker nor a reusable capability.
    pub fn evaluate_native_script(&mut self, source: &str) -> Result<serde_json::Value, String> {
        self.evaluate_native_script_for_frame(0, source)
    }

    #[doc(hidden)]
    pub fn evaluate_native_script_for_frame(&mut self, frame_id: u32, source: &str) -> Result<serde_json::Value, String> {
        let generation = self.input_state(frame_id)?.borrow().document_generation;
        let route = InputRoute { frame_id, generation, node: None, x: 0.0, y: 0.0, owners: Vec::new() };
        self.evaluate_input_script(&route, source)
    }

    /// Dispatch the real child's completion on its native owner, never a
    /// page-supplied iframe registry entry or replaced Event constructor.
    #[doc(hidden)]
    pub fn dispatch_frame_owner_load(&mut self, parent_frame_id: u32, frame_id: u32) -> Result<(), String> {
        self.input_state(frame_id)?;
        let parent = self.input_state(parent_frame_id)?;
        let owner = {
            let realms = self.realm_states();
            let realms = realms.borrow();
            let found = realms.owned_children(&parent).find_map(|(node, generation, child)|
                (child.borrow().frame_id == frame_id).then_some((node, generation)));
            found
        }.ok_or("stale_reference: native frame owner is unavailable")?;
        let route = InputRoute {
            frame_id: parent_frame_id, generation: owner.1, node: Some(owner.0.index() as u32),
            x: 0.0, y: 0.0, owners: Vec::new(),
        };
        self.evaluate_input_script(&route,
            "dispatchTrustedEvent(nativeTarget,createNativeEvent('Event','load',{bubbles:false,cancelable:false}))"
        ).map(|_| ())
    }

    pub(crate) fn evaluate_native_script_in_realm(
        &mut self,
        context: &v8::Global<v8::Context>,
        source: &str,
    ) -> Result<serde_json::Value, String> {
        let state = self.realm_states().borrow().owner_for_context(context)
            .ok_or("stale_reference: native event realm is unavailable")?;
        let (frame_id, generation) = {
            let state = state.borrow();
            (state.frame_id, state.document_generation)
        };
        if self.native_cdp_context(Some(frame_id))? != *context {
            return Err("stale_reference: native event context was replaced".into());
        }
        let route = InputRoute { frame_id, generation, node: None, x: 0.0, y: 0.0, owners: Vec::new() };
        self.evaluate_input_script_in_task(&route, source, false)
    }

    fn call_input_member(&mut self, route: &InputRoute, name: &str) -> Result<Option<u32>, String> {
        self.validate_input_route(route)?;
        let context = self.native_cdp_context(Some(route.frame_id))?;
        let mut entered = self.runtime();
        let isolate = entered.v8_isolate();
        v8::scope_with_context!(scope, isolate, context);
        v8::tc_scope!(let scope, scope);
        let target = original_target(scope, route.node)?;
        let (members, function) = member(scope, name)?;
        let result = function.call(scope, members.into(), &[target]).ok_or_else(|| super::exception_text(scope))?;
        let Ok(result) = v8::Local::<v8::Object>::try_from(result) else { return Ok(None); };
        Ok(crate::remote_properties::own_data(scope, result, "_nid")
            .filter(|value| value.is_uint32()).and_then(|value| value.uint32_value(scope)))
    }

    fn cancel_pointer_in(&mut self, frame: (u32, u64)) -> Result<(), String> {
        let route = InputRoute { frame_id: frame.0, generation: frame.1, node: None, x: 0.0, y: 0.0, owners: Vec::new() };
        if self.validate_input_route(&route).is_ok() {
            self.call_input_member(&route, "cancelPointer")?;
        }
        // A retired realm cannot receive another event or retain a live down.
        Ok(())
    }

    #[doc(hidden)]
    pub fn prepare_pointer_input(&mut self, route: &InputRoute, event: &str, _root_x: f64, _root_y: f64) -> Result<(), String> {
        self.validate_input_route(route)?;
        let current = (route.frame_id, route.generation);
        if self.input_hover_frame.is_some_and(|old| old != current) {
            let old = self.input_hover_frame.take().unwrap();
            #[cfg(feature = "render")]
            let point = {
                let registry = self.realm_states();
                let registry = registry.borrow();
                point_in_frame(&self.state, &registry, old.0, _root_x, _root_y, 0)
            };
            #[cfg(feature = "render")]
            if let Some((x, y)) = point {
                let old_route = InputRoute { frame_id: old.0, generation: old.1, node: None, x, y, owners: Vec::new() };
                if self.validate_input_route(&old_route).is_ok() {
                    let code = format!("(function(){{var old=globalThis.__obscura_mouse_over_target||globalThis.__obscura_hover_target;globalThis.__obscura_mouse_over_target=null;globalThis.__obscura_hover_target=null;globalThis.__obscura_setHovered(null);if(!old||!old.isConnected)return;var init={{bubbles:true,cancelable:true,composed:true,view:globalThis,clientX:{x},clientY:{y},pointerId:1,pointerType:'mouse',isPrimary:true,relatedTarget:null}};dispatchTrustedEvent(old, createNativeEvent('PointerEvent', 'pointerout',init));dispatchTrustedEvent(old, createNativeEvent('MouseEvent', 'mouseout',init));for(var node=old;node;node=node.parentNode){{dispatchTrustedEvent(node, createNativeEvent('PointerEvent', 'pointerleave',Object.assign({{}},init,{{bubbles:false,cancelable:false}})));dispatchTrustedEvent(node, createNativeEvent('MouseEvent', 'mouseleave',Object.assign({{}},init,{{bubbles:false,cancelable:false}})));}}}})()");
                    self.evaluate_input_script(&old_route, &code)?;
                }
            }
        }
        self.input_hover_frame = Some(current);
        if event == "mousePressed" {
            self.input_focus_before_press = self.state.borrow().input_focus.get();
            if let Some(old) = self.input_pointer_down.take() { self.cancel_pointer_in(old)?; }
            self.cancel_pointer_in(current)?;
            self.input_pointer_down = Some(current);
        } else if event == "mouseReleased" {
            self.input_focus_before_press = None;
            let down = self.input_pointer_down.take();
            if down != Some(current) {
                if let Some(old) = down { self.cancel_pointer_in(old)?; }
                self.cancel_pointer_in(current)?;
            }
        }
        Ok(())
    }

    #[doc(hidden)]
    pub fn focus_pointer_owners(&mut self, route: &InputRoute) -> Result<(), String> {
        if let Some((frame, node, generation)) = self.input_focus_before_press.take() {
            let ancestor = route.owners.iter().any(|owner|
                owner.frame_id == frame && owner.node == node && owner.generation == generation);
            if frame != route.frame_id && !ancestor {
                let previous = InputRoute { frame_id: frame, generation, node: Some(node), x: 0.0, y: 0.0, owners: Vec::new() };
                if self.validate_input_route(&previous).is_ok()
                    && self.input_state(frame)?.borrow().dom.as_ref().is_some_and(|dom| dom.is_focused(NodeId::new(node)))
                {
                    self.call_input_member(&previous, "blurOwner")?;
                }
            }
        }
        let probe = InputRoute { frame_id: route.frame_id, generation: route.generation, node: None, x: 0.0, y: 0.0, owners: Vec::new() };
        let selected = self.call_input_member(&probe, "focusedNode")?;
        let focus = self.state.borrow().input_focus.get();
        // A blur handler may genuinely redirect focus to another browsing
        // context. Do not replace that focus with the last pointer location.
        if focus.is_some_and(|(frame, node, generation)| frame != route.frame_id
            && !route.owners.iter().any(|owner|
                owner.frame_id == frame && owner.node == node && owner.generation == generation))
        {
            return Ok(());
        }
        let focus = selected.and_then(|node| {
            let state = self.input_state(route.frame_id).ok()?;
            let state = state.borrow();
            state.dom.as_ref().is_some_and(|dom| dom.is_connected(NodeId::new(node)) && dom.is_focused(NodeId::new(node)))
                .then_some((route.frame_id, node, state.document_generation))
        });
        for owner in &route.owners {
            let owner = InputRoute { frame_id: owner.frame_id, generation: owner.generation, node: Some(owner.node), x: 0.0, y: 0.0, owners: Vec::new() };
            self.call_input_member(&owner, "focusOwner")?;
        }
        // An already-focused iframe owner need not invoke its native focus op
        // again. Retain that actual owner when the child's old control blurred,
        // so subsequent keyboard input still targets this child document.
        let focus = focus.or_else(|| route.owners.last()
            .map(|owner| (owner.frame_id, owner.node, owner.generation)));
        if let Some(focus) = focus {
            let state = self.input_state(focus.0)?;
            let state = state.borrow();
            if state.document_generation == focus.2 && state.dom.as_ref().is_some_and(|dom|
                dom.is_connected(NodeId::new(focus.1)) && dom.is_focused(NodeId::new(focus.1)))
            {
                self.state.borrow().input_focus.set(Some(focus));
            }
        }
        Ok(())
    }

    #[doc(hidden)]
    pub fn focused_input_route(&mut self) -> Result<InputRoute, String> {
        let focused = self.state.borrow().input_focus.get();
        let (mut frame, mut node, mut generation) = focused.map_or((0, None, self.state.borrow().document_generation),
            |(frame, node, generation)| (frame, Some(node), generation));
        let limit = self.realm_states().borrow().live_count() + 1;
        for _ in 0..=limit {
            let state = self.input_state(frame)?;
            let child = {
                let state_ref = state.borrow();
                if state_ref.document_generation != generation { return Err("stale_reference: focused document was replaced".into()); }
                if let Some(node) = node {
                    if !state_ref.dom.as_ref().is_some_and(|dom| dom.is_connected(NodeId::new(node)) && dom.is_focused(NodeId::new(node))) {
                        return Err("stale_reference: native focused node is unavailable".into());
                    }
                    self.realm_states().borrow().owned_children(&state)
                        .find(|(owner, owner_generation, _)| owner.index() as u32 == node && *owner_generation == generation)
                        .map(|(_, _, child)| child.clone())
                } else { None }
            };
            if let Some(child) = child {
                let child = child.borrow();
                frame = child.frame_id;
                generation = child.document_generation;
                drop(child);
                let probe = InputRoute { frame_id: frame, generation, node: None, x: 0.0, y: 0.0, owners: Vec::new() };
                node = self.call_input_member(&probe, "focusedNode")?;
                continue;
            }
            if node.is_none() {
                node = state.borrow().dom.as_ref().and_then(|dom| dom.query_selector("body").ok().flatten())
                    .map(|node| node.index() as u32);
            }
            return Ok(InputRoute { frame_id: frame, generation, node, x: 0.0, y: 0.0, owners: Vec::new() });
        }
        Err("Invalid focused native frame owner chain".into())
    }

    #[doc(hidden)]
    pub fn evaluate_focused_input_script(&mut self, code: &str) -> Result<serde_json::Value, String> {
        let route = self.focused_input_route()?;
        self.evaluate_input_script(&route, code)
    }

    #[doc(hidden)]
    pub fn input_uses_meta_shortcuts(&self) -> bool {
        self.state.borrow().environment_identity.as_ref().and_then(|identity| identity.get("platform"))
            .and_then(serde_json::Value::as_str)
            .map(|platform| platform.starts_with("Mac") || platform.eq_ignore_ascii_case("macos") || platform.eq_ignore_ascii_case("darwin"))
            .unwrap_or(cfg!(target_os = "macos"))
    }
}

#[cfg(test)]
mod tests {
    use super::ObscuraJsRuntime;
    use serde_json::json;

    #[test]
    fn raw_runtime_native_callbacks_are_registered_before_document_init_and_never_global() {
        let mut runtime = ObscuraJsRuntime::new();
        assert_eq!(runtime.evaluate_native_script(
            "[typeof nativeDocument.pollIndexedDb,typeof nativeDocument.createEvent,typeof nativeDocument.dispatchTrusted]"
        ).unwrap(), json!(["function","function","function"]));
        runtime.evaluate_native_script("nativeDocument.pollIndexedDb([])").unwrap();
        assert_eq!(runtime.evaluate(
            "[typeof nativeDocument,typeof createNativeEvent,typeof dispatchTrustedEvent,typeof __obscura_markTrusted,typeof __obscura_poll_indexeddb]"
        ).unwrap(), json!(["undefined","undefined","undefined","undefined","undefined"]));
        runtime.evaluate(
            "globalThis.authorProbe=function(){try{return authorProbe.caller===null}catch(error){return true}}"
        ).unwrap();
        assert_eq!(runtime.evaluate_native_script("authorProbe()").unwrap(), json!(true),
            "an author callback cannot recover the strict privileged executor through caller");
    }
}
