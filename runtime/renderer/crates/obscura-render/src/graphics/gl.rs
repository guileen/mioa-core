//! macOS CGL offscreen OpenGL 2.1 implementation. No vendor strings or shader results are fabricated.
use glow::HasContext;
use serde_json::{json, Value};
use std::{collections::HashMap, ffi::{c_char,c_void,CString}, ptr};
type Cgl = *mut c_void;
#[link(name="OpenGL",kind="framework")]
unsafe extern "C" {
    fn CGLChoosePixelFormat(attrs:*const i32, format:*mut Cgl, count:*mut i32)->i32;
    fn CGLCreateContext(format:Cgl,share:Cgl,context:*mut Cgl)->i32;
    fn CGLDestroyPixelFormat(format:Cgl)->i32;
    fn CGLDestroyContext(context:Cgl)->i32;
    fn CGLSetCurrentContext(context:Cgl)->i32;
    fn CGLGetCurrentContext()->Cgl;
    fn glGetShaderiv(shader:u32, pname:u32, value:*mut i32);
    fn glGetDoublev(pname:u32,value:*mut f64);
}
unsafe extern "C" { fn dlopen(path:*const c_char, flags:i32)->*mut c_void; fn dlsym(handle:*mut c_void,name:*const c_char)->*mut c_void; fn dlclose(handle:*mut c_void)->i32; }
type ShaderPrecisionQuery=unsafe extern "C" fn(u32,u32,*mut i32,*mut i32);
pub struct GlContext {
    context:Cgl, library:*mut c_void, gl:std::mem::ManuallyDrop<glow::Context>, width:u32,height:u32,
    backend:&'static str,
    fbo:glow::NativeFramebuffer, color:glow::NativeTexture, depth:glow::NativeRenderbuffer,
    shaders:HashMap<u32,glow::NativeShader>, programs:HashMap<u32,glow::NativeProgram>,
    buffers:HashMap<u32,glow::NativeBuffer>, textures:HashMap<u32,glow::NativeTexture>,
    framebuffers:HashMap<u32,glow::NativeFramebuffer>, renderbuffers:HashMap<u32,glow::NativeRenderbuffer>,
    uniforms:HashMap<u32,glow::NativeUniformLocation>, next_uniform:u32,
    precision_query:Option<ShaderPrecisionQuery>,
    errors:std::collections::VecDeque<u32>,
}
struct Current(Cgl);
impl Drop for Current { fn drop(&mut self) { unsafe { CGLSetCurrentContext(self.0); } } }
struct Allocation {context:Cgl,library:*mut c_void,armed:bool}
impl Drop for Allocation {fn drop(&mut self){if self.armed{unsafe{if CGLGetCurrentContext()==self.context{CGLSetCurrentContext(ptr::null_mut());}CGLDestroyContext(self.context);dlclose(self.library);}}}}
impl GlContext {
    pub fn new(width:u32,height:u32)->Result<Self,String>{Self::new_with_backend(width,height,super::configured_gl_backend()?)}
    pub fn backend(&self)->&'static str{self.backend}
    pub fn new_with_backend(width:u32,height:u32,backend:&str)->Result<Self,String> { unsafe {
        let backend=super::validate_gl_backend(backend)?;
        if width>16384 || height>16384 { return Err("Invalid CGL surface dimensions".into()); }
        // All modes use the legacy GLSL 1.20 profile. Renderer selection is
        // a real CGL pixel-format constraint, never a replacement vendor label.
        let attrs:&[i32]=match backend{
            "cgl-accelerated"=>&[73,8,24,11,8,12,24,13,8,0],
            "cgl-generic-float"=>&[1,70,0x20400,8,24,11,8,12,24,13,8,0],
            _=>&[8,24,11,8,12,24,13,8,0],
        };
        let mut format=ptr::null_mut(); let mut count=0;
        let error=CGLChoosePixelFormat(attrs.as_ptr(),&mut format,&mut count);
        if error!=0 || format.is_null() { return Err(format!("CGLChoosePixelFormat: {error}")); }
        let mut context=ptr::null_mut();
        let error=CGLCreateContext(format,ptr::null_mut(),&mut context);
        CGLDestroyPixelFormat(format);
        if error!=0 || context.is_null() { return Err(format!("CGLCreateContext: {error}")); }
        let previous=Current(CGLGetCurrentContext());
        if CGLSetCurrentContext(context)!=0 { CGLDestroyContext(context); return Err("CGLSetCurrentContext failed".into()); }
        let library=dlopen(c"/System/Library/Frameworks/OpenGL.framework/OpenGL".as_ptr(),1);
        if library.is_null() { CGLDestroyContext(context); return Err("OpenGL framework unavailable".into()); }
        let mut allocation=Allocation {context,library,armed:true};
        let precision_address=dlsym(library,c"glGetShaderPrecisionFormat".as_ptr());
        let precision_query=if precision_address.is_null(){None}else{Some(std::mem::transmute::<*mut c_void,ShaderPrecisionQuery>(precision_address))};
        let gl=glow::Context::from_loader_function(|name| {
            let name=CString::new(name).unwrap();
            let p=dlsym(library,name.as_ptr());
            if !p.is_null() { return p.cast_const(); }
            // GL 2.1 exposes framebuffer entrypoints through EXT aliases.
            let name=CString::new(format!("{}EXT",name.to_string_lossy())).unwrap();
            dlsym(library,name.as_ptr()).cast_const()
        });
        let fbo=gl.create_framebuffer()?; let color=gl.create_texture()?; let depth=gl.create_renderbuffer()?;
        gl.bind_framebuffer(glow::FRAMEBUFFER,Some(fbo));
        gl.bind_texture(glow::TEXTURE_2D,Some(color));
        gl.tex_image_2d(glow::TEXTURE_2D,0,glow::RGBA8 as i32,width.max(1) as i32,height.max(1) as i32,0,glow::RGBA,glow::UNSIGNED_BYTE,glow::PixelUnpackData::Slice(None));
        gl.tex_parameter_i32(glow::TEXTURE_2D,glow::TEXTURE_MIN_FILTER,glow::NEAREST as i32);
        gl.tex_parameter_i32(glow::TEXTURE_2D,glow::TEXTURE_MAG_FILTER,glow::NEAREST as i32);
        gl.framebuffer_texture_2d(glow::FRAMEBUFFER,glow::COLOR_ATTACHMENT0,glow::TEXTURE_2D,Some(color),0);
        gl.bind_renderbuffer(glow::RENDERBUFFER,Some(depth));
        gl.renderbuffer_storage(glow::RENDERBUFFER,glow::DEPTH24_STENCIL8,width.max(1) as i32,height.max(1) as i32);
        gl.framebuffer_renderbuffer(glow::FRAMEBUFFER,glow::DEPTH_ATTACHMENT,glow::RENDERBUFFER,Some(depth));
        gl.framebuffer_renderbuffer(glow::FRAMEBUFFER,glow::STENCIL_ATTACHMENT,glow::RENDERBUFFER,Some(depth));
        if gl.check_framebuffer_status(glow::FRAMEBUFFER)!=glow::FRAMEBUFFER_COMPLETE { gl.delete_texture(color); gl.delete_renderbuffer(depth); gl.delete_framebuffer(fbo); return Err("CGL offscreen framebuffer incomplete".into()); }
        gl.viewport(0,0,width as i32,height as i32); gl.clear_color(0.,0.,0.,0.); gl.clear(glow::COLOR_BUFFER_BIT|glow::DEPTH_BUFFER_BIT|glow::STENCIL_BUFFER_BIT);
        gl.bind_texture(glow::TEXTURE_2D,None); gl.bind_renderbuffer(glow::RENDERBUFFER,None);
        drop(previous);
        allocation.armed=false;
        Ok(Self {context,library,gl:std::mem::ManuallyDrop::new(gl),width,height,backend,fbo,color,depth,precision_query,errors:std::collections::VecDeque::new(),shaders:HashMap::new(),programs:HashMap::new(),buffers:HashMap::new(),textures:HashMap::new(),framebuffers:HashMap::new(),renderbuffers:HashMap::new(),uniforms:HashMap::new(),next_uniform:1})
    } }
    fn current(&self)->Result<Current,String> { unsafe { let old=CGLGetCurrentContext(); if CGLSetCurrentContext(self.context)!=0 { return Err("CGL context unavailable".into()); } Ok(Current(old)) } }
    /// Completes submitted GPU work before an observed canvas writer is released.
    pub fn finish(&mut self)->Result<(),String>{
        let _current=self.current()?;
        unsafe{self.gl.finish();}
        Ok(())
    }
    /// Uploads borrowed pixels directly; the driver consumes them synchronously.
    pub fn upload_texture_2d(&mut self,target:u32,level:i32,internal:i32,width:i32,height:i32,border:i32,format:u32,kind:u32,pixels:Option<&[u8]>)->Result<(),String>{
        let _current=self.current()?;
        let required=texture_byte_len(&self.gl,width,height,format,kind)?;
        if pixels.is_some_and(|pixels|pixels.len()<required){return Err("Texture pixel buffer too short".into());}
        unsafe{self.gl.tex_image_2d(target,level,internal,width,height,border,format,kind,glow::PixelUnpackData::Slice(pixels));}
        Ok(())
    }
    pub fn upload_texture_sub_2d(&mut self,target:u32,level:i32,x_offset:i32,y_offset:i32,width:i32,height:i32,format:u32,kind:u32,pixels:&[u8])->Result<(),String>{
        let _current=self.current()?;
        let required=texture_byte_len(&self.gl,width,height,format,kind)?;
        if pixels.len()<required{return Err("Texture pixel buffer too short".into());}
        unsafe{self.gl.tex_sub_image_2d(target,level,x_offset,y_offset,width,height,format,kind,glow::PixelUnpackData::Slice(Some(pixels)));}
        Ok(())
    }
    /// Reads the currently bound framebuffer into a borrowed, tightly packed
    /// RGBA/UNSIGNED_BYTE destination without allocating or changing pack state.
    pub fn read_pixels(&mut self,x:i32,y:i32,width:i32,height:i32,format:u32,kind:u32,pixels:&mut[u8])->Result<(),String>{
        let _current=self.current()?;
        let required=read_byte_len(width,height,format,kind)?;
        if pixels.len()<required{return Err("readPixels destination too short".into());}
        unsafe{
            let alignment=self.gl.get_parameter_i32(glow::PACK_ALIGNMENT);
            self.gl.pixel_store_i32(glow::PACK_ALIGNMENT,1);
            self.gl.read_pixels(x,y,width,height,format,kind,glow::PixelPackData::Slice(Some(&mut pixels[..required])));
            self.gl.pixel_store_i32(glow::PACK_ALIGNMENT,alignment);
        }
        Ok(())
    }
    /// Replaces the transferred default drawing bitmap without resetting any
    /// configured drawing state. Scissor/viewport boxes and pack state are
    /// never modified; front/back stencil masks are restored independently.
    pub fn clear_bitmap(&mut self)->Result<(),String>{
        let _current=self.current()?;
        unsafe{
            let gl=&self.gl;
            for _ in 0..16{let error=gl.get_error();if error==glow::NO_ERROR{break;}self.errors.push_back(error);}
            let framebuffer=gl.get_parameter_framebuffer(glow::FRAMEBUFFER_BINDING);
            let scissor=gl.is_enabled(glow::SCISSOR_TEST);
            let color_mask=gl.get_parameter_bool_array::<4>(glow::COLOR_WRITEMASK);
            let depth_mask=gl.get_parameter_bool(glow::DEPTH_WRITEMASK);
            let stencil_front=gl.get_parameter_i32(glow::STENCIL_WRITEMASK) as u32;
            let stencil_back=gl.get_parameter_i32(glow::STENCIL_BACK_WRITEMASK) as u32;
            let mut clear_color=[0.;4];gl.get_parameter_f32_slice(glow::COLOR_CLEAR_VALUE,&mut clear_color);
            let mut clear_depth=0.;glGetDoublev(glow::DEPTH_CLEAR_VALUE,&mut clear_depth);
            let clear_stencil=gl.get_parameter_i32(glow::STENCIL_CLEAR_VALUE);
            gl.bind_framebuffer(glow::FRAMEBUFFER,Some(self.fbo));
            gl.disable(glow::SCISSOR_TEST);
            gl.color_mask(true,true,true,true);gl.depth_mask(true);
            gl.stencil_mask_separate(glow::FRONT,u32::MAX);gl.stencil_mask_separate(glow::BACK,u32::MAX);
            gl.clear_color(0.,0.,0.,0.);gl.clear_depth_f64(1.);gl.clear_stencil(0);
            gl.clear(glow::COLOR_BUFFER_BIT|glow::DEPTH_BUFFER_BIT|glow::STENCIL_BUFFER_BIT);
            let error=gl.get_error();
            gl.clear_color(clear_color[0],clear_color[1],clear_color[2],clear_color[3]);
            gl.clear_depth_f64(clear_depth);gl.clear_stencil(clear_stencil);
            gl.color_mask(color_mask[0],color_mask[1],color_mask[2],color_mask[3]);gl.depth_mask(depth_mask);
            gl.stencil_mask_separate(glow::FRONT,stencil_front);gl.stencil_mask_separate(glow::BACK,stencil_back);
            if scissor{gl.enable(glow::SCISSOR_TEST);}
            gl.bind_framebuffer(glow::FRAMEBUFFER,framebuffer);
            if error!=glow::NO_ERROR{self.errors.push_back(error);return Err(format!("Native GL bitmap clear failed: 0x{error:04x}"));}
        }
        Ok(())
    }
    /// Resizes only the default bitmap attachments. The context, default FBO,
    /// user objects, bindings, viewport and drawing configuration remain live.
    pub fn resize(&mut self,width:u32,height:u32)->Result<(),String>{
        if width>16384||height>16384{return Err("Invalid CGL surface dimensions".into());}
        if self.width==width&&self.height==height{return self.clear_bitmap();}
        let _current=self.current()?;
        unsafe{
            for _ in 0..16{let error=self.gl.get_error();if error==glow::NO_ERROR{break;}self.errors.push_back(error);}
            let framebuffer=self.gl.get_parameter_framebuffer(glow::FRAMEBUFFER_BINDING);
            let texture=self.gl.get_parameter_texture(glow::TEXTURE_BINDING_2D);
            let renderbuffer=self.gl.get_parameter_renderbuffer(glow::RENDERBUFFER_BINDING);
            let color=self.gl.create_texture()?;
            let depth=match self.gl.create_renderbuffer(){Ok(depth)=>depth,Err(error)=>{self.gl.delete_texture(color);return Err(error);}};
            let result=(||->Result<(),String>{
                self.gl.bind_texture(glow::TEXTURE_2D,Some(color));
                self.gl.tex_image_2d(glow::TEXTURE_2D,0,glow::RGBA8 as i32,width.max(1) as i32,height.max(1) as i32,0,glow::RGBA,glow::UNSIGNED_BYTE,glow::PixelUnpackData::Slice(None));
                self.gl.tex_parameter_i32(glow::TEXTURE_2D,glow::TEXTURE_MIN_FILTER,glow::NEAREST as i32);
                self.gl.tex_parameter_i32(glow::TEXTURE_2D,glow::TEXTURE_MAG_FILTER,glow::NEAREST as i32);
                self.gl.bind_renderbuffer(glow::RENDERBUFFER,Some(depth));
                self.gl.renderbuffer_storage(glow::RENDERBUFFER,glow::DEPTH24_STENCIL8,width.max(1) as i32,height.max(1) as i32);
                self.gl.bind_framebuffer(glow::FRAMEBUFFER,Some(self.fbo));
                self.gl.framebuffer_texture_2d(glow::FRAMEBUFFER,glow::COLOR_ATTACHMENT0,glow::TEXTURE_2D,Some(color),0);
                self.gl.framebuffer_renderbuffer(glow::FRAMEBUFFER,glow::DEPTH_ATTACHMENT,glow::RENDERBUFFER,Some(depth));
                self.gl.framebuffer_renderbuffer(glow::FRAMEBUFFER,glow::STENCIL_ATTACHMENT,glow::RENDERBUFFER,Some(depth));
                let status=self.gl.check_framebuffer_status(glow::FRAMEBUFFER);
                let error=self.gl.get_error();
                if error!=glow::NO_ERROR{self.errors.push_back(error);return Err(format!("Native GL resize failed: 0x{error:04x}"));}
                if status!=glow::FRAMEBUFFER_COMPLETE{return Err(format!("Native GL resized framebuffer incomplete: 0x{status:04x}"));}
                self.clear_bitmap()
            })();
            if result.is_ok(){
                self.gl.delete_texture(self.color);self.gl.delete_renderbuffer(self.depth);
                self.color=color;self.depth=depth;self.width=width;self.height=height;
            }else{
                self.gl.bind_framebuffer(glow::FRAMEBUFFER,Some(self.fbo));
                self.gl.framebuffer_texture_2d(glow::FRAMEBUFFER,glow::COLOR_ATTACHMENT0,glow::TEXTURE_2D,Some(self.color),0);
                self.gl.framebuffer_renderbuffer(glow::FRAMEBUFFER,glow::DEPTH_ATTACHMENT,glow::RENDERBUFFER,Some(self.depth));
                self.gl.framebuffer_renderbuffer(glow::FRAMEBUFFER,glow::STENCIL_ATTACHMENT,glow::RENDERBUFFER,Some(self.depth));
                self.gl.delete_texture(color);self.gl.delete_renderbuffer(depth);
            }
            self.gl.bind_texture(glow::TEXTURE_2D,texture);
            self.gl.bind_renderbuffer(glow::RENDERBUFFER,renderbuffer);
            self.gl.bind_framebuffer(glow::FRAMEBUFFER,framebuffer);
            result
        }
    }
    pub fn command(&mut self, request:&Value)->Result<Value,String> {
        let _current=self.current()?;
        let op=request["method"].as_str().ok_or("Missing GL method")?;
        let args=request["args"].as_array().ok_or("Missing GL args")?;
        let u=|i:usize| args.get(i).and_then(Value::as_u64).unwrap_or(0) as u32;
        let n=|i:usize| args.get(i).and_then(Value::as_i64).unwrap_or(0) as i32;
        let f=|i:usize| args.get(i).and_then(Value::as_f64).unwrap_or(0.) as f32;
        let b=|i:usize| args.get(i).and_then(Value::as_bool).unwrap_or(false);
        let s=|i:usize| args.get(i).and_then(Value::as_str).unwrap_or("");
        let floats=|i:usize| args.get(i).and_then(Value::as_array).map(|a| a.iter().map(|v|v.as_f64().unwrap_or(0.) as f32).collect::<Vec<_>>()).unwrap_or_default();
        let integers=|i:usize| args.get(i).and_then(Value::as_array).map(|a|a.iter().map(|v|v.as_i64().unwrap_or(0) as i32).collect::<Vec<_>>()).unwrap_or_default();
        let bytes=|i:usize| args.get(i).and_then(Value::as_array).map(|a| a.iter().map(|v|v.as_u64().unwrap_or(0) as u8).collect::<Vec<_>>()).unwrap_or_default();
        let gl=&self.gl;
        unsafe { match op {
            "createShader" => { let x=gl.create_shader(u(0))?; let id=x.0.get(); self.shaders.insert(id,x); return Ok(json!(id)); },
            "createProgram" => { let x=gl.create_program()?; let id=x.0.get(); self.programs.insert(id,x); return Ok(json!(id)); },
            "createBuffer" => { let x=gl.create_buffer()?; let id=x.0.get(); self.buffers.insert(id,x); return Ok(json!(id)); },
            "createTexture" => { let x=gl.create_texture()?; let id=x.0.get(); self.textures.insert(id,x); return Ok(json!(id)); },
            "createFramebuffer" => { let x=gl.create_framebuffer()?; let id=x.0.get(); self.framebuffers.insert(id,x); return Ok(json!(id)); },
            "createRenderbuffer" => { let x=gl.create_renderbuffer()?; let id=x.0.get(); self.renderbuffers.insert(id,x); return Ok(json!(id)); },
            "isProgram"=>return Ok(json!(self.programs.get(&u(0)).is_some_and(|object|gl.is_program(*object)))),
            "isShader"=>return Ok(json!(self.shaders.get(&u(0)).is_some_and(|object|gl.is_shader(*object)))),
            "isBuffer"=>return Ok(json!(self.buffers.get(&u(0)).is_some_and(|object|gl.is_buffer(*object)))),
            "isTexture"=>return Ok(json!(self.textures.get(&u(0)).is_some_and(|object|gl.is_texture(*object)))),
            "isFramebuffer"=>return Ok(json!(self.framebuffers.get(&u(0)).is_some_and(|object|gl.is_framebuffer(*object)))),
            "isRenderbuffer"=>return Ok(json!(self.renderbuffers.get(&u(0)).is_some_and(|object|gl.is_renderbuffer(*object)))),
            "shaderSource" => gl.shader_source(*self.shaders.get(&u(0)).ok_or("Unknown shader")?,&desktop_shader(s(1))),
            "compileShader" => gl.compile_shader(*self.shaders.get(&u(0)).ok_or("Unknown shader")?),
            "getShaderParameter" => { let x=*self.shaders.get(&u(0)).ok_or("Unknown shader")?; let mut value=0; glGetShaderiv(x.0.get(),u(1),&mut value); return Ok(if matches!(u(1),glow::COMPILE_STATUS|glow::DELETE_STATUS) {json!(value!=0)}else{json!(value)}); },
            "getShaderPrecisionFormat"=>{
                let Some(query)=self.precision_query else{return Ok(Value::Null);};
                // Preserve older errors so this optional capability probe does
                // not erase the caller's pending native GL error state.
                for _ in 0..16{let error=gl.get_error();if error==glow::NO_ERROR{break;}self.errors.push_back(error);}
                let mut range=[0;2];let mut precision=0;
                query(u(0),u(1),range.as_mut_ptr(),&mut precision);
                let error=gl.get_error();
                if error!=glow::NO_ERROR{self.errors.push_back(error);return Ok(Value::Null);}
                return Ok(json!({"rangeMin":range[0],"rangeMax":range[1],"precision":precision}));
            },
            "getShaderInfoLog" => return Ok(json!(gl.get_shader_info_log(*self.shaders.get(&u(0)).ok_or("Unknown shader")?))),
            "attachShader" => gl.attach_shader(*self.programs.get(&u(0)).ok_or("Unknown program")?,*self.shaders.get(&u(1)).ok_or("Unknown shader")?),
            "detachShader" => gl.detach_shader(*self.programs.get(&u(0)).ok_or("Unknown program")?,*self.shaders.get(&u(1)).ok_or("Unknown shader")?),
            "bindAttribLocation" => gl.bind_attrib_location(*self.programs.get(&u(0)).ok_or("Unknown program")?,u(1),s(2)),
            "linkProgram" => gl.link_program(*self.programs.get(&u(0)).ok_or("Unknown program")?),
            "validateProgram" => gl.validate_program(*self.programs.get(&u(0)).ok_or("Unknown program")?),
            "getProgramParameter" => { let x=*self.programs.get(&u(0)).ok_or("Unknown program")?; return Ok(match u(1) { glow::LINK_STATUS=>json!(gl.get_program_link_status(x)),glow::VALIDATE_STATUS|glow::DELETE_STATUS=>json!(gl.get_program_parameter_i32(x,u(1))!=0),_=>json!(gl.get_program_parameter_i32(x,u(1))) }); },
            "getProgramInfoLog" => return Ok(json!(gl.get_program_info_log(*self.programs.get(&u(0)).ok_or("Unknown program")?))),
            "getAttribLocation" => return Ok(json!(gl.get_attrib_location(*self.programs.get(&u(0)).ok_or("Unknown program")?,s(1)).map(|v|v as i32).unwrap_or(-1))),
            "getUniformLocation" => { let location=gl.get_uniform_location(*self.programs.get(&u(0)).ok_or("Unknown program")?,s(1)); return Ok(if let Some(location)=location { let id=self.next_uniform; self.next_uniform+=1; self.uniforms.insert(id,location); json!(id) } else { Value::Null }); },
            "useProgram" => gl.use_program(self.programs.get(&u(0)).copied()),
            "bindBuffer" => {
                if !matches!(u(0),glow::ARRAY_BUFFER|glow::ELEMENT_ARRAY_BUFFER){return Err("Only WebGL vertex/index buffer targets are supported".into());}
                gl.bind_buffer(u(0),self.buffers.get(&u(1)).copied());
            },
            "bufferData" => { if args.get(1).is_some_and(Value::is_number) {gl.buffer_data_size(u(0),n(1),u(2));} else {gl.buffer_data_u8_slice(u(0),&bytes(1),u(2));} },
            "bufferSubData" => gl.buffer_sub_data_u8_slice(u(0),n(1),&bytes(2)),
            "enableVertexAttribArray" => gl.enable_vertex_attrib_array(u(0)), "disableVertexAttribArray"=>gl.disable_vertex_attrib_array(u(0)),
            "vertexAttribPointer"=>{
                if gl.get_parameter_i32(glow::ARRAY_BUFFER_BINDING)==0{return Err("vertexAttribPointer requires a native vertex buffer".into());}
                if n(5)<0{return Err("Negative vertex buffer offset".into());}
                gl.vertex_attrib_pointer_f32(u(0),n(1),u(2),b(3),n(4),n(5));
            },
            "vertexAttrib1f"=>gl.vertex_attrib_1_f32(u(0),f(1)), "vertexAttrib2f"=>gl.vertex_attrib_2_f32(u(0),f(1),f(2)), "vertexAttrib3f"=>gl.vertex_attrib_3_f32(u(0),f(1),f(2),f(3)), "vertexAttrib4f"=>gl.vertex_attrib_4_f32(u(0),f(1),f(2),f(3),f(4)),
            "uniform1f"=>gl.uniform_1_f32(self.uniforms.get(&u(0)),f(1)), "uniform2f"=>gl.uniform_2_f32(self.uniforms.get(&u(0)),f(1),f(2)), "uniform3f"=>gl.uniform_3_f32(self.uniforms.get(&u(0)),f(1),f(2),f(3)), "uniform4f"=>gl.uniform_4_f32(self.uniforms.get(&u(0)),f(1),f(2),f(3),f(4)),
            "uniform1i"=>gl.uniform_1_i32(self.uniforms.get(&u(0)),n(1)), "uniform2i"=>gl.uniform_2_i32(self.uniforms.get(&u(0)),n(1),n(2)), "uniform3i"=>gl.uniform_3_i32(self.uniforms.get(&u(0)),n(1),n(2),n(3)), "uniform4i"=>gl.uniform_4_i32(self.uniforms.get(&u(0)),n(1),n(2),n(3),n(4)),
            "uniform1fv"=>gl.uniform_1_f32_slice(self.uniforms.get(&u(0)),&floats(1)), "uniform2fv"=>gl.uniform_2_f32_slice(self.uniforms.get(&u(0)),&floats(1)), "uniform3fv"=>gl.uniform_3_f32_slice(self.uniforms.get(&u(0)),&floats(1)), "uniform4fv"=>gl.uniform_4_f32_slice(self.uniforms.get(&u(0)),&floats(1)),
            "uniform1iv"=>gl.uniform_1_i32_slice(self.uniforms.get(&u(0)),&integers(1)), "uniform2iv"=>gl.uniform_2_i32_slice(self.uniforms.get(&u(0)),&integers(1)), "uniform3iv"=>gl.uniform_3_i32_slice(self.uniforms.get(&u(0)),&integers(1)), "uniform4iv"=>gl.uniform_4_i32_slice(self.uniforms.get(&u(0)),&integers(1)),
            "uniformMatrix2fv"=>gl.uniform_matrix_2_f32_slice(self.uniforms.get(&u(0)),b(1),&floats(2)), "uniformMatrix3fv"=>gl.uniform_matrix_3_f32_slice(self.uniforms.get(&u(0)),b(1),&floats(2)), "uniformMatrix4fv"=>gl.uniform_matrix_4_f32_slice(self.uniforms.get(&u(0)),b(1),&floats(2)),
            "bindTexture"=>gl.bind_texture(u(0),self.textures.get(&u(1)).copied()), "activeTexture"=>gl.active_texture(u(0)),
            "texParameteri"=>gl.tex_parameter_i32(u(0),u(1),n(2)), "texParameterf"=>gl.tex_parameter_f32(u(0),u(1),f(2)),
            "texImage2D"=>{
                let data=bytes(8);
                let required=texture_byte_len(gl,n(3),n(4),u(6),u(7))?;
                let empty=args.get(8).is_none_or(Value::is_null);
                if !empty && data.len()<required {return Err("Texture pixel buffer too short".into());}
                gl.tex_image_2d(u(0),n(1),n(2),n(3),n(4),n(5),u(6),u(7),glow::PixelUnpackData::Slice(if empty{None}else{Some(&data)}));
            },
            "generateMipmap"=>gl.generate_mipmap(u(0)),
            "pixelStorei"=>{
                if !matches!(u(0),glow::PACK_ALIGNMENT|glow::UNPACK_ALIGNMENT)||!matches!(n(1),1|2|4|8){return Err("Unsupported GL pixel storage parameter".into());}
                gl.pixel_store_i32(u(0),n(1));
            },
            "bindFramebuffer"=>gl.bind_framebuffer(u(0),Some(self.framebuffers.get(&u(1)).copied().unwrap_or(self.fbo))),
            "bindRenderbuffer"=>gl.bind_renderbuffer(u(0),self.renderbuffers.get(&u(1)).copied()),
            "renderbufferStorage"=>gl.renderbuffer_storage(u(0),u(1),n(2),n(3)),
            "framebufferTexture2D"=>gl.framebuffer_texture_2d(u(0),u(1),u(2),self.textures.get(&u(3)).copied(),n(4)),
            "framebufferRenderbuffer"=>gl.framebuffer_renderbuffer(u(0),u(1),u(2),self.renderbuffers.get(&u(3)).copied()),
            "checkFramebufferStatus"=>return Ok(json!(gl.check_framebuffer_status(u(0)))),
            "clearColor"=>gl.clear_color(f(0),f(1),f(2),f(3)), "clearDepth"=>gl.clear_depth_f32(f(0)), "clearStencil"=>gl.clear_stencil(n(0)), "clear"=>gl.clear(u(0)),
            "viewport"=>gl.viewport(n(0),n(1),n(2),n(3)), "scissor"=>gl.scissor(n(0),n(1),n(2),n(3)),
            "enable"=>gl.enable(u(0)), "disable"=>gl.disable(u(0)), "isEnabled"=>return Ok(json!(gl.is_enabled(u(0)))),
            "blendFunc"=>gl.blend_func(u(0),u(1)), "blendFuncSeparate"=>gl.blend_func_separate(u(0),u(1),u(2),u(3)), "blendEquation"=>gl.blend_equation(u(0)), "blendEquationSeparate"=>gl.blend_equation_separate(u(0),u(1)), "blendColor"=>gl.blend_color(f(0),f(1),f(2),f(3)),
            "depthFunc"=>gl.depth_func(u(0)), "depthMask"=>gl.depth_mask(b(0)), "depthRange"=>gl.depth_range_f32(f(0),f(1)), "colorMask"=>gl.color_mask(b(0),b(1),b(2),b(3)),
            "cullFace"=>gl.cull_face(u(0)), "frontFace"=>gl.front_face(u(0)), "lineWidth"=>gl.line_width(f(0)), "polygonOffset"=>gl.polygon_offset(f(0),f(1)),
            "stencilFunc"=>gl.stencil_func(u(0),n(1),u(2)), "stencilMask"=>gl.stencil_mask(u(0)), "stencilOp"=>gl.stencil_op(u(0),u(1),u(2)),
            "drawArrays"=>gl.draw_arrays(u(0),n(1),n(2)),
            "drawElements"=>{
                if gl.get_parameter_i32(glow::ELEMENT_ARRAY_BUFFER_BINDING)==0||n(3)<0{return Err("drawElements requires a native index buffer and nonnegative offset".into());}
                gl.draw_elements(u(0),n(1),u(2),n(3));
            },
            "finish"=>gl.finish(), "flush"=>gl.flush(), "getError"=>return Ok(json!(self.errors.pop_front().unwrap_or_else(||gl.get_error()))),
            "contextStatus"=>return Ok(json!(CGLGetCurrentContext()!=self.context)),
            "getActiveUniform"=>return Ok(gl.get_active_uniform(*self.programs.get(&u(0)).ok_or("Unknown program")?,u(1)).map(|v|json!({"name":v.name,"size":v.size,"type":v.utype})).unwrap_or(Value::Null)),
            "getActiveAttrib"=>return Ok(gl.get_active_attribute(*self.programs.get(&u(0)).ok_or("Unknown program")?,u(1)).map(|v|json!({"name":v.name,"size":v.size,"type":v.atype})).unwrap_or(Value::Null)),
            "getBufferParameter"=>return Ok(json!(gl.get_buffer_parameter_i32(u(0),u(1)))),
            "getTexParameter"=>{
                if !matches!(u(1),glow::TEXTURE_MIN_FILTER|glow::TEXTURE_MAG_FILTER|glow::TEXTURE_WRAP_S|glow::TEXTURE_WRAP_T){return Err("Unsupported WebGL texture parameter".into());}
                return Ok(json!(gl.get_tex_parameter_i32(u(0),u(1))));
            },
            "getParameter"=>return Ok(match u(0) {
                glow::VENDOR|glow::RENDERER|glow::VERSION|glow::SHADING_LANGUAGE_VERSION=>json!(gl.get_parameter_string(u(0))),
                glow::VIEWPORT|glow::SCISSOR_BOX=>{let mut v=[0;4];gl.get_parameter_i32_slice(u(0),&mut v);json!(v)},
                glow::COLOR_CLEAR_VALUE|glow::BLEND_COLOR=>{let mut v=[0.;4];gl.get_parameter_f32_slice(u(0),&mut v);json!(v)},
                glow::MAX_VIEWPORT_DIMS=>{let mut v=[0;2];gl.get_parameter_i32_slice(u(0),&mut v);json!(v)},
                glow::ALIASED_POINT_SIZE_RANGE|glow::ALIASED_LINE_WIDTH_RANGE|glow::DEPTH_RANGE=>{let mut v=[0.;2];gl.get_parameter_f32_slice(u(0),&mut v);json!(v)},
                glow::COLOR_WRITEMASK=>json!(gl.get_parameter_bool_array::<4>(u(0))),
                glow::COMPRESSED_TEXTURE_FORMATS=>{
                    let count=gl.get_parameter_i32(glow::NUM_COMPRESSED_TEXTURE_FORMATS);
                    if !(0..=4096).contains(&count){return Err(format!("Invalid native compressed texture format count: {count}"));}
                    let mut formats=vec![0;count as usize];
                    if count!=0{gl.get_parameter_i32_slice(glow::COMPRESSED_TEXTURE_FORMATS,&mut formats);}
                    json!(formats)
                },
                glow::BLEND|glow::CULL_FACE|glow::DEPTH_TEST|glow::DITHER|glow::SCISSOR_TEST|glow::STENCIL_TEST|glow::DEPTH_WRITEMASK|glow::SAMPLE_COVERAGE_INVERT=>json!(gl.get_parameter_bool(u(0))),
                glow::DEPTH_CLEAR_VALUE|glow::LINE_WIDTH|glow::POLYGON_OFFSET_FACTOR|glow::POLYGON_OFFSET_UNITS|glow::SAMPLE_COVERAGE_VALUE=>json!(gl.get_parameter_f32(u(0))),
                0x8DFB=>json!(gl.get_parameter_i32(glow::MAX_VERTEX_UNIFORM_COMPONENTS)/4),
                0x8DFD=>json!(gl.get_parameter_i32(glow::MAX_FRAGMENT_UNIFORM_COMPONENTS)/4),
                0x8DFC=>json!(gl.get_parameter_i32(glow::MAX_VARYING_FLOATS)/4),
                glow::ACTIVE_TEXTURE|glow::ARRAY_BUFFER_BINDING|glow::ELEMENT_ARRAY_BUFFER_BINDING|glow::CURRENT_PROGRAM|glow::FRAMEBUFFER_BINDING|glow::RENDERBUFFER_BINDING|glow::TEXTURE_BINDING_2D|glow::TEXTURE_BINDING_CUBE_MAP
                |glow::MAX_TEXTURE_SIZE|glow::MAX_CUBE_MAP_TEXTURE_SIZE|glow::MAX_RENDERBUFFER_SIZE|glow::MAX_VERTEX_ATTRIBS|glow::MAX_TEXTURE_IMAGE_UNITS|glow::MAX_VERTEX_TEXTURE_IMAGE_UNITS|glow::MAX_COMBINED_TEXTURE_IMAGE_UNITS
                |glow::PACK_ALIGNMENT|glow::UNPACK_ALIGNMENT|glow::BLEND_SRC_RGB|glow::BLEND_SRC_ALPHA|glow::BLEND_DST_RGB|glow::BLEND_DST_ALPHA|glow::BLEND_EQUATION_RGB|glow::BLEND_EQUATION_ALPHA|glow::DEPTH_FUNC
                |glow::CULL_FACE_MODE|glow::FRONT_FACE|glow::STENCIL_FUNC|glow::STENCIL_VALUE_MASK|glow::STENCIL_REF|glow::STENCIL_WRITEMASK|glow::STENCIL_FAIL|glow::STENCIL_PASS_DEPTH_FAIL|glow::STENCIL_PASS_DEPTH_PASS
                |glow::STENCIL_BACK_FUNC|glow::STENCIL_BACK_REF|glow::STENCIL_BACK_VALUE_MASK|glow::STENCIL_BACK_WRITEMASK|glow::STENCIL_BACK_FAIL|glow::STENCIL_BACK_PASS_DEPTH_FAIL|glow::STENCIL_BACK_PASS_DEPTH_PASS
                |glow::RED_BITS|glow::GREEN_BITS|glow::BLUE_BITS|glow::ALPHA_BITS|glow::DEPTH_BITS|glow::STENCIL_BITS|glow::SAMPLES|glow::SAMPLE_BUFFERS|glow::SUBPIXEL_BITS=>json!(gl.get_parameter_i32(u(0))),
                _=>{if !self.errors.contains(&glow::INVALID_ENUM){self.errors.push_back(glow::INVALID_ENUM);}Value::Null},
            }),
            "readPixels"=>{
                let len=read_byte_len(n(2),n(3),u(4),u(5))?;
                let mut data=vec![0;len];
                let alignment=gl.get_parameter_i32(glow::PACK_ALIGNMENT);
                gl.pixel_store_i32(glow::PACK_ALIGNMENT,1);
                gl.read_pixels(n(0),n(1),n(2),n(3),u(4),u(5),glow::PixelPackData::Slice(Some(&mut data)));
                gl.pixel_store_i32(glow::PACK_ALIGNMENT,alignment);
                return Ok(json!(data));
            },
            // Shader/program deletion is deferred by GL while referenced.
            // Retain the name so is* remains an actual driver liveness query.
            "deleteShader"=>{if let Some(x)=self.shaders.get(&u(0)){gl.delete_shader(*x);}},
            "deleteProgram"=>{if let Some(x)=self.programs.get(&u(0)){gl.delete_program(*x);}},
            "deleteBuffer"=>{if let Some(x)=self.buffers.remove(&u(0)){gl.delete_buffer(x);}}, "deleteTexture"=>{if let Some(x)=self.textures.remove(&u(0)){gl.delete_texture(x);}},
            "deleteFramebuffer"=>{if let Some(x)=self.framebuffers.remove(&u(0)){gl.delete_framebuffer(x);}}, "deleteRenderbuffer"=>{if let Some(x)=self.renderbuffers.remove(&u(0)){gl.delete_renderbuffer(x);}},
            _=>return Err(format!("Unsupported native GL operation: {op}")),
        } }
        Ok(Value::Null)
    }
    /// Copies the default offscreen framebuffer to Canvas's top-down straight RGBA bridge.
    pub fn readback(&mut self,pixels:&mut[u8])->Result<(),String>{
        if pixels.len()!=self.width as usize*self.height as usize*4{return Err("GL surface byte length mismatch".into());}
        if self.width==0||self.height==0{return Ok(());}
        let _current=self.current()?;
        unsafe {
            let old=self.gl.get_parameter_framebuffer(glow::FRAMEBUFFER_BINDING);
            let alignment=self.gl.get_parameter_i32(glow::PACK_ALIGNMENT);
            self.gl.bind_framebuffer(glow::FRAMEBUFFER,Some(self.fbo));
            self.gl.pixel_store_i32(glow::PACK_ALIGNMENT,1);
            self.gl.read_pixels(0,0,self.width as i32,self.height as i32,glow::RGBA,glow::UNSIGNED_BYTE,glow::PixelPackData::Slice(Some(&mut *pixels)));
            self.gl.pixel_store_i32(glow::PACK_ALIGNMENT,alignment);
            self.gl.bind_framebuffer(glow::FRAMEBUFFER,old);
        }
        let stride=self.width as usize*4;
        for y in 0..self.height as usize/2 {let (a,b)=pixels.split_at_mut((self.height as usize-1-y)*stride);a[y*stride..(y+1)*stride].swap_with_slice(&mut b[..stride]);}
        Ok(())
    }
}
impl Drop for GlContext {
    fn drop(&mut self){unsafe{
        let old=CGLGetCurrentContext();
        CGLSetCurrentContext(self.context);
        self.gl.delete_framebuffer(self.fbo);self.gl.delete_texture(self.color);self.gl.delete_renderbuffer(self.depth);
        std::mem::ManuallyDrop::drop(&mut self.gl);
        CGLSetCurrentContext(if old==self.context{ptr::null_mut()}else{old});
        CGLDestroyContext(self.context);dlclose(self.library);
    }}
}
/// GLSL ES 1.00 and desktop 1.20 share syntax except precision declarations.
/// The native driver still performs all compilation, linking and diagnostics.
fn desktop_shader(source:&str)->String {
    let mut output=String::with_capacity(source.len()+16);
    output.push_str("#version 120\n");
    let bytes=source.as_bytes();
    let mut i=0;let mut precision=false;let mut line_start=true;
    while i<bytes.len() {
        if line_start {
            let end=source[i..].find('\n').map_or(bytes.len(),|end|i+end);
            if source[i..end].trim_start().starts_with("#version") {i=end;if i<bytes.len(){output.push('\n');i+=1;}continue;}
        }
        if bytes[i..].starts_with(b"//") {
            let end=source[i..].find('\n').map_or(bytes.len(),|end|i+end);
            output.push_str(&source[i..end]);i=end;continue;
        }
        if bytes[i..].starts_with(b"/*") {
            let end=source[i+2..].find("*/").map_or(bytes.len(),|end|i+2+end+2);
            output.push_str(&source[i..end]);i=end;continue;
        }
        if bytes[i].is_ascii_alphabetic()||bytes[i]==b'_' {
            let start=i;i+=1;
            while i<bytes.len()&&(bytes[i].is_ascii_alphanumeric()||bytes[i]==b'_'){i+=1;}
            let token=&source[start..i];
            if token=="precision"{precision=true;}else if !precision&&!matches!(token,"lowp"|"mediump"|"highp"){output.push_str(token);}
            line_start=false;continue;
        }
        let ch=source[i..].chars().next().unwrap();
        if !precision||ch=='\n'{output.push(ch);}
        if precision&&ch==';'{precision=false;}
        line_start=ch=='\n'||(line_start&&ch.is_whitespace());
        i+=ch.len_utf8();
    }
    output
}

fn texture_byte_len(gl:&glow::Context,width:i32,height:i32,format:u32,kind:u32)->Result<usize,String>{
    if width<0||height<0{return Err("Negative texture dimensions".into());}
    let channels=match format{glow::RGBA=>4,glow::RGB=>3,glow::LUMINANCE_ALPHA=>2,glow::ALPHA|glow::LUMINANCE=>1,_=>return Err("Unsupported texture pixel format".into())};
    let bpp=match kind{glow::UNSIGNED_BYTE=>channels,glow::FLOAT=>channels*4,glow::UNSIGNED_SHORT_4_4_4_4|glow::UNSIGNED_SHORT_5_5_5_1|glow::UNSIGNED_SHORT_5_6_5=>2,_=>return Err("Unsupported texture pixel type".into())};
    let row=(width as usize).checked_mul(bpp).ok_or("Texture too large")?;
    let align=unsafe{gl.get_parameter_i32(glow::UNPACK_ALIGNMENT)}.max(1) as usize;
    let stride=row.checked_add(align-1).ok_or("Texture too large")?/align*align;
    if height==0{Ok(0)}else{stride.checked_mul(height as usize-1).and_then(|n|n.checked_add(row)).ok_or_else(||"Texture too large".into())}
}
fn read_byte_len(width:i32,height:i32,format:u32,kind:u32)->Result<usize,String>{
    if format!=glow::RGBA||kind!=glow::UNSIGNED_BYTE||width<0||height<0{return Err("readPixels requires nonnegative dimensions and RGBA/UNSIGNED_BYTE".into());}
    (width as usize).checked_mul(height as usize).and_then(|n|n.checked_mul(4)).filter(|n|*n<=268435456).ok_or_else(||"readPixels dimensions too large".into())
}
