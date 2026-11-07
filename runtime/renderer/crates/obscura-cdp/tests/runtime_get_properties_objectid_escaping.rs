//! Property names never become executable lookup expressions or handle recipes.
//! Opaque native handles retain the exact original child and DOM wrapper even
//! when page code replaces its own references or installs a decoy registry.

use obscura_cdp::dispatch::{dispatch, CdpContext};
use obscura_cdp::types::CdpRequest;
use serde_json::{json, Value};

async fn cdp(ctx: &mut CdpContext, id: u64, method: &str, params: Value, session_id: &str) -> Value {
    let resp = dispatch(
        &CdpRequest {
            id,
            method: method.to_string(),
            params,
            session_id: Some(session_id.to_string()),
        },
        ctx,
    )
    .await;
    assert!(resp.error.is_none(), "CDP {method} failed: {:?}", resp.error);
    resp.result.unwrap_or_else(|| json!({}))
}

/// The `objectId` of the property descriptor named `name`.
fn child_object_id(props: &Value, name: &str) -> Option<String> {
    props
        .get("result")?
        .as_array()?
        .iter()
        .find(|d| d.get("name").and_then(|v| v.as_str()) == Some(name))
        .and_then(|d| d.pointer("/value/objectId"))
        .and_then(|v| v.as_str())
        .map(|s| s.to_string())
}

/// The string value of the property descriptor named `name`.
fn string_property(props: &Value, name: &str) -> Option<String> {
    props
        .get("result")?
        .as_array()?
        .iter()
        .find(|d| d.get("name").and_then(|v| v.as_str()) == Some(name))
        .and_then(|d| d.pointer("/value/value"))
        .and_then(|v| v.as_str())
        .map(|s| s.to_string())
}

// Include controls and quoting characters in page-owned keys. Native handles
// must not depend on any encoding of these names.
const SETUP: &str = r#"(function () {
    var LF = String.fromCharCode(10);
    var CR = String.fromCharCode(13);
    var NUL = String.fromCharCode(0);
    var BACKSLASH = String.fromCharCode(92);
    var o = {};
    o["lf" + LF + "x"] = { mark: "lf" };
    o["cr" + CR + "x"] = { mark: "cr" };
    o["nul" + NUL + "x"] = { mark: "nul" };
    o["quote'" + BACKSLASH + "x"] = { mark: "quote" };
    globalThis.__t = o;
    return o;
})()"#;

#[tokio::test(flavor = "current_thread")]
async fn get_properties_walks_into_a_child_whose_key_holds_a_control_character() {
    let mut ctx = CdpContext::new();
    let page_id = ctx.create_page();
    let session_id = "session-1";
    ctx.sessions.insert(session_id.to_string(), page_id.clone());

    cdp(
        &mut ctx,
        1,
        "Page.navigate",
        json!({"url": "about:blank", "waitUntil": "load"}),
        session_id,
    )
    .await;

    // returnByValue omitted, so the result is a handle rather than a copy.
    let root = cdp(
        &mut ctx,
        2,
        "Runtime.evaluate",
        json!({"expression": SETUP}),
        session_id,
    )
    .await;
    let root_oid = root
        .pointer("/result/objectId")
        .and_then(|v| v.as_str())
        .expect("Runtime.evaluate must hand back an objectId for an object result")
        .to_string();

    let top = cdp(
        &mut ctx,
        3,
        "Runtime.getProperties",
        json!({"objectId": root_oid}),
        session_id,
    )
    .await;

    // Capture every original child, then replace page references before reading
    // their descriptors. A recipe-based implementation would return replacements.
    let mut children = Vec::new();
    let cases = [
        ("lf\nx", "lf"),
        ("cr\rx", "cr"),
        ("nul\0x", "nul"),
        ("quote'\\x", "quote"),
    ];

    for (key, mark) in cases {
        children.push((key, mark, child_object_id(&top, key).unwrap_or_else(|| {
            panic!("no child objectId for key {key:?}; descriptors were {top:#?}")
        })));
    }
    let registry = cdp(&mut ctx, 4, "Runtime.evaluate", json!({
        "expression":"typeof globalThis.__obscura_objects",
        "returnByValue":true
    }), session_id).await;
    assert_eq!(registry["result"]["value"], json!("undefined"));
    cdp(&mut ctx, 5, "Runtime.evaluate", json!({
        "expression":"for (const key of Object.keys(__t)) __t[key] = {mark:'replacement'}; globalThis.__obscura_objects = new Proxy({}, {get(){throw new Error('decoy registry read')}}); delete globalThis.__t;"
    }), session_id).await;
    for (id, (key, mark, child_oid)) in children.into_iter().enumerate() {
        assert!(!child_oid.contains(key), "handle must not encode a property recipe");
        let child = cdp(&mut ctx, 10 + id as u64, "Runtime.getProperties",
            json!({"objectId":child_oid,"ownProperties":true}), session_id).await;
        assert_eq!(string_property(&child, "mark").as_deref(), Some(mark),
            "the handle must retain the original child for {key:?}");
    }
}

// DOM consumers resolve the same native roots, not a second page-side lookup.
const SETUP_NODES: &str = r#"(function () {
    var LF = String.fromCharCode(10);
    var o = {};
    o["plain"] = document.getElementById("a");
    o["lf" + LF + "x"] = document.getElementById("b");
    globalThis.__n = o;
    return o;
})()"#;

#[tokio::test(flavor = "current_thread")]
async fn describe_node_resolves_a_child_handle_whose_key_holds_a_control_character() {
    let mut ctx = CdpContext::new();
    let page_id = ctx.create_page();
    let session_id = "session-1";
    ctx.sessions.insert(session_id.to_string(), page_id.clone());

    cdp(
        &mut ctx,
        1,
        "Page.navigate",
        json!({"url": "data:text/html,<div id=a></div><p id=b></p>", "waitUntil": "load"}),
        session_id,
    )
    .await;

    let root = cdp(
        &mut ctx,
        2,
        "Runtime.evaluate",
        json!({"expression": SETUP_NODES}),
        session_id,
    )
    .await;
    let root_oid = root
        .pointer("/result/objectId")
        .and_then(|v| v.as_str())
        .expect("Runtime.evaluate must hand back an objectId for an object result")
        .to_string();

    let top = cdp(
        &mut ctx,
        3,
        "Runtime.getProperties",
        json!({"objectId": root_oid}),
        session_id,
    )
    .await;
    cdp(&mut ctx, 4, "Runtime.evaluate", json!({
        "expression":"__n.plain = null; __n['lf\\nx'] = null; delete globalThis.__n; globalThis.__obscura_objects = new Proxy({}, {get(){throw new Error('decoy registry read')}});"
    }), session_id).await;

    for (id, (key, node_name)) in [("plain", "DIV"), ("lf\nx", "P")].iter().enumerate() {
        let child_oid = child_object_id(&top, key).unwrap_or_else(|| {
            panic!("no child objectId for key {key:?}; descriptors were {top:#?}")
        });
        let descriptor = top["result"].as_array().unwrap().iter()
            .find(|descriptor| descriptor["name"].as_str() == Some(*key)).unwrap();
        assert_eq!(descriptor["value"]["subtype"], json!("node"));
        let identity = cdp(&mut ctx, 10 + id as u64, "Runtime.callFunctionOn", json!({
            "objectId":child_oid,
            "functionDeclaration":"function(other) { return this === other && this.nodeName; }",
            "arguments":[{"objectId":child_oid}],
            "returnByValue":true
        }), session_id).await;
        assert_eq!(identity["result"]["value"], json!(node_name));

        let described = cdp(
            &mut ctx,
            20 + id as u64,
            "DOM.describeNode",
            json!({"objectId": child_oid}),
            session_id,
        )
        .await;

        assert_eq!(
            described.pointer("/node/nodeName").and_then(|v| v.as_str()),
            Some(*node_name),
            "the handle keyed {key:?} must describe its own element; got {described:#?}"
        );
    }
}
