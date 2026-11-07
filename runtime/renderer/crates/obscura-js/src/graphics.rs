//! Per-isolate reusable graphics resources; OpState owns their native lifetime.
use std::collections::HashMap;
use deno_core::{op2, OpState};
use obscura_render::graphics::{CanvasTextEngine, GlContext, TextRequest};
use serde::Deserialize;
use serde_json::{json, Value};
#[derive(Default)]
struct GraphicsState { text: Option<CanvasTextEngine>, gl: HashMap<(u32,u32),GlContext> }
fn resources(state: &mut OpState) -> &mut GraphicsState {
    if state.try_borrow::<GraphicsState>().is_none() { state.put(GraphicsState::default()); }
    state.borrow_mut::<GraphicsState>()
}
#[derive(Deserialize)]
struct TextInput { width:u32, height:u32, measure:bool, #[serde(flatten)] text:TextRequest }
#[op2]
#[serde]
pub fn op_graphics_text(state:&mut OpState, #[string] request:&str, #[buffer] pixels:&mut[u8])->serde_json::Value {
    let result=std::panic::catch_unwind(std::panic::AssertUnwindSafe(||->Result<Value,String>{
        let request:TextInput=serde_json::from_str(request).map_err(|e|e.to_string())?;
        let resources=resources(state);
        if resources.text.is_none() {
            let files=obscura_render::graphics::explicit_font_files()?.ok_or("Explicit asset_paths.fonts not provisioned")?;
            resources.text=Some(CanvasTextEngine::from_font_files(&files)?);
        }
        let engine=resources.text.as_mut().ok_or("Text engine unavailable")?;
        let metrics=if request.measure {engine.measure(&request.text)?}else{engine.draw(&request.text,request.width,request.height,pixels)?};
        serde_json::to_value(metrics).map_err(|e|e.to_string())
    })).unwrap_or_else(|_| Err("Native Canvas text backend panicked".into()));
    match result {Ok(value)=>json!({"ok":true,"value":value}),Err(error)=>json!({"ok":false,"error":error})}
}
#[op2]
#[serde]
pub fn op_graphics_gl(state:&mut OpState, #[string] request:&str, #[buffer] pixels:&mut[u8])->serde_json::Value {
    let result=std::panic::catch_unwind(std::panic::AssertUnwindSafe(||->Result<Value,String>{
        let request:Value=serde_json::from_str(request).map_err(|e|e.to_string())?;
        let node=request["nid"].as_u64().and_then(|n|u32::try_from(n).ok()).ok_or("Missing canvas id")?;
        let frame=request["frame_id"].as_u64().and_then(|n|u32::try_from(n).ok()).ok_or("Missing canvas realm")?;
        let nid=(frame,node);
        let resources=resources(state);
        match request["method"].as_str().ok_or("Missing GL method")? {
            "create"=>{let w=request["width"].as_u64().ok_or("Missing width")? as u32;let h=request["height"].as_u64().ok_or("Missing height")? as u32;resources.gl.insert(nid,GlContext::new(w,h)?);Ok(Value::Null)},
            "resize"=>{
                let width=request["width"].as_u64().and_then(|n|u32::try_from(n).ok()).ok_or("Invalid GL width")?;
                let height=request["height"].as_u64().and_then(|n|u32::try_from(n).ok()).ok_or("Invalid GL height")?;
                resources.gl.get_mut(&nid).ok_or("GL context unavailable")?.resize(width,height)?;
                Ok(Value::Null)
            },
            "destroy"=>{resources.gl.remove(&nid);Ok(Value::Null)},
            "snapshot"=>{resources.gl.get_mut(&nid).ok_or("GL context unavailable")?.readback(pixels)?;Ok(Value::Null)},
            "uploadTexture2D"=>{
                let a=gl_arguments::<8>(&request)?;
                resources.gl.get_mut(&nid).ok_or("GL context unavailable")?.upload_texture_2d(a[0] as u32,a[1],a[2],a[3],a[4],a[5],a[6] as u32,a[7] as u32,if request["nullPixels"].as_bool().unwrap_or(false){None}else{Some(pixels)})?;
                Ok(Value::Null)
            },
            "uploadTextureSub2D"=>{
                let a=gl_arguments::<8>(&request)?;
                resources.gl.get_mut(&nid).ok_or("GL context unavailable")?.upload_texture_sub_2d(a[0] as u32,a[1],a[2],a[3],a[4],a[5],a[6] as u32,a[7] as u32,pixels)?;
                Ok(Value::Null)
            },
            "readPixelsInto"=>{
                let a=gl_arguments::<6>(&request)?;
                resources.gl.get_mut(&nid).ok_or("GL context unavailable")?.read_pixels(a[0],a[1],a[2],a[3],a[4] as u32,a[5] as u32,pixels)?;
                Ok(Value::Null)
            },
            _=>resources.gl.get_mut(&nid).ok_or("GL context unavailable")?.command(&request),
        }
    })).unwrap_or_else(|_| Err("Native GL backend panicked".into()));
    match result {Ok(value)=>json!({"ok":true,"value":value}),Err(error)=>json!({"ok":false,"error":error})}
}
fn gl_arguments<const N:usize>(request:&Value)->Result<[i32;N],String>{
    let args=request["args"].as_array().ok_or("Missing GL arguments")?;
    if args.len()!=N{return Err("Invalid GL argument count".into());}
    let mut result=[0;N];
    for (i,value) in args.iter().enumerate(){result[i]=value.as_i64().and_then(|n|i32::try_from(n).ok()).ok_or("GL arguments must be signed 32-bit integers")?;}
    Ok(result)
}

#[op2]
#[serde]
pub fn op_graphics_canvas(#[string] request:&str, #[buffer] pixels:&mut[u8])->serde_json::Value {
    let result=std::panic::catch_unwind(std::panic::AssertUnwindSafe(||->Result<(),String>{
        let request:obscura_render::graphics::canvas::CanvasDrawRequest=serde_json::from_str(request).map_err(|e|e.to_string())?;
        obscura_render::graphics::canvas::draw(&request,pixels)
    })).unwrap_or_else(|_|Err("Native Canvas geometry backend panicked".into()));
    match result {Ok(())=>json!({"ok":true}),Err(error)=>json!({"ok":false,"error":error})}
}

#[op2]
#[serde]
pub fn op_graphics_fonts()->serde_json::Value {
    let result=std::panic::catch_unwind(||->Result<Value,String>{
        let Some(files)=obscura_render::graphics::explicit_font_files()? else{return Ok(Value::Null);};
        Ok(json!(obscura_render::graphics::font_families(&files)?))
    }).unwrap_or_else(|_|Err("Native font inventory panicked".into()));
    match result{Ok(value)=>json!({"ok":true,"value":value}),Err(error)=>json!({"ok":false,"error":error})}
}

#[op2]
#[serde]
pub fn op_graphics_color(#[string] css:&str)->serde_json::Value {
    std::panic::catch_unwind(||serde_json::to_value(obscura_render::graphics::parse_color(css)).unwrap_or(Value::Null)).unwrap_or(Value::Null)
}

#[op2]
#[serde]
pub fn op_graphics_image(#[string] request:&str, #[buffer] source:&[u8], #[buffer] pixels:&mut[u8])->serde_json::Value {
    let result=std::panic::catch_unwind(std::panic::AssertUnwindSafe(||->Result<(),String>{
        let request:obscura_render::graphics::image::ImageDrawRequest=serde_json::from_str(request).map_err(|e|e.to_string())?;
        obscura_render::graphics::image::draw(&request,source,pixels)
    })).unwrap_or_else(|_|Err("Native Canvas image backend panicked".into()));
    match result {Ok(())=>json!({"ok":true}),Err(error)=>json!({"ok":false,"error":error})}
}

/// Called by environment startup before READY; every capability is derived
/// from a real initialized backend, not the configured browser identity.
pub fn preflight() -> Result<Value,String> {
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let files=obscura_render::graphics::explicit_font_files()?.ok_or("Explicit asset_paths.fonts not provisioned")?;
        let mut text=CanvasTextEngine::from_font_files(&files)?;
        let mut pixels=[0;64];
        text.draw(&TextRequest {text:"Canvas".into(),font_size:3.,y:3.,..Default::default()},4,4,&mut pixels)?;
        let families=text.font_families();
        let mut gl=GlContext::new(4,4)?;
        for (kind,source) in [
            (0x8B31,"attribute vec4 position; void main(){gl_Position=position;}"),
            (0x8B30,"precision mediump float; void main(){gl_FragColor=vec4(1.0);}"),
        ] {
            let shader=gl.command(&json!({"method":"createShader","args":[kind]}))?;
            gl.command(&json!({"method":"shaderSource","args":[shader,source]}))?;
            gl.command(&json!({"method":"compileShader","args":[shader]}))?;
            if gl.command(&json!({"method":"getShaderParameter","args":[shader,0x8B81]}))? != Value::Bool(true) {
                let log=gl.command(&json!({"method":"getShaderInfoLog","args":[shader]}))?;
                return Err(format!("Native GL compiler unavailable: {log}"));
            }
        }
        let mut query=|parameter|gl.command(&json!({"method":"getParameter","args":[parameter]}));
        let vendor=query(0x1F00)?;
        let renderer=query(0x1F01)?;
        let version=query(0x1F02)?;
        let shading_language_version=query(0x8B8C)?;
        let max_texture_size=query(0x0D33)?;
        let fonts_sha256=obscura_render::graphics::font_assets_sha256(&files)?;
        let selected_backend=gl.backend();
        Ok(json!({
            "fonts_sha256":fonts_sha256,
            "canvas":{"backend":"cosmic-text/Swash/tiny-skia","font_files":files,"font_families":families,"font_discovery":false,
                "geometry":["paths","bezier","quadratic","arcs","affine transforms","clip intersections","linear/radial gradients","dashed strokes"],
                "unsupported":["Gradient text paints","Image patterns","Conic gradients","Canvas shadows/filters","Path2D hit testing","DOM image decoding into Canvas","Full CSS font shorthand and fallback-stack parsing"]},
            "gl":{"backend":"macOS CGL/OpenGL","selection":selected_backend,"vendor":vendor,"renderer":renderer,"version":version,
                "shading_language_version":shading_language_version,"max_texture_size":max_texture_size,
                "webgl2":false,"shader_language":"GLSL ES 1.00 syntax translated to desktop GLSL 1.20",
                "unsupported":["WebGL2","DOM image texture-upload overloads","WebGL extension APIs beyond debug_renderer_info","Full WebGL conformance validation"]},
        }))
    })).unwrap_or_else(|_|Err("Native graphics preflight panicked".into()))
}
