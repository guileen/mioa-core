use deno_core::{v8, OpState};
use serde_json::{json, Value};

// Only inspect descriptor records supplied by V8, never invoke a page getter.
fn field<'s>(scope: &mut v8::PinScope<'s, '_>, object: v8::Local<'s, v8::Object>, name: &str) -> Result<v8::Local<'s, v8::Value>, String> {
    let key = v8::String::new(scope, name).ok_or("Property name allocation failed")?;
    object.get(scope, key.into()).ok_or_else(|| "Property descriptor inspection failed".into())
}

pub(crate) fn own_data<'s>(scope: &mut v8::PinScope<'s, '_>, object: v8::Local<'s, v8::Object>, name: &str) -> Option<v8::Local<'s, v8::Value>> {
    if object.is_proxy() { return None; }
    let key = v8::String::new(scope, name)?;
    let descriptor = object.get_own_property_descriptor(scope, key.into())?;
    let descriptor = v8::Local::<v8::Object>::try_from(descriptor).ok()?;
    field(scope, descriptor, "value").ok()
}

pub(crate) fn describe_remote_value<'s>(scope: &mut v8::PinScope<'s, '_>, state: &mut OpState, value: v8::Local<'s, v8::Value>) -> Result<Value, String> {
    if value.is_undefined() { return Ok(json!({"type":"undefined"})); }
    if value.is_null() { return Ok(json!({"type":"object","subtype":"null","value":null})); }
    if value.is_boolean() { return Ok(json!({"type":"boolean","value":value.boolean_value(scope)})); }
    if value.is_string() { return Ok(json!({"type":"string","value":value.to_rust_string_lossy(scope)})); }
    if value.is_number() {
        let number = value.number_value(scope).ok_or("Number inspection failed")?;
        let special = if number.is_nan() { Some("NaN") } else if number == f64::INFINITY { Some("Infinity") } else if number == f64::NEG_INFINITY { Some("-Infinity") } else if number == 0.0 && number.is_sign_negative() { Some("-0") } else { None };
        return Ok(match special { Some(text) => json!({"type":"number","unserializableValue":text,"description":text}), None => json!({"type":"number","value":number,"description":number.to_string()}) });
    }
    if value.is_big_int() {
        let text = value.to_string(scope).ok_or("BigInt inspection failed")?.to_rust_string_lossy(scope) + "n";
        return Ok(json!({"type":"bigint","unserializableValue":text,"description":text}));
    }
    let object_id = crate::remote_objects::retain(scope, state, value)?;
    if value.is_symbol() {
        let symbol = v8::Local::<v8::Symbol>::try_from(value).map_err(|_| "Symbol inspection failed")?;
        let description = symbol.description(scope);
        let description = if description.is_undefined() { "Symbol()".to_owned() } else { format!("Symbol({})", description.to_rust_string_lossy(scope)) };
        return Ok(json!({"type":"symbol","objectId":object_id,"description":description}));
    }
    let object = v8::Local::<v8::Object>::try_from(value).map_err(|_| "Unsupported remote value")?;
    let class_name = object.get_constructor_name().to_rust_string_lossy(scope);
    let mut remote = json!({"type":if value.is_function() { "function" } else { "object" },"objectId":object_id,"className":class_name,"description":class_name});
    if value.is_array() {
        let array = v8::Local::<v8::Array>::try_from(value).map_err(|_| "Array inspection failed")?;
        remote["subtype"] = json!("array");
        remote["description"] = json!(format!("Array({})", array.length()));
    } else if own_data(scope, object, "_nid").is_some_and(|id| id.is_number()) {
        remote["subtype"] = json!("node");
        if class_name == "Document" {
            remote["className"] = json!("HTMLDocument");
            remote["description"] = json!("#document");
        }
    } else {
        let subtype = if value.is_date() { Some("date") } else if value.is_reg_exp() { Some("regexp") } else if value.is_map() { Some("map") } else if value.is_set() { Some("set") } else if value.is_native_error() { Some("error") } else if value.is_promise() { Some("promise") } else if value.is_typed_array() { Some("typedarray") } else if value.is_array_buffer() { Some("arraybuffer") } else if value.is_data_view() { Some("dataview") } else if value.is_proxy() { Some("proxy") } else { None };
        if let Some(subtype) = subtype { remote["subtype"] = json!(subtype); }
        if value.is_native_error() {
            if let Some(message) = own_data(scope, object, "message").filter(|value| value.is_string()) {
                remote["description"] = json!(format!("{class_name}: {}", message.to_rust_string_lossy(scope)));
            }
        }
    }
    Ok(remote)
}

impl crate::runtime::ObscuraJsRuntime {
    pub fn get_properties_for_cdp(&mut self, object_id: &str, own_properties: bool, accessor_only: bool) -> Result<Value, String> {
        let mut runtime = self.runtime();
        let state = runtime.op_state();
        let root = crate::remote_objects::resolve(&mut state.borrow_mut(), object_id)?;
        v8::scope_with_context!(scope, runtime.v8_isolate(), root.context);
        v8::tc_scope!(let scope, scope);
        let value = v8::Local::new(scope, root.value);
        let mut object = v8::Local::<v8::Object>::try_from(value).map_err(|_| "Remote object has no properties")?;
        let mut descriptors = Vec::new();
        let mut seen = Vec::<v8::Local<v8::Name>>::new();
        let mut own = true;
        loop {
            let args = v8::GetPropertyNamesArgs {
                mode: v8::KeyCollectionMode::OwnOnly,
                property_filter: v8::PropertyFilter::ALL_PROPERTIES,
                index_filter: v8::IndexFilter::IncludeIndices,
                key_conversion: v8::KeyConversionMode::ConvertToString,
            };
            let keys = object.get_own_property_names(scope, args).ok_or("Property enumeration failed")?;
            for index in 0..keys.length() {
                let key = keys.get_index(scope, index).ok_or("Property enumeration failed")?;
                let key = v8::Local::<v8::Name>::try_from(key).map_err(|_| "Invalid property key")?;
                if seen.iter().any(|seen| seen.strict_equals(key.into())) { continue; }
                seen.push(key);
                let descriptor = object.get_own_property_descriptor(scope, key).ok_or("Property descriptor inspection failed")?;
                if descriptor.is_undefined() { continue; }
                let descriptor = v8::Local::<v8::Object>::try_from(descriptor).map_err(|_| "Invalid property descriptor")?;
                let getter = field(scope, descriptor, "get")?;
                let setter = field(scope, descriptor, "set")?;
                let value_key = v8::String::new(scope, "value").ok_or("Property name allocation failed")?;
                let data = descriptor.has_own_property(scope, value_key.into()).ok_or("Property descriptor inspection failed")?;
                if accessor_only && data { continue; }
                let name = if key.is_symbol() {
                    let symbol = v8::Local::<v8::Symbol>::try_from(key).map_err(|_| "Invalid symbol key")?;
                    let text = symbol.description(scope);
                    if text.is_undefined() { "Symbol()".to_owned() } else { format!("Symbol({})", text.to_rust_string_lossy(scope)) }
                } else { key.to_rust_string_lossy(scope) };
                let configurable = field(scope, descriptor, "configurable")?.boolean_value(scope);
                let enumerable = field(scope, descriptor, "enumerable")?.boolean_value(scope);
                let mut result = json!({"name":name,"configurable":configurable,"enumerable":enumerable,"isOwn":own});
                if data {
                    let value = field(scope, descriptor, "value")?;
                    result["value"] = describe_remote_value(scope, &mut state.borrow_mut(), value)?;
                    result["writable"] = json!(field(scope, descriptor, "writable")?.boolean_value(scope));
                } else {
                    // Accessors with both hooks undefined are still accessor descriptors.
                    result["get"] = describe_remote_value(scope, &mut state.borrow_mut(), getter)?;
                    result["set"] = describe_remote_value(scope, &mut state.borrow_mut(), setter)?;
                }
                if key.is_symbol() { result["symbol"] = describe_remote_value(scope, &mut state.borrow_mut(), key.into())?; }
                descriptors.push(result);
            }
            if own_properties { break; }
            let prototype = object.get_prototype(scope).ok_or("Prototype inspection failed")?;
            if prototype.is_null() { break; }
            object = v8::Local::<v8::Object>::try_from(prototype).map_err(|_| "Invalid prototype")?;
            own = false;
        }
        Ok(json!({"result":descriptors,"internalProperties":[]}))
    }
}
