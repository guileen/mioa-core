// Capability contract for the native C-ABI bridges.
//
// These checks fail while a bridge reports success without doing the work it
// claims (e.g. an HTML parser that invokes no tree-builder callback, or a
// renderer that returns 0 and emits no bytes). They are deliberately
// independent of V8 so `make test` can expose the gap at LEVEL=1.
//
// ABI declarations mirror src/c/html_bridge.cpp and src/c/render_bridge.cpp.

#include <cstdint>
#include <cstddef>
#include <cstdio>
#include <cstring>

extern "C" {

// ---- html_bridge ABI ----
typedef void* Ref;

struct StringSlice {
    const uint8_t* ptr;
    size_t len;
};

struct CNullableStringSlice {
    uint8_t tag;
    StringSlice value;
};

struct CQualName {
    CNullableStringSlice prefix;
    StringSlice ns;
    StringSlice local;
};

struct CNodeOrText {
    uint8_t tag;
    Ref node;
    StringSlice text;
};

struct DecodeResult {
    uint8_t had_errors;
    size_t bytes_read;
    size_t bytes_written;
};

struct EncodingInfo {
    uint8_t found;
    const void* handle;
    size_t name_len;
    const uint8_t* name_ptr;
};

typedef Ref (*CreateElementCallback)(Ref, const void*, CQualName, void*);
typedef Ref (*CreateCommentCallback)(Ref, StringSlice);
typedef void (*AppendDoctypeToDocumentCallback)(Ref, StringSlice, StringSlice, StringSlice);
typedef Ref (*CreateProcessingInstruction)(Ref, StringSlice, StringSlice);
typedef void (*AppendCallback)(Ref, Ref, CNodeOrText);
typedef void (*ParseErrorCallback)(Ref, StringSlice);
typedef void (*PopCallback)(Ref, Ref);
typedef void (*AddAttrsIfMissingCallback)(Ref, Ref, void*);
typedef Ref (*GetTemplateContentsCallback)(Ref, Ref);
typedef uint8_t (*AttachDeclarativeShadowCallback)(Ref, Ref, Ref, uint8_t);
typedef void (*RemoveFromParentCallback)(Ref, Ref);
typedef void (*ReparentChildrenCallback)(Ref, Ref, Ref);
typedef void (*AppendBeforeSiblingCallback)(Ref, Ref, CNodeOrText);
typedef void (*AppendBasedOnParentNodeCallback)(Ref, Ref, Ref, CNodeOrText);

void html5ever_parse_document(
    uint8_t* html,
    size_t len,
    Ref document,
    Ref ctx,
    CreateElementCallback create_element,
    AppendCallback append,
    ParseErrorCallback parse_error,
    PopCallback pop,
    CreateCommentCallback create_comment,
    CreateProcessingInstruction create_pi,
    AppendDoctypeToDocumentCallback append_doctype,
    AddAttrsIfMissingCallback add_attrs,
    GetTemplateContentsCallback get_template,
    RemoveFromParentCallback remove_from_parent,
    ReparentChildrenCallback reparent_children,
    AppendBeforeSiblingCallback append_before_sibling,
    AppendBasedOnParentNodeCallback append_based_on_parent,
    AttachDeclarativeShadowCallback attach_shadow,
    bool allow_declarative_shadow);

EncodingInfo encoding_for_label(const uint8_t* label, size_t len);
DecodeResult encoding_decode(
    const void* handle,
    const uint8_t* input,
    size_t input_len,
    uint8_t* output,
    size_t output_len,
    uint8_t is_last);

// ---- render_bridge ABI ----
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

typedef bool (*WriteCallback)(void*, const uint8_t*, size_t);

void* lp_render_new(const void* fonts, size_t fonts_len);
void lp_render_free(void* renderer);
int32_t lp_render_png(
    void* renderer,
    const LpBlock* blocks,
    size_t blocks_len,
    LpRenderOpts opts,
    uint32_t* content_height,
    void* ctx,
    WriteCallback write);

void* lp_canvas_new(uint32_t width, uint32_t height, const uint8_t* fonts, size_t len);
void lp_canvas_free(void* canvas);
bool lp_canvas_put(void* canvas, const uint8_t* data, uint32_t width, uint32_t height, int32_t x, int32_t y);
bool lp_canvas_pixels(const void* canvas, uint8_t* out, size_t len);

// ---------------------------------------------------------------------------
// The rest of the surface the Zig engine imports. These declarations exist so
// the contract links every symbol the engine needs: a bridge that loses one
// must fail `make bridge-contract`, not only the engine link. Their parameter
// lists are irrelevant here because nothing calls them through these
// declarations (C linkage keys on the name alone).
// ---------------------------------------------------------------------------
void html5ever_parse_document_with_encoding();
void html5ever_parse_fragment();
void* html5ever_streaming_parser_create();
int32_t html5ever_streaming_parser_feed();
void html5ever_streaming_parser_finish();
void html5ever_streaming_parser_destroy();
void xml5ever_parse_document();
void xml5ever_validate_document();
void html5ever_attribute_iterator_next();
size_t html5ever_attribute_iterator_count();
uint8_t html5ever_named_character_reference();
void* encoding_decoder_new();
void encoding_decoder_free();
struct EncodeResult {
    uint8_t had_errors;
    size_t bytes_read;
    size_t bytes_written;
};
DecodeResult encoding_decoder_decode();
size_t encoding_max_utf8_buffer_length();
size_t encoding_max_encode_buffer_length();
EncodeResult encoding_encode_with_ncr();

} // extern "C"

namespace {

int failures = 0;
int passes = 0;

void report(const char* name, bool ok, const char* detail) {
    std::printf("[bridge-contract] %s %-16s %s\n", ok ? "PASS" : "FAIL", name, detail);
    if (ok) {
        ++passes;
    } else {
        ++failures;
    }
}

struct HtmlProbe {
    int elements = 0;
    int appends = 0;
};

Ref on_create_element(Ref ctx, const void*, CQualName, void*) {
    static_cast<HtmlProbe*>(ctx)->elements += 1;
    return ctx;
}
Ref on_create_comment(Ref ctx, StringSlice) { return ctx; }
Ref on_create_pi(Ref ctx, StringSlice, StringSlice) { return ctx; }
Ref on_get_template(Ref ctx, Ref) { return ctx; }
void on_append(Ref ctx, Ref, CNodeOrText) { static_cast<HtmlProbe*>(ctx)->appends += 1; }
void on_parse_error(Ref, StringSlice) {}
void on_pop(Ref, Ref) {}
void on_append_doctype(Ref, StringSlice, StringSlice, StringSlice) {}
void on_add_attrs(Ref, Ref, void*) {}
void on_remove_from_parent(Ref, Ref) {}
void on_reparent(Ref, Ref, Ref) {}
void on_append_before(Ref, Ref, CNodeOrText) {}
void on_append_based_on_parent(Ref, Ref, Ref, CNodeOrText) {}
uint8_t on_attach_shadow(Ref, Ref, Ref, uint8_t) { return 0; }

struct Sink {
    size_t bytes = 0;
};

bool on_write(void* ctx, const uint8_t*, size_t len) {
    static_cast<Sink*>(ctx)->bytes += len;
    return true;
}

void check_html_parse() {
    HtmlProbe probe;
    uint8_t html[] = "<p>hi</p>";
    html5ever_parse_document(
        html, sizeof(html) - 1, &probe, &probe,
        on_create_element, on_append, on_parse_error, on_pop,
        on_create_comment, on_create_pi, on_append_doctype, on_add_attrs,
        on_get_template, on_remove_from_parent, on_reparent,
        on_append_before, on_append_based_on_parent, on_attach_shadow, false);
    char detail[96];
    std::snprintf(detail, sizeof(detail), "createElement=%d append=%d (expected >=1 each)",
                  probe.elements, probe.appends);
    report("html.parse", probe.elements >= 1 && probe.appends >= 1, detail);
}

void check_render_png() {
    const uint8_t text[] = "hello";
    LpSpan span = {text, sizeof(text) - 1, 0, 0xffffffffu, nullptr, 0};
    LpBlock block = {&span, 1, nullptr, 0, 0, 0, 0, 0, 0};
    LpRenderOpts opts = {100, 100, 0, 0, 100, 100, 1.0f, 0};
    Sink sink;
    uint32_t content_height = 0;
    void* renderer = lp_render_new(nullptr, 0);
    int32_t rc = lp_render_png(renderer, &block, 1, opts, &content_height, &sink, on_write);
    lp_render_free(renderer);
    char detail[96];
    std::snprintf(detail, sizeof(detail), "rc=%d bytes=%zu (success must emit bytes)", rc, sink.bytes);
    report("render.png", rc == 0 && sink.bytes > 0, detail);
}

void check_canvas_put() {
    void* canvas = lp_canvas_new(2, 2, nullptr, 0);
    const uint8_t red[4] = {255, 0, 0, 255};
    bool put = lp_canvas_put(canvas, red, 1, 1, 0, 0);
    uint8_t pixels[16] = {0};
    bool got = lp_canvas_pixels(canvas, pixels, sizeof(pixels));
    lp_canvas_free(canvas);
    bool ok = put && got && pixels[0] == 255 && pixels[1] == 0 && pixels[2] == 0 && pixels[3] == 255;
    char detail[96];
    std::snprintf(detail, sizeof(detail), "pixel(0,0)=(%u,%u,%u,%u) expected (255,0,0,255)",
                  pixels[0], pixels[1], pixels[2], pixels[3]);
    report("canvas.put", ok, detail);
}

void check_encoding_decode() {
    const uint8_t label[] = "iso-8859-1";
    EncodingInfo info = encoding_for_label(label, sizeof(label) - 1);
    uint8_t input = 0xE9; // 'e' with acute in iso-8859-1 / windows-1252
    uint8_t output[8] = {0};
    DecodeResult res = encoding_decode(info.handle, &input, 1, output, sizeof(output), 1);
    bool ok = info.found == 1 && res.bytes_written == 2 && output[0] == 0xC3 && output[1] == 0xA9;
    char detail[96];
    std::snprintf(detail, sizeof(detail), "label_found=%u bytes=%zu first=%02X%02X expected 2 C3A9",
                  info.found, res.bytes_written, output[0], output[1]);
    report("encoding.decode", ok, detail);
}

// Every symbol the Zig sink imports must resolve at link time. Writing the
// addresses through a volatile sink keeps the relocations, so deleting a
// bridge definition fails here instead of at the engine link.
void check_link_surface() {
    // `volatile` qualifies the pointer object, not the pointee, so every
    // address store is an observable side effect and keeps its relocation.
    const void* volatile sink = nullptr;
    sink = (const void*)&html5ever_parse_document;
    sink = (const void*)&html5ever_parse_document_with_encoding;
    sink = (const void*)&html5ever_parse_fragment;
    sink = (const void*)&html5ever_streaming_parser_create;
    sink = (const void*)&html5ever_streaming_parser_feed;
    sink = (const void*)&html5ever_streaming_parser_finish;
    sink = (const void*)&html5ever_streaming_parser_destroy;
    sink = (const void*)&xml5ever_parse_document;
    sink = (const void*)&xml5ever_validate_document;
    sink = (const void*)&html5ever_attribute_iterator_next;
    sink = (const void*)&html5ever_attribute_iterator_count;
    sink = (const void*)&html5ever_named_character_reference;
    sink = (const void*)&encoding_for_label;
    sink = (const void*)&encoding_decode;
    sink = (const void*)&encoding_decoder_new;
    sink = (const void*)&encoding_decoder_decode;
    sink = (const void*)&encoding_decoder_free;
    sink = (const void*)&encoding_max_utf8_buffer_length;
    sink = (const void*)&encoding_max_encode_buffer_length;
    sink = (const void*)&encoding_encode_with_ncr;
    sink = (const void*)&lp_render_new;
    sink = (const void*)&lp_render_free;
    sink = (const void*)&lp_render_png;
    sink = (const void*)&lp_canvas_new;
    sink = (const void*)&lp_canvas_free;
    sink = (const void*)&lp_canvas_put;
    sink = (const void*)&lp_canvas_pixels;
    report("abi.symbols", sink != nullptr, "engine-imported C symbols resolved");
}

} // namespace

int main() {
    check_html_parse();
    check_render_png();
    check_canvas_put();
    check_encoding_decode();
    check_link_surface();
    std::printf("[bridge-contract] %d passed, %d failed\n", passes, failures);
    if (failures != 0) {
        std::printf("[bridge-contract] fix: runtime/engine/src/c/{html_bridge,render_bridge}.cpp "
                    "or restore the deleted src/rust implementation\n");
    }
    return failures == 0 ? 0 : 1;
}
