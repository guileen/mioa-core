use std::cell::RefCell;
use std::collections::HashMap;
use std::rc::{Rc, Weak};
use std::sync::atomic::{AtomicU64, Ordering};

use deno_core::{OpState, v8};

use crate::ops::{ObscuraState, RealmStates};

static NEXT_OBJECT_ID: AtomicU64 = AtomicU64::new(1);
static PROCESS_NONCE: std::sync::LazyLock<Result<String, String>> = std::sync::LazyLock::new(|| {
    let mut bytes = [0u8; 16];
    getrandom::getrandom(&mut bytes).map_err(|error| format!("Cannot allocate opaque object namespace: {error}"))?;
    Ok(format!("{:032x}", u128::from_be_bytes(bytes)))
});
const UNAVAILABLE: &str = "stale_reference: remote object is unavailable or its realm was replaced";

#[derive(Clone)]
pub(crate) struct RemoteRoot {
    pub context: v8::Global<v8::Context>,
    pub value: v8::Global<v8::Value>,
}

struct RetainedObject {
    root: RemoteRoot,
    owner: Weak<RefCell<ObscuraState>>,
    document_generation: u64,
    frame_id: u32,
    internal: bool,
}

#[derive(Default)]
struct RemoteObjects {
    objects: HashMap<String, RetainedObject>,
}

// This type never reaches JavaScript. Keeping the store in OpState also gives
// fresh worker isolates the same ownership rules without snapshot state.
fn store(state: &mut OpState) -> Rc<RefCell<RemoteObjects>> {
    if let Some(store) = state.try_borrow::<Rc<RefCell<RemoteObjects>>>() {
        return store.clone();
    }
    let store = Rc::new(RefCell::new(RemoteObjects::default()));
    state.put(store.clone());
    store
}

pub(crate) fn retain<'s>(
    scope: &mut v8::PinScope<'s, '_>,
    state: &mut OpState,
    value: v8::Local<'s, v8::Value>,
) -> Result<String, String> {
    // Cross-realm values belong to their creation context, not to the context
    // evaluating the expression or to the realm that owns the native op.
    let context = v8::Local::<v8::Object>::try_from(value).ok()
        .and_then(|object| object.get_creation_context(scope))
        .unwrap_or_else(|| scope.get_current_context());
    let context = v8::Global::new(scope, context);
    let registry = state.try_borrow::<Rc<RefCell<RealmStates>>>()
        .ok_or_else(|| UNAVAILABLE.to_string())?;
    let registry = registry.try_borrow().map_err(|_| "Realm registry is busy".to_string())?;
    let owner = registry.owner_for_context(&context).ok_or_else(|| UNAVAILABLE.to_string())?;
    let owner_state = owner.try_borrow().map_err(|_| "Remote object owner is busy".to_string())?;
    let frame_id = owner_state.frame_id;
    let document_generation = owner_state.document_generation;
    drop(owner_state);
    if frame_id != 0 && registry.frame_timers_active(frame_id) != Some(true) {
        return Err(UNAVAILABLE.to_string());
    }
    drop(registry);
    discard_stale(state);

    let id = NEXT_OBJECT_ID.try_update(Ordering::Relaxed, Ordering::Relaxed, |id| id.checked_add(1))
        .map_err(|_| "Remote object identifiers exhausted".to_string())?;
    let namespace = PROCESS_NONCE.as_ref().map_err(Clone::clone)?;
    let id = format!("remote-{namespace}-{id:016x}");
    let retained = RetainedObject {
        root: RemoteRoot { context, value: v8::Global::new(scope, value) },
        owner: Rc::downgrade(&owner),
        document_generation,
        frame_id,
        internal: false,
    };
    store(state).borrow_mut().objects.insert(id.clone(), retained);
    Ok(id)
}

/// Pending evaluations retain a private realm-generation ticket. Releasing a
/// client's object group must not invalidate an unrelated in-flight command.
pub(crate) fn retain_realm<'s>(
    scope: &mut v8::PinScope<'s, '_>,
    state: &mut OpState,
    value: v8::Local<'s, v8::Value>,
) -> Result<String, String> {
    let id = retain(scope, state, value)?;
    if let Some(object) = store(state).borrow_mut().objects.get_mut(&id) {
        object.internal = true;
    }
    Ok(id)
}

fn is_live(state: &OpState, object: &RetainedObject) -> Result<bool, String> {
    let Some(owner) = object.owner.upgrade() else { return Ok(false) };
    let owner_state = owner.try_borrow().map_err(|_| "Remote object owner is busy".to_string())?;
    if owner_state.document_generation != object.document_generation || owner_state.frame_id != object.frame_id {
        return Ok(false);
    }
    drop(owner_state);
    let Some(registry) = state.try_borrow::<Rc<RefCell<RealmStates>>>() else { return Ok(false) };
    let registry = registry.try_borrow().map_err(|_| "Realm registry is busy".to_string())?;
    let Some(registered) = registry.owner_for_context(&object.root.context) else { return Ok(false) };
    if !Rc::ptr_eq(&owner, &registered) {
        return Ok(false);
    }
    match object.frame_id {
        0 => Ok(true),
        frame_id => registry.frame_timers_active(frame_id)
            .ok_or_else(|| "Remote object frame owner is busy".to_string()),
    }
}

fn discard_stale(state: &mut OpState) {
    let store = store(state);
    store.borrow_mut().objects.retain(|_, object| !matches!(is_live(state, object), Ok(false)));
}

pub(crate) fn resolve(state: &mut OpState, id: &str) -> Result<RemoteRoot, String> {
    let store = store(state);
    let mut store = store.borrow_mut();
    let object = store.objects.get(id).ok_or_else(|| UNAVAILABLE.to_string())?;
    if is_live(state, object)? {
        return Ok(object.root.clone());
    }
    store.objects.remove(id);
    Err(UNAVAILABLE.to_string())
}

pub(crate) fn release(state: &mut OpState, id: &str) {
    if let Some(store) = state.try_borrow::<Rc<RefCell<RemoteObjects>>>() {
        store.borrow_mut().objects.remove(id);
    }
}

pub(crate) fn release_all(state: &mut OpState) {
    if let Some(store) = state.try_borrow::<Rc<RefCell<RemoteObjects>>>() {
        store.borrow_mut().objects.retain(|_, object| object.internal);
    }
}

#[cfg(test)]
pub(crate) fn retained_count(state: &OpState) -> usize {
    state.try_borrow::<Rc<RefCell<RemoteObjects>>>()
        .map_or(0, |store| store.borrow().objects.values().filter(|root| !root.internal).count())
}
