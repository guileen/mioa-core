use crate::ops::SharedState;
use crate::runtime::ObscuraJsRuntime;

impl ObscuraJsRuntime {
    /// Seed a new/resumed runtime before author scripts observe the document.
    pub fn initialize_document_active(&mut self, active: bool) {
        let state = self.runtime().op_state();
        state.borrow().borrow::<SharedState>().borrow().document_active.set(active);
    }

    /// Activate the existing runtime without replacing its heap or remote roots.
    pub fn set_document_active(&mut self, active: bool) -> Result<(), String> {
        let previous = {
            let state = self.runtime().op_state();
            let state = state.borrow();
            let state = state.borrow::<SharedState>().borrow();
            state.document_active.replace(active)
        };
        if previous == active { return Ok(()); }
        let budget = std::env::var("OBSCURA_CDP_COMMAND_TIMEOUT_MS")
            .ok().and_then(|value| value.parse::<u64>().ok()).unwrap_or(60_000);
        let watchdog = (budget != 0).then(|| crate::cdp_watchdog::arm(
            self.isolate_handle(), std::time::Duration::from_millis(budget),
        ));
        let source = if active {
            "nativeDocument.activationChanged(true)"
        } else {
            "nativeDocument.activationChanged(false)"
        };
        let result = self.evaluate_native_script(source).map(|_| ());
        if watchdog.is_some_and(crate::cdp_watchdog::disarm) {
            self.cancel_termination();
            return Err("Native target activation event handler exceeded the command deadline".into());
        }
        result
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn language_and_focused_element_are_owned_private_state_not_window_markers() {
        let mut runtime = ObscuraJsRuntime::new();
        runtime.set_locale("zh-CN");
        runtime.set_dom(obscura_dom::parse_html("<input id='original'>"));
        runtime.run_page_init();
        assert_eq!(runtime.evaluate(r#"(() => {
            document.getElementById('original').focus();
            return [Reflect.ownKeys(globalThis).filter(key => typeof key==='string' && ['__obscura_focused','__obscura_language','__obscura_environment'].includes(key)), navigator.language, document.activeElement.id];
        })()"#).unwrap(),serde_json::json!([[],"zh-CN","original"]));
        runtime.execute_script("poison-obsolete-public-storage", r#"
            for (const name of ['__obscura_focused','__obscura_language','__obscura_environment']) {
                Object.defineProperty(globalThis,name,{get(){throw Error('obsolete public storage was read')},configurable:true});
            }
        "#).unwrap();
        runtime.set_locale("fr-CA");
        assert_eq!(runtime.evaluate("[navigator.language,navigator.languages,document.activeElement.id]").unwrap(),serde_json::json!(["fr-CA",["fr-CA","fr"],"original"]));
        runtime.execute_script("release-caller-created-markers", "delete globalThis.__obscura_focused;delete globalThis.__obscura_language;delete globalThis.__obscura_environment;").unwrap();
    }

    #[tokio::test(flavor = "current_thread")]
    async fn activation_changes_native_focus_visibility_and_preserves_original_handles() {
        let mut runtime = ObscuraJsRuntime::new();
        runtime.set_dom(obscura_dom::parse_html("<input id='original'>"));
        runtime.run_page_init();
        runtime.execute_script("activation-observers", "globalThis.activationEvents=[]; addEventListener('focus',()=>activationEvents.push('focus')); addEventListener('blur',()=>activationEvents.push('blur')); document.addEventListener('visibilitychange',()=>activationEvents.push(document.visibilityState));").unwrap();
        let handle = runtime.store_object_with_meta("({document,node:document.getElementById('original')})").unwrap().object_id.unwrap();
        runtime.set_document_active(false).unwrap();
        assert_eq!(runtime.evaluate("[document.hasFocus(),document.hidden,document.visibilityState]").unwrap(), serde_json::json!([false,true,"hidden"]));
        runtime.set_document_active(true).unwrap();
        let identity = runtime.call_function_on_for_cdp("function(){return this.document===document && this.node===document.getElementById('original');}",Some(&handle),&[],true,false).await.unwrap();
        assert_eq!(identity.value,Some(serde_json::json!(true)));
        assert_eq!(runtime.evaluate("[document.hasFocus(),document.hidden,document.visibilityState,activationEvents]").unwrap(),serde_json::json!([true,false,"visible",["blur","hidden","visible","focus"]]));
    }
}
