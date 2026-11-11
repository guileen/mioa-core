//! Read-only LP DOM projection. This module has no JS runtime or HTTP client.
use std::{collections::HashMap, ffi::c_void, panic::{catch_unwind, AssertUnwindSafe}};
use html5ever::{LocalName, Namespace, QualName};
use obscura_dom::{Attribute, DomTree, NodeData, NodeId};
use obscura_render::{PreparedRender, RenderResourceCache, ResolvedScrollState};
use serde::Deserialize;

#[derive(Deserialize)]
struct Node {
    key: usize, parent: Option<usize>, tag: Option<String>, text: Option<String>,
    namespace: Option<String>, attrs: Vec<(String, String)>, scroll_x: f32, scroll_y: f32,
    sheet: Option<String>, shadow_host: Option<usize>, shadow_closed: bool,
    focused: bool, focus_visible: bool, hovered: bool, active: bool,
    selection_focus: Option<usize>,
}
type Load = unsafe extern "C" fn(*mut c_void, *const u8, usize, *mut usize) -> *const u8;
type Surface = unsafe extern "C" fn(*mut c_void,usize,*mut u32,*mut u32,*mut usize)->*const u8;
pub struct Document {
    tree: DomTree, prepared: PreparedRender, resources: RenderResourceCache,
    ids: HashMap<usize, NodeId>, keys: HashMap<NodeId, usize>, scroll: ResolvedScrollState,
    context: *mut c_void, surface: Surface,
    painted: bool,
    // CSS-px, document-space rect the next capture may repaint instead of the
    // whole viewport. Only the caller knows what it changed.
    dirty: Option<(f32, f32, f32, f32)>,
    // The caller proved the projected frame is byte-identical to the last one,
    // so the shadow can be republished without touching the rasteriser.
    unchanged: bool,
}

const TILE_PX: u32 = 128;
#[repr(C)]
pub struct Rect { x: f64, y: f64, width: f64, height: f64 }
#[repr(C)]
#[derive(Default)]
pub struct Metrics {
    x:f64,y:f64,width:f64,height:f64,client_width:f64,client_height:f64,
    scroll_width:f64,scroll_height:f64,scroll_x:f64,scroll_y:f64,max_x:f64,max_y:f64,
    border_left:f64,border_top:f64,
}

struct Surfaces<'a> { keys: &'a HashMap<NodeId,usize>, context:*mut c_void, surface:Surface }
impl obscura_render::CanvasSurfaceSource for Surfaces<'_> {
    fn surface(&self,id:NodeId)->Option<obscura_render::CanvasSurface<'_>> {
        let key=*self.keys.get(&id)?;
        let(mut width,mut height,mut len)=(0,0,0);
        let bytes=unsafe{(self.surface)(self.context,key,&mut width,&mut height,&mut len)};
        if bytes.is_null(){return None;}
        obscura_render::CanvasSurface::from_rgba8(width,height,unsafe{std::slice::from_raw_parts(bytes,len)})
    }
}

#[no_mangle]
pub unsafe extern "C" fn lp_document_new(
    json: *const u8, len: usize, base: *const u8, base_len: usize,
    width: f32, height: f32, x: f32, y: f32, context: *mut c_void, load: Load, surface:Surface,
) -> *mut Document {
    catch_unwind(AssertUnwindSafe(|| {
        let nodes: Vec<Node> = serde_json::from_slice(std::slice::from_raw_parts(json, len)).ok()?;
        let base = std::str::from_utf8(std::slice::from_raw_parts(base, base_len)).ok()?;
        let tree = DomTree::new();
        let mut ids = HashMap::new();
        let mut keys = HashMap::new();
        let mut requested = HashMap::new();
        for node in nodes {
            let is_element=node.tag.is_some();
            let mut text=node.text;
            let id = if let Some(host)=node.shadow_host {
                tree.attach_shadow_root(*ids.get(&host)?,if node.shadow_closed {obscura_dom::ShadowRootMode::Closed}else{obscura_dom::ShadowRootMode::Open}).ok()?
            } else if node.tag.is_none() && text.is_none() { NodeId::new(0) } else {
                let data = if let Some(tag) = node.tag {
                    NodeData::Element {
                        name: QualName::new(None, Namespace::from(node.namespace.as_deref().unwrap_or("http://www.w3.org/1999/xhtml")), LocalName::from(tag)),
                        attrs: node.attrs.into_iter().map(|(name,value)| Attribute {
                            name: QualName::new(None, Namespace::default(), LocalName::from(name)), value,
                        }).collect(), template_contents: None, mathml_annotation_xml_integration_point: false,
                    }
                } else { NodeData::Text { contents: text.take().unwrap_or_default() } };
                let id = tree.new_node(data);
                tree.append_child(node.parent.and_then(|key| ids.get(&key).copied()).unwrap_or(NodeId::new(0)), id);
                id
            };
            if is_element { if let Some(text)=text {tree.append_text(id,&text);} }
            if let Some(sheet)=node.sheet {tree.append_external_stylesheet(id,sheet,true);}
            requested.insert(id, (node.scroll_x,node.scroll_y));
            ids.insert(node.key,id); keys.insert(id,node.key);
            if is_element {
                if let Some(focus)=node.selection_focus {
                    tree.update_form_control_state(id, |state| state.selection_focus = Some(focus));
                }
            }
            if node.focused { tree.set_focused(Some(id)); tree.set_focus_visible(node.focus_visible); }
            if node.hovered { tree.set_hovered(Some(id)); }
            if node.active { tree.set_active(Some(id)); }
        }
        // The callback only returns LP-owned bytes already fetched by LP. A miss
        // remains missing: the renderer must never fetch a resource itself.
        let mut resources = RenderResourceCache::with_loader(move |url: &str| {
            let mut len = 0;
            let bytes = load(context, url.as_ptr(), url.len(), &mut len);
            if bytes.is_null() { None } else { Some(std::slice::from_raw_parts(bytes,len).to_vec()) }
        });
        let prepared = obscura_render::prepare_dom_with_dynamic_fonts(&tree,(width,height),Some(base),&mut resources,&[])?;
        let scroll = prepared.resolve_scroll_state(&tree,(x,y),&requested);
        Some(Box::into_raw(Box::new(Document { tree,prepared,resources,ids,keys,scroll,context,surface,painted:false, dirty:None, unchanged:false })))
    })).ok().flatten().unwrap_or(std::ptr::null_mut())
}
#[no_mangle]
pub unsafe extern "C" fn lp_document_free(document: *mut Document) {
    if !document.is_null() { drop(Box::from_raw(document)); }
}
#[no_mangle]
pub unsafe extern "C" fn lp_document_clear_paint(document:*mut Document) {
    if let Some(d)=document.as_mut() {d.painted=false;}
}
#[no_mangle]
pub unsafe extern "C" fn lp_document_rect(document: *const Document,key:usize,out:*mut Rect)->bool {
    let Some(document)=document.as_ref() else {return false};
    let Some(id)=document.ids.get(&key) else {return false};
    let Some(rect)=document.prepared.viewport_rect_with_scroll(*id,&document.scroll) else {return false};
    *out=Rect{x:rect.x as f64,y:rect.y as f64,width:rect.width as f64,height:rect.height as f64}; true
}

/// The offset is a native DOM UTF-16 boundary for ordinary text. Controls use
/// the live UTF-8 selection_focus projected before the exact last paint.
#[no_mangle]
pub unsafe extern "C" fn lp_document_caret(
    document:*const Document,key:usize,offset:usize,control:bool,out:*mut Rect,
)->bool {
    let Some(d)=document.as_ref() else {return false};
    if out.is_null() || !d.painted {return false;}
    let Some(id)=d.ids.get(&key) else {return false};
    let rect=if control {d.prepared.native_control_caret_rect(*id)}
        else {d.prepared.caret_rect(&d.tree,&d.scroll,*id,offset)};
    let Some(rect)=rect else {return false};
    *out=Rect{x:rect.x as f64,y:rect.y as f64,width:rect.width as f64,height:rect.height as f64};
    true
}

/// Map a child viewport into precisely the content destination used for its
/// iframe surface. Axis-preserving affine transforms are exact for a Rect;
/// rotation/skew would lose insertion geometry through nested AABBs, so callers
/// must explicitly fall back rather than claiming a shaped insertion there.
#[no_mangle]
pub unsafe extern "C" fn lp_document_embed_rect(
    document:*const Document,key:usize,width:u32,height:u32,out:*mut Rect,
)->bool {
    let Some(d)=document.as_ref() else {return false};
    if out.is_null() || !d.painted || width==0 || height==0 {return false;}
    let Some(id)=d.ids.get(&key) else {return false};
    let layout=d.prepared.layout();
    let Some(border)=layout.rects.get(id) else {return false};
    let Some(style)=layout.styles.get(id) else {return false};
    let transform=layout.transforms.get(id).copied().unwrap_or_default();
    if transform.b!=0. || transform.c!=0. || transform.a==0. || transform.d==0. {return false;}
    let Some(viewport)=d.prepared.viewport_rect_with_scroll(*id,&d.scroll) else {return false};
    let visual=transform.map_rect(*border);
    let content=obscura_render::Rect {
        x:border.x+style.border.left+style.padding.left,
        y:border.y+style.border.top+style.padding.top,
        width:(border.width-style.border.left-style.border.right-style.padding.left-style.padding.right).max(0.),
        height:(border.height-style.border.top-style.border.bottom-style.padding.top-style.padding.bottom).max(0.),
    };
    let value=&*out;
    let local=obscura_render::Rect {
        x:content.x+(value.x/width as f64*content.width as f64) as f32,
        y:content.y+(value.y/height as f64*content.height as f64) as f32,
        width:(value.width/width as f64*content.width as f64) as f32,
        height:(value.height/height as f64*content.height as f64) as f32,
    };
    if content.width<=0. || content.height<=0. {return false;}
    let mapped=transform.map_rect(local);
    *out=Rect{x:(mapped.x+viewport.x-visual.x) as f64,y:(mapped.y+viewport.y-visual.y) as f64,
        width:mapped.width as f64,height:mapped.height as f64};
    true
}
#[no_mangle]
pub unsafe extern "C" fn lp_document_hit(document:*const Document,x:f32,y:f32)->usize {
    let Some(document)=document.as_ref() else {return 0};
    document.prepared.hit_test(&document.tree,&document.scroll,x,y)
        .and_then(|id|document.keys.get(&id).copied()).unwrap_or(0)
}
#[no_mangle]
pub unsafe extern "C" fn lp_document_height(document:*const Document)->f32 {
    document.as_ref().map(|d|d.prepared.content_size().1).unwrap_or(0.0)
}
#[no_mangle]
pub unsafe extern "C" fn lp_document_png(document:*mut Document,opts:super::LpRenderOpts,context:*mut c_void,write:unsafe extern "C" fn(*mut c_void,*const u8,usize)->bool)->i32 {
    catch_unwind(AssertUnwindSafe(|| {
        let Some(d)=document.as_mut() else {return -1};
        let height=if opts.height==0 {d.prepared.content_size().1} else {opts.height as f32};
        let region=if opts.clip_w>0.0 {
            obscura_render::CaptureRegion::new(opts.clip_x,opts.clip_y,opts.clip_w,opts.clip_h,opts.scale)
        } else {obscura_render::CaptureRegion::new(0.0,0.0,opts.width as f32,height,opts.scale)};
        let surfaces=Surfaces{keys:&d.keys,context:d.context,surface:d.surface};
        let Ok(bytes)=obscura_render::screenshot_prepared_region_with_scroll_and_surface_color_and_canvas_surfaces(&d.tree,&mut d.prepared,&mut d.resources,&d.scroll,region,[255,255,255,255],&surfaces) else {return -1};
        d.painted=true;
        if write(context,bytes.as_ptr(),bytes.len()) {0} else {-1}
    })).unwrap_or(-1)
}

#[no_mangle]
pub unsafe extern "C" fn lp_document_metrics(document:*const Document,key:usize,out:*mut Metrics)->bool {
    let Some(d)=document.as_ref()else{return false};let Some(id)=d.ids.get(&key)else{return false};
    let Some(rect)=d.prepared.layout().rects.get(id)else{return false};
    let(client_width,client_height)=d.prepared.client_size(*id).unwrap_or((0.,0.));
    let scroll=d.prepared.element_scroll_metrics(*id,&d.scroll);
    let(content,offset,max)=scroll.map(|s|(s.content_size,s.offset,s.max_offset)).unwrap_or(((client_width,client_height),(0.,0.),(0.,0.)));
    let border=d.prepared.layout().styles.get(id).map(|style|(style.border.left,style.border.top)).unwrap_or((0.,0.));
    *out=Metrics{x:rect.x as f64,y:rect.y as f64,width:rect.width as f64,height:rect.height as f64,client_width:client_width as f64,client_height:client_height as f64,scroll_width:content.0 as f64,scroll_height:content.1 as f64,scroll_x:offset.0 as f64,scroll_y:offset.1 as f64,max_x:max.0 as f64,max_y:max.1 as f64,border_left:border.0 as f64,border_top:border.1 as f64};true
}
#[no_mangle]
pub unsafe extern "C" fn lp_document_size(document:*const Document,out:*mut Rect)->bool {
    let Some(d)=document.as_ref()else{return false};let(width,height)=d.prepared.content_size();
    *out=Rect{x:0.,y:0.,width:width as f64,height:height as f64};true
}

#[no_mangle]
pub unsafe extern "C" fn lp_document_rgba(document:*mut Document,context:*mut c_void,write:unsafe extern "C" fn(*mut c_void,*const u8,usize)->bool)->bool {
    catch_unwind(AssertUnwindSafe(||{
        let Some(d)=document.as_mut()else{return false};
        let surfaces=Surfaces{keys:&d.keys,context:d.context,surface:d.surface};
        let Some(mut image)=obscura_render::paint_prepared_with_scroll_and_surface_color_and_canvas_surfaces(&d.tree,&mut d.prepared,&mut d.resources,&d.scroll,[255,255,255,255],&surfaces)else{return false};
        d.painted=true;
        obscura_render::graphics::canvas::unpremultiply(image.data_mut());
        write(context,image.data().as_ptr(),image.data().len())
    })).unwrap_or(false)
}

/// LPFR v2 direct raw raster export. Does not publish: Python validates the
/// observation before the C11 release exchange transfers slot ownership.
#[repr(C)]
pub struct RawFrame { width: u32, height: u32, stride: u32, theme: u32, fingerprint: [u8;32] }

#[no_mangle]
pub unsafe extern "C" fn lp_document_raw_frame(
    document: *mut Document, path: *const u8, path_len: usize, slot: u32,
    width: u32, height: u32, scale: f32, out: *mut RawFrame,
) -> bool {
    use std::os::unix::{fs::{FileExt, MetadataExt, OpenOptionsExt}, ffi::OsStrExt};
    catch_unwind(AssertUnwindSafe(|| {
        let Some(d) = document.as_mut() else { return false };
        if path.is_null() || out.is_null() || slot > 2 || width == 0 || height == 0
            || !scale.is_finite() || !(0.25..=8.0).contains(&scale) { return false; }
        let capacity = 3840u64 * 2160 * 4;
        let pw = (width as f64 * scale as f64).ceil() as u64;
        let ph = (height as f64 * scale as f64).ceil() as u64;
        if pw == 0 || ph == 0 || pw.checked_mul(ph).and_then(|n| n.checked_mul(4)).is_none_or(|n| n > capacity) {
            return false;
        }
        let path = std::path::Path::new(std::ffi::OsStr::from_bytes(std::slice::from_raw_parts(path,path_len)));
        if !path.is_absolute() { return false; }
        let nofollow = if cfg!(target_os="macos") { 0x100 } else { 0x20000 };
        let Ok(file) = std::fs::OpenOptions::new().read(true).write(true).custom_flags(nofollow).open(path) else { return false };
        let Ok(meta) = file.metadata() else { return false };
        let Ok(parent) = std::fs::symlink_metadata(path.parent().unwrap()) else { return false };
        if !meta.is_file() || meta.mode() & 0o777 != 0o600 || !parent.is_dir()
            || parent.mode() & 0o777 != 0o700 || parent.uid() != meta.uid()
            || meta.len() != 64 + 3 * (64 + capacity) { return false; }
        let mut header = [0u8;16];
        if file.read_exact_at(&mut header,0).is_err() || u32::from_le_bytes(header[0..4].try_into().unwrap()) != 0x4c504652
            || u16::from_le_bytes(header[4..6].try_into().unwrap()) != 2
            || u32::from_le_bytes(header[12..16].try_into().unwrap()) as u64 != capacity { return false; }
        emit_frame(d,&file,slot,capacity,width,height,scale,out)
    })).unwrap_or(false)
}


/// The renderer re-parses the document on every capture (hover changes the
/// projection digest) and the client rotates the frame ring, so patching a slot
/// in place is unsafe on both counts. Instead the partial paint is composited
/// into a process-level shadow of the *current* frame and the whole shadow is
/// published: one 3.9-15.6 MB write instead of a 30 ms full raster.
struct FrameShadow { pw: u32, ph: u32, pixels: Vec<u8>, tiles: Vec<[u8; 32]> }
static SHADOW: std::sync::Mutex<Option<FrameShadow>> = std::sync::Mutex::new(None);

/// Which published frame each ring slot currently holds. A reuse into a slot
/// that already carries this frame needs no write at all; once all three slots
/// are current, redundant captures become free.
static SLOT_FRAME: std::sync::Mutex<[u64; 3]> = std::sync::Mutex::new([u64::MAX; 3]);
static FRAME_SEQ: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

fn frame_seq() -> u64 { FRAME_SEQ.load(std::sync::atomic::Ordering::Acquire) }

fn slot_holds(slot: u32, seq: u64) -> bool {
    SLOT_FRAME.lock().map(|guard| guard.get(slot as usize).copied() == Some(seq)).unwrap_or(false)
}

fn mark_published(slot: u32, seq: u64) {
    if let Ok(mut guard) = SLOT_FRAME.lock() {
        if let Some(entry) = guard.get_mut(slot as usize) { *entry = seq; }
    }
    FRAME_SEQ.store(seq, std::sync::atomic::Ordering::Release);
}

fn shadow_matches(pw: u32, ph: u32) -> bool {
    SHADOW.lock().map(|guard| guard.as_ref().is_some_and(|s| (s.pw, s.ph) == (pw, ph))).unwrap_or(false)
}

fn store_shadow(pw: u32, ph: u32, pixels: Vec<u8>, tiles: Vec<[u8; 32]>) {
    if let Ok(mut guard) = SHADOW.lock() {
        *guard = Some(FrameShadow { pw, ph, pixels, tiles });
    }
}

fn tile_geometry(pw:u32,ph:u32)->(u32,u32){
    ((pw+TILE_PX-1)/TILE_PX,(ph+TILE_PX-1)/TILE_PX)
}

fn fingerprint_from_tiles(tiles:&[[u8;32]])->[u8;32]{
    use sha2::{Digest,Sha256};
    let mut h=Sha256::new();
    for t in tiles { h.update(t); }
    h.finalize().into()
}

/// Digest of one tile, read from `src` whose top-left is (ox,oy) in device px.
fn hash_tile(src:&[u8],iw:u32,ox:u32,oy:u32,tx:u32,ty:u32,pw:u32,ph:u32)->[u8;32]{
    use sha2::{Digest,Sha256};
    let mut h=Sha256::new();
    let x0=tx*TILE_PX; let y0=ty*TILE_PX;
    let x1=(x0+TILE_PX).min(pw); let y1=(y0+TILE_PX).min(ph);
    let (Some(rx),Some(_ry))=(x0.checked_sub(ox),y0.checked_sub(oy)) else { return h.finalize().into() };
    for y in y0..y1 {
        let row=((y-oy) as usize)*(iw as usize)*4;
        let start=row+(rx as usize)*4;
        let end=row+((x1-ox) as usize)*4;
        if end <= src.len() && start <= end { h.update(&src[start..end]); }
    }
    h.finalize().into()
}

fn tiles_of(src:&[u8],pw:u32,ph:u32)->Vec<[u8;32]>{
    let (tw,th)=tile_geometry(pw,ph);
    let mut tiles=Vec::with_capacity((tw as usize)*(th as usize));
    for ty in 0..th { for tx in 0..tw { tiles.push(hash_tile(src,pw,0,0,tx,ty,pw,ph)); } }
    tiles
}

/// Snap a CSS-px rect outward to the tile grid of the device-space viewport.
fn snap_rect(x:f32,y:f32,w:f32,h:f32,scale:f32,pw:u32,ph:u32)->Option<(u32,u32,u32,u32)>{
    let s=scale as f64;
    let t=TILE_PX as i64;
    let mut x0=((x as f64)*s).floor().max(0.0) as i64;
    let mut y0=((y as f64)*s).floor().max(0.0) as i64;
    let mut x1=(((x as f64)+(w as f64))*s).ceil() as i64;
    let mut y1=(((y as f64)+(h as f64))*s).ceil() as i64;
    x1=x1.min(pw as i64); y1=y1.min(ph as i64);
    if x0>=x1 || y0>=y1 { return None; }
    x0=(x0/t)*t; y0=(y0/t)*t;
    x1=(((x1+t-1)/t)*t).min(pw as i64);
    y1=(((y1+t-1)/t)*t).min(ph as i64);
    if x0>=x1 || y0>=y1 { return None; }
    Some((x0 as u32,y0 as u32,x1 as u32,y1 as u32))
}

fn slot_base(slot:u32,capacity:u64)->u64{ 64 + slot as u64 * (64 + capacity) + 64 }

/// CSS-px, document-space rect for the next capture. A zero rect clears it.
#[no_mangle]
pub unsafe extern "C" fn lp_document_set_dirty_rect(
    document:*mut Document,x:f32,y:f32,width:f32,height:f32,
)->bool {
    let Some(d)=document.as_mut() else { return false };
    if !(x.is_finite() && y.is_finite() && width.is_finite() && height.is_finite())
        || width <= 0.0 || height <= 0.0 { d.dirty=None; return false; }
    d.dirty = Some(match d.dirty {
        None => (x,y,width,height),
        Some((ax,ay,aw,ah)) => {
            let x0=ax.min(x); let y0=ay.min(y);
            let x1=(ax+aw).max(x+width); let y1=(ay+ah).max(y+height);
            (x0,y0,x1-x0,y1-y0)
        }
    });
    true
}

/// Declare that the projected frame equals the last published one. The renderer
/// then republishes the shadow instead of re-rasterising: the frame ring rotates,
/// so the slot still needs the bytes even when the content did not change.
#[no_mangle]
pub unsafe extern "C" fn lp_document_set_frame_unchanged(document:*mut Document, unchanged: bool) -> bool {
    let Some(d)=document.as_mut() else { return false };
    d.unchanged = unchanged;
    true
}

fn emit_reuse(file:&std::fs::File,slot:u32,capacity:u64,pw:u32,ph:u32,out:*mut RawFrame)->bool {
    use std::os::unix::fs::FileExt;
    let seq=frame_seq();
    let (fingerprint, wrote) = {
        let guard = match SHADOW.lock() { Ok(g)=>g, Err(_)=>return false };
        let Some(shadow)=guard.as_ref() else { return false };
        if (shadow.pw,shadow.ph)!=(pw,ph) { return false; }
        let needs_write = !slot_holds(slot,seq);
        if needs_write && file.write_all_at(&shadow.pixels,slot_base(slot,capacity)).is_err() {
            return false;
        }
        (fingerprint_from_tiles(&shadow.tiles), needs_write)
    };
    if wrote { mark_published(slot,seq); }
    let mut theme=[0u8;4];
    let _=file.read_exact_at(&mut theme,slot_base(slot,capacity));
    unsafe { *out=RawFrame{ width:pw,height:ph,stride:pw*4,
        theme:u32::from_le_bytes(theme), fingerprint }; }
    true
}

fn emit_full(d:&mut Document,file:&std::fs::File,slot:u32,capacity:u64,
             width:u32,height:u32,scale:f32,out:*mut RawFrame)->bool {
    use std::os::unix::fs::FileExt;
    let Some(image)=paint_raw_frame(d,width,height,scale) else { return false };
    let bytes=image.data();
    if bytes.len() as u64>capacity || bytes.len()<4 { return false; }
    if file.write_all_at(bytes,slot_base(slot,capacity)).is_err(){ return false; }
    let pw=image.width(); let ph=image.height();
    let tiles=tiles_of(bytes,pw,ph);
    let fingerprint=fingerprint_from_tiles(&tiles);
    store_shadow(pw,ph,bytes.to_vec(),tiles);
    let seq=frame_seq().wrapping_add(1);
    mark_published(slot,seq);
    unsafe { *out=RawFrame{ width:pw,height:ph,stride:pw*4,
        theme:u32::from_le_bytes(bytes[..4].try_into().unwrap()), fingerprint }; }
    true
}

fn emit_dirty(d:&mut Document,file:&std::fs::File,slot:u32,capacity:u64,
              _width:u32,_height:u32,scale:f32,
              ox:u32,oy:u32,ox2:u32,oy2:u32,pw:u32,ph:u32,out:*mut RawFrame)->bool {
    use std::os::unix::fs::FileExt;
    let s=scale as u32;
    if s==0 { return false; }
    let (dx,dy)=(ox/s,oy/s);
    let (dw,dh)=((ox2-ox)/s,(oy2-oy)/s);
    if dw==0 || dh==0 { return false; }
    let Some(image)=paint_raw_frame_region(d,dx as f32,dy as f32,dw as f32,dh as f32,scale) else { return false };
    let iw=image.width();
    if iw < ox2-ox || image.height() < oy2-oy { return false; }
    let src=image.data();
    let row_bytes=((ox2-ox) as usize)*4;
    let mut guard = match SHADOW.lock() { Ok(g)=>g, Err(_)=>return false };
    let Some(shadow)=guard.as_mut() else { return false };
    if (shadow.pw,shadow.ph)!=(pw,ph) || shadow.pixels.len()!=(pw as usize)*(ph as usize)*4 { return false; }
    for row in 0..(oy2-oy) as usize {
        let s_off=row*(iw as usize)*4;
        let d_off=((oy as usize)+row)*(pw as usize)*4+(ox as usize)*4;
        if s_off+row_bytes>src.len() || d_off+row_bytes>shadow.pixels.len() { return false; }
        shadow.pixels[d_off..d_off+row_bytes].copy_from_slice(&src[s_off..s_off+row_bytes]);
    }
    let (tw,th)=tile_geometry(pw,ph);
    if shadow.tiles.len()!=(tw as usize)*(th as usize) { return false; }
    for ty in (oy/TILE_PX)..((oy2+TILE_PX-1)/TILE_PX) {
        for tx in (ox/TILE_PX)..((ox2+TILE_PX-1)/TILE_PX) {
            let idx=(ty*tw+tx) as usize;
            if idx>=shadow.tiles.len() { continue; }
            shadow.tiles[idx]=hash_tile(&shadow.pixels,pw,0,0,tx,ty,pw,ph);
        }
    }
    if file.write_all_at(&shadow.pixels,slot_base(slot,capacity)).is_err(){ return false; }
    let seq=frame_seq().wrapping_add(1);
    mark_published(slot,seq);
    let fingerprint=fingerprint_from_tiles(&shadow.tiles);
    let mut theme=[0u8;4];
    let _=file.read_exact_at(&mut theme,slot_base(slot,capacity));
    unsafe { *out=RawFrame{ width:pw,height:ph,stride:pw*4,
        theme:u32::from_le_bytes(theme), fingerprint }; }
    true
}

fn note_decision(tag:&str,slot:u32,rect:Option<(f32,f32,f32,f32)>,us:u128){
    use std::io::Write;
    let Some(path)=std::env::var("MIOA_ENGINE_TIMING").ok().filter(|p|!p.is_empty()) else { return };
    let line=match rect {
        Some((x,y,w,h))=>format!("paint {tag} slot={slot} us={us} rect={x:.0},{y:.0},{w:.0},{h:.0}\n"),
        None=>format!("paint {tag} slot={slot} us={us}\n"),
    };
    if let Ok(mut f)=std::fs::OpenOptions::new().create(true).append(true).open(path){
        let _=f.write_all(line.as_bytes());
    }
}

fn emit_frame(d:&mut Document,file:&std::fs::File,slot:u32,capacity:u64,
              width:u32,height:u32,scale:f32,out:*mut RawFrame)->bool {
    let pw=(width as f64*scale as f64).ceil() as u32;
    let ph=(height as f64*scale as f64).ceil() as u32;
    let probe=std::env::var_os("MIOA_PAINT_PROBE").is_some();
    let integral=scale.fract()==0.0 && scale>=1.0;
    let rect=d.dirty.take();
    let has_shadow = shadow_matches(pw,ph);
    if !probe && integral && pw>0 && ph>0 && has_shadow && d.unchanged {
        let started=std::time::Instant::now();
        if emit_reuse(file,slot,capacity,pw,ph,out) {
            note_decision("reuse",slot,None,started.elapsed().as_micros());
            return true;
        }
    }
    let tag = if probe { "full:probe" }
        else if !integral { "full:scale" }
        else if pw==0 || ph==0 { "full:size" }
        else if !has_shadow { "full:no-shadow" }
        else if rect.is_none() { "full:no-rect" }
        else { "" };
    if tag.is_empty() {
        if let Some((x,y,w,h))=rect {
            if let Some((ox,oy,ox2,oy2))=snap_rect(x,y,w,h,scale,pw,ph) {
                let area=(ox2-ox) as u64*(oy2-oy) as u64;
                let whole=(pw as u64)*(ph as u64);
                if area*10 < whole*6 {
                    let started=std::time::Instant::now();
                    if emit_dirty(d,file,slot,capacity,width,height,scale,ox,oy,ox2,oy2,pw,ph,out) {
                        note_decision("dirty",slot,rect,started.elapsed().as_micros());
                        if std::env::var_os("MIOA_PAINT_VERIFY").is_some() {
                            verify_full(d,width,height,scale,out);
                        }
                        return true;
                    }
                    note_decision("full:dirty-failed",slot,rect,0);
                } else { note_decision("full:big-rect",slot,rect,0); }
            } else { note_decision("full:snap-none",slot,rect,0); }
        }
    } else { note_decision(tag,slot,rect,0); }
    emit_full(d,file,slot,capacity,width,height,scale,out)
}

fn verify_full(d:&mut Document,width:u32,height:u32,scale:f32,out:*mut RawFrame) {
    use std::io::Write;
    let Some(path)=std::env::var("MIOA_PAINT_VERIFY").ok().filter(|p|!p.is_empty()) else { return };
    let Some(image)=paint_raw_frame(d,width,height,scale) else { return };
    let (pw,ph)=(image.width(),image.height());
    let want=fingerprint_from_tiles(&tiles_of(image.data(),pw,ph));
    let got=unsafe{(*out).fingerprint};
    let line=if want==got { "ok\n" } else { "mismatched dirty frame\n" };
    if let Ok(mut f)=std::fs::OpenOptions::new().create(true).append(true).open(path){
        let _=f.write_all(line.as_bytes());
    }
}

fn paint_raw_frame(d:&mut Document,width:u32,height:u32,scale:f32)->Option<tiny_skia::Pixmap> {
    if width==0 || height==0 || !scale.is_finite() || !(0.25..=8.).contains(&scale) {return None;}
    // Measurement probe: clamp the painted area so fixed and per-pixel costs can
    // be separated. Inert unless MIOA_PAINT_PROBE is set.
    let (width, height) = match std::env::var("MIOA_PAINT_PROBE").ok().and_then(|v| v.parse::<u32>().ok()) {
        Some(cap) if cap > 0 => (width.min(cap), height.min(cap)),
        _ => (width, height),
    };
    paint_raw_frame_region(d,0.0,0.0,width as f32,height as f32,scale)
}

fn paint_raw_frame_region(d:&mut Document,dx:f32,dy:f32,width:f32,height:f32,scale:f32)->Option<tiny_skia::Pixmap> {
    if width<=0.0 || height<=0.0 || !scale.is_finite() || !(0.25..=8.).contains(&scale) {return None;}
    let pw=(width as f64*scale as f64).ceil() as u64;
    let ph=(height as f64*scale as f64).ceil() as u64;
    if pw.checked_mul(ph)?.checked_mul(4)? > 3840*2160*4 {return None;}
    let root=d.scroll.root_offset();
    let region=obscura_render::CaptureRegion::new(root.0+dx,root.1+dy,width,height,scale);
    let surfaces=Surfaces{keys:&d.keys,context:d.context,surface:d.surface};
    let image=obscura_render::paint_prepared_region_with_scroll_and_surface_color_and_canvas_surfaces(
        &d.tree,&mut d.prepared,&mut d.resources,&d.scroll,region,[255,255,255,255],&surfaces,
    ).ok()?;
    d.painted=true;
    Some(image)
}

#[no_mangle]
pub unsafe extern "C" fn lp_document_frame_fingerprint(
    document:*mut Document,width:u32,height:u32,scale:f32,out:*mut u8,
)->bool {
    catch_unwind(AssertUnwindSafe(|| {
        let Some(d)=document.as_mut()else{return false};
        if out.is_null(){return false;}
        let Some(image)=paint_raw_frame(d,width,height,scale)else{return false};
        let (pw,ph)=(image.width(),image.height());
        let digest=fingerprint_from_tiles(&tiles_of(image.data(),pw,ph));
        std::ptr::copy_nonoverlapping(digest.as_ptr(),out,32);
        true
    })).unwrap_or(false)
}

#[cfg(test)]
mod caret_tests {
    use super::*;
    use serde_json::{json,Value};

    unsafe extern "C" fn no_load(_: *mut c_void,_: *const u8,_: usize,len: *mut usize)->*const u8 {
        *len=0;
        std::ptr::null()
    }
    unsafe extern "C" fn no_surface(_: *mut c_void,_: usize,_: *mut u32,_: *mut u32,len: *mut usize)->*const u8 {
        *len=0;
        std::ptr::null()
    }
    fn node(key:usize,parent:Option<usize>,tag:Option<&str>,text:Option<&str>,attrs:Value,focus:Option<usize>)->Value {
        json!({"key":key,"parent":parent,"tag":tag,"text":text,"namespace":null,"attrs":attrs,
            "scroll_x":0,"scroll_y":0,"sheet":null,"shadow_host":null,"shadow_closed":false,
            "focused":focus.is_some(),"focus_visible":focus.is_some(),"hovered":false,"active":false,
            "selection_focus":focus})
    }
    fn document(children:Vec<Value>)->Box<Document> {
        document_at(children,0.,0.)
    }
    fn document_at(mut children:Vec<Value>,x:f32,y:f32)->Box<Document> {
        let mut nodes=vec![
            node(1,None,Some("html"),None,json!([]),None),
            node(2,Some(1),Some("body"),None,json!([["style","margin:0"]]),None),
        ];
        nodes.append(&mut children);
        let bytes=serde_json::to_vec(&nodes).unwrap();
        let base=b"https://caret.test/";
        let raw=unsafe{lp_document_new(bytes.as_ptr(),bytes.len(),base.as_ptr(),base.len(),
            400.,200.,x,y,std::ptr::null_mut(),no_load,no_surface)};
        assert!(!raw.is_null());
        unsafe{Box::from_raw(raw)}
    }
    fn caret(d:&Document,key:usize,offset:usize,control:bool)->Option<Rect> {
        let mut rect=Rect{x:0.,y:0.,width:0.,height:0.};
        unsafe{lp_document_caret(d,key,offset,control,&mut rect)}.then_some(rect)
    }
    fn input(focus:usize)->Box<Document> {
        document(vec![node(3,Some(2),Some("input"),None,
            json!([["value","A😀中"],["style","width:240px;font:20px sans-serif"]]),Some(focus))])
    }

    #[test]
    fn native_control_query_requires_paint_and_uses_projected_utf8_focus() {
        let mut before=input(1);
        assert!(caret(&before,3,0,true).is_none());
        assert_eq!(before.tree.form_control_state(before.ids[&3]).unwrap().selection_focus,Some(1));
        paint_raw_frame(&mut before,400,200,1.).unwrap();
        let first=caret(&before,3,0,true).unwrap();
        let shaped=before.prepared.native_control_caret_rect(before.ids[&3]).unwrap();
        assert_eq!((first.x,first.y,first.width,first.height),
            (shaped.x as f64,shaped.y as f64,shaped.width as f64,shaped.height as f64));
        let mut after=input(5); // after the emoji: bytes, not UTF-16 units
        paint_raw_frame(&mut after,400,200,1.).unwrap();
        assert!(caret(&after,3,0,true).unwrap().x>first.x);
        assert!(caret(&after,999,0,true).is_none());
    }

    #[test]
    fn textarea_live_value_and_dom_utf16_caret_use_native_buffers() {
        let mut area=document(vec![node(3,Some(2),Some("textarea"),Some("A\nB"),
            json!([["style","width:200px;height:80px;font:20px sans-serif"]]),Some(2))]);
        paint_raw_frame(&mut area,400,200,1.).unwrap();
        let actual=caret(&area,3,0,true).unwrap();
        let shaped=area.prepared.native_control_caret_rect(area.ids[&3]).unwrap();
        assert_eq!((actual.x,actual.y,actual.height),(shaped.x as f64,shaped.y as f64,shaped.height as f64));
        let mut dom=document(vec![
            node(3,Some(2),Some("div"),None,json!([["contenteditable","true"],["style","font:20px sans-serif"]]),None),
            node(4,Some(3),None,Some("A😀中"),json!([]),None),
        ]);
        assert!(caret(&dom,4,3,false).is_none());
        paint_raw_frame(&mut dom,400,200,1.).unwrap();
        let actual=caret(&dom,4,3,false).unwrap();
        let shaped=dom.prepared.caret_rect(&dom.tree,&dom.scroll,dom.ids[&4],3).unwrap();
        assert_eq!((actual.x,actual.y,actual.height),(shaped.x as f64,shaped.y as f64,shaped.height as f64));
        assert!(actual.x>caret(&dom,4,1,false).unwrap().x);
    }

    #[test]
    fn iframe_caret_mapping_includes_content_insets_and_scale() {
        let mut d=document(vec![node(3,Some(2),Some("iframe"),None,
            json!([["style","position:absolute;left:30px;top:40px;width:100px;height:80px;border:3px solid;padding:4px;transform:scale(2);transform-origin:0 0"]]),None)]);
        paint_raw_frame(&mut d,400,200,1.).unwrap();
        let mut rect=Rect{x:5.,y:6.,width:0.,height:10.};
        assert!(unsafe{lp_document_embed_rect(&*d,3,100,80,&mut rect)});
        assert_eq!((rect.x,rect.y,rect.width,rect.height),(54.,66.,0.,20.));
        let mut rotated=document(vec![node(3,Some(2),Some("iframe"),None,
            json!([["style","width:100px;height:80px;transform:rotate(20deg)"]]),None)]);
        paint_raw_frame(&mut rotated,400,200,1.).unwrap();
        assert!(!unsafe{lp_document_embed_rect(&*rotated,3,100,80,&mut rect)});
    }

    #[test]
    fn raw_viewport_pixels_follow_native_root_scroll_at_each_scale() {
        let children=vec![
            node(3,Some(2),Some("div"),None,
                json!([["style","width:800px;height:800px;background:white"]]),None),
            node(4,Some(2),Some("div"),None,
                json!([["style","position:absolute;left:150px;top:150px;width:100px;height:100px;background:red"]]),None),
            node(5,Some(2),Some("div"),None,
                json!([["style","position:fixed;left:300px;top:20px;width:40px;height:40px;background:lime"]]),None),
        ];
        for scale in [1.,2.] {
            let mut before=document_at(children.clone(),0.,0.);
            let mut after=document_at(children.clone(),100.,100.);
            let a=paint_raw_frame(&mut before,400,200,scale).unwrap();
            let b=paint_raw_frame(&mut after,400,200,scale).unwrap();
            let pixel=|image:&tiny_skia::Pixmap,x:u32,y:u32| {
                let p=image.pixel(x*scale as u32,y*scale as u32).unwrap();
                (p.red(),p.green(),p.blue(),p.alpha())
            };
            assert_eq!((b.width(),b.height()),(400*scale as u32,200*scale as u32));
            assert_eq!(pixel(&a,60,60),(255,255,255,255));
            assert_eq!(pixel(&b,60,60),(255,0,0,255));
            assert_eq!(pixel(&a,310,30),(0,255,0,255));
            assert_eq!(pixel(&b,310,30),(0,255,0,255));
            let mut rect=Rect{x:0.,y:0.,width:0.,height:0.};
            assert!(unsafe{lp_document_rect(&*after,4,&mut rect)});
            assert_eq!((rect.x,rect.y,rect.width,rect.height),(50.,50.,100.,100.));
            let mut fingerprint=[0u8;32];
            assert!(unsafe{lp_document_frame_fingerprint(&mut *after,400,200,scale,fingerprint.as_mut_ptr())});
            use sha2::{Digest,Sha256};
            assert_eq!(fingerprint,<[u8;32]>::from(Sha256::digest(b.data())));
        }
    }

    #[test]
    fn raw_viewport_preserves_container_scroll_and_overflow_clip() {
        let children=vec![
            node(3,Some(2),Some("div"),None,
                json!([["style","width:800px;height:800px;background:white"]]),None),
            node(4,Some(2),Some("div"),None,
                json!([["style","position:fixed;left:10px;top:10px;width:100px;height:100px;overflow:auto"]]),None),
            node(5,Some(4),Some("div"),None,
                json!([["style","height:100px;background:red"]]),None),
            node(6,Some(4),Some("div"),None,
                json!([["style","height:300px;background:blue"]]),None),
        ];
        let mut scrolled=children.clone();
        scrolled[1]["scroll_y"]=json!(100);
        let mut before=document_at(children,100.,100.);
        let mut after=document_at(scrolled,100.,100.);
        let a=paint_raw_frame(&mut before,400,200,1.).unwrap();
        let b=paint_raw_frame(&mut after,400,200,1.).unwrap();
        assert_eq!(a.pixel(20,20).unwrap().red(),255);
        assert_eq!(b.pixel(20,20).unwrap().blue(),255);
        let outside=b.pixel(20,120).unwrap();
        assert_eq!((outside.red(),outside.green(),outside.blue()),(255,255,255));
        let mut rect=Rect{x:0.,y:0.,width:0.,height:0.};
        assert!(unsafe{lp_document_rect(&*after,6,&mut rect)});
        assert_eq!((rect.x,rect.y),(10.,10.));
    }
}
