//! Canvas text uses the same cosmic-text/Swash stack as CSS, on explicit font files.
//! Pixels cross the public boundary as straight-alpha sRGB RGBA, as ImageData requires.
use std::{path::PathBuf, sync::Arc};
use cosmic_text::{fontdb, Attrs, Buffer, Family, FontSystem, Metrics, Shaping, Style, SwashCache, Weight, Wrap};
use serde::{Deserialize, Serialize};
use sha2::{Digest,Sha256};
use swash::scale::{image::Content, Render, ScaleContext, Source};
use tiny_skia::{Pixmap, PixmapMut, PixmapPaint, Transform};

pub mod canvas;
pub mod image;
pub fn parse_color(css:&str)->Option<[u8;4]>{crate::style::parse_color(css)}
pub fn font_locale()->String {FONT_LOCALE.get_or_init(||std::env::var("OBSCURA_ENVIRONMENT_LOCALE").unwrap_or_else(|_|"en-US".into())).clone()}
pub fn validate_gl_backend(value:&str)->Result<&'static str,String>{match value{
    "cgl-default"=>Ok("cgl-default"),"cgl-accelerated"=>Ok("cgl-accelerated"),"cgl-generic-float"=>Ok("cgl-generic-float"),
    _=>Err(format!("Unsupported native GL backend: {value}")),
}}
pub fn configured_gl_backend()->Result<&'static str,String>{match std::env::var("OBSCURA_ENVIRONMENT_GL_BACKEND"){
    Ok(value)=>validate_gl_backend(&value),Err(std::env::VarError::NotPresent)=>Ok("cgl-default"),Err(e)=>Err(e.to_string()),
}}
#[cfg(target_os = "macos")]
mod gl;
#[cfg(target_os = "macos")]
pub use gl::GlContext;
#[cfg(not(target_os = "macos"))]
pub struct GlContext;
#[cfg(not(target_os = "macos"))]
impl GlContext {
    pub fn new(_: u32, _: u32) -> Result<Self, String> { Err("Native GL requires macOS CGL/OpenGL".into()) }
    pub fn new_with_backend(_:u32,_:u32,_:&str)->Result<Self,String>{Err("Native GL requires macOS CGL/OpenGL".into())}
    pub fn backend(&self)->&'static str{"unavailable"}
    pub fn command(&mut self, _: &serde_json::Value) -> Result<serde_json::Value, String> { Err("Native GL unavailable".into()) }
    pub fn readback(&mut self, _: &mut [u8]) -> Result<(), String> { Err("Native GL unavailable".into()) }
    pub fn upload_texture_2d(&mut self,_:u32,_:i32,_:i32,_:i32,_:i32,_:i32,_:u32,_:u32,_:Option<&[u8]>)->Result<(),String>{Err("Native GL unavailable".into())}
    pub fn upload_texture_sub_2d(&mut self,_:u32,_:i32,_:i32,_:i32,_:i32,_:i32,_:u32,_:u32,_:&[u8])->Result<(),String>{Err("Native GL unavailable".into())}
    pub fn read_pixels(&mut self,_:i32,_:i32,_:i32,_:i32,_:u32,_:u32,_:&mut[u8])->Result<(),String>{Err("Native GL unavailable".into())}
    pub fn clear_bitmap(&mut self)->Result<(),String>{Err("Native GL unavailable".into())}
    pub fn resize(&mut self,_:u32,_:u32)->Result<(),String>{Err("Native GL unavailable".into())}
}

static FONT_FILES: std::sync::OnceLock<Vec<PathBuf>> = std::sync::OnceLock::new();
static FONT_FAMILIES:std::sync::OnceLock<Vec<String>>=std::sync::OnceLock::new();
static FONT_LOCALE:std::sync::OnceLock<String>=std::sync::OnceLock::new();
pub fn configure_font_locale(locale:&str)->Result<(),String>{
    if locale.is_empty(){return Err("Font shaping locale must not be empty".into());}
    if FONT_LOCALE.get().is_some_and(|value|value==locale){return Ok(());}
    FONT_LOCALE.set(locale.into()).map_err(|_|"Font shaping locale has already been initialized".into())
}
pub fn configure_font_families(families:&[String])->Result<(),String>{
    let mut families:Vec<_>=families.iter().map(|name|name.to_lowercase()).collect();families.sort();families.dedup();
    if let Some(frozen)=FROZEN_FONTS.get(){let frozen=frozen.as_ref().map_err(Clone::clone)?;if frozen.families!=families{return Err("Font family selection has already been initialized".into());}}
    if FONT_FAMILIES.get().is_some_and(|value|value==&families){return Ok(());}
    FONT_FAMILIES.set(families).map_err(|_|"Font family selection has already been initialized".into())
}
/// Embedders must call this before any native text/CSS operation.
pub fn configure_fonts(files:&[PathBuf],families:&[String],locale:&str)->Result<(),String>{
    configure_font_families(families)?;configure_font_locale(locale)?;configure_font_files(files)
}
/// Fixes the exact CSS font asset list before the first renderer preparation.
pub fn configure_font_files(files: &[PathBuf]) -> Result<(),String> {
    if FONT_FILES.get().is_some_and(|current| current == files) { return Ok(()); }
    if crate::inline::font_database_initialized() { return Err("CSS font database was initialized before explicit assets".into()); }
    frozen_fonts(files)?;
    FONT_FILES.set(files.to_vec()).map_err(|_| "CSS font files have already been configured".into())
}
pub fn explicit_font_files() -> Result<Option<Vec<PathBuf>>,String> {
    if let Some(files)=FONT_FILES.get() { return Ok(Some(files.clone())); }
    match std::env::var("OBSCURA_ENVIRONMENT_FONT_FILES") {
        Ok(files)=>serde_json::from_str(&files).map(Some).map_err(|e|format!("Invalid explicit font file list: {e}")),
        Err(std::env::VarError::NotPresent)=>Ok(None),
        Err(e)=>Err(e.to_string()),
    }
}
pub fn explicit_fonts_active()->bool { FONT_FILES.get().is_some() || std::env::var_os("OBSCURA_ENVIRONMENT_FONT_FILES").is_some() }
struct FrozenFonts {files:Vec<PathBuf>,families:Vec<String>,database:fontdb::Database,sha256:Vec<String>}
static FROZEN_FONTS:std::sync::OnceLock<Result<FrozenFonts,String>>=std::sync::OnceLock::new();
fn frozen_fonts(files:&[PathBuf])->Result<&'static FrozenFonts,String>{
    let mut key=files.to_vec();key.sort();key.dedup();
    let mut families:Vec<String>=if let Some(families)=FONT_FAMILIES.get(){families.clone()}else{match std::env::var("OBSCURA_ENVIRONMENT_FONT_FAMILIES"){
        Ok(value)=>serde_json::from_str(&value).map_err(|e|format!("Invalid environment font family list: {e}"))?,
        Err(std::env::VarError::NotPresent)=>Vec::new(),Err(e)=>return Err(e.to_string()),
    }};
    for name in &mut families{*name=name.to_lowercase();}
    families.sort();families.dedup();
    let entry=FROZEN_FONTS.get_or_init(||load_font_database(&key,&families)
        .map(|(database,sha256)|FrozenFonts{files:key.clone(),families:families.clone(),database,sha256}));
    let fonts=entry.as_ref().map_err(Clone::clone)?;
    if fonts.files!=key||fonts.families!=families{return Err("Font asset/family configuration changed after native initialization".into());}
    Ok(fonts)
}
/// The immutable database owns the exact bytes observed by preflight. Clones
/// share font byte Arcs; no later page or Canvas call reopens the asset files.
pub fn font_database(files:&[PathBuf])->Result<fontdb::Database,String>{Ok(frozen_fonts(files)?.database.clone())}
pub fn font_assets_sha256(files:&[PathBuf])->Result<Vec<String>,String>{Ok(frozen_fonts(files)?.sha256.clone())}
pub fn font_families(files:&[PathBuf])->Result<Vec<String>,String>{
    let mut names:Vec<_>=frozen_fonts(files)?.database.faces().flat_map(|f|f.families.iter().map(|f|f.0.clone())).collect();
    names.sort();names.dedup();Ok(names)
}
fn load_font_database(files:&[PathBuf],allowed:&[String])->Result<(fontdb::Database,Vec<String>),String>{
    if files.is_empty() { return Err("asset_paths.fonts must contain explicit font files".into()); }
    let mut db = fontdb::Database::new();
    let mut assets=Vec::with_capacity(files.len());
    for path in files {
        let bytes=std::fs::read(path).map_err(|e|format!("font {}: {e}",path.display()))?;
        let digest:[u8;32]=Sha256::digest(&bytes).into();
        assets.push((digest,bytes));
    }
    assets.sort_unstable_by_key(|asset|asset.0);
    assets.dedup_by(|a,b|a.0==b.0 && a.1==b.1);
    let sha256=assets.iter().map(|(digest,_)|{
        const HEX:&[u8;16]=b"0123456789abcdef";
        let mut hex=String::with_capacity(64);
        for byte in digest{hex.push(HEX[(byte>>4) as usize] as char);hex.push(HEX[(byte&15) as usize] as char);}
        hex
    }).collect();
    for (digest,bytes) in assets {
        if db.load_font_source(fontdb::Source::Binary(Arc::new(bytes))).is_empty() {
            return Err(format!("Pinned font asset {:02x?} contains no supported faces",digest));
        }
    }
    if !allowed.is_empty() {
        for name in allowed {
            if !db.faces().any(|f|f.families.iter().any(|f|f.0.eq_ignore_ascii_case(name))) {
                return Err(format!("Configured font family is absent from pinned assets: {name}"));
            }
        }
        let remove:Vec<_>=db.faces().filter(|f|!f.families.iter().any(|family|allowed.iter().any(|name|family.0.eq_ignore_ascii_case(name)))).map(|f|f.id).collect();
        for id in remove {db.remove_face(id);}
    }
    let named = |names: &[&str]| names.iter().find_map(|name| db.faces().flat_map(|face| &face.families).find(|family| family.0.eq_ignore_ascii_case(name)).map(|family| family.0.clone()));
    let first = db.faces().next().and_then(|face| face.families.first()).map(|family| family.0.as_str()).ok_or("No font faces")?;
    let sans = named(&["Arial", "Helvetica Neue", "Helvetica", "Verdana", "Noto Sans", "DejaVu Sans", "Liberation Sans", "Noto Sans CJK SC"])
        .or_else(|| db.faces().find(|face| !face.monospaced).and_then(|face| face.families.first()).map(|family| family.0.clone())).unwrap_or_else(|| first.to_owned());
    let mono = named(&["Menlo", "Courier New", "Monaco", "DejaVu Sans Mono", "Liberation Mono"])
        .or_else(|| db.faces().find(|face| face.monospaced).and_then(|face| face.families.first()).map(|family| family.0.clone())).unwrap_or_else(|| sans.clone());
    let serif = named(&["Times New Roman", "Times", "Georgia", "Noto Serif", "DejaVu Serif", "Liberation Serif"]).unwrap_or_else(|| sans.clone());
    db.set_sans_serif_family(&sans);
    db.set_serif_family(&serif);
    db.set_monospace_family(&mono);
    Ok((db,sha256))
}

#[derive(Deserialize)]
#[serde(default)]
pub struct TextRequest {
    pub text: String, pub font_size: f32, pub family: String, pub weight: u16,
    pub italic: bool, pub x: f32, pub y: f32, pub align: String, pub baseline: String,
    pub color: [u8; 4], pub max_width: Option<f32>, pub stroke_width: f32, pub composite: String,
    pub transform: [f32;6], pub clips: Vec<canvas::ClipPath>,
}
impl Default for TextRequest {
    fn default() -> Self { Self { text: String::new(), font_size: 10., family: "sans-serif".into(), weight: 400, italic: false, x: 0., y: 0., align: "left".into(), baseline: "alphabetic".into(), color: [0,0,0,255], max_width: None, stroke_width: 0., composite: "source-over".into(), transform: canvas::identity(), clips: Vec::new() } }
}
#[derive(Default, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct TextMetrics {
    pub width: f32, pub actual_bounding_box_left: f32, pub actual_bounding_box_right: f32,
    pub actual_bounding_box_ascent: f32, pub actual_bounding_box_descent: f32,
    pub font_bounding_box_ascent: f32, pub font_bounding_box_descent: f32,
    pub em_height_ascent: f32, pub em_height_descent: f32,
    pub alphabetic_baseline: f32, pub hanging_baseline: f32, pub ideographic_baseline: f32,
}
pub struct CanvasTextEngine { fonts: FontSystem, cache: SwashCache, stroke: ScaleContext }
impl CanvasTextEngine {
    pub fn font_families(&self)->Vec<String> {
        let mut names:Vec<_>=self.fonts.db().faces().flat_map(|f|f.families.iter().map(|f|f.0.clone())).collect();
        names.sort();names.dedup();names
    }
    pub fn from_font_files(files: &[PathBuf]) -> Result<Self, String> {
        Ok(Self { fonts: FontSystem::new_with_locale_and_db(font_locale(), font_database(files)?), cache: SwashCache::new(), stroke: ScaleContext::new() })
    }
    fn shape(&mut self, req: &TextRequest) -> Result<Buffer, String> {
        if !req.font_size.is_finite() || req.font_size <= 0. { return Err("Invalid Canvas font size".into()); }
        let family = match req.family.as_str() { "sans-serif" | "system-ui" => Family::SansSerif, "serif" => Family::Serif, "monospace" => Family::Monospace, name => Family::Name(name) };
        let attrs = Attrs::new().family(family).weight(Weight(req.weight)).style(if req.italic { Style::Italic } else { Style::Normal });
        let mut buffer = Buffer::new(&mut self.fonts, Metrics::new(req.font_size, req.font_size * 1.2));
        buffer.set_wrap(&mut self.fonts, Wrap::None);
        buffer.set_size(&mut self.fonts, None, None);
        let text = if req.text.chars().any(|c| matches!(c, '\n' | '\r' | '\t' | '\u{000C}')) {
            std::borrow::Cow::Owned(req.text.chars().map(|c| if matches!(c, '\n' | '\r' | '\t' | '\u{000C}') { ' ' } else { c }).collect::<String>())
        } else { std::borrow::Cow::Borrowed(req.text.as_str()) };
        buffer.set_text(&mut self.fonts, &text, &attrs, Shaping::Advanced);
        buffer.shape_until_scroll(&mut self.fonts, false);
        Ok(buffer)
    }
    fn metrics(&mut self, buffer: &Buffer, req: &TextRequest) -> TextMetrics {
        let mut m = TextMetrics::default();
        let family = match req.family.as_str() { "sans-serif" | "system-ui" => Family::SansSerif, "serif" => Family::Serif, "monospace" => Family::Monospace, name => Family::Name(name) };
        let selected = self.fonts.db().query(&fontdb::Query { families: &[family], weight: Weight(req.weight), style: if req.italic { Style::Italic } else { Style::Normal }, ..Default::default() });
        if let Some(font) = selected.and_then(|id| self.fonts.get_font(id)) {
            let fm = font.as_swash().metrics(&[]).scale(req.font_size);
            m.font_bounding_box_ascent = fm.ascent;
            m.font_bounding_box_descent = fm.descent.abs();
        }
        let mut bounds: Option<(f32,f32,f32,f32)> = None;
        for run in buffer.layout_runs() {
            m.width = m.width.max(run.line_w);
            for g in run.glyphs {
                if let Some(font) = self.fonts.get_font(g.font_id) {
                    let fm = font.as_swash().metrics(&[]).scale(req.font_size);
                    m.font_bounding_box_ascent = m.font_bounding_box_ascent.max(fm.ascent);
                    m.font_bounding_box_descent = m.font_bounding_box_descent.max(fm.descent.abs());
                }
                let p = g.physical((0.,0.),1.);
                if let Some(image) = self.cache.get_image(&mut self.fonts, p.cache_key) {
                    if image.placement.width == 0 || image.placement.height == 0 { continue; }
                    let left = (p.x + image.placement.left) as f32;
                    let top = (p.y - image.placement.top) as f32;
                    let right = left + image.placement.width as f32;
                    let bottom = top + image.placement.height as f32;
                    bounds = Some(match bounds { Some((l,t,r,b)) => (l.min(left),t.min(top),r.max(right),b.max(bottom)), None => (left,top,right,bottom) });
                }
            }
        }
        let anchor = match req.align.as_str() { "center" => m.width / 2., "right" | "end" => m.width, _ => 0. };
        let baseline = baseline_offset(req, &m);
        if let Some((l,t,r,b)) = bounds { m.actual_bounding_box_left = anchor-l; m.actual_bounding_box_right = r-anchor; m.actual_bounding_box_ascent = -t-baseline; m.actual_bounding_box_descent = b+baseline; }
        m.em_height_ascent = m.font_bounding_box_ascent-baseline;
        m.em_height_descent = m.font_bounding_box_descent+baseline;
        m.alphabetic_baseline = -baseline;
        m.hanging_baseline = m.font_bounding_box_ascent*0.8-baseline;
        m.ideographic_baseline = -m.font_bounding_box_descent-baseline;
        m
    }
    pub fn measure(&mut self, req: &TextRequest) -> Result<TextMetrics, String> { let b = self.shape(req)?; Ok(self.metrics(&b, req)) }
    pub fn draw(&mut self, req: &TextRequest, width: u32, height: u32, pixels: &mut [u8]) -> Result<TextMetrics, String> {
        if width > 32767 || height > 32767 || pixels.len() != width as usize * height as usize * 4 { return Err("Canvas surface byte length mismatch".into()); }
        let b = self.shape(req)?;
        let m = self.metrics(&b, req);
        if width == 0 || height == 0 || req.max_width.is_some_and(|w| !w.is_finite() || w <= 0.) { return Ok(m); }
        let scale_x = if m.width > 0. { req.max_width.map_or(1., |w| (w/m.width).min(1.)) } else { 1. };
        let anchor = match req.align.as_str() { "center" => m.width/2., "right" | "end" => m.width, _ => 0. };
        let baseline = req.y + baseline_offset(req,&m);
        let blend = canvas::blend_mode(&req.composite)?;
        let mask = canvas::clip_mask(&req.clips,width,height)?;
        canvas::premultiply(pixels);
        {
            let mut target = PixmapMut::from_bytes(pixels,width,height).ok_or("Invalid Canvas surface")?;
            for run in b.layout_runs() {
                for g in run.glyphs {
                    let physical = g.physical((0.,0.),1.);
                    let stroke_image;
                    let image = if req.stroke_width > 0. {
                        let Some(font) = self.fonts.get_font(g.font_id) else { continue; };
                        let mut scaler = self.stroke.builder(font.as_swash()).size(g.font_size).hint(true).build();
                        stroke_image = Render::new(&[Source::Outline]).style(swash::zeno::Stroke::new(req.stroke_width)).render(&mut scaler,g.glyph_id);
                        stroke_image.as_ref()
                    } else { self.cache.get_image(&mut self.fonts,physical.cache_key).as_ref() };
                    let Some(image) = image else { continue; };
                    let Some(mut pixmap) = Pixmap::new(image.placement.width,image.placement.height) else { continue; };
                    for (i,p) in pixmap.data_mut().chunks_exact_mut(4).enumerate() {
                        let color = match image.content { Content::Color => [image.data[i*4],image.data[i*4+1],image.data[i*4+2],((image.data[i*4+3] as u16*req.color[3] as u16)/255) as u8], Content::Mask => [req.color[0],req.color[1],req.color[2],((image.data[i] as u16*req.color[3] as u16)/255) as u8], Content::SubpixelMask => [req.color[0],req.color[1],req.color[2],((image.data[i*4] as u16*req.color[3] as u16)/255) as u8] };
                        for c in 0..3 { p[c] = ((color[c] as u16*color[3] as u16+127)/255) as u8; } p[3] = color[3];
                    }
                    let x = req.x + ((physical.x+image.placement.left) as f32-anchor)*scale_x;
                    let y = baseline + (physical.y-image.placement.top) as f32;
                    target.draw_pixmap(0,0,pixmap.as_ref(),&PixmapPaint { blend_mode: blend, ..Default::default() },canvas::matrix(req.transform).pre_concat(Transform::from_row(scale_x,0.,0.,1.,x,y)),mask.as_ref());
                }
            }
        }
        canvas::unpremultiply(pixels);
        Ok(m)
    }
}
fn baseline_offset(req: &TextRequest, m: &TextMetrics) -> f32 {
    match req.baseline.as_str() { "top" => m.font_bounding_box_ascent, "hanging" => m.font_bounding_box_ascent*0.8, "middle" => (m.font_bounding_box_ascent-m.font_bounding_box_descent)/2., "bottom" | "ideographic" => -m.font_bounding_box_descent, _ => 0. }
}
