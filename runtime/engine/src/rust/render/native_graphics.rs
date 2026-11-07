//! C boundary for the real macOS CGL driver and CPU rasterizer.
use std::{ffi::c_void, panic::{catch_unwind,AssertUnwindSafe}};
use obscura_render::graphics::GlContext;
type Write=unsafe extern "C" fn(*mut c_void,*const u8,usize)->bool;
static GL_BACKEND:std::sync::OnceLock<String>=std::sync::OnceLock::new();
// A transferred placeholder may be painted on the Frame thread while its
// Worker issues GPU commands. Never create aliased &mut GlContext at the ABI.
pub struct Backend { gl: std::sync::Mutex<GlContext> }
unsafe fn with_gl(value:*mut Backend,operation:impl FnOnce(&mut GlContext)->bool)->bool {
    catch_unwind(AssertUnwindSafe(|| {
        let Some(backend)=value.as_ref() else{return false};
        let Ok(mut gl)=backend.gl.lock() else{return false};
        operation(&mut gl)
    })).unwrap_or(false)
}
unsafe fn input<'a>(data:*const u8,len:usize)->Option<&'a [u8]> {
    if len==0 {Some(&[])} else if data.is_null(){None} else {Some(std::slice::from_raw_parts(data,len))}
}
unsafe fn output<'a>(data:*mut u8,len:usize)->Option<&'a mut [u8]> {
    if len==0 {Some(&mut [])} else if data.is_null(){None} else {Some(std::slice::from_raw_parts_mut(data,len))}
}
#[no_mangle]
pub extern "C" fn lp_gl_new(width:u32,height:u32)->*mut Backend {
    catch_unwind(AssertUnwindSafe(|| {
        let gl=GlContext::new_with_backend(width,height,GL_BACKEND.get().map(String::as_str).unwrap_or("cgl-default")).ok()?;
        Some(Box::into_raw(Box::new(Backend{gl:std::sync::Mutex::new(gl)})))
    })).ok().flatten().unwrap_or(std::ptr::null_mut())
}
#[no_mangle]
pub unsafe extern "C" fn lp_gl_free(value:*mut Backend){if !value.is_null(){drop(Box::from_raw(value));}}
#[no_mangle]
pub unsafe extern "C" fn lp_gl_command(value:*mut Backend,json:*const u8,len:usize,context:*mut c_void,write:Write)->bool{
    with_gl(value,|gl|{
        let Some(json)=input(json,len) else{return false};
        let Ok(request)=serde_json::from_slice(json) else{return false};
        let Ok(result)=gl.command(&request) else{return false};
        let Ok(bytes)=serde_json::to_vec(&result) else{return false};
        write(context,bytes.as_ptr(),bytes.len())
    })
}
#[no_mangle]
pub unsafe extern "C" fn lp_gl_readback(value:*mut Backend,pixels:*mut u8,len:usize)->bool{
    with_gl(value,|gl|output(pixels,len).is_some_and(|pixels|gl.readback(pixels).is_ok()))
}

#[no_mangle]
pub unsafe extern "C" fn lp_gl_resize(value:*mut Backend,width:u32,height:u32)->bool {
    with_gl(value,|gl|gl.resize(width,height).is_ok())
}

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct Configuration {files:Vec<std::path::PathBuf>,families:Vec<String>,locale:String,fonts_sha256:String,gl_backend:String}
#[no_mangle]
pub unsafe extern "C" fn lp_graphics_configure(json:*const u8,len:usize)->bool {
    catch_unwind(AssertUnwindSafe(||{
        let Ok(config)=serde_json::from_slice::<Configuration>(std::slice::from_raw_parts(json,len)) else{return false};
        if obscura_render::graphics::configure_fonts(&config.files,&config.families,&config.locale).is_err(){return false;}
        let Ok(hashes)=obscura_render::graphics::font_assets_sha256(&config.files) else{return false};
        let Ok(canonical)=serde_json::to_vec(&hashes) else{return false};
        use sha2::Digest;
        if format!("{:x}",sha2::Sha256::digest(&canonical))!=config.fonts_sha256{return false;}
        if GlContext::new_with_backend(1,1,&config.gl_backend).is_err(){return false;}
        if GL_BACKEND.get().is_some_and(|backend|backend!=&config.gl_backend){return false;}
        let _=GL_BACKEND.set(config.gl_backend);
        true
    })).unwrap_or(false)
}

#[repr(C)]
pub struct TextureParameters {target:u32,level:i32,internal:i32,width:i32,height:i32,border:i32,format:u32,kind:u32}
#[no_mangle]
pub unsafe extern "C" fn lp_gl_tex_image(value:*mut Backend,parameters:*const TextureParameters,data:*const u8,len:usize)->bool{
    with_gl(value,|gl|{
        let Some(p)=parameters.as_ref()else{return false};
        let pixels=if data.is_null(){if len!=0{return false};None}else{let Some(bytes)=input(data,len)else{return false};Some(bytes)};
        gl.upload_texture_2d(p.target,p.level,p.internal,p.width,p.height,p.border,p.format,p.kind,pixels).is_ok()
    })
}
#[no_mangle]
pub unsafe extern "C" fn lp_gl_tex_sub_image(value:*mut Backend,target:u32,level:i32,x:i32,y:i32,width:i32,height:i32,format:u32,kind:u32,data:*const u8,len:usize)->bool{
    with_gl(value,|gl|input(data,len).is_some_and(|pixels|gl.upload_texture_sub_2d(target,level,x,y,width,height,format,kind,pixels).is_ok()))
}
#[no_mangle]
pub unsafe extern "C" fn lp_gl_read_pixels(value:*mut Backend,x:i32,y:i32,width:i32,height:i32,format:u32,kind:u32,data:*mut u8,len:usize)->bool{
    with_gl(value,|gl|output(data,len).is_some_and(|pixels|gl.read_pixels(x,y,width,height,format,kind,pixels).is_ok()))
}

#[no_mangle]
pub unsafe extern "C" fn lp_gl_clear_bitmap(value:*mut Backend)->bool{
    with_gl(value,|gl|gl.clear_bitmap().is_ok())
}
