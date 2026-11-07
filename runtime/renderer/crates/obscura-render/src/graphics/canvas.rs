//! Native Canvas paths, clipping, affine transforms and paint on the shared surface.
use serde::Deserialize;
use tiny_skia::{BlendMode,Color,FillRule,GradientStop,LineCap,LineJoin,LinearGradient,Mask,Paint,Path,PathBuilder,PixmapMut,Point,RadialGradient,SpreadMode,Stroke,StrokeDash,Transform};
#[derive(Clone,Copy,Deserialize)]
#[serde(tag="t")]
pub enum PathCommand {
    M{x:f32,y:f32}, L{x:f32,y:f32}, C{x1:f32,y1:f32,x2:f32,y2:f32,x:f32,y:f32}, Q{x1:f32,y1:f32,x:f32,y:f32}, Z,
}
#[derive(Clone,Deserialize)]
pub struct ClipPath {pub path:Vec<PathCommand>, #[serde(default)] pub evenodd:bool}
#[derive(Deserialize)]
pub struct ColorStop {pub offset:f32,pub color:[u8;4]}
#[derive(Deserialize)]
pub struct CanvasPaint {
    pub color:[u8;4], pub kind:Option<String>, #[serde(default)] pub stops:Vec<ColorStop>,
    #[serde(default)] pub points:[f32;6], #[serde(default="identity")] pub transform:[f32;6],
}
pub fn identity()->[f32;6] {[1.,0.,0.,1.,0.,0.]}
pub fn matrix(m:[f32;6])->Transform {Transform::from_row(m[0],m[1],m[2],m[3],m[4],m[5])}
pub fn path(commands:&[PathCommand])->Option<Path>{
    let mut p=PathBuilder::new();
    for c in commands {match *c {
        PathCommand::M{x,y}=>p.move_to(x,y),PathCommand::L{x,y}=>p.line_to(x,y),
        PathCommand::C{x1,y1,x2,y2,x,y}=>p.cubic_to(x1,y1,x2,y2,x,y),
        PathCommand::Q{x1,y1,x,y}=>p.quad_to(x1,y1,x,y),PathCommand::Z=>p.close(),
    }}
    p.finish()
}
pub fn clip_mask(clips:&[ClipPath],width:u32,height:u32)->Result<Option<Mask>,String>{
    if clips.is_empty(){return Ok(None);}
    let mut mask=Mask::new(width,height).ok_or("Invalid Canvas clip dimensions")?;
    for (i,clip) in clips.iter().enumerate(){
        let Some(p)=path(&clip.path) else {mask.data_mut().fill(0);return Ok(Some(mask));};
        let rule=if clip.evenodd{FillRule::EvenOdd}else{FillRule::Winding};
        if i==0{mask.fill_path(&p,rule,true,Transform::identity());}else{mask.intersect_path(&p,rule,true,Transform::identity());}
    }
    Ok(Some(mask))
}
pub fn blend_mode(name:&str)->Result<BlendMode,String>{Ok(match name {
    "source-over"=>BlendMode::SourceOver,"source-in"=>BlendMode::SourceIn,"source-out"=>BlendMode::SourceOut,"source-atop"=>BlendMode::SourceAtop,
    "destination-over"=>BlendMode::DestinationOver,"destination-in"=>BlendMode::DestinationIn,"destination-out"=>BlendMode::DestinationOut,"destination-atop"=>BlendMode::DestinationAtop,
    "lighter"=>BlendMode::Plus,"copy"=>BlendMode::Source,"xor"=>BlendMode::Xor,"multiply"=>BlendMode::Multiply,"screen"=>BlendMode::Screen,
    "overlay"=>BlendMode::Overlay,"darken"=>BlendMode::Darken,"lighten"=>BlendMode::Lighten,"color-dodge"=>BlendMode::ColorDodge,"color-burn"=>BlendMode::ColorBurn,
    "hard-light"=>BlendMode::HardLight,"soft-light"=>BlendMode::SoftLight,"difference"=>BlendMode::Difference,"exclusion"=>BlendMode::Exclusion,
    "hue"=>BlendMode::Hue,"saturation"=>BlendMode::Saturation,"color"=>BlendMode::Color,"luminosity"=>BlendMode::Luminosity,
    _=>return Err(format!("Unsupported Canvas composite operation: {name}")),
})}
pub fn premultiply(pixels:&mut[u8]){for p in pixels.chunks_exact_mut(4){for c in 0..3{p[c]=((p[c] as u16*p[3] as u16+127)/255) as u8;}}}
pub fn unpremultiply(pixels:&mut[u8]){for p in pixels.chunks_exact_mut(4){if p[3]!=0{for c in 0..3{p[c]=((p[c] as u32*255+p[3] as u32/2)/p[3] as u32).min(255) as u8;}}}}
#[derive(Deserialize)]
pub struct CanvasDrawRequest {
    pub width:u32,pub height:u32,pub path:Vec<PathCommand>,pub kind:String,pub paint:CanvasPaint,
    pub alpha:f32,pub composite:String,pub transform:[f32;6],#[serde(default)]pub clips:Vec<ClipPath>,
    #[serde(default)]pub evenodd:bool,#[serde(default="one")]pub line_width:f32,
    #[serde(default)]pub line_cap:String,#[serde(default)]pub line_join:String,#[serde(default="ten")]pub miter_limit:f32,
    #[serde(default)]pub dash:Vec<f32>,#[serde(default)]pub dash_offset:f32,
}
fn one()->f32{1.} fn ten()->f32{10.}
pub fn draw(req:&CanvasDrawRequest,pixels:&mut[u8])->Result<(),String>{
    if req.width>32767||req.height>32767||pixels.len()!=req.width as usize*req.height as usize*4{return Err("Canvas surface byte length mismatch".into());}
    if req.width==0||req.height==0{return Ok(());}
    let Some(mut p)=path(&req.path)else{return Ok(());};
    let mask=clip_mask(&req.clips,req.width,req.height)?;
    let clear=req.kind=="clear";
    let mut paint=Paint::default();paint.anti_alias=true;
    paint.blend_mode=if clear{BlendMode::Clear}else{blend_mode(&req.composite)?};
    let color=|c:[u8;4]|Color::from_rgba8(c[0],c[1],c[2],if clear{255}else{(c[3] as f32*req.alpha.clamp(0.,1.)).round() as u8});
    paint.set_color(color(req.paint.color));
    if !clear {if let Some(kind)=req.paint.kind.as_deref(){
        let stops=req.paint.stops.iter().map(|s|GradientStop::new(s.offset,color(s.color))).collect();
        let q=req.paint.points;let transform=matrix(req.paint.transform);
        paint.shader=match kind {
            "linear"=>LinearGradient::new(Point::from_xy(q[0],q[1]),Point::from_xy(q[2],q[3]),stops,SpreadMode::Pad,transform),
            "radial"=>RadialGradient::new(Point::from_xy(q[0],q[1]),q[2],Point::from_xy(q[3],q[4]),q[5],stops,SpreadMode::Pad,transform),
            _=>return Err(format!("Unsupported Canvas paint: {kind}")),
        }.ok_or("Invalid Canvas gradient")?;
    }}
    let mut transform=Transform::identity();
    let stroke=if req.kind=="stroke"{
        transform=matrix(req.transform);
        let Some(inverse)=transform.invert()else{return Ok(());};
        p=p.transform(inverse).ok_or("Invalid Canvas stroke transform")?;
        // The gradient lives in device space; the stroke painter applies its CTM.
        paint.shader.transform(inverse);
        Some(Stroke {width:req.line_width,miter_limit:req.miter_limit,
            line_cap:match req.line_cap.as_str(){"round"=>LineCap::Round,"square"=>LineCap::Square,_=>LineCap::Butt},
            line_join:match req.line_join.as_str(){"round"=>LineJoin::Round,"bevel"=>LineJoin::Bevel,_=>LineJoin::Miter},
            dash:if req.dash.is_empty(){None}else{Some(StrokeDash::new(req.dash.clone(),req.dash_offset).ok_or("Invalid Canvas line dash")?)},
        })
    }else{None};
    premultiply(pixels);
    {
        let mut target=PixmapMut::from_bytes(pixels,req.width,req.height).ok_or("Invalid Canvas surface")?;
        if let Some(stroke)=stroke{target.stroke_path(&p,&paint,&stroke,transform,mask.as_ref());}
        else{target.fill_path(&p,&paint,if req.evenodd{FillRule::EvenOdd}else{FillRule::Winding},Transform::identity(),mask.as_ref());}
    }
    unpremultiply(pixels);
    Ok(())
}
