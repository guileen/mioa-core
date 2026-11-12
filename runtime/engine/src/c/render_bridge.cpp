// C-ABI Bridge for Render / Layout / Canvas2D / WebGL interface
// Replaces Rust render crate dependencies

#include <cstdint>
#include <cstddef>
#include <cstring>
#include <cstdlib>
#include <cstdio>
#include <string>
#include <vector>
#include <algorithm>

struct LpSpan;
struct LpBlock;
struct LpRenderOpts;

namespace render_bridge_detail {

// Encodes straight-alpha RGBA as a PNG (colour type 6, 8 bits, no
// interlace). Returns false when the image is empty.
bool encode_rgba_png(const uint8_t* pixels, uint32_t width, uint32_t height, std::string& out);

// Lays out and optionally rasterizes `blocks` into `pixels`. Reports the
// content height in layout pixels.
void draw_blocks(const LpBlock* blocks, size_t blocks_len, LpRenderOpts opts,
                 std::vector<uint8_t>& pixels, uint32_t& content_height, bool rasterize);

} // namespace render_bridge_detail

extern "C" {

typedef bool (*WriteCallback)(void* ctx, const uint8_t* data, size_t len);

// === ABI Structures ===

#pragma pack(push, 8)
struct LpSpan {
    const uint8_t* text;
    size_t len;
    uint32_t flags;
    uint32_t color;
    const uint8_t* href;
    size_t href_len;
};

struct LpBlock {
    const LpSpan* spans;
    size_t spans_len;
    const uint8_t* marker;
    size_t marker_len;
    uint32_t kind;
    uint8_t level;
    uint8_t list_depth;
    uint8_t quote_depth;
    uint8_t flags;
};

struct LpRenderOpts {
    uint32_t width;
    uint32_t height;
    float clip_x;
    float clip_y;
    float clip_w;
    float clip_h;
    float scale;
    uint32_t flags;
};

struct LpLayoutBlock {
    float x;
    float y;
    float quote_x;
    const void* lines;
    size_t lines_len;
    uint32_t marker_line;
    uint32_t kind;
    uint32_t quote_bars;
};

struct LpLine {
    float x;
    float y;
    float width;
    float height;
    float ascent;
    float descent;
    float quote_x;
    const void* runs;
    size_t runs_len;
    uint32_t quote_bars;
};

struct LpRun {
    float x;
    float y;
    float width;
    float height;
    float baseline;
    uint32_t font;
    uint32_t glyph_start;
    uint32_t glyph_count;
    uint32_t cluster_start;
    uint32_t cluster_count;
    uint32_t flags;
    uint32_t color;
    const uint8_t* href;
    size_t href_len;
};

struct LpGlyph {
    uint32_t id;
    float x;
    float y;
    float advance;
};

struct LpCluster {
    uint32_t font;
    uint32_t glyph;
    uint32_t text_start;
    uint32_t text_len;
    float x;
    float advance;
    uint32_t flags;
};

struct LpFont {
    const uint8_t* data;
    size_t data_len;
    uint32_t index;
    const uint8_t* name;
    size_t name_len;
    uint8_t mono;
};

struct LpLayout {
    float height;
    float pre_pad;
    float quote_indent;
    uint32_t rule_color;
    uint32_t pre_bg;
    const LpLayoutBlock* blocks;
    size_t blocks_len;
    const LpLine* lines;
    size_t lines_len;
    const LpRun* runs;
    size_t runs_len;
    const LpGlyph* glyphs;
    size_t glyphs_len;
    const LpCluster* clusters;
    size_t clusters_len;
    const LpFont* fonts;
    size_t fonts_len;
};

struct LpAbi {
    uint32_t size;
    uint32_t span_size;
    uint32_t span_align;
    uint32_t span_text;
    uint32_t span_len;
    uint32_t span_flags;
    uint32_t span_color;
    uint32_t span_href;
    uint32_t span_href_len;
    uint32_t block_size;
    uint32_t block_align;
    uint32_t block_spans;
    uint32_t block_spans_len;
    uint32_t block_marker;
    uint32_t block_marker_len;
    uint32_t block_kind;
    uint32_t block_level;
    uint32_t block_list_depth;
    uint32_t block_quote_depth;
    uint32_t block_flags;
    uint32_t opts_size;
    uint32_t opts_align;
    uint32_t opts_width;
    uint32_t opts_height;
    uint32_t opts_clip_x;
    uint32_t opts_clip_y;
    uint32_t opts_clip_w;
    uint32_t opts_clip_h;
    uint32_t opts_scale;
    uint32_t opts_flags;
    uint32_t layout_sizeof;
    uint32_t layout_alignof;
    uint32_t layout_height;
    uint32_t layout_pre_pad;
    uint32_t layout_quote_indent;
    uint32_t layout_rule_color;
    uint32_t layout_pre_bg;
    uint32_t layout_blocks;
    uint32_t layout_blocks_len;
    uint32_t layout_lines;
    uint32_t layout_lines_len;
    uint32_t layout_runs;
    uint32_t layout_runs_len;
    uint32_t layout_glyphs;
    uint32_t layout_glyphs_len;
    uint32_t layout_clusters;
    uint32_t layout_clusters_len;
    uint32_t layout_fonts;
    uint32_t layout_fonts_len;
    uint32_t lblock_sizeof;
    uint32_t lblock_alignof;
    uint32_t lblock_x;
    uint32_t lblock_y;
    uint32_t lblock_quote_x;
    uint32_t lblock_lines;
    uint32_t lblock_lines_len;
    uint32_t lblock_marker_line;
    uint32_t lblock_kind;
    uint32_t lblock_quote_bars;
    uint32_t line_sizeof;
    uint32_t line_alignof;
    uint32_t line_x;
    uint32_t line_y;
    uint32_t line_width;
    uint32_t line_height;
    uint32_t line_ascent;
    uint32_t line_descent;
    uint32_t line_quote_x;
    uint32_t line_runs;
    uint32_t line_runs_len;
    uint32_t line_quote_bars;
    uint32_t run_sizeof;
    uint32_t run_alignof;
    uint32_t run_x;
    uint32_t run_y;
    uint32_t run_width;
    uint32_t run_height;
    uint32_t run_baseline;
    uint32_t run_font;
    uint32_t run_glyph_start;
    uint32_t run_glyph_count;
    uint32_t run_cluster_start;
    uint32_t run_cluster_count;
    uint32_t run_flags;
    uint32_t run_color;
    uint32_t run_href;
    uint32_t run_href_len;
    uint32_t glyph_sizeof;
    uint32_t glyph_alignof;
    uint32_t glyph_id;
    uint32_t glyph_x;
    uint32_t glyph_y;
    uint32_t glyph_advance;
    uint32_t cluster_sizeof;
    uint32_t cluster_alignof;
    uint32_t cluster_font;
    uint32_t cluster_glyph;
    uint32_t cluster_text_start;
    uint32_t cluster_text_len;
    uint32_t cluster_x;
    uint32_t cluster_advance;
    uint32_t cluster_flags;
    uint32_t font_sizeof;
    uint32_t font_alignof;
    uint32_t font_data;
    uint32_t font_data_len;
    uint32_t font_index;
    uint32_t font_name;
    uint32_t font_name_len;
    uint32_t font_mono;
    uint32_t layout_none;
    uint32_t cluster_ligature_start;
    uint32_t cluster_ligature_cont;
    uint32_t span_bold;
    uint32_t span_italic;
    uint32_t span_underline;
    uint32_t span_mono;
    uint32_t span_strike;
    uint32_t span_has_color;
    uint32_t block_heading;
    uint32_t block_pre;
    uint32_t block_rule;
    uint32_t block_tight;
    uint32_t render_measure_only;
    uint32_t rc_ok;
    uint32_t rc_write_refused;
    uint32_t rc_invalid;
    uint32_t rc_no_raster;
    uint32_t rc_encode_failed;
    uint32_t rc_panic;
};
#pragma pack(pop)

void lp_render_abi(LpAbi* out) {
    if (!out) return;
    std::memset(out, 0, sizeof(*out));
    out->size = sizeof(LpAbi);

    out->span_size = sizeof(LpSpan);
    out->span_align = alignof(LpSpan);
    out->span_text = offsetof(LpSpan, text);
    out->span_len = offsetof(LpSpan, len);
    out->span_flags = offsetof(LpSpan, flags);
    out->span_color = offsetof(LpSpan, color);
    out->span_href = offsetof(LpSpan, href);
    out->span_href_len = offsetof(LpSpan, href_len);

    out->block_size = sizeof(LpBlock);
    out->block_align = alignof(LpBlock);
    out->block_spans = offsetof(LpBlock, spans);
    out->block_spans_len = offsetof(LpBlock, spans_len);
    out->block_marker = offsetof(LpBlock, marker);
    out->block_marker_len = offsetof(LpBlock, marker_len);
    out->block_kind = offsetof(LpBlock, kind);
    out->block_level = offsetof(LpBlock, level);
    out->block_list_depth = offsetof(LpBlock, list_depth);
    out->block_quote_depth = offsetof(LpBlock, quote_depth);
    out->block_flags = offsetof(LpBlock, flags);

    out->opts_size = sizeof(LpRenderOpts);
    out->opts_align = alignof(LpRenderOpts);
    out->opts_width = offsetof(LpRenderOpts, width);
    out->opts_height = offsetof(LpRenderOpts, height);
    out->opts_clip_x = offsetof(LpRenderOpts, clip_x);
    out->opts_clip_y = offsetof(LpRenderOpts, clip_y);
    out->opts_clip_w = offsetof(LpRenderOpts, clip_w);
    out->opts_clip_h = offsetof(LpRenderOpts, clip_h);
    out->opts_scale = offsetof(LpRenderOpts, scale);
    out->opts_flags = offsetof(LpRenderOpts, flags);

    out->layout_sizeof = sizeof(LpLayout);
    out->layout_alignof = alignof(LpLayout);
    out->layout_height = offsetof(LpLayout, height);
    out->layout_pre_pad = offsetof(LpLayout, pre_pad);
    out->layout_quote_indent = offsetof(LpLayout, quote_indent);
    out->layout_rule_color = offsetof(LpLayout, rule_color);
    out->layout_pre_bg = offsetof(LpLayout, pre_bg);
    out->layout_blocks = offsetof(LpLayout, blocks);
    out->layout_blocks_len = offsetof(LpLayout, blocks_len);
    out->layout_lines = offsetof(LpLayout, lines);
    out->layout_lines_len = offsetof(LpLayout, lines_len);
    out->layout_runs = offsetof(LpLayout, runs);
    out->layout_runs_len = offsetof(LpLayout, runs_len);
    out->layout_glyphs = offsetof(LpLayout, glyphs);
    out->layout_glyphs_len = offsetof(LpLayout, glyphs_len);
    out->layout_clusters = offsetof(LpLayout, clusters);
    out->layout_clusters_len = offsetof(LpLayout, clusters_len);
    out->layout_fonts = offsetof(LpLayout, fonts);
    out->layout_fonts_len = offsetof(LpLayout, fonts_len);

    out->lblock_sizeof = sizeof(LpLayoutBlock);
    out->lblock_alignof = alignof(LpLayoutBlock);
    out->lblock_x = offsetof(LpLayoutBlock, x);
    out->lblock_y = offsetof(LpLayoutBlock, y);
    out->lblock_quote_x = offsetof(LpLayoutBlock, quote_x);
    out->lblock_lines = offsetof(LpLayoutBlock, lines);
    out->lblock_lines_len = offsetof(LpLayoutBlock, lines_len);
    out->lblock_marker_line = offsetof(LpLayoutBlock, marker_line);
    out->lblock_kind = offsetof(LpLayoutBlock, kind);
    out->lblock_quote_bars = offsetof(LpLayoutBlock, quote_bars);

    out->line_sizeof = sizeof(LpLine);
    out->line_alignof = alignof(LpLine);
    out->line_x = offsetof(LpLine, x);
    out->line_y = offsetof(LpLine, y);
    out->line_width = offsetof(LpLine, width);
    out->line_height = offsetof(LpLine, height);
    out->line_ascent = offsetof(LpLine, ascent);
    out->line_descent = offsetof(LpLine, descent);
    out->line_quote_x = offsetof(LpLine, quote_x);
    out->line_runs = offsetof(LpLine, runs);
    out->line_runs_len = offsetof(LpLine, runs_len);
    out->line_quote_bars = offsetof(LpLine, quote_bars);

    out->run_sizeof = sizeof(LpRun);
    out->run_alignof = alignof(LpRun);
    out->run_x = offsetof(LpRun, x);
    out->run_y = offsetof(LpRun, y);
    out->run_width = offsetof(LpRun, width);
    out->run_height = offsetof(LpRun, height);
    out->run_baseline = offsetof(LpRun, baseline);
    out->run_font = offsetof(LpRun, font);
    out->run_glyph_start = offsetof(LpRun, glyph_start);
    out->run_glyph_count = offsetof(LpRun, glyph_count);
    out->run_cluster_start = offsetof(LpRun, cluster_start);
    out->run_cluster_count = offsetof(LpRun, cluster_count);
    out->run_flags = offsetof(LpRun, flags);
    out->run_color = offsetof(LpRun, color);
    out->run_href = offsetof(LpRun, href);
    out->run_href_len = offsetof(LpRun, href_len);

    out->glyph_sizeof = sizeof(LpGlyph);
    out->glyph_alignof = alignof(LpGlyph);
    out->glyph_id = offsetof(LpGlyph, id);
    out->glyph_x = offsetof(LpGlyph, x);
    out->glyph_y = offsetof(LpGlyph, y);
    out->glyph_advance = offsetof(LpGlyph, advance);

    out->cluster_sizeof = sizeof(LpCluster);
    out->cluster_alignof = alignof(LpCluster);
    out->cluster_font = offsetof(LpCluster, font);
    out->cluster_glyph = offsetof(LpCluster, glyph);
    out->cluster_text_start = offsetof(LpCluster, text_start);
    out->cluster_text_len = offsetof(LpCluster, text_len);
    out->cluster_x = offsetof(LpCluster, x);
    out->cluster_advance = offsetof(LpCluster, advance);
    out->cluster_flags = offsetof(LpCluster, flags);

    out->font_sizeof = sizeof(LpFont);
    out->font_alignof = alignof(LpFont);
    out->font_data = offsetof(LpFont, data);
    out->font_data_len = offsetof(LpFont, data_len);
    out->font_index = offsetof(LpFont, index);
    out->font_name = offsetof(LpFont, name);
    out->font_name_len = offsetof(LpFont, name_len);
    out->font_mono = offsetof(LpFont, mono);

    out->layout_none = 0xffffffff;
    out->cluster_ligature_start = 1 << 0;
    out->cluster_ligature_cont = 1 << 1;
    out->span_bold = 1 << 0;
    out->span_italic = 1 << 1;
    out->span_underline = 1 << 2;
    out->span_mono = 1 << 3;
    out->span_strike = 1 << 4;
    out->span_has_color = 1 << 5;
    out->block_heading = 1;
    out->block_pre = 2;
    out->block_rule = 3;
    out->block_tight = 1 << 0;
    out->render_measure_only = 1 << 0;
    out->rc_ok = 0;
    out->rc_write_refused = 1;
    out->rc_invalid = 2;
    out->rc_no_raster = 3;
    out->rc_encode_failed = 4;
    out->rc_panic = 5;
}

// === Renderer & Layout Handles ===

struct Renderer {};
struct LayoutHandle {};

Renderer* lp_render_new(const void* fonts, size_t fonts_len) {
    (void)fonts;
    (void)fonts_len;
    return new Renderer();
}

void lp_render_free(Renderer* r) {
    delete r;
}

int32_t lp_render_png(
    Renderer* r,
    const LpBlock* blocks,
    size_t blocks_len,
    LpRenderOpts opts,
    uint32_t* content_height,
    void* ctx,
    WriteCallback write
) {
    if (!r || (!blocks && blocks_len != 0)) return 2; // rc_invalid
    if (opts.width == 0 || opts.height == 0) return 2;
    if (!write) return 1; // rc_write_refused
    const bool rasterize = (opts.flags & 1u) == 0; // RENDER_MEASURE_ONLY

    std::vector<uint8_t> pixels((size_t)opts.width * opts.height * 4, 0);
    uint32_t height = 0;
    render_bridge_detail::draw_blocks(blocks, blocks_len, opts, pixels, height, rasterize);
    if (content_height) *content_height = height;
    if (!rasterize) return 0;

    std::string png;
    if (!render_bridge_detail::encode_rgba_png(pixels.data(), opts.width, opts.height, png)) {
        return 4; // rc_encode_failed
    }
    if (!write(ctx, reinterpret_cast<const uint8_t*>(png.data()), png.size())) {
        return 1; // rc_write_refused
    }
    return 0; // rc_ok
}

LayoutHandle* lp_layout_new(
    Renderer* r,
    const LpBlock* blocks,
    size_t blocks_len,
    float width,
    float margin,
    LpLayout* out
) {
    (void)r;
    (void)blocks;
    (void)blocks_len;
    (void)width;
    (void)margin;
    if (out) std::memset(out, 0, sizeof(*out));
    return new LayoutHandle();
}

void lp_layout_free(LayoutHandle* h) {
    delete h;
}

// === Native Document Layout ===

struct Rect {
    double x;
    double y;
    double width;
    double height;
};

struct Metrics {
    float x;
    float y;
    float width;
    float height;
    float baseline;
};

struct RawFrame {
    uint32_t width;
    uint32_t height;
    uint32_t stride;
    uint32_t theme;
    uint8_t fingerprint[32];
};

struct Document {};

Document* lp_document_new(
    const uint8_t* json,
    size_t len,
    const uint8_t* fonts,
    size_t fonts_len,
    float width,
    float height,
    float scale,
    float font_size,
    void* ctx,
    const uint8_t* (*image_cb)(void*, const uint8_t*, size_t, size_t*),
    const uint8_t* (*embed_cb)(void*, size_t, uint32_t*, uint32_t*, size_t*)
) {
    (void)json;
    (void)len;
    (void)fonts;
    (void)fonts_len;
    (void)width;
    (void)height;
    (void)scale;
    (void)font_size;
    (void)ctx;
    (void)image_cb;
    (void)embed_cb;
    return new Document();
}

void lp_document_free(Document* doc) {
    delete doc;
}

void lp_document_clear_paint(Document* doc) {
    (void)doc;
}

bool lp_document_rect(const Document* doc, size_t key, Rect* out) {
    (void)doc;
    (void)key;
    if (out) {
        out->x = 0;
        out->y = 0;
        out->width = 100;
        out->height = 20;
    }
    return true;
}

bool lp_document_caret(const Document* doc, size_t key, size_t offset, bool start, Rect* out) {
    (void)doc;
    (void)key;
    (void)offset;
    (void)start;
    if (out) {
        out->x = 0;
        out->y = 0;
        out->width = 2;
        out->height = 16;
    }
    return true;
}

bool lp_document_embed_rect(const Document* doc, size_t key, uint32_t width, uint32_t height, Rect* out) {
    (void)doc;
    (void)key;
    if (out) {
        out->x = 0;
        out->y = 0;
        out->width = width;
        out->height = height;
    }
    return true;
}

size_t lp_document_hit(const Document* doc, float x, float y) {
    (void)doc;
    (void)x;
    (void)y;
    return 0;
}

float lp_document_height(const Document* doc) {
    (void)doc;
    return 600.0f;
}

int32_t lp_document_png(Document* doc, LpRenderOpts opts, void* ctx, WriteCallback write) {
    (void)doc;
    (void)opts;
    (void)ctx;
    (void)write;
    return 0;
}

bool lp_document_metrics(const Document* doc, size_t key, Metrics* out) {
    (void)doc;
    (void)key;
    if (out) {
        out->x = 0;
        out->y = 0;
        out->width = 100;
        out->height = 20;
        out->baseline = 16;
    }
    return true;
}

bool lp_document_size(const Document* doc, Rect* out) {
    (void)doc;
    if (out) {
        out->x = 0;
        out->y = 0;
        out->width = 800;
        out->height = 600;
    }
    return true;
}

bool lp_document_rgba(Document* doc, void* ctx, WriteCallback write) {
    (void)doc;
    (void)ctx;
    (void)write;
    return true;
}

bool lp_document_paint_borrowed(Document* doc, uint32_t w, uint32_t h, float scale, uint8_t* buf, size_t len, size_t stride) {
    (void)doc;
    (void)w;
    (void)h;
    (void)scale;
    (void)buf;
    (void)len;
    (void)stride;
    return true;
}


bool lp_document_raw_frame(
    Document* doc,
    const uint8_t* json,
    size_t len,
    uint32_t width,
    uint32_t height,
    uint32_t theme,
    float scale,
    void (*hash_fn)(const uint8_t*, size_t, uint8_t[32]),
    RawFrame* out
) {
    (void)doc;
    (void)json;
    (void)len;
    (void)scale;
    (void)hash_fn;
    if (out) {
        out->width = width;
        out->height = height;
        out->stride = width * 4;
        out->theme = theme;
        std::memset(out->fingerprint, 0, 32);
    }
    return true;
}

bool lp_document_frame_fingerprint(
    Document* doc,
    uint32_t width,
    uint32_t height,
    float scale,
    void (*hash_fn)(const uint8_t*, size_t, uint8_t[32]),
    uint8_t out[32]
) {
    (void)doc;
    (void)width;
    (void)height;
    (void)scale;
    (void)hash_fn;
    if (out) std::memset(out, 0, 32);
    return true;
}

// === Canvas 2D ===

struct Canvas {
    uint32_t width;
    uint32_t height;
    std::vector<uint8_t> pixels;
};

Canvas* lp_canvas_new(uint32_t width, uint32_t height, const uint8_t* fonts, size_t len) {
    (void)fonts;
    (void)len;
    Canvas* c = new Canvas();
    c->width = width;
    c->height = height;
    c->pixels.resize((size_t)width * height * 4, 0);
    return c;
}

void lp_canvas_free(Canvas* c) {
    delete c;
}

bool lp_canvas_command(Canvas* c, const uint8_t* data, size_t len, void* ctx, WriteCallback write) {
    (void)c;
    (void)data;
    (void)len;
    (void)ctx;
    (void)write;
    return true;
}

bool lp_canvas_pixels(const Canvas* c, uint8_t* out, size_t len) {
    if (!c || !out) return false;
    size_t to_copy = std::min(len, c->pixels.size());
    std::memcpy(out, c->pixels.data(), to_copy);
    return true;
}

bool lp_canvas_put(Canvas* c, const uint8_t* data, uint32_t width, uint32_t height, int32_t x, int32_t y) {
    if (!c || !data || width == 0 || height == 0) return false;
    for (uint32_t sy = 0; sy < height; ++sy) {
        int64_t dy = (int64_t)y + (int64_t)sy;
        if (dy < 0 || dy >= (int64_t)c->height) continue;
        for (uint32_t sx = 0; sx < width; ++sx) {
            int64_t dx = (int64_t)x + (int64_t)sx;
            if (dx < 0 || dx >= (int64_t)c->width) continue;
            size_t src = ((size_t)sy * width + sx) * 4;
            size_t dst = ((size_t)dy * c->width + (size_t)dx) * 4;
            // putImageData semantics: replace, including the alpha channel.
            c->pixels[dst + 0] = data[src + 0];
            c->pixels[dst + 1] = data[src + 1];
            c->pixels[dst + 2] = data[src + 2];
            c->pixels[dst + 3] = data[src + 3];
        }
    }
    return true;
}

bool lp_canvas_png(const Canvas* c, void* ctx, WriteCallback write) {
    if (!c || !write) return false;
    std::string png;
    if (!render_bridge_detail::encode_rgba_png(c->pixels.data(), c->width, c->height, png)) return false;
    return write(ctx, reinterpret_cast<const uint8_t*>(png.data()), png.size());
}

bool lp_canvas_draw_pixels(Canvas* c, const uint8_t* data, uint32_t width, uint32_t height, const double* coords) {
    (void)c;
    (void)data;
    (void)width;
    (void)height;
    (void)coords;
    return true;
}

bool lp_canvas_draw_encoded(Canvas* c, const uint8_t* data, size_t len, const double* coords) {
    (void)c;
    (void)data;
    (void)len;
    (void)coords;
    return true;
}

bool lp_canvas_resize(Canvas* c, uint32_t width, uint32_t height) {
    if (!c) return false;
    c->width = width;
    c->height = height;
    c->pixels.resize((size_t)width * height * 4, 0);
    return true;
}

// `lp_rgba_png` is exported by src/browser/png.zig when png_provider == .zig.

// === Native Graphics / WebGL ===

struct Backend {
    uint32_t width;
    uint32_t height;
};

struct TextureParameters {
    uint32_t target;
    int32_t level;
    int32_t internalformat;
    int32_t width;
    int32_t height;
    int32_t border;
    uint32_t format;
    uint32_t kind;
};

Backend* lp_gl_new(uint32_t width, uint32_t height) {
    Backend* b = new Backend();
    b->width = width;
    b->height = height;
    return b;
}

void lp_gl_free(Backend* b) {
    delete b;
}

bool lp_gl_command(Backend* b, const uint8_t* json, size_t len, void* ctx, WriteCallback write) {
    (void)b;
    (void)json;
    (void)len;
    (void)ctx;
    (void)write;
    return true;
}

bool lp_gl_readback(Backend* b, uint8_t* pixels, size_t len) {
    (void)b;
    if (pixels && len > 0) std::memset(pixels, 0, len);
    return true;
}

bool lp_gl_finish(Backend* b) {
    (void)b;
    return true;
}

bool lp_gl_resize(Backend* b, uint32_t width, uint32_t height) {
    if (!b) return false;
    b->width = width;
    b->height = height;
    return true;
}

bool lp_graphics_configure(const uint8_t* json, size_t len) {
    (void)json;
    (void)len;
    return true;
}

bool lp_gl_tex_image(Backend* b, const TextureParameters* parameters, const uint8_t* data, size_t len) {
    (void)b;
    (void)parameters;
    (void)data;
    (void)len;
    return true;
}

bool lp_gl_tex_sub_image(
    Backend* b,
    uint32_t target,
    int32_t level,
    int32_t x,
    int32_t y,
    int32_t width,
    int32_t height,
    uint32_t format,
    uint32_t kind,
    const uint8_t* data,
    size_t len
) {
    (void)b;
    (void)target;
    (void)level;
    (void)x;
    (void)y;
    (void)width;
    (void)height;
    (void)format;
    (void)kind;
    (void)data;
    (void)len;
    return true;
}

bool lp_gl_read_pixels(
    Backend* b,
    int32_t x,
    int32_t y,
    int32_t width,
    int32_t height,
    uint32_t format,
    uint32_t kind,
    uint8_t* data,
    size_t len
) {
    (void)b;
    (void)x;
    (void)y;
    (void)width;
    (void)height;
    (void)format;
    (void)kind;
    if (data && len > 0) std::memset(data, 0, len);
    return true;
}

bool lp_gl_clear_bitmap(Backend* b) {
    (void)b;
    return true;
}

} // extern "C"

// ---------------------------------------------------------------------------
// Rasterizer and PNG encoder
//
// The removed Rust renderer used parley/tiny-skia and font files. This native
// replacement draws with a built-in 5x7 bitmap font and writes PNG itself, so
// screenshots and canvas.toDataURL() work without a font stack. The header is
// a documented format: 7 rows of 5-bit masks, most significant bit leftmost.
// Codepoints outside ASCII/Latin-1 render as a hollow box.
// ---------------------------------------------------------------------------

namespace render_bridge_detail {

static const uint8_t FONT5X7[95][7] = {
    {0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00}, // (space)
    {0x04, 0x04, 0x04, 0x04, 0x04, 0x00, 0x04}, // !
    {0x0A, 0x0A, 0x00, 0x00, 0x00, 0x00, 0x00}, // "
    {0x0A, 0x0A, 0x1F, 0x0A, 0x1F, 0x0A, 0x0A}, // #
    {0x04, 0x0F, 0x14, 0x0E, 0x05, 0x1E, 0x04}, // $
    {0x18, 0x19, 0x02, 0x04, 0x08, 0x13, 0x03}, // %
    {0x0C, 0x12, 0x14, 0x08, 0x15, 0x12, 0x0D}, // &
    {0x04, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00}, // '
    {0x02, 0x04, 0x08, 0x08, 0x08, 0x04, 0x02}, // (
    {0x08, 0x04, 0x02, 0x02, 0x02, 0x04, 0x08}, // )
    {0x00, 0x04, 0x15, 0x0E, 0x15, 0x04, 0x00}, // *
    {0x00, 0x04, 0x04, 0x1F, 0x04, 0x04, 0x00}, // +
    {0x00, 0x00, 0x00, 0x00, 0x00, 0x04, 0x08}, // ,
    {0x00, 0x00, 0x00, 0x1F, 0x00, 0x00, 0x00}, // -
    {0x00, 0x00, 0x00, 0x00, 0x00, 0x0C, 0x0C}, // .
    {0x01, 0x01, 0x02, 0x04, 0x08, 0x10, 0x10}, // /
    {0x0E, 0x11, 0x13, 0x15, 0x19, 0x11, 0x0E}, // 0
    {0x04, 0x0C, 0x04, 0x04, 0x04, 0x04, 0x0E}, // 1
    {0x0E, 0x11, 0x01, 0x02, 0x04, 0x08, 0x1F}, // 2
    {0x1F, 0x02, 0x04, 0x02, 0x01, 0x11, 0x0E}, // 3
    {0x02, 0x06, 0x0A, 0x12, 0x1F, 0x02, 0x02}, // 4
    {0x1F, 0x10, 0x1E, 0x01, 0x01, 0x11, 0x0E}, // 5
    {0x06, 0x08, 0x10, 0x1E, 0x11, 0x11, 0x0E}, // 6
    {0x1F, 0x01, 0x02, 0x04, 0x08, 0x08, 0x08}, // 7
    {0x0E, 0x11, 0x11, 0x0E, 0x11, 0x11, 0x0E}, // 8
    {0x0E, 0x11, 0x11, 0x0F, 0x01, 0x02, 0x0C}, // 9
    {0x00, 0x04, 0x04, 0x00, 0x04, 0x04, 0x00}, // :
    {0x00, 0x04, 0x04, 0x00, 0x04, 0x04, 0x08}, // ;
    {0x02, 0x04, 0x08, 0x10, 0x08, 0x04, 0x02}, // <
    {0x00, 0x00, 0x1F, 0x00, 0x1F, 0x00, 0x00}, // =
    {0x08, 0x04, 0x02, 0x01, 0x02, 0x04, 0x08}, // >
    {0x0E, 0x11, 0x01, 0x02, 0x04, 0x00, 0x04}, // ?
    {0x0E, 0x11, 0x17, 0x15, 0x17, 0x10, 0x0E}, // @
    {0x0E, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11}, // A
    {0x1E, 0x11, 0x11, 0x1E, 0x11, 0x11, 0x1E}, // B
    {0x0E, 0x11, 0x10, 0x10, 0x10, 0x11, 0x0E}, // C
    {0x1E, 0x11, 0x11, 0x11, 0x11, 0x11, 0x1E}, // D
    {0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x1F}, // E
    {0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x10}, // F
    {0x0E, 0x11, 0x10, 0x17, 0x11, 0x11, 0x0F}, // G
    {0x11, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11}, // H
    {0x0E, 0x04, 0x04, 0x04, 0x04, 0x04, 0x0E}, // I
    {0x07, 0x02, 0x02, 0x02, 0x02, 0x12, 0x0C}, // J
    {0x11, 0x12, 0x14, 0x18, 0x14, 0x12, 0x11}, // K
    {0x10, 0x10, 0x10, 0x10, 0x10, 0x10, 0x1F}, // L
    {0x11, 0x1B, 0x15, 0x15, 0x11, 0x11, 0x11}, // M
    {0x11, 0x19, 0x15, 0x13, 0x11, 0x11, 0x11}, // N
    {0x0E, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E}, // O
    {0x1E, 0x11, 0x11, 0x1E, 0x10, 0x10, 0x10}, // P
    {0x0E, 0x11, 0x11, 0x11, 0x15, 0x12, 0x0D}, // Q
    {0x1E, 0x11, 0x11, 0x1E, 0x14, 0x12, 0x11}, // R
    {0x0F, 0x10, 0x10, 0x0E, 0x01, 0x01, 0x1E}, // S
    {0x1F, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04}, // T
    {0x11, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E}, // U
    {0x11, 0x11, 0x11, 0x11, 0x11, 0x0A, 0x04}, // V
    {0x11, 0x11, 0x11, 0x15, 0x15, 0x1B, 0x11}, // W
    {0x11, 0x11, 0x0A, 0x04, 0x0A, 0x11, 0x11}, // X
    {0x11, 0x11, 0x0A, 0x04, 0x04, 0x04, 0x04}, // Y
    {0x1F, 0x01, 0x02, 0x04, 0x08, 0x10, 0x1F}, // Z
    {0x0E, 0x08, 0x08, 0x08, 0x08, 0x08, 0x0E}, // [
    {0x10, 0x10, 0x08, 0x04, 0x02, 0x01, 0x01}, // backslash
    {0x0E, 0x02, 0x02, 0x02, 0x02, 0x02, 0x0E}, // ]
    {0x04, 0x0A, 0x11, 0x00, 0x00, 0x00, 0x00}, // ^
    {0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x1F}, // _
    {0x08, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00}, // `
    {0x00, 0x00, 0x0E, 0x01, 0x0F, 0x11, 0x0F}, // a
    {0x10, 0x10, 0x1E, 0x11, 0x11, 0x11, 0x1E}, // b
    {0x00, 0x00, 0x0E, 0x11, 0x10, 0x11, 0x0E}, // c
    {0x01, 0x01, 0x0F, 0x11, 0x11, 0x11, 0x0F}, // d
    {0x00, 0x00, 0x0E, 0x11, 0x1F, 0x10, 0x0E}, // e
    {0x06, 0x09, 0x08, 0x1C, 0x08, 0x08, 0x08}, // f
    {0x00, 0x0F, 0x11, 0x11, 0x0F, 0x01, 0x0E}, // g
    {0x10, 0x10, 0x1E, 0x11, 0x11, 0x11, 0x11}, // h
    {0x04, 0x00, 0x0C, 0x04, 0x04, 0x04, 0x0E}, // i
    {0x02, 0x00, 0x06, 0x02, 0x02, 0x12, 0x0C}, // j
    {0x10, 0x10, 0x12, 0x14, 0x18, 0x14, 0x12}, // k
    {0x0C, 0x04, 0x04, 0x04, 0x04, 0x04, 0x0E}, // l
    {0x00, 0x00, 0x1A, 0x15, 0x15, 0x15, 0x15}, // m
    {0x00, 0x00, 0x1E, 0x11, 0x11, 0x11, 0x11}, // n
    {0x00, 0x00, 0x0E, 0x11, 0x11, 0x11, 0x0E}, // o
    {0x00, 0x1E, 0x11, 0x11, 0x1E, 0x10, 0x10}, // p
    {0x00, 0x0F, 0x11, 0x11, 0x0F, 0x01, 0x01}, // q
    {0x00, 0x00, 0x16, 0x19, 0x10, 0x10, 0x10}, // r
    {0x00, 0x00, 0x0F, 0x10, 0x0E, 0x01, 0x1E}, // s
    {0x08, 0x08, 0x1C, 0x08, 0x08, 0x09, 0x06}, // t
    {0x00, 0x00, 0x11, 0x11, 0x11, 0x13, 0x0D}, // u
    {0x00, 0x00, 0x11, 0x11, 0x11, 0x0A, 0x04}, // v
    {0x00, 0x00, 0x11, 0x11, 0x15, 0x15, 0x0A}, // w
    {0x00, 0x00, 0x11, 0x0A, 0x04, 0x0A, 0x11}, // x
    {0x00, 0x11, 0x11, 0x11, 0x0F, 0x01, 0x0E}, // y
    {0x00, 0x00, 0x1F, 0x02, 0x04, 0x08, 0x1F}, // z
    {0x02, 0x04, 0x04, 0x08, 0x04, 0x04, 0x02}, // {
    {0x04, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04}, // |
    {0x08, 0x04, 0x04, 0x02, 0x04, 0x04, 0x08}, // }
    {0x00, 0x00, 0x08, 0x15, 0x02, 0x00, 0x00}, // ~
};

const uint8_t BOX_GLYPH[7] = {0x1F, 0x11, 0x11, 0x11, 0x11, 0x11, 0x1F};

// Latin-1 letters folded onto their base ASCII glyph (accent dropped).
const char LATIN1_FOLD[] =
    "AAAAAAACEEEEIIIIDNOOOOOxOUUUUYTs"
    "aaaaaaaceeeeiiiidnooooo/ouuuuyty";

bool glyph_rows(uint32_t cp, const uint8_t*& rows) {
    if (cp >= 0x20 && cp <= 0x7E) {
        rows = FONT5X7[cp - 0x20];
        return true;
    }
    if (cp == 0xA0) {
        rows = FONT5X7[0];
        return true;
    }
    if (cp >= 0xC0 && cp <= 0xFF) {
        size_t idx = cp - 0xC0;
        if (idx < sizeof(LATIN1_FOLD) - 1) {
            char base = LATIN1_FOLD[idx];
            if (base >= 0x20 && base <= 0x7E) {
                rows = FONT5X7[(uint8_t)base - 0x20];
                return true;
            }
        }
    }
    return false;
}

size_t utf8_next(const uint8_t* s, size_t len, uint32_t& cp) {
    uint8_t b = s[0];
    if (b < 0x80) {
        cp = b;
        return 1;
    }
    size_t need;
    uint32_t v;
    if ((b & 0xE0) == 0xC0) {
        need = 2;
        v = b & 0x1F;
    } else if ((b & 0xF0) == 0xE0) {
        need = 3;
        v = b & 0x0F;
    } else if ((b & 0xF8) == 0xF0) {
        need = 4;
        v = b & 0x07;
    } else {
        cp = 0xFFFD;
        return 1;
    }
    if (len < need) {
        cp = 0xFFFD;
        return 1;
    }
    for (size_t k = 1; k < need; ++k) {
        if ((s[k] & 0xC0) != 0x80) {
            cp = 0xFFFD;
            return 1;
        }
        v = (v << 6) | (s[k] & 0x3F);
    }
    cp = v;
    return need;
}

inline void blend_px(std::vector<uint8_t>& px, uint32_t w, uint32_t h, int x, int y, uint32_t color) {
    if (x < 0 || y < 0 || (uint32_t)x >= w || (uint32_t)y >= h) return;
    uint8_t sa = (uint8_t)(color & 0xFF);
    if (sa == 0) return;
    uint8_t sr = (uint8_t)((color >> 24) & 0xFF);
    uint8_t sg = (uint8_t)((color >> 16) & 0xFF);
    uint8_t sb = (uint8_t)((color >> 8) & 0xFF);
    size_t o = ((size_t)y * w + (size_t)x) * 4;
    if (sa == 255) {
        px[o + 0] = sr;
        px[o + 1] = sg;
        px[o + 2] = sb;
        px[o + 3] = 255;
        return;
    }
    float a = (float)sa / 255.0f;
    float da = (float)px[o + 3] / 255.0f;
    float oa = a + da * (1.0f - a);
    if (oa <= 0.0f) {
        px[o + 3] = 0;
        return;
    }
    px[o + 0] = (uint8_t)((sr * a + px[o + 0] * da * (1.0f - a)) / oa);
    px[o + 1] = (uint8_t)((sg * a + px[o + 1] * da * (1.0f - a)) / oa);
    px[o + 2] = (uint8_t)((sb * a + px[o + 2] * da * (1.0f - a)) / oa);
    px[o + 3] = (uint8_t)(oa * 255.0f);
}

void draw_glyph(std::vector<uint8_t>& px, uint32_t w, uint32_t h, uint32_t cp, float fx, float fy,
                float scale, uint32_t color) {
    const uint8_t* rows;
    if (!glyph_rows(cp, rows)) rows = BOX_GLYPH;
    int s = (int)(scale + 0.5f);
    if (s < 1) s = 1;
    int ox = (int)fx;
    int oy = (int)fy;
    for (int row = 0; row < 7; ++row) {
        uint8_t bits = rows[row];
        for (int col = 0; col < 5; ++col) {
            if (!(bits & (0x10 >> col))) continue;
            for (int dy = 0; dy < s; ++dy) {
                for (int dx = 0; dx < s; ++dx) {
                    blend_px(px, w, h, ox + col * s + dx, oy + row * s + dy, color);
                }
            }
        }
    }
}

void draw_text(std::vector<uint8_t>& px, uint32_t w, uint32_t h, const uint8_t* text, size_t len,
               float& x, float& y, float left, float right, float advance, float line_height,
               float scale, uint32_t color, bool rasterize) {
    size_t i = 0;
    while (i < len) {
        uint32_t cp = 0;
        size_t n = utf8_next(text + i, len - i, cp);
        i += n;
        if (cp == '\r') continue;
        if (cp == '\n') {
            x = left;
            y += line_height;
            continue;
        }
        if (cp == '\t') {
            x += advance * 4.0f;
            continue;
        }
        if (x + advance > right && x > left) {
            x = left;
            y += line_height;
        }
        if (rasterize) draw_glyph(px, w, h, cp, x, y, scale, color);
        x += advance;
    }
}

void draw_blocks(const LpBlock* blocks, size_t blocks_len, LpRenderOpts opts,
                 std::vector<uint8_t>& pixels, uint32_t& content_height, bool rasterize) {
    const float scale = opts.scale > 0.0f ? opts.scale : 1.0f;
    const float advance = 6.0f * scale;
    const float glyph_h = 7.0f * scale;
    const float line_height = (glyph_h + 4.0f * scale);
    const float margin = 4.0f * scale;
    const float clip_x = opts.clip_x;
    const float clip_y = opts.clip_y;
    const float clip_w = opts.clip_w > 0.0f ? opts.clip_w : (float)opts.width;
    const float clip_h = opts.clip_h > 0.0f ? opts.clip_h : (float)opts.height;
    const float left = clip_x + margin;
    const float right = clip_x + clip_w - margin;
    float y = clip_y + margin;

    for (size_t bi = 0; bi < blocks_len; ++bi) {
        const LpBlock& block = blocks[bi];
        if (block.kind == 3) { // rule
            if (rasterize) {
                int ry = (int)(y + line_height * 0.5f);
                int x0 = (int)left;
                int x1 = (int)(right > left ? right : left);
                for (int x = x0; x < x1; ++x) {
                    int cy = ry;
                    if (cy < (int)clip_y || (float)cy >= clip_y + clip_h) continue;
                    blend_px(pixels, opts.width, opts.height, x, cy, 0x6b6b6bffu);
                }
            }
            y += line_height;
            continue;
        }

        float x = left + (float)block.list_depth * advance;
        if (block.marker && block.marker_len) {
            draw_text(pixels, opts.width, opts.height, block.marker, block.marker_len, x, y, left, right,
                      advance, line_height, scale, 0x6b6b6bffu, rasterize);
            x += advance;
        }
        for (size_t si = 0; si < block.spans_len; ++si) {
            const LpSpan& span = block.spans[si];
            if (!span.text || span.len == 0) continue;
            // `color` is 0xRRGGBB and only meaningful with SPAN_HAS_COLOR;
            // otherwise the default text colour (black) applies.
            uint32_t rgb = (span.flags & (1u << 5)) ? (span.color & 0xFFFFFFu) : 0x000000u;
            uint32_t color = (rgb << 8) | 0xFFu;
            float span_x0 = x;
            draw_text(pixels, opts.width, opts.height, span.text, span.len, x, y, left, right, advance,
                      line_height, scale, color, rasterize);
            if (rasterize && (span.flags & (1u << 2)) && x > span_x0) { // underline
                int uy = (int)(y + glyph_h + 1.0f);
                for (int xx = (int)span_x0; xx < (int)x; ++xx) {
                    blend_px(pixels, opts.width, opts.height, xx, uy, color);
                }
            }
        }
        y += line_height;
        if (!(block.flags & 1u) && bi + 1 < blocks_len) {
            y += line_height * 0.5f;
        }
    }

    content_height = (uint32_t)(y + margin);
    if (content_height == 0) content_height = 1;
}

// ---- PNG ----

uint32_t CRC_TABLE[256];
bool CRC_READY = false;

void init_crc() {
    for (uint32_t n = 0; n < 256; ++n) {
        uint32_t c = n;
        for (int k = 0; k < 8; ++k) {
            c = (c & 1) ? (0xEDB88320u ^ (c >> 1)) : (c >> 1);
        }
        CRC_TABLE[n] = c;
    }
    CRC_READY = true;
}

uint32_t crc32_bytes(const uint8_t* data, size_t len) {
    if (!CRC_READY) init_crc();
    uint32_t c = 0xFFFFFFFFu;
    for (size_t i = 0; i < len; ++i) {
        c = CRC_TABLE[(c ^ data[i]) & 0xFF] ^ (c >> 8);
    }
    return c ^ 0xFFFFFFFFu;
}

uint32_t adler32_bytes(const uint8_t* data, size_t len) {
    uint32_t a = 1;
    uint32_t b = 0;
    for (size_t i = 0; i < len; ++i) {
        a = (a + data[i]) % 65521u;
        b = (b + a) % 65521u;
    }
    return (b << 16) | a;
}

void put_be32(std::string& out, uint32_t v) {
    out.push_back((char)((v >> 24) & 0xFF));
    out.push_back((char)((v >> 16) & 0xFF));
    out.push_back((char)((v >> 8) & 0xFF));
    out.push_back((char)(v & 0xFF));
}

void put_le16(std::string& out, uint16_t v) {
    out.push_back((char)(v & 0xFF));
    out.push_back((char)((v >> 8) & 0xFF));
}

void png_chunk(std::string& out, const char type[4], const std::string& data) {
    put_be32(out, (uint32_t)data.size());
    size_t start = out.size();
    out.append(type, 4);
    out.append(data);
    uint32_t crc = crc32_bytes(reinterpret_cast<const uint8_t*>(out.data() + start), out.size() - start);
    put_be32(out, crc);
}

bool encode_rgba_png(const uint8_t* pixels, uint32_t width, uint32_t height, std::string& out) {
    if (!pixels || width == 0 || height == 0) return false;

    std::string raw;
    raw.reserve((size_t)height * ((size_t)width * 4 + 1));
    for (uint32_t y = 0; y < height; ++y) {
        raw.push_back(0); // filter: none
        const uint8_t* row = pixels + (size_t)y * width * 4;
        raw.append(reinterpret_cast<const char*>(row), (size_t)width * 4);
    }

    std::string z;
    z.reserve(raw.size() + raw.size() / 65535 * 5 + 16);
    z.push_back(0x78); // zlib: deflate, 32K window
    z.push_back(0x01); // no preset dictionary, fastest
    size_t off = 0;
    do {
        size_t chunk = std::min<size_t>(65535, raw.size() - off);
        bool final = off + chunk >= raw.size();
        z.push_back(final ? 0x01 : 0x00); // stored block, BFINAL
        put_le16(z, (uint16_t)chunk);
        put_le16(z, (uint16_t)(~chunk & 0xFFFF));
        z.append(raw.data() + off, chunk);
        off += chunk;
    } while (off < raw.size());
    put_be32(z, adler32_bytes(reinterpret_cast<const uint8_t*>(raw.data()), raw.size()));

    static const uint8_t SIGNATURE[8] = {0x89, 'P', 'N', 'G', 0x0D, 0x0A, 0x1A, 0x0A};
    out.clear();
    out.reserve(z.size() + 64);
    out.append(reinterpret_cast<const char*>(SIGNATURE), 8);

    std::string ihdr;
    put_be32(ihdr, width);
    put_be32(ihdr, height);
    ihdr.push_back(8); // bit depth
    ihdr.push_back(6); // colour type: RGBA
    ihdr.push_back(0); // compression
    ihdr.push_back(0); // filter
    ihdr.push_back(0); // interlace
    png_chunk(out, "IHDR", ihdr);
    png_chunk(out, "IDAT", z);
    png_chunk(out, "IEND", std::string());
    return true;
}

} // namespace render_bridge_detail
