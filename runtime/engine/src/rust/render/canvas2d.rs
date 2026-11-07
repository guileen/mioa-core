//! LP-owned surfaces painted by the shared CPU path/text/image components.
//! Handles own their pixels, drawing state and fonts; no DOM, execution or realm pointers.
//! Command ABI: `method`, `args`; paints carry RGBA `color`, `alpha`, `composite`,
//! stroke fields `line_width`, `line_cap`, `line_join`, `miter_limit`, `dash`, `dash_offset`.
//! Gradients carry `kind`, six `coords`, RGBA `stops`, and creation-time `transform`.
//! Patterns carry RGBA `pixels` + `width`/`height` or original `encoded` bytes,
//! `repetition` and their local `transform`. Explicit Path2D `path` commands are local
//! coordinates (M/L/Q/C/Z), optionally transformed by `path_transform` before the CTM.
//! `evenodd` applies to fill/clip/hit tests. Hit `args` are device-space [x,y].
//! Before either image C call, `drawState` sets `alpha`, `composite`, `smooth`;
//! those calls keep the existing ABI and use the same saved native CTM and clip.
use std::{ffi::c_void, path::PathBuf, panic::{catch_unwind, AssertUnwindSafe}};
use obscura_render::graphics::{CanvasTextEngine, TextRequest, canvas::{self, PathCommand, ClipPath, CanvasPaint, CanvasDrawRequest}, image::{self, ImageDrawRequest}};
use serde_json::{Value, json};
use tiny_skia::{FillRule, LineCap, LineJoin, Paint, Path, PathSegment, Pixmap, PixmapMut, Pattern, FilterQuality, SpreadMode, Stroke, StrokeDash, Transform};

#[derive(Clone)]
struct State {
    transform: [f32; 6], clips: Vec<ClipPath>, alpha: f32, composite: String, smooth: bool,
}
impl Default for State {
    fn default() -> Self { Self { transform: canvas::identity(), clips: Vec::new(), alpha: 1., composite: "source-over".into(), smooth: true } }
}
pub struct Canvas {
    width: u32, height: u32, pixels: Vec<u8>, path: Vec<PathCommand>,
    state: State, saved: Vec<State>, text: CanvasTextEngine,
}
type Write = unsafe extern "C" fn(*mut c_void, *const u8, usize) -> bool;
fn number(r: &Value, key: &str, default: f32) -> f32 { r[key].as_f64().map_or(default, |n| n as f32) }
fn matrix_field(r: &Value, key: &str) -> Result<[f32; 6], String> {
    if r[key].is_null() { return Ok(canvas::identity()); }
    let m: [f32; 6] = serde_json::from_value(r[key].clone()).map_err(|e| e.to_string())?;
    if !m.iter().all(|n| n.is_finite()) { return Err("Nonfinite Canvas transform".into()); }
    Ok(m)
}
fn mapped(m: [f32; 6], x: f32, y: f32) -> (f32, f32) { (m[0]*x+m[2]*y+m[4], m[1]*x+m[3]*y+m[5]) }
fn transformed(commands: &[PathCommand], m: [f32; 6]) -> Vec<PathCommand> {
    commands.iter().map(|c| match *c {
        PathCommand::M { x, y } => { let (x,y)=mapped(m,x,y); PathCommand::M{x,y} },
        PathCommand::L { x, y } => { let (x,y)=mapped(m,x,y); PathCommand::L{x,y} },
        PathCommand::Q { x1,y1,x,y } => { let (x1,y1)=mapped(m,x1,y1);let (x,y)=mapped(m,x,y);PathCommand::Q{x1,y1,x,y} },
        PathCommand::C { x1,y1,x2,y2,x,y } => { let (x1,y1)=mapped(m,x1,y1);let (x2,y2)=mapped(m,x2,y2);let (x,y)=mapped(m,x,y);PathCommand::C{x1,y1,x2,y2,x,y} },
        PathCommand::Z => PathCommand::Z,
    }).collect()
}
fn stroke(r: &Value) -> Result<Stroke, String> {
    let mut dash: Vec<f32> = if r["dash"].is_null() { Vec::new() } else { serde_json::from_value(r["dash"].clone()).map_err(|e| e.to_string())? };
    if dash.iter().any(|n| !n.is_finite() || *n < 0.) { return Err("Invalid Canvas line dash".into()); }
    if dash.len()%2 == 1 { dash.extend_from_within(..); }
    if dash.iter().all(|n| *n == 0.) { dash.clear(); }
    Ok(Stroke {
        width: number(r,"line_width",1.), miter_limit: number(r,"miter_limit",10.),
        line_cap: match r["line_cap"].as_str() { Some("round")=>LineCap::Round, Some("square")=>LineCap::Square, _=>LineCap::Butt },
        line_join: match r["line_join"].as_str() { Some("round")=>LineJoin::Round, Some("bevel")=>LineJoin::Bevel, _=>LineJoin::Miter },
        dash: if dash.is_empty() { None } else { Some(StrokeDash::new(dash,number(r,"dash_offset",0.)).ok_or("Invalid Canvas line dash")?) },
    })
}
impl Canvas {
    fn move_to(&mut self,x:f32,y:f32) { let (x,y)=mapped(self.state.transform,x,y);self.path.push(PathCommand::M{x,y}); }
    fn line_to(&mut self,x:f32,y:f32) { if self.path.is_empty() { self.move_to(x,y); } else { let (x,y)=mapped(self.state.transform,x,y);self.path.push(PathCommand::L{x,y}); } }
    fn rect_path(&mut self,x:f32,y:f32,w:f32,h:f32) { self.move_to(x,y);self.line_to(x+w,y);self.line_to(x+w,y+h);self.line_to(x,y+h);self.path.push(PathCommand::Z); }
    fn current(&self) -> Option<(f32,f32)> {
        let mut current=None;let mut start=None;
        for c in &self.path { match *c {
            PathCommand::M{x,y}=>{current=Some((x,y));start=current;},
            PathCommand::L{x,y}|PathCommand::Q{x,y,..}|PathCommand::C{x,y,..}=>current=Some((x,y)),
            PathCommand::Z=>current=start,
        }} current
    }
    fn arc(&mut self,cx:f32,cy:f32,rx:f32,ry:f32,rotation:f32,start:f32,end:f32,ccw:bool) -> Result<(),String> {
        if rx<0. || ry<0. { return Err("Negative arc radius".into()); }
        let tau=std::f32::consts::TAU;let delta=end-start;
        let delta=if ccw { if delta<=-tau {-tau} else {-(-delta).rem_euclid(tau)} } else if delta>=tau {tau} else {delta.rem_euclid(tau)};
        let (s,c)=rotation.sin_cos();let p=|a:f32| {let (sa,ca)=a.sin_cos();(cx+rx*ca*c-ry*sa*s,cy+rx*ca*s+ry*sa*c)};
        let derivative=|a:f32| {let (sa,ca)=a.sin_cos();(-rx*sa*c-ry*ca*s,-rx*sa*s+ry*ca*c)};
        let (x,y)=p(start);self.line_to(x,y);
        let steps=(delta.abs()/std::f32::consts::FRAC_PI_2).ceil() as usize;
        for i in 0..steps {
            let a=start+delta*i as f32/steps as f32;let b=start+delta*(i+1) as f32/steps as f32;
            let k=4./3.*((b-a)/4.).tan();let (ax,ay)=p(a);let (bx,by)=p(b);let (adx,ady)=derivative(a);let (bdx,bdy)=derivative(b);
            let (x1,y1)=mapped(self.state.transform,ax+k*adx,ay+k*ady);let (x2,y2)=mapped(self.state.transform,bx-k*bdx,by-k*bdy);let (x,y)=mapped(self.state.transform,bx,by);
            self.path.push(PathCommand::C{x1,y1,x2,y2,x,y});
        } Ok(())
    }
    fn selected_path(&self,r:&Value) -> Result<Vec<PathCommand>,String> {
        if r["path"].is_null() { return Ok(self.path.clone()); }
        let path:Vec<PathCommand>=serde_json::from_value(r["path"].clone()).map_err(|e|e.to_string())?;
        let local=matrix_field(r,"path_transform")?;
        let m=canvas::matrix(self.state.transform).pre_concat(canvas::matrix(local));
        Ok(transformed(&path,[m.sx,m.ky,m.kx,m.sy,m.tx,m.ty]))
    }
    fn request(&self,r:&Value,kind:&str,path:Vec<PathCommand>) -> Result<CanvasDrawRequest,String> {
        let color=if r["color"].is_null() {[0,0,0,255]} else {serde_json::from_value(r["color"].clone()).map_err(|e|e.to_string())?};
        let gradient=&r["gradient"];
        let paint=CanvasPaint {color,kind:gradient["kind"].as_str().map(str::to_owned),
            stops:if gradient["stops"].is_null(){Vec::new()}else{serde_json::from_value(gradient["stops"].clone()).map_err(|e|e.to_string())?},
            points:if gradient["coords"].is_null(){[0.;6]}else{serde_json::from_value(gradient["coords"].clone()).map_err(|e|e.to_string())?},
            transform:matrix_field(gradient,"transform")?};
        let style=stroke(r)?;
        let dash=if r["dash"].is_null(){Vec::new()}else{let mut d:Vec<f32>=serde_json::from_value(r["dash"].clone()).map_err(|e|e.to_string())?;if d.len()%2==1{d.extend_from_within(..);}if d.iter().all(|n|*n==0.){d.clear();}d};
        Ok(CanvasDrawRequest {width:self.width,height:self.height,path,kind:kind.into(),paint,
            alpha:number(r,"alpha",self.state.alpha),composite:r["composite"].as_str().unwrap_or(&self.state.composite).into(),
            transform:self.state.transform,clips:self.state.clips.clone(),evenodd:r["evenodd"].as_bool().unwrap_or(false),
            line_width:style.width,line_cap:r["line_cap"].as_str().unwrap_or("butt").into(),line_join:r["line_join"].as_str().unwrap_or("miter").into(),miter_limit:style.miter_limit,dash,dash_offset:number(r,"dash_offset",0.)})
    }
    fn paint(&mut self,r:&Value,kind:&str,path:Vec<PathCommand>) -> Result<(),String> {
        let mut req=self.request(r,kind,path)?;
        if self.width==0 || self.height==0 {return Ok(());}
        if kind!="clear" && unbounded(&req.composite) {
            let composite=std::mem::replace(&mut req.composite,"source-over".into());req.clips.clear();
            let destination=std::mem::replace(&mut self.pixels,vec![0;self.width as usize*self.height as usize*4]);
            let result=self.paint_direct(r,&req);
            let layer=std::mem::replace(&mut self.pixels,destination);result?;
            return self.composite_layer(&layer,1.,&composite);
        }
        self.paint_direct(r,&req)
    }
    fn paint_direct(&mut self,r:&Value,req:&CanvasDrawRequest) -> Result<(),String> {
        let kind=req.kind.as_str();
        if kind!="clear" && (!r["pattern"].is_null() || req.paint.kind.as_deref()==Some("conic")) {
            let source=self.paint_pixels(r)?;self.paint_shader(&req,source)
        } else if kind!="clear" && transparent_gradient(&req.paint) {
            let source=vec![0;self.pixels.len()];self.paint_shader(&req,source)
        } else { canvas::draw(&req,&mut self.pixels) }
    }
    // Conic and image-pattern paints use device-space shaders, not a second surface identity.
    fn paint_pixels(&self,r:&Value) -> Result<Vec<u8>,String> {
        let mut pixels=vec![0;self.pixels.len()];
        if !r["pattern"].is_null() {
            let p=&r["pattern"];
            let (source,w,h)=if !p["encoded"].is_null() {let bytes:Vec<u8>=serde_json::from_value(p["encoded"].clone()).map_err(|e|e.to_string())?;let im=::image::load_from_memory(&bytes).map_err(|e|e.to_string())?.to_rgba8();let (w,h)=im.dimensions();(im.into_raw(),w,h)} else {
                let w=p["width"].as_u64().and_then(|n|u32::try_from(n).ok()).ok_or("Invalid pattern width")?;
                let h=p["height"].as_u64().and_then(|n|u32::try_from(n).ok()).ok_or("Invalid pattern height")?;
                let bytes:Vec<u8>=serde_json::from_value(p["pixels"].clone()).map_err(|e|e.to_string())?;(bytes,w,h)
            };
            if w==0||h==0||buffer_len(w,h)!=Some(source.len()) {return Err("Invalid Canvas pattern source".into());}
            let m=canvas::matrix(self.state.transform).pre_concat(canvas::matrix(matrix_field(p,"transform")?));
            let Some(inv)=m.invert()else{return Ok(pixels)};
            let repetition=p["repetition"].as_str().unwrap_or("repeat");
            let (repeat_x,repeat_y)=match repetition {"repeat"=>(true,true),"repeat-x"=>(true,false),"repeat-y"=>(false,true),"no-repeat"=>(false,false),_=>return Err("Invalid pattern repetition".into())};
            let smooth=r["smooth"].as_bool().unwrap_or(self.state.smooth);
            let sample=|x:i64,y:i64|->[u8;4] {
                if (!repeat_x&&(x<0||x>=w as i64))||(!repeat_y&&(y<0||y>=h as i64)){return [0;4];}
                let i=(y.rem_euclid(h as i64)as usize*w as usize+x.rem_euclid(w as i64)as usize)*4;
                source[i..i+4].try_into().unwrap()
            };
            for y in 0..self.height {for x in 0..self.width {
                let (sx,sy)=mapped([inv.sx,inv.ky,inv.kx,inv.sy,inv.tx,inv.ty],x as f32+0.5,y as f32+0.5);
                let color=if smooth {let px=sx-0.5;let py=sy-0.5;let ix=px.floor()as i64;let iy=py.floor()as i64;let fx=px-px.floor();let fy=py-py.floor();let samples=[(sample(ix,iy),(1.-fx)*(1.-fy)),(sample(ix+1,iy),fx*(1.-fy)),(sample(ix,iy+1),(1.-fx)*fy),(sample(ix+1,iy+1),fx*fy)];
                    let mut accum=[0.;4];for (c,weight) in samples {accum[3]+=c[3]as f32*weight;for k in 0..3{accum[k]+=c[k]as f32*c[3]as f32/255.*weight;}}let mut c=[0;4];c[3]=accum[3].round()as u8;if accum[3]>0.{for k in 0..3{c[k]=(accum[k]*255./accum[3]).round()as u8;}}c
                }else{sample(sx.floor()as i64,sy.floor()as i64)};
                let i=(y as usize*self.width as usize+x as usize)*4;pixels[i..i+4].copy_from_slice(&color);
            }}
        } else {
            let req=self.request(r,"fill",Vec::new())?;
            let Some(inv)=canvas::matrix(req.paint.transform).invert()else{return Ok(pixels)};
            let q=req.paint.points;let mut stops=req.paint.stops;stops.sort_by(|a,b|a.offset.total_cmp(&b.offset));
            if stops.is_empty(){return Ok(pixels);}
            for y in 0..self.height {for x in 0..self.width {
                let (px,py)=mapped([inv.sx,inv.ky,inv.kx,inv.sy,inv.tx,inv.ty],x as f32+0.5,y as f32+0.5);
                let t=((py-q[2]).atan2(px-q[1])-q[0]).rem_euclid(std::f32::consts::TAU)/std::f32::consts::TAU;
                let right=stops.partition_point(|s|s.offset<=t);
                let c=if right==0{stops[0].color}else if right==stops.len(){stops[right-1].color}else{let a=&stops[right-1];let b=&stops[right];let f=(t-a.offset)/(b.offset-a.offset);let alpha=a.color[3]as f32*(1.-f)+b.color[3]as f32*f;let mut c=[0;4];c[3]=alpha.round()as u8;if alpha>0.{for k in 0..3{c[k]=((a.color[k]as f32*a.color[3]as f32*(1.-f)+b.color[k]as f32*b.color[3]as f32*f)/alpha).round()as u8;}}c};
                let i=(y as usize*self.width as usize+x as usize)*4;pixels[i..i+4].copy_from_slice(&c);
            }}
        } Ok(pixels)
    }
    fn paint_shader(&mut self,req:&CanvasDrawRequest,mut source:Vec<u8>) -> Result<(),String> {
        let Some(mut path)=canvas::path(&req.path)else{return Ok(())};
        canvas::premultiply(&mut source);
        let pixmap=Pixmap::from_vec(source,tiny_skia::IntSize::from_wh(self.width,self.height).ok_or("Invalid paint dimensions")?).ok_or("Invalid paint bytes")?;
        let mut paint=Paint::default();paint.anti_alias=true;paint.blend_mode=canvas::blend_mode(&req.composite)?;
        paint.shader=Pattern::new(pixmap.as_ref(),SpreadMode::Pad,FilterQuality::Nearest,req.alpha.clamp(0.,1.),Transform::identity());
        let mask=canvas::clip_mask(&req.clips,self.width,self.height)?;
        let mut transform=Transform::identity();
        let style=if req.kind=="stroke" {
            transform=canvas::matrix(req.transform);let Some(inv)=transform.invert()else{return Ok(())};
            path=path.transform(inv).ok_or("Invalid Canvas stroke transform")?;paint.shader.transform(inv);
            Some(Stroke{width:req.line_width,miter_limit:req.miter_limit,line_cap:match req.line_cap.as_str(){"round"=>LineCap::Round,"square"=>LineCap::Square,_=>LineCap::Butt},line_join:match req.line_join.as_str(){"round"=>LineJoin::Round,"bevel"=>LineJoin::Bevel,_=>LineJoin::Miter},dash:if req.dash.is_empty(){None}else{Some(StrokeDash::new(req.dash.clone(),req.dash_offset).ok_or("Invalid Canvas dash")?)}})
        } else {None};
        canvas::premultiply(&mut self.pixels);
        {let mut target=PixmapMut::from_bytes(&mut self.pixels,self.width,self.height).ok_or("Invalid Canvas surface")?;
        if let Some(style)=style{target.stroke_path(&path,&paint,&style,transform,mask.as_ref());}else{target.fill_path(&path,&paint,if req.evenodd{FillRule::EvenOdd}else{FillRule::Winding},transform,mask.as_ref());}}
        canvas::unpremultiply(&mut self.pixels);Ok(())
    }
    fn composite_layer(&mut self,source:&[u8],alpha:f32,composite:&str)->Result<(),String>{
        image::draw(&ImageDrawRequest{width:self.width,height:self.height,source_width:self.width,source_height:self.height,source_rect:[0.,0.,self.width as f32,self.height as f32],dest_rect:[0.,0.,self.width as f32,self.height as f32],transform:canvas::identity(),clips:self.state.clips.clone(),alpha,composite:composite.into(),smooth:false},source,&mut self.pixels)
    }
    fn draw_pixels(&mut self,source:&[u8],source_width:u32,source_height:u32,mut coords:[f32;8]) -> Result<(),String> {
        if !coords.iter().all(|n|n.is_finite()){return Ok(());}
        for (origin,size) in [(0,2),(1,3),(4,6),(5,7)] {if coords[size]<0.{coords[origin]+=coords[size];coords[size]= -coords[size];}}
        if coords[2]==0.||coords[3]==0.||coords[6]==0.||coords[7]==0.{return Ok(());}
        // Clip the source rectangle without smearing edge pixels into out-of-bounds areas.
        for (origin,size,dest,extent,limit) in [(0,2,4,6,source_width as f32),(1,3,5,7,source_height as f32)] {
            let lo=coords[origin].max(0.);let hi=(coords[origin]+coords[size]).min(limit);if hi<=lo{return Ok(());}
            let ratio=coords[extent]/coords[size];coords[dest]+=(lo-coords[origin])*ratio;coords[extent]=(hi-lo)*ratio;coords[origin]=lo;coords[size]=hi-lo;
        }
        let mut req=ImageDrawRequest{width:self.width,height:self.height,source_width,source_height,source_rect:[coords[0],coords[1],coords[2],coords[3]],dest_rect:[coords[4],coords[5],coords[6],coords[7]],transform:self.state.transform,clips:self.state.clips.clone(),alpha:self.state.alpha,composite:self.state.composite.clone(),smooth:self.state.smooth};
        if unbounded(&req.composite) {
            let composite=std::mem::replace(&mut req.composite,"source-over".into());req.clips.clear();
            let mut layer=vec![0;self.pixels.len()];image::draw(&req,source,&mut layer)?;
            self.composite_layer(&layer,1.,&composite)
        }else{image::draw(&req,source,&mut self.pixels)}
    }
    fn hit(&self,r:&Value,x:f32,y:f32,is_stroke:bool) -> Result<bool,String> {
        if !x.is_finite()||!y.is_finite(){return Ok(false);}
        let Some(mut p)=canvas::path(&self.selected_path(r)?)else{return Ok(false)};
        if is_stroke {
            let transform=canvas::matrix(self.state.transform);let Some(inv)=transform.invert()else{return Ok(false)};
            p=p.transform(inv).ok_or("Invalid stroke path")?;let style=stroke(r)?;
            let scale=self.state.transform.iter().take(4).fold(1f32,|s,n|s.max(n.abs()));
            if let Some(dash)=&style.dash {let Some(dashed)=p.dash(dash,scale)else{return Ok(false)};p=dashed;}
            let Some(outline)=p.stroke(&Stroke{dash:None,..style},scale)else{return Ok(false)};
            p=outline.transform(transform).ok_or("Invalid stroke outline")?;
        }
        Ok(contains(&p,x as f64,y as f64,!is_stroke&&r["evenodd"].as_bool().unwrap_or(false)))
    }
    fn command(&mut self,r:&Value) -> Result<Value,String> {
        let op=r["method"].as_str().ok_or("Missing drawing command")?;
        let args=r["args"].as_array().ok_or("Missing drawing arguments")?;
        // Web canvas ignores non-finite geometry rather than introducing invalid native paths.
        if args.iter().filter(|v|v.is_number()).any(|v|v.as_f64().is_none_or(|n|!n.is_finite()||!(n as f32).is_finite())) {return Ok(if op=="isPointInPath"||op=="isPointInStroke"{json!(false)}else{Value::Null});}
        let f=|i:usize|args.get(i).and_then(Value::as_f64).unwrap_or(0.)as f32;
        match op {
            "save"=>self.saved.push(self.state.clone()), "restore"=>{if let Some(s)=self.saved.pop(){self.state=s;}},
            "reset"=>{self.pixels.fill(0);self.path.clear();self.saved.clear();self.state=State::default();},
            "drawState"=>{let alpha=number(r,"alpha",1.);if alpha.is_finite()&&(0. ..=1.).contains(&alpha){self.state.alpha=alpha;}let composite=r["composite"].as_str().unwrap_or("source-over");canvas::blend_mode(composite)?;self.state.composite=composite.into();self.state.smooth=r["smooth"].as_bool().unwrap_or(true);},
            "setTransform"=>self.state.transform=[f(0),f(1),f(2),f(3),f(4),f(5)],
            "transform"=>{let m=canvas::matrix(self.state.transform).pre_concat(canvas::matrix([f(0),f(1),f(2),f(3),f(4),f(5)]));self.state.transform=[m.sx,m.ky,m.kx,m.sy,m.tx,m.ty];},
            "resetTransform"=>self.state.transform=canvas::identity(),"beginPath"=>self.path.clear(),"closePath"=>{if !self.path.is_empty(){self.path.push(PathCommand::Z);}},
            "moveTo"=>self.move_to(f(0),f(1)),"lineTo"=>self.line_to(f(0),f(1)),
            "quadraticCurveTo"=>{if self.path.is_empty(){self.move_to(f(0),f(1));}let(x1,y1)=mapped(self.state.transform,f(0),f(1));let(x,y)=mapped(self.state.transform,f(2),f(3));self.path.push(PathCommand::Q{x1,y1,x,y});},
            "bezierCurveTo"=>{if self.path.is_empty(){self.move_to(f(0),f(1));}let(x1,y1)=mapped(self.state.transform,f(0),f(1));let(x2,y2)=mapped(self.state.transform,f(2),f(3));let(x,y)=mapped(self.state.transform,f(4),f(5));self.path.push(PathCommand::C{x1,y1,x2,y2,x,y});},
            "rect"=>self.rect_path(f(0),f(1),f(2),f(3)),
            "arc"=>self.arc(f(0),f(1),f(2),f(2),0.,f(3),f(4),args.get(5).and_then(Value::as_bool).unwrap_or(false))?,
            "ellipse"=>self.arc(f(0),f(1),f(2),f(3),f(4),f(5),f(6),args.get(7).and_then(Value::as_bool).unwrap_or(false))?,
            "arcTo"=>{
                let radius=f(4);if radius<0.{return Err("Negative arc radius".into());}
                let Some(current)=self.current()else{self.move_to(f(0),f(1));return Ok(Value::Null)};
                let Some(inv)=canvas::matrix(self.state.transform).invert()else{return Ok(Value::Null)};
                let (x0,y0)=mapped([inv.sx,inv.ky,inv.kx,inv.sy,inv.tx,inv.ty],current.0,current.1);
                let (x1,y1,x2,y2)=(f(0),f(1),f(2),f(3));let (ux,uy)=(x0-x1,y0-y1);let (vx,vy)=(x2-x1,y2-y1);let ul=ux.hypot(uy);let vl=vx.hypot(vy);
                if ul==0.||vl==0.||radius==0.||(ux*vy-uy*vx).abs()<f32::EPSILON*ul*vl {self.line_to(x1,y1);}else{
                    let (ux,uy,vx,vy)=(ux/ul,uy/ul,vx/vl,vy/vl);let angle=(ux*vx+uy*vy).clamp(-1.,1.).acos();let tangent=radius/(angle/2.).tan();let (ax,ay)=(x1+ux*tangent,y1+uy*tangent);let (bx,by)=(x1+vx*tangent,y1+vy*tangent);let cross=ux*vy-uy*vx;let side=if cross<0.{-1.}else{1.};let (cx,cy)=(ax-uy*radius*side,ay+ux*radius*side);
                    self.line_to(ax,ay);self.arc(cx,cy,radius,radius,0.,(ay-cy).atan2(ax-cx),(by-cy).atan2(bx-cx),cross>0.)?;
                }
            },
            "roundRect"=>{
                let mut radii:[f32;4]=if r["radii"].is_null(){[0.;4]}else{serde_json::from_value(r["radii"].clone()).map_err(|e|e.to_string())?};
                if radii.iter().any(|v|!v.is_finite()||*v<0.){return Err("Invalid roundRect radius".into());}
                let (mut x,mut y,mut w,mut h)=(f(0),f(1),f(2),f(3));
                if w<0.{x+=w;w= -w;radii.swap(0,1);radii.swap(3,2);}
                if h<0.{y+=h;h= -h;radii.swap(0,3);radii.swap(1,2);}
                let mut scale=1f32;
                for (extent,sum) in [(w,radii[0]+radii[1]),(w,radii[3]+radii[2]),(h,radii[0]+radii[3]),(h,radii[1]+radii[2])]{if sum>0.{scale=scale.min(extent/sum);}}
                for radius in &mut radii{*radius*=scale;}
                let [tl,tr,br,bl]=radii;let half=std::f32::consts::FRAC_PI_2;
                self.move_to(x+tl,y);self.line_to(x+w-tr,y);self.arc(x+w-tr,y+tr,tr,tr,0.,-half,0.,false)?;
                self.line_to(x+w,y+h-br);self.arc(x+w-br,y+h-br,br,br,0.,0.,half,false)?;
                self.line_to(x+bl,y+h);self.arc(x+bl,y+h-bl,bl,bl,0.,half,2.*half,false)?;
                self.line_to(x,y+tl);self.arc(x+tl,y+tl,tl,tl,0.,2.*half,3.*half,false)?;
                self.path.push(PathCommand::Z);self.move_to(x+tl,y);
            },
            "fillRect"|"clearRect"|"strokeRect"=>{let saved=std::mem::take(&mut self.path);self.rect_path(f(0),f(1),f(2),f(3));let path=std::mem::replace(&mut self.path,saved);self.paint(r,if op=="clearRect"{"clear"}else if op=="strokeRect"{"stroke"}else{"fill"},path)?;},
            "fill"|"stroke"=>{let path=self.selected_path(r)?;self.paint(r,op,path)?;},
            "clip"=>{let path=self.selected_path(r)?;self.state.clips.push(ClipPath{path,evenodd:r["evenodd"].as_bool().unwrap_or(false)});},
            "isPointInPath"|"isPointInStroke"=>return Ok(json!(self.hit(r,f(0),f(1),op=="isPointInStroke")?)),
            "fillText"|"strokeText"|"measureText"=>{
                let mut request:TextRequest=serde_json::from_value(r["text"].clone()).map_err(|e|e.to_string())?;
                if op=="measureText"{return serde_json::to_value(self.text.measure(&request)?).map_err(|e|e.to_string());}
                if self.width==0||self.height==0{return Ok(Value::Null);}
                if op=="strokeText"{request.stroke_width=number(r,"line_width",1.);}
                request.transform=self.state.transform;request.clips=Vec::new();request.composite="source-over".into();
                let req=self.request(r,"fill",Vec::new())?;
                if req.paint.kind.is_none()&&r["pattern"].is_null() {
                    request.color=req.paint.color;
                    if req.composite=="source-over" {
                        request.color[3]=(request.color[3]as f32*req.alpha.clamp(0.,1.)).round()as u8;
                        request.clips=self.state.clips.clone();self.text.draw(&request,self.width,self.height,&mut self.pixels)?;
                    }else{
                        let mut layer=vec![0;self.pixels.len()];self.text.draw(&request,self.width,self.height,&mut layer)?;
                        self.composite_layer(&layer,req.alpha,&req.composite)?;
                    }
                }else{
                    request.color=[255;4];let mut layer=vec![0;self.pixels.len()];self.text.draw(&request,self.width,self.height,&mut layer)?;
                    let mut paint=if !r["pattern"].is_null()||req.paint.kind.as_deref()==Some("conic"){self.paint_pixels(r)?}else{
                        let mut target=vec![0;self.pixels.len()];let mut full=self.request(r,"fill",vec![PathCommand::M{x:0.,y:0.},PathCommand::L{x:self.width as f32,y:0.},PathCommand::L{x:self.width as f32,y:self.height as f32},PathCommand::L{x:0.,y:self.height as f32},PathCommand::Z])?;full.alpha=1.;full.clips.clear();full.composite="copy".into();
                        if transparent_gradient(&full.paint){target}else{canvas::draw(&full,&mut target)?;target}
                    };
                    for (p,mask) in paint.chunks_exact_mut(4).zip(layer.chunks_exact(4)){p[3]=((p[3]as u16*mask[3]as u16+127)/255)as u8;}
                    self.composite_layer(&paint,req.alpha,&req.composite)?;
                }
            },
            _=>return Err(format!("Unsupported Canvas drawing operation {op}")),
        } Ok(Value::Null)
    }
}

fn unbounded(composite:&str)->bool{matches!(composite,"source-in"|"source-out"|"destination-in"|"destination-atop"|"copy")}
fn transparent_gradient(p:&CanvasPaint)->bool{
    p.kind.is_some()&&(p.stops.is_empty()||canvas::matrix(p.transform).invert().is_none()
        ||(p.kind.as_deref()==Some("linear")&&p.points[0..2]==p.points[2..4])
        ||(p.kind.as_deref()==Some("radial")&&p.points[0..3]==p.points[3..6]))
}
// Flatten only for winding tests. Raster and stroke outlines use the shared exact curves.
fn contains(path:&Path,x:f64,y:f64,evenodd:bool)->bool {
    fn edge(a:(f64,f64),b:(f64,f64),x:f64,y:f64,w:&mut i32,boundary:&mut bool){
        let cross=(b.0-a.0)*(y-a.1)-(b.1-a.1)*(x-a.0);
        let tolerance=1e-7*((b.0-a.0).abs()+(b.1-a.1).abs()).max(1.);
        if cross.abs()<=tolerance&&x>=a.0.min(b.0)-1e-7&&x<=a.0.max(b.0)+1e-7&&y>=a.1.min(b.1)-1e-7&&y<=a.1.max(b.1)+1e-7{*boundary=true;}
        if a.1<=y {if b.1>y&&cross>0.{*w+=1;}}else if b.1<=y&&cross<0.{*w-=1;}
    }
    fn curve(p:[(f64,f64);4],depth:u8,x:f64,y:f64,w:&mut i32,b:&mut bool){
        let dx=p[3].0-p[0].0;let dy=p[3].1-p[0].1;
        let flat=p[1..3].iter().all(|q|((q.0-p[0].0)*dy-(q.1-p[0].1)*dx).abs()<=0.00001*dx.hypot(dy).max(0.00001))
            &&p[1..3].iter().all(|q|q.0>=p[0].0.min(p[3].0)-0.00001&&q.0<=p[0].0.max(p[3].0)+0.00001&&q.1>=p[0].1.min(p[3].1)-0.00001&&q.1<=p[0].1.max(p[3].1)+0.00001);
        if depth==20||flat{edge(p[0],p[3],x,y,w,b);return;}
        let mid=|a:(f64,f64),b:(f64,f64)|((a.0+b.0)*0.5,(a.1+b.1)*0.5);
        let a=mid(p[0],p[1]);let c=mid(p[1],p[2]);let d=mid(p[2],p[3]);let e=mid(a,c);let f=mid(c,d);let g=mid(e,f);
        curve([p[0],a,e,g],depth+1,x,y,w,b);curve([g,f,d,p[3]],depth+1,x,y,w,b);
    }
    let point=|p:tiny_skia::Point|(p.x as f64,p.y as f64);
    let mut current=(0.,0.);let mut start=None;let mut winding=0;let mut boundary=false;
    for seg in path.segments(){match seg{
        PathSegment::MoveTo(p)=>{if let Some(s)=start{edge(current,s,x,y,&mut winding,&mut boundary);}current=point(p);start=Some(current);},
        PathSegment::LineTo(p)=>{let end=point(p);edge(current,end,x,y,&mut winding,&mut boundary);current=end;},
        PathSegment::QuadTo(a,b)=>{let a=point(a);let end=point(b);let c1=(current.0+(a.0-current.0)*2./3.,current.1+(a.1-current.1)*2./3.);let c2=(end.0+(a.0-end.0)*2./3.,end.1+(a.1-end.1)*2./3.);curve([current,c1,c2,end],0,x,y,&mut winding,&mut boundary);current=end;},
        PathSegment::CubicTo(a,b,c)=>{let end=point(c);curve([current,point(a),point(b),end],0,x,y,&mut winding,&mut boundary);current=end;},
        PathSegment::Close=>{if let Some(s)=start{edge(current,s,x,y,&mut winding,&mut boundary);current=s;start=None;}},
    }}
    if let Some(s)=start{edge(current,s,x,y,&mut winding,&mut boundary);}
    boundary||if evenodd{winding%2!=0}else{winding!=0}
}
fn buffer_len(width:u32,height:u32)->Option<usize>{(width as usize).checked_mul(height as usize)?.checked_mul(4).filter(|n|*n<=268435456)}
fn surface_len(width:u32,height:u32)->Option<usize>{if width>16384||height>16384{return None;}buffer_len(width,height)}
unsafe fn bytes<'a>(data:*const u8,len:usize)->Option<&'a[u8]>{if len==0{Some(&[])}else if data.is_null(){None}else{Some(std::slice::from_raw_parts(data,len))}}
#[no_mangle]
pub unsafe extern "C" fn lp_canvas_new(width:u32,height:u32,fonts:*const u8,len:usize)->*mut Canvas {
    catch_unwind(AssertUnwindSafe(||{let length=surface_len(width,height)?;let fonts:Vec<PathBuf>=serde_json::from_slice(bytes(fonts,len)?).ok()?;
        Some(Box::into_raw(Box::new(Canvas{width,height,pixels:vec![0;length],path:Vec::new(),state:State::default(),saved:Vec::new(),text:CanvasTextEngine::from_font_files(&fonts).ok()?})))
    })).ok().flatten().unwrap_or(std::ptr::null_mut())
}
#[no_mangle]
pub unsafe extern "C" fn lp_canvas_free(value:*mut Canvas){if !value.is_null(){drop(Box::from_raw(value));}}
#[no_mangle]
pub unsafe extern "C" fn lp_canvas_command(value:*mut Canvas,data:*const u8,len:usize,context:*mut c_void,write:Write)->bool {
    catch_unwind(AssertUnwindSafe(||{let Some(value)=value.as_mut()else{return false};let Some(data)=bytes(data,len)else{return false};let Ok(request)=serde_json::from_slice(data)else{return false};let Ok(result)=value.command(&request)else{return false};let Ok(data)=serde_json::to_vec(&result)else{return false};write(context,data.as_ptr(),data.len())})).unwrap_or(false)
}
#[no_mangle]
pub unsafe extern "C" fn lp_canvas_pixels(value:*const Canvas,out:*mut u8,len:usize)->bool {
    let Some(value)=value.as_ref()else{return false};if len!=value.pixels.len(){return false;}if len==0{return true;}if out.is_null(){return false;}std::slice::from_raw_parts_mut(out,len).copy_from_slice(&value.pixels);true
}
#[no_mangle]
pub unsafe extern "C" fn lp_canvas_put(value:*mut Canvas,data:*const u8,width:u32,height:u32,x:i32,y:i32)->bool {
    let Some(value)=value.as_mut()else{return false};let Some(len)=buffer_len(width,height)else{return false};let Some(data)=bytes(data,len)else{return false};
    let sx0=(-(x as i64)).max(0).min(width as i64)as usize;let sy0=(-(y as i64)).max(0).min(height as i64)as usize;
    let sx1=(value.width as i64-x as i64).max(0).min(width as i64)as usize;let sy1=(value.height as i64-y as i64).max(0).min(height as i64)as usize;
    if sx1<=sx0||sy1<=sy0{return true;}let row_bytes=(sx1-sx0)*4;
    for sy in sy0..sy1{let src=(sy*width as usize+sx0)*4;let dst=((y as i64+sy as i64)as usize*value.width as usize+(x as i64+sx0 as i64)as usize)*4;value.pixels[dst..dst+row_bytes].copy_from_slice(&data[src..src+row_bytes]);}true
}
#[no_mangle]
pub unsafe extern "C" fn lp_canvas_png(value:*const Canvas,context:*mut c_void,write:Write)->bool {
    let Some(value)=value.as_ref()else{return false};lp_rgba_png(value.pixels.as_ptr(),value.width,value.height,context,write)
}
#[no_mangle]
pub unsafe extern "C" fn lp_canvas_draw_pixels(value:*mut Canvas,data:*const u8,width:u32,height:u32,coords:*const f64)->bool {
    catch_unwind(AssertUnwindSafe(||{let Some(value)=value.as_mut()else{return false};let Some(len)=buffer_len(width,height)else{return false};let Some(data)=bytes(data,len)else{return false};if coords.is_null(){return false;}let coords=std::slice::from_raw_parts(coords,8);value.draw_pixels(data,width,height,std::array::from_fn(|i|coords[i]as f32)).is_ok()})).unwrap_or(false)
}
#[no_mangle]
pub unsafe extern "C" fn lp_canvas_draw_encoded(value:*mut Canvas,data:*const u8,len:usize,coords:*const f64)->bool {
    catch_unwind(AssertUnwindSafe(||{let Some(value)=value.as_mut()else{return false};let Some(data)=bytes(data,len)else{return false};if coords.is_null(){return false;}let Ok(image)=::image::load_from_memory(data)else{return false};let image=image.to_rgba8();let coords=std::slice::from_raw_parts(coords,8);value.draw_pixels(image.as_raw(),image.width(),image.height(),std::array::from_fn(|i|coords[i]as f32)).is_ok()})).unwrap_or(false)
}
#[no_mangle]
pub unsafe extern "C" fn lp_canvas_resize(value:*mut Canvas,width:u32,height:u32)->bool {
    let Some(value)=value.as_mut()else{return false};let Some(length)=surface_len(width,height)else{return false};
    value.width=width;value.height=height;value.pixels.resize(length,0);value.pixels.fill(0);value.path.clear();value.saved.clear();value.state=State::default();true
}
#[no_mangle]
pub unsafe extern "C" fn lp_rgba_png(data:*const u8,width:u32,height:u32,context:*mut c_void,write:Write)->bool {
    let Some(length)=buffer_len(width,height)else{return false};if width==0||height==0{return false;}let Some(data)=bytes(data,length)else{return false};let mut output=Vec::new();
    {let mut encoder=png::Encoder::new(&mut output,width,height);encoder.set_color(png::ColorType::Rgba);encoder.set_depth(png::BitDepth::Eight);let Ok(mut encoder)=encoder.write_header()else{return false};if encoder.write_image_data(data).is_err(){return false;}}
    write(context,output.as_ptr(),output.len())
}
