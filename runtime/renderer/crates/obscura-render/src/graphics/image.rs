//! ImageData-to-surface compositing with the same CTM, clip and blend rules as paths.
use serde::Deserialize;
use tiny_skia::{FilterQuality,Paint,PathBuilder,Pattern,Pixmap,PixmapMut,Rect,SpreadMode,Transform};
use super::canvas;
#[derive(Deserialize)]
pub struct ImageDrawRequest {
    pub width:u32,pub height:u32,pub source_width:u32,pub source_height:u32,
    pub source_rect:[f32;4],pub dest_rect:[f32;4],pub transform:[f32;6],
    #[serde(default)]pub clips:Vec<canvas::ClipPath>,pub alpha:f32,pub composite:String,pub smooth:bool,
}
pub fn draw(req:&ImageDrawRequest,source:&[u8],pixels:&mut[u8])->Result<(),String>{
    if req.width>32767||req.height>32767||req.source_width>32767||req.source_height>32767
        ||pixels.len()!=req.width as usize*req.height as usize*4
        ||source.len()!=req.source_width as usize*req.source_height as usize*4{return Err("Invalid Canvas image surface".into());}
    if req.width==0||req.height==0||req.source_width==0||req.source_height==0{return Ok(());}
    let [sx,sy,sw,sh]=req.source_rect;let [dx,dy,dw,dh]=req.dest_rect;
    if ![sx,sy,sw,sh,dx,dy,dw,dh].iter().all(|v|v.is_finite())||sw<=0.||sh<=0.||dw<=0.||dh<=0.{return Ok(());}
    let rect=Rect::from_xywh(dx,dy,dw,dh).ok_or("Invalid Canvas image destination")?;
    let path=PathBuilder::from_rect(rect);
    let mask=canvas::clip_mask(&req.clips,req.width,req.height)?;
    // Source is immutable shared ImageData, so premultiplication requires owned pixels.
    let mut source=source.to_vec();canvas::premultiply(&mut source);
    let source=Pixmap::from_vec(source,tiny_skia::IntSize::from_wh(req.source_width,req.source_height).ok_or("Invalid image size")?).ok_or("Invalid image bytes")?;
    let mut paint=Paint::default();paint.anti_alias=true;paint.blend_mode=canvas::blend_mode(&req.composite)?;
    paint.shader=Pattern::new(source.as_ref(),SpreadMode::Pad,if req.smooth{FilterQuality::Bilinear}else{FilterQuality::Nearest},req.alpha.clamp(0.,1.),Transform::from_row(dw/sw,0.,0.,dh/sh,dx-sx*dw/sw,dy-sy*dh/sh));
    canvas::premultiply(pixels);
    {
        let mut target=PixmapMut::from_bytes(pixels,req.width,req.height).ok_or("Invalid Canvas surface")?;
        target.fill_path(&path,&paint,tiny_skia::FillRule::Winding,canvas::matrix(req.transform),mask.as_ref());
    }
    canvas::unpremultiply(pixels);
    Ok(())
}
