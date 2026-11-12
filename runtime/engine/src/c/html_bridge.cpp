// C-ABI bridge for the HTML5 / XML tree builder and the encoding API.
//
// This is a native implementation: a tokenizer plus a tree-construction state
// machine that drives the sink callbacks declared by
// src/browser/parser/html5ever.zig. It replaces the removed Rust html5ever /
// encoding_rs crates without changing the Zig-visible ABI.
//
// Scope notes (documented, not silent):
//   * The tree builder implements the common HTML5 insertion modes: implied
//     html/head/body, void elements, raw-text/RCDATA elements, the usual
//     auto-closing (p, li, dd/dt, option, table cells/rows, headings),
//     comments, doctypes, character references and parse errors.
//   * It does not implement the adoption agency algorithm, foster parenting,
//     <template> content documents or full SVG name casing. Those paths build
//     a flat approximation; see the CHANGELOG entry.
//   * The streaming parser feeds an incremental tokenizer; `document.write`
//     re-entrancy is serialized by the Zig caller.

#include <cstdint>
#include <cstddef>
#include <cstring>
#include <cstdlib>
#include <cctype>
#include <string>
#include <vector>
#include <algorithm>

template <typename T>
struct CNullable {
    uint8_t tag; // 0 = None, 1 = Some
    T value;
};

extern "C" {

typedef void* Ref;

struct StringSlice {
    const uint8_t* ptr;
    size_t len;
};

struct CQualName {
    CNullable<StringSlice> prefix;
    StringSlice ns;
    StringSlice local;
};

struct CAttribute {
    CQualName name;
    StringSlice value;
};

struct CNodeOrText {
    uint8_t tag; // 0 = node, 1 = text
    Ref node;
    StringSlice text;
};

typedef Ref (*CreateElementCallback)(Ref ctx, const void* data, CQualName name, void* attributes);
typedef Ref (*CreateCommentCallback)(Ref ctx, StringSlice str);
typedef void (*AppendDoctypeToDocumentCallback)(Ref ctx, StringSlice name, StringSlice public_id, StringSlice system_id);
typedef Ref (*CreateProcessingInstruction)(Ref ctx, StringSlice target, StringSlice data);
typedef void (*AppendCallback)(Ref ctx, Ref parent, CNodeOrText node_or_text);
typedef void (*ParseErrorCallback)(Ref ctx, StringSlice str);
typedef void (*PopCallback)(Ref ctx, Ref node);
typedef void (*AddAttrsIfMissingCallback)(Ref ctx, Ref target, void* attributes);
typedef Ref (*GetTemplateContentsCallback)(Ref ctx, Ref target);
typedef uint8_t (*AttachDeclarativeShadowCallback)(Ref ctx, Ref host, Ref template_ref, uint8_t mode_is_open);
typedef void (*RemoveFromParentCallback)(Ref ctx, Ref target);
typedef void (*ReparentChildrenCallback)(Ref ctx, Ref node, Ref new_parent);
typedef void (*AppendBeforeSiblingCallback)(Ref ctx, Ref sibling, CNodeOrText node_or_text);
typedef void (*AppendBasedOnParentNodeCallback)(Ref ctx, Ref element, Ref prev_element, CNodeOrText node_or_text);

// Internal attribute representation, iterated by the Zig sink through
// html5ever_attribute_iterator_next/count. The callback parameter is the
// single pointer field of Zig's `AttributeIterator` struct (passed by value,
// which is the pointer itself on every supported ABI).
struct InternalAttr {
    std::string prefix;
    std::string ns;
    std::string local;
    std::string value;
};

struct CAttributeIterator {
    std::vector<InternalAttr> attrs;
    size_t pos;
};

struct EncodingInfo {
    uint8_t found;
    const void* handle;
    size_t name_len;
    const uint8_t* name_ptr;
};

struct DecodeResult {
    uint8_t had_errors;
    size_t bytes_read;
    size_t bytes_written;
};

struct EncodeResult {
    uint8_t had_errors;
    size_t bytes_read;
    size_t bytes_written;
};

CNullable<CAttribute> html5ever_attribute_iterator_next(const void* c_iter);
size_t html5ever_attribute_iterator_count(const void* c_iter);
uint8_t html5ever_named_character_reference(const uint8_t* name, size_t len, uint32_t out[2]);

EncodingInfo encoding_for_label(const uint8_t* label, size_t label_len);
size_t encoding_max_utf8_buffer_length(const void* handle, size_t input_len);
DecodeResult encoding_decode(const void* handle, const uint8_t* input, size_t input_len, uint8_t* output, size_t output_len, uint8_t is_last);
void* encoding_decoder_new(const void* handle);
DecodeResult encoding_decoder_decode(void* decoder_ptr, const uint8_t* input, size_t input_len, uint8_t* output, size_t output_len, uint8_t is_last);
void encoding_decoder_free(void* decoder_ptr);
size_t encoding_max_encode_buffer_length(const void* handle, size_t input_len);
EncodeResult encoding_encode_with_ncr(const void* handle, const uint8_t* input, size_t input_len, uint8_t* output, size_t output_len);

void html5ever_parse_document(uint8_t* html, size_t len, Ref document, Ref ctx,
    CreateElementCallback create_element, AppendCallback append, ParseErrorCallback parse_error,
    PopCallback pop, CreateCommentCallback create_comment, CreateProcessingInstruction create_pi,
    AppendDoctypeToDocumentCallback append_doctype, AddAttrsIfMissingCallback add_attrs,
    GetTemplateContentsCallback get_template, RemoveFromParentCallback remove_from_parent,
    ReparentChildrenCallback reparent_children, AppendBeforeSiblingCallback append_before_sibling,
    AppendBasedOnParentNodeCallback append_based_on_parent, AttachDeclarativeShadowCallback attach_shadow,
    bool allow_declarative_shadow);

void html5ever_parse_document_with_encoding(uint8_t* html, size_t len, const uint8_t* charset,
    size_t charset_len, Ref document, Ref ctx,
    CreateElementCallback create_element, AppendCallback append, ParseErrorCallback parse_error,
    PopCallback pop, CreateCommentCallback create_comment, CreateProcessingInstruction create_pi,
    AppendDoctypeToDocumentCallback append_doctype, AddAttrsIfMissingCallback add_attrs,
    GetTemplateContentsCallback get_template, RemoveFromParentCallback remove_from_parent,
    ReparentChildrenCallback reparent_children, AppendBeforeSiblingCallback append_before_sibling,
    AppendBasedOnParentNodeCallback append_based_on_parent, AttachDeclarativeShadowCallback attach_shadow,
    bool allow_declarative_shadow);

void html5ever_parse_fragment(uint8_t* html, size_t len, const uint8_t* context_name, size_t context_name_len,
    uint8_t context_ns, Ref document, Ref ctx,
    CreateElementCallback create_element, CreateElementCallback create_context_element,
    AppendCallback append, ParseErrorCallback parse_error, PopCallback pop,
    CreateCommentCallback create_comment, CreateProcessingInstruction create_pi,
    AppendDoctypeToDocumentCallback append_doctype, AddAttrsIfMissingCallback add_attrs,
    GetTemplateContentsCallback get_template, RemoveFromParentCallback remove_from_parent,
    ReparentChildrenCallback reparent_children, AppendBeforeSiblingCallback append_before_sibling,
    AppendBasedOnParentNodeCallback append_based_on_parent, AttachDeclarativeShadowCallback attach_shadow,
    bool allow_declarative_shadow);

void* html5ever_streaming_parser_create(Ref document, Ref ctx,
    CreateElementCallback create_element, AppendCallback append, ParseErrorCallback parse_error,
    PopCallback pop, CreateCommentCallback create_comment, CreateProcessingInstruction create_pi,
    AppendDoctypeToDocumentCallback append_doctype, AddAttrsIfMissingCallback add_attrs,
    GetTemplateContentsCallback get_template, RemoveFromParentCallback remove_from_parent,
    ReparentChildrenCallback reparent_children, AppendBeforeSiblingCallback append_before_sibling,
    AppendBasedOnParentNodeCallback append_based_on_parent, AttachDeclarativeShadowCallback attach_shadow,
    bool allow_declarative_shadow);
int32_t html5ever_streaming_parser_feed(void* parser_ptr, const uint8_t* html, size_t len);
void html5ever_streaming_parser_finish(void* parser_ptr);
void html5ever_streaming_parser_destroy(void* parser_ptr);

void xml5ever_parse_document(uint8_t* xml, size_t len, Ref document, Ref ctx,
    CreateElementCallback create_element, AppendCallback append, ParseErrorCallback parse_error,
    PopCallback pop, CreateCommentCallback create_comment, CreateProcessingInstruction create_pi,
    AppendDoctypeToDocumentCallback append_doctype, AddAttrsIfMissingCallback add_attrs,
    GetTemplateContentsCallback get_template, RemoveFromParentCallback remove_from_parent,
    ReparentChildrenCallback reparent_children, AppendBeforeSiblingCallback append_before_sibling,
    AppendBasedOnParentNodeCallback append_based_on_parent, AttachDeclarativeShadowCallback attach_shadow,
    bool allow_declarative_shadow);

void xml5ever_validate_document(uint8_t* xml, size_t len, Ref ctx, ParseErrorCallback parse_error);

} // extern "C"

namespace {

const char NS_HTML[] = "http://www.w3.org/1999/xhtml";
const char NS_SVG[] = "http://www.w3.org/2000/svg";
const char NS_MATHML[] = "http://www.w3.org/1998/Math/MathML";
const char NS_XML[] = "http://www.w3.org/XML/1998/namespace";
const char NS_XMLNS[] = "http://www.w3.org/2000/xmlns/";

// ---------------------------------------------------------------------------
// Encoding
// ---------------------------------------------------------------------------

enum EncKind { ENC_UTF8 = 0, ENC_WIN1252, ENC_LATIN1, ENC_UTF16LE, ENC_UTF16BE };

struct EncTag {
    EncKind kind;
    const char* name;
};

const EncTag TAG_UTF8 = {ENC_UTF8, "UTF-8"};
const EncTag TAG_WIN1252 = {ENC_WIN1252, "windows-1252"};
const EncTag TAG_LATIN1 = {ENC_LATIN1, "ISO-8859-1"};
const EncTag TAG_UTF16LE = {ENC_UTF16LE, "UTF-16LE"};
const EncTag TAG_UTF16BE = {ENC_UTF16BE, "UTF-16BE"};

struct EncLabel {
    const char* label;
    const EncTag* tag;
};

// WHATWG-style label map. Browsers fold the Latin-1 labels onto
// windows-1252; this bridge keeps ISO-8859-1 distinct because the Zig side
// reports the canonical name it was given.
const EncLabel ENCODING_LABELS[] = {
    {"utf-8", &TAG_UTF8},
    {"utf8", &TAG_UTF8},
    {"unicode-1-1-utf-8", &TAG_UTF8},
    {"unicode11utf8", &TAG_UTF8},
    {"utf-16", &TAG_UTF16LE},
    {"utf-16le", &TAG_UTF16LE},
    {"ucs-2", &TAG_UTF16LE},
    {"utf-16be", &TAG_UTF16BE},
    {"iso-8859-1", &TAG_LATIN1},
    {"iso8859-1", &TAG_LATIN1},
    {"iso_8859-1", &TAG_LATIN1},
    {"iso-ir-100", &TAG_LATIN1},
    {"latin1", &TAG_LATIN1},
    {"l1", &TAG_LATIN1},
    {"cp819", &TAG_LATIN1},
    {"ibm819", &TAG_LATIN1},
    {"csisolatin1", &TAG_LATIN1},
    {"windows-1252", &TAG_WIN1252},
    {"cp1252", &TAG_WIN1252},
    {"x-cp1252", &TAG_WIN1252},
    {"ascii", &TAG_WIN1252},
    {"us-ascii", &TAG_WIN1252},
    {"ansi_x3.4-1968", &TAG_WIN1252},
};

// windows-1252 code points for 0x80..0x9F.
const uint16_t WIN1252_HIGH[32] = {
    0x20AC, 0x0081, 0x201A, 0x0192, 0x201E, 0x2026, 0x2020, 0x2021,
    0x02C6, 0x2030, 0x0160, 0x2039, 0x0152, 0x008D, 0x017D, 0x008F,
    0x0090, 0x2018, 0x2019, 0x201C, 0x201D, 0x2022, 0x2013, 0x2014,
    0x02DC, 0x2122, 0x0161, 0x203A, 0x0153, 0x009D, 0x017E, 0x0178,
};

bool ascii_ws(uint8_t c) {
    return c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\f';
}

char ascii_lower(char c) {
    return (c >= 'A' && c <= 'Z') ? (char)(c - 'A' + 'a') : c;
}

bool ascii_alpha(uint8_t c) {
    return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z');
}

bool ascii_alnum(uint8_t c) {
    return ascii_alpha(c) || (c >= '0' && c <= '9');
}

bool is_all_ws(const std::string& s) {
    for (char ch : s) {
        if (!ascii_ws((uint8_t)ch)) return false;
    }
    return true;
}

void append_utf8(std::string& out, uint32_t cp) {
    if (cp <= 0x7F) {
        out.push_back((char)cp);
    } else if (cp <= 0x7FF) {
        out.push_back((char)(0xC0 | (cp >> 6)));
        out.push_back((char)(0x80 | (cp & 0x3F)));
    } else if (cp <= 0xFFFF) {
        out.push_back((char)(0xE0 | (cp >> 12)));
        out.push_back((char)(0x80 | ((cp >> 6) & 0x3F)));
        out.push_back((char)(0x80 | (cp & 0x3F)));
    } else {
        out.push_back((char)(0xF0 | (cp >> 18)));
        out.push_back((char)(0x80 | ((cp >> 12) & 0x3F)));
        out.push_back((char)(0x80 | ((cp >> 6) & 0x3F)));
        out.push_back((char)(0x80 | (cp & 0x3F)));
    }
}

uint32_t win1252_cp(uint8_t b) {
    if (b >= 0x80 && b <= 0x9F) return WIN1252_HIGH[b - 0x80];
    return b;
}

int win1252_byte(uint32_t cp) {
    if (cp < 0x80) return (int)cp;
    if (cp >= 0xA0 && cp <= 0xFF) return (int)cp;
    for (int i = 0; i < 32; ++i) {
        if (WIN1252_HIGH[i] == cp) return 0x80 + i;
    }
    return -1;
}

std::string lower_nospace_label(const uint8_t* label, size_t len) {
    std::string s;
    s.reserve(len);
    for (size_t i = 0; i < len; ++i) {
        char c = (char)label[i];
        if (ascii_ws((uint8_t)c)) continue;
        s.push_back(ascii_lower(c));
    }
    return s;
}

const EncTag* find_encoding(const uint8_t* label, size_t len) {
    std::string norm = lower_nospace_label(label, len);
    for (const EncLabel& entry : ENCODING_LABELS) {
        if (norm == entry.label) return entry.tag;
    }
    return nullptr;
}

// Decodes `input` from `kind` into UTF-8, appending to `out`. Stops when the
// output buffer would overflow; `consumed` reports the input bytes handled.
void decode_into(EncKind kind, const uint8_t* input, size_t input_len, std::string& out, size_t limit,
                 size_t& consumed, bool& had_errors, bool is_last) {
    consumed = 0;
    had_errors = false;
    size_t i = 0;
    if (kind == ENC_UTF16LE || kind == ENC_UTF16BE) {
        bool little = kind == ENC_UTF16LE;
        // BOM sniffing (first call, nothing consumed yet).
        if (input_len >= 2) {
            uint16_t bom = little ? (uint16_t)(input[0] | (input[1] << 8))
                                  : (uint16_t)((input[0] << 8) | input[1]);
            if (bom == 0xFFFE) {
                little = !little;
                i = 2;
            } else if (bom == 0xFEFF) {
                i = 2;
            }
        }
        while (i + 1 < input_len) {
            uint16_t unit = little ? (uint16_t)(input[i] | (input[i + 1] << 8))
                                   : (uint16_t)((input[i] << 8) | input[i + 1]);
            i += 2;
            uint32_t cp = unit;
            if (unit >= 0xD800 && unit <= 0xDBFF && i + 1 < input_len) {
                uint16_t lo = little ? (uint16_t)(input[i] | (input[i + 1] << 8))
                                     : (uint16_t)((input[i] << 8) | input[i + 1]);
                if (lo >= 0xDC00 && lo <= 0xDFFF) {
                    cp = 0x10000 + (((uint32_t)(unit - 0xD800)) << 10) + (lo - 0xDC00);
                    i += 2;
                } else {
                    had_errors = true;
                    cp = 0xFFFD;
                }
            } else if (unit >= 0xD800 && unit <= 0xDFFF) {
                had_errors = true;
                cp = 0xFFFD;
            }
            if (out.size() + 4 > limit) {
                i -= 2;
                break;
            }
            append_utf8(out, cp);
        }
        if (i < input_len && is_last) {
            had_errors = true;
            i = input_len;
        }
        consumed = i;
        return;
    }

    if (kind == ENC_UTF8) {
        while (i < input_len) {
            uint8_t b = input[i];
            size_t need = 0;
            uint32_t cp = 0;
            if (b < 0x80) {
                cp = b;
                need = 1;
            } else if ((b & 0xE0) == 0xC0) {
                cp = b & 0x1F;
                need = 2;
            } else if ((b & 0xF0) == 0xE0) {
                cp = b & 0x0F;
                need = 3;
            } else if ((b & 0xF8) == 0xF0) {
                cp = b & 0x07;
                need = 4;
            } else {
                had_errors = true;
                cp = 0xFFFD;
                need = 1;
            }
            if (need > 1) {
                if (i + need > input_len) {
                    if (!is_last) break;
                    had_errors = true;
                    cp = 0xFFFD;
                    need = 1;
                } else {
                    bool ok = true;
                    for (size_t k = 1; k < need; ++k) {
                        uint8_t c = input[i + k];
                        if ((c & 0xC0) != 0x80) {
                            ok = false;
                            break;
                        }
                        cp = (cp << 6) | (c & 0x3F);
                    }
                    if (!ok) {
                        had_errors = true;
                        cp = 0xFFFD;
                        need = 1;
                    }
                }
            }
            if (out.size() + 4 > limit) break;
            append_utf8(out, cp);
            i += need;
        }
        consumed = i;
        return;
    }

    // Single-byte encodings.
    while (i < input_len) {
        uint8_t b = input[i++];
        uint32_t cp = kind == ENC_WIN1252 ? win1252_cp(b) : (uint32_t)b;
        if (out.size() + 4 > limit) {
            --i;
            break;
        }
        append_utf8(out, cp);
    }
    consumed = i;
}

void encode_cp(std::string& out, EncKind kind, uint32_t cp) {
    switch (kind) {
    case ENC_UTF8:
        append_utf8(out, cp);
        return;
    case ENC_LATIN1:
        if (cp <= 0xFF) {
            out.push_back((char)cp);
            return;
        }
        break;
    case ENC_WIN1252: {
        int b = win1252_byte(cp);
        if (b >= 0) {
            out.push_back((char)b);
            return;
        }
        break;
    }
    case ENC_UTF16LE:
    case ENC_UTF16BE: {
        bool little = kind == ENC_UTF16LE;
        if (cp <= 0xFFFF) {
            uint16_t u = (uint16_t)cp;
            if (little) {
                out.push_back((char)(u & 0xFF));
                out.push_back((char)(u >> 8));
            } else {
                out.push_back((char)(u >> 8));
                out.push_back((char)(u & 0xFF));
            }
            return;
        }
        uint32_t v = cp - 0x10000;
        uint16_t hi = (uint16_t)(0xD800 + (v >> 10));
        uint16_t lo = (uint16_t)(0xDC00 + (v & 0x3FF));
        for (uint16_t u : {hi, lo}) {
            if (little) {
                out.push_back((char)(u & 0xFF));
                out.push_back((char)(u >> 8));
            } else {
                out.push_back((char)(u >> 8));
                out.push_back((char)(u & 0xFF));
            }
        }
        return;
    }
    }
    // Unrepresentable: numeric character reference.
    char tmp[16];
    int n = std::snprintf(tmp, sizeof(tmp), "&#x%X;", cp);
    if (n > 0) out.append(tmp, (size_t)n);
}

// ---------------------------------------------------------------------------
// Character references
// ---------------------------------------------------------------------------

struct NamedEntity {
    const char* name;
    uint32_t cp;
};

const NamedEntity NAMED_ENTITIES[] = {
    {"amp", 0x26}, {"lt", 0x3C}, {"gt", 0x3E}, {"quot", 0x22}, {"apos", 0x27},
    {"nbsp", 0xA0}, {"iexcl", 0xA1}, {"cent", 0xA2}, {"pound", 0xA3}, {"curren", 0xA4},
    {"yen", 0xA5}, {"brvbar", 0xA6}, {"sect", 0xA7}, {"uml", 0xA8}, {"copy", 0xA9},
    {"ordf", 0xAA}, {"laquo", 0xAB}, {"not", 0xAC}, {"shy", 0xAD}, {"reg", 0xAE},
    {"macr", 0xAF}, {"deg", 0xB0}, {"plusmn", 0xB1}, {"sup2", 0xB2}, {"sup3", 0xB3},
    {"acute", 0xB4}, {"micro", 0xB5}, {"para", 0xB6}, {"middot", 0xB7}, {"cedil", 0xB8},
    {"sup1", 0xB9}, {"ordm", 0xBA}, {"raquo", 0xBB}, {"frac14", 0xBC}, {"frac12", 0xBD},
    {"frac34", 0xBE}, {"iquest", 0xBF}, {"Agrave", 0xC0}, {"Aacute", 0xC1}, {"Acirc", 0xC2},
    {"Atilde", 0xC3}, {"Auml", 0xC4}, {"Aring", 0xC5}, {"AElig", 0xC6}, {"Ccedil", 0xC7},
    {"Egrave", 0xC8}, {"Eacute", 0xC9}, {"Ecirc", 0xCA}, {"Euml", 0xCB}, {"Igrave", 0xCC},
    {"Iacute", 0xCD}, {"Icirc", 0xCE}, {"Iuml", 0xCF}, {"ETH", 0xD0}, {"Ntilde", 0xD1},
    {"Ograve", 0xD2}, {"Oacute", 0xD3}, {"Ocirc", 0xD4}, {"Otilde", 0xD5}, {"Ouml", 0xD6},
    {"times", 0xD7}, {"Oslash", 0xD8}, {"Ugrave", 0xD9}, {"Uacute", 0xDA}, {"Ucirc", 0xDB},
    {"Uuml", 0xDC}, {"Yacute", 0xDD}, {"THORN", 0xDE}, {"szlig", 0xDF}, {"agrave", 0xE0},
    {"aacute", 0xE1}, {"acirc", 0xE2}, {"atilde", 0xE3}, {"auml", 0xE4}, {"aring", 0xE5},
    {"aelig", 0xE6}, {"ccedil", 0xE7}, {"egrave", 0xE8}, {"eacute", 0xE9}, {"ecirc", 0xEA},
    {"euml", 0xEB}, {"igrave", 0xEC}, {"iacute", 0xED}, {"icirc", 0xEE}, {"iuml", 0xEF},
    {"eth", 0xF0}, {"ntilde", 0xF1}, {"ograve", 0xF2}, {"oacute", 0xF3}, {"ocirc", 0xF4},
    {"otilde", 0xF5}, {"ouml", 0xF6}, {"divide", 0xF7}, {"oslash", 0xF8}, {"ugrave", 0xF9},
    {"uacute", 0xFA}, {"ucirc", 0xFB}, {"uuml", 0xFC}, {"yacute", 0xFD}, {"thorn", 0xFE},
    {"yuml", 0xFF},
    {"OElig", 0x152}, {"oelig", 0x153}, {"Scaron", 0x160}, {"scaron", 0x161},
    {"Yuml", 0x178}, {"fnof", 0x192}, {"circ", 0x2C6}, {"tilde", 0x2DC},
    {"ensp", 0x2002}, {"emsp", 0x2003}, {"thinsp", 0x2009}, {"zwnj", 0x200C},
    {"zwj", 0x200D}, {"lrm", 0x200E}, {"rlm", 0x200F}, {"ndash", 0x2013},
    {"mdash", 0x2014}, {"lsquo", 0x2018}, {"rsquo", 0x2019}, {"sbquo", 0x201A},
    {"ldquo", 0x201C}, {"rdquo", 0x201D}, {"bdquo", 0x201E}, {"dagger", 0x2020},
    {"Dagger", 0x2021}, {"bull", 0x2022}, {"hellip", 0x2026}, {"permil", 0x2030},
    {"prime", 0x2032}, {"Prime", 0x2033}, {"lsaquo", 0x2039}, {"rsaquo", 0x203A},
    {"oline", 0x203E}, {"frasl", 0x2044}, {"euro", 0x20AC}, {"trade", 0x2122},
    {"larr", 0x2190}, {"uarr", 0x2191}, {"rarr", 0x2192}, {"darr", 0x2193},
    {"harr", 0x2194}, {"minus", 0x2212}, {"lowast", 0x2217}, {"radic", 0x221A},
    {"infin", 0x221E}, {"sum", 0x2211}, {"prod", 0x220F}, {"part", 0x2202},
    {"int", 0x222B}, {"ne", 0x2260}, {"le", 0x2264}, {"ge", 0x2265},
    {"asymp", 0x2248}, {"equiv", 0x2261}, {"there4", 0x2234}, {"sim", 0x223C},
    {"cong", 0x2245}, {"alpha", 0x3B1}, {"beta", 0x3B2}, {"gamma", 0x3B3},
    {"delta", 0x3B4}, {"epsilon", 0x3B5}, {"lambda", 0x3BB}, {"mu", 0x3BC},
    {"pi", 0x3C0}, {"sigma", 0x3C3}, {"omega", 0x3C9}, {"spades", 0x2660},
    {"clubs", 0x2663}, {"hearts", 0x2665}, {"diams", 0x2666},
};

bool legacy_entity(const std::string& name) {
    static const char* LEGACY[] = {"amp", "lt", "gt", "quot", "nbsp", "copy", "reg", "AMP", "LT", "GT", "QUOT"};
    for (const char* l : LEGACY) {
        if (name == l) return true;
    }
    return false;
}

const NamedEntity* find_entity_prefix(const std::string& name, size_t& matched_len) {
    const NamedEntity* best = nullptr;
    matched_len = 0;
    for (const NamedEntity& e : NAMED_ENTITIES) {
        size_t len = std::strlen(e.name);
        if (len > name.size() || len <= matched_len) continue;
        if (name.compare(0, len, e.name) == 0) {
            best = &e;
            matched_len = len;
        }
    }
    return best;
}

uint32_t remap_numeric(uint32_t cp) {
    if (cp == 0 || cp > 0x10FFFF || (cp >= 0xD800 && cp <= 0xDFFF)) return 0xFFFD;
    if (cp >= 0x80 && cp <= 0x9F) return WIN1252_HIGH[cp - 0x80];
    return cp;
}

std::string decode_entities(const std::string& in, bool in_attribute) {
    (void)in_attribute;
    std::string out;
    out.reserve(in.size());
    size_t i = 0;
    while (i < in.size()) {
        if (in[i] != '&') {
            out.push_back(in[i++]);
            continue;
        }
        size_t j = i + 1;
        if (j < in.size() && in[j] == '#') {
            size_t k = j + 1;
            bool hex = false;
            if (k < in.size() && (in[k] == 'x' || in[k] == 'X')) {
                hex = true;
                ++k;
            }
            size_t digits_start = k;
            uint32_t cp = 0;
            while (k < in.size()) {
                char c = in[k];
                int v;
                if (c >= '0' && c <= '9') v = c - '0';
                else if (hex && c >= 'a' && c <= 'f') v = c - 'a' + 10;
                else if (hex && c >= 'A' && c <= 'F') v = c - 'A' + 10;
                else break;
                cp = cp * (hex ? 16 : 10) + (uint32_t)v;
                if (cp > 0x10FFFF) cp = 0x110000;
                ++k;
            }
            if (k > digits_start) {
                if (k < in.size() && in[k] == ';') ++k;
                append_utf8(out, remap_numeric(cp));
                i = k;
                continue;
            }
        }
        if (j < in.size() && ascii_alnum((uint8_t)in[j])) {
            size_t k = j;
            while (k < in.size() && ascii_alnum((uint8_t)in[k])) ++k;
            bool semi = (k < in.size() && in[k] == ';');
            std::string name = in.substr(j, k - j);
            size_t matched = 0;
            const NamedEntity* e = find_entity_prefix(name, matched);
            bool complete = matched == name.size();
            bool allowed = e != nullptr && (semi || (complete && legacy_entity(name) && k < in.size()));
            if (allowed) {
                append_utf8(out, e->cp);
                i = j + matched + (semi ? 1 : 0);
                continue;
            }
            // A shorter legacy alias at a non-alphanumeric boundary.
            if (e != nullptr && !complete && legacy_entity(in.substr(j, matched))) {
                size_t after = j + matched;
                if (after < in.size() && !ascii_alnum((uint8_t)in[after])) {
                    append_utf8(out, e->cp);
                    i = after;
                    continue;
                }
            }
        }
        out.push_back('&');
        ++i;
    }
    return out;
}

// ---------------------------------------------------------------------------
// HTML tokens
// ---------------------------------------------------------------------------

enum class TokKind { Text, StartTag, EndTag, Comment, Doctype, PI, Eof };

struct Attr {
    std::string prefix;
    std::string ns;
    std::string local;
    std::string value;
};

struct Token {
    TokKind kind = TokKind::Eof;
    std::string name;
    std::string text;
    std::string public_id;
    std::string system_id;
    bool has_public = false;
    bool has_system = false;
    bool self_closing = false;
    std::vector<Attr> attrs;

    void clear() {
        kind = TokKind::Eof;
        name.clear();
        text.clear();
        public_id.clear();
        system_id.clear();
        has_public = false;
        has_system = false;
        self_closing = false;
        attrs.clear();
    }
};

struct Sink {
    Ref ctx = nullptr;
    Ref document = nullptr;
    CreateElementCallback create_element = nullptr;
    CreateElementCallback create_context_element = nullptr;
    AppendCallback append = nullptr;
    ParseErrorCallback parse_error = nullptr;
    PopCallback pop = nullptr;
    CreateCommentCallback create_comment = nullptr;
    CreateProcessingInstruction create_pi = nullptr;
    AppendDoctypeToDocumentCallback append_doctype = nullptr;
    AddAttrsIfMissingCallback add_attrs = nullptr;
    GetTemplateContentsCallback get_template = nullptr;
    bool allow_declarative = false;
    RemoveFromParentCallback remove_from_parent = nullptr;
    ReparentChildrenCallback reparent_children = nullptr;
    AppendBeforeSiblingCallback append_before_sibling = nullptr;
    AppendBasedOnParentNodeCallback append_based_on_parent = nullptr;
    AttachDeclarativeShadowCallback attach_shadow = nullptr;
};

inline StringSlice slice_of(const std::string& s) {
    StringSlice sl;
    sl.ptr = reinterpret_cast<const uint8_t*>(s.data());
    sl.len = s.size();
    return sl;
}

struct OpenEl {
    Ref ref;
    std::string name;
    std::string ns;
    // Where children of this element are appended. Differs from `ref` only for
    // <template>, whose children live in its content fragment (and, for a
    // declarative shadow root, in the shadow tree).
    Ref append_to = nullptr;
};

bool void_element(const std::string& name) {
    static const char* VOID[] = {"area", "base", "br", "col", "embed", "hr", "img", "input",
                                 "keygen", "link", "meta", "param", "source", "track", "wbr"};
    for (const char* v : VOID) {
        if (name == v) return true;
    }
    return false;
}

bool block_closes_p(const std::string& name) {
    static const char* BLOCKS[] = {"address", "article", "aside", "blockquote", "center", "details",
                                   "dialog", "dir", "div", "dl", "fieldset", "figcaption", "figure",
                                   "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header",
                                   "hgroup", "hr", "main", "menu", "nav", "ol", "p", "pre",
                                   "section", "summary", "table", "ul", "listing"};
    for (const char* b : BLOCKS) {
        if (name == b) return true;
    }
    return false;
}

bool raw_text_element(const std::string& name, bool& rcdata) {
    rcdata = false;
    if (name == "title" || name == "textarea") {
        rcdata = true;
        return true;
    }
    if (name == "script" || name == "style" || name == "xmp" || name == "iframe" ||
        name == "noembed" || name == "noframes" || name == "plaintext") {
        return true;
    }
    return false;
}

// ---------------------------------------------------------------------------
// HTML parser (tokenizer + tree builder)
// ---------------------------------------------------------------------------

enum TokResult { TOK_OK, TOK_NEED_MORE, TOK_EOF };

enum Mode {
    M_INITIAL,
    M_BEFORE_HTML,
    M_BEFORE_HEAD,
    M_IN_HEAD,
    M_AFTER_HEAD,
    M_IN_BODY,
    M_TEXT,
    M_AFTER_BODY,
    M_AFTER_AFTER_BODY,
};

enum RawMode { RAW_DATA = 0, RAW_TEXT, RAW_RCDATA, RAW_PLAINTEXT };

class HtmlParser {
public:
    Sink sink;
    std::vector<OpenEl> stack;
    bool html_seen = false;
    bool head_seen = false;
    bool body_seen = false;
    bool fragment = false;
    bool failed = false;
    bool finalized = false;
    int mode = M_INITIAL;
    int text_return_mode = M_IN_BODY;
    int raw_mode = RAW_DATA;
    std::string raw_tag;
    std::string buf;
    size_t pos = 0;

    void feed(const uint8_t* data, size_t len, bool final) {
        if (failed) return;
        if (data && len) {
            buf.append(reinterpret_cast<const char*>(data), len);
        }
        for (;;) {
            Token t;
            TokResult r = next_token(pos, final, t);
            if (r == TOK_NEED_MORE) break;
            if (r == TOK_EOF) {
                if (final) finalize();
                break;
            }
            pos = token_out;
            process(t);
            if (failed) break;
        }
        if (pos > 4096 && pos <= buf.size()) {
            buf.erase(0, pos);
            pos = 0;
        }
    }

    // Fragment context elements are materialized through the dedicated
    // createContextElementCallback, never through createElementCallback.
    Ref make_context_element(const std::string& name, const std::string& ns) {
        static const std::vector<Attr> NONE;
        return create_element(name, ns, NONE, true);
    }

private:
    // ---- tokenizer -------------------------------------------------------

    size_t find_raw_end(const std::string& s, size_t start, const std::string& tag) const {
        if (tag.empty()) return std::string::npos;
        size_t need = tag.size() + 2;
        if (s.size() < need) return std::string::npos;
        size_t last = s.size() - need;
        for (size_t i = start; i <= last; ++i) {
            if (s[i] != '<' || s[i + 1] != '/') continue;
            bool ok = true;
            for (size_t k = 0; k < tag.size(); ++k) {
                if (ascii_lower(s[i + 2 + k]) != tag[k]) {
                    ok = false;
                    break;
                }
            }
            if (!ok) continue;
            size_t j = i + 2 + tag.size();
            if (j < s.size()) {
                char c = s[j];
                if (ascii_ws((uint8_t)c) || c == '>' || c == '/') return i;
            }
        }
        return std::string::npos;
    }

    static bool ieq_at(const std::string& s, size_t pos, const char* lit) {
        size_t len = std::strlen(lit);
        if (pos + len > s.size()) return false;
        for (size_t i = 0; i < len; ++i) {
            if (ascii_lower(s[pos + i]) != ascii_lower(lit[i])) return false;
        }
        return true;
    }

    TokResult next_token(size_t start, bool final, Token& t) {
        size_t& out_pos = token_out;
        size_t n = buf.size();
        t.clear();

        if (raw_mode == RAW_PLAINTEXT) {
            if (start >= n) {
                if (!final) return TOK_NEED_MORE;
                return TOK_EOF;
            }
            t.kind = TokKind::Text;
            t.text = buf.substr(start);
            out_pos = n;
            raw_mode = RAW_DATA;
            return TOK_OK;
        }

        if (raw_mode != RAW_DATA) {
            if (start >= n) {
                if (!final) return TOK_NEED_MORE;
                raw_mode = RAW_DATA;
                return TOK_EOF;
            }
            size_t et = find_raw_end(buf, start, raw_tag);
            if (et == std::string::npos) {
                if (!final) {
                    size_t keep = raw_tag.size() + 2;
                    size_t end = n > keep ? n - keep : start;
                    if (end <= start) return TOK_NEED_MORE;
                    t.kind = TokKind::Text;
                    t.text = raw_mode == RAW_RCDATA ? decode_entities(buf.substr(start, end - start), false)
                                                    : buf.substr(start, end - start);
                    out_pos = end;
                    return TOK_OK;
                }
                t.kind = TokKind::Text;
                t.text = raw_mode == RAW_RCDATA ? decode_entities(buf.substr(start), false) : buf.substr(start);
                out_pos = n;
                raw_mode = RAW_DATA;
                return TOK_OK;
            }
            if (et > start) {
                t.kind = TokKind::Text;
                t.text = raw_mode == RAW_RCDATA ? decode_entities(buf.substr(start, et - start), false)
                                                : buf.substr(start, et - start);
                out_pos = et;
                return TOK_OK;
            }
            raw_mode = RAW_DATA; // fall through to the end tag at `start`
        }

        const std::string& s = buf;
        size_t lt = s.find('<', start);
        if (lt == std::string::npos) {
            if (start >= n) return final ? TOK_EOF : TOK_NEED_MORE;
            t.kind = TokKind::Text;
            t.text = decode_entities(s.substr(start), false);
            out_pos = n;
            return TOK_OK;
        }
        if (lt > start) {
            t.kind = TokKind::Text;
            t.text = decode_entities(s.substr(start, lt - start), false);
            out_pos = lt;
            return TOK_OK;
        }
        // lt == start
        if (n - lt < 2) {
            if (!final) return TOK_NEED_MORE;
            t.kind = TokKind::Text;
            t.text = "<";
            out_pos = lt + 1;
            return TOK_OK;
        }
        char c = s[lt + 1];

        if (c == '!') {
            if (n - lt >= 4 && s.compare(lt, 4, "<!--") == 0) {
                size_t end = s.find("-->", lt + 4);
                if (end == std::string::npos) {
                    if (!final) return TOK_NEED_MORE;
                    t.kind = TokKind::Comment;
                    t.text = s.substr(lt + 4);
                    out_pos = n;
                    return TOK_OK;
                }
                t.kind = TokKind::Comment;
                t.text = s.substr(lt + 4, end - (lt + 4));
                out_pos = end + 3;
                return TOK_OK;
            }
            if (n - lt < 4 && !final) return TOK_NEED_MORE;
            size_t after = lt + 2;
            if (!final && n - after < 7) {
                // A doctype keyword split across feeds: keep the bytes.
                bool prefix_ok = true;
                for (size_t k = 0; after + k < n; ++k) {
                    if (ascii_lower(s[after + k]) != "doctype"[k]) {
                        prefix_ok = false;
                        break;
                    }
                }
                if (prefix_ok) return TOK_NEED_MORE;
            }
            if (ieq_at(s, after, "doctype")) {
                after += 7;
                while (after < n && ascii_ws((uint8_t)s[after])) ++after;
                size_t name_start = after;
                while (after < n && !ascii_ws((uint8_t)s[after]) && s[after] != '>') ++after;
                std::string dname = s.substr(name_start, after - name_start);
                for (char& ch : dname) ch = ascii_lower(ch);
                t.kind = TokKind::Doctype;
                t.name = dname;
                while (after < n && ascii_ws((uint8_t)s[after])) ++after;
                auto read_quoted = [&](std::string& out) -> bool {
                    if (after >= n || (s[after] != '"' && s[after] != '\'')) return false;
                    char q = s[after++];
                    size_t begin = after;
                    while (after < n && s[after] != q) ++after;
                    if (after >= n) return false;
                    out = s.substr(begin, after - begin);
                    ++after;
                    return true;
                };
                std::string id;
                if (ieq_at(s, after, "public")) {
                    after += 6;
                    while (after < n && ascii_ws((uint8_t)s[after])) ++after;
                    t.has_public = read_quoted(id);
                    t.public_id = id;
                    while (after < n && ascii_ws((uint8_t)s[after])) ++after;
                    id.clear();
                    if (read_quoted(id)) {
                        t.has_system = true;
                        t.system_id = id;
                    }
                } else if (ieq_at(s, after, "system")) {
                    after += 6;
                    while (after < n && ascii_ws((uint8_t)s[after])) ++after;
                    if (read_quoted(id)) {
                        t.has_system = true;
                        t.system_id = id;
                    }
                }
                while (after < n && s[after] != '>') ++after;
                if (after >= n) {
                    if (!final) return TOK_NEED_MORE;
                    out_pos = n;
                } else {
                    out_pos = after + 1;
                }
                return TOK_OK;
            }
            // Bogus comment.
            size_t gt = s.find('>', lt + 2);
            if (gt == std::string::npos) {
                if (!final) return TOK_NEED_MORE;
                t.kind = TokKind::Comment;
                t.text = s.substr(lt + 2);
                out_pos = n;
                return TOK_OK;
            }
            t.kind = TokKind::Comment;
            t.text = s.substr(lt + 2, gt - (lt + 2));
            out_pos = gt + 1;
            return TOK_OK;
        }

        if (c == '/') {
            if (n - lt < 3) {
                if (!final) return TOK_NEED_MORE;
                t.kind = TokKind::Text;
                t.text = s.substr(lt);
                out_pos = n;
                return TOK_OK;
            }
            if (!ascii_alpha((uint8_t)s[lt + 2])) {
                size_t gt = s.find('>', lt + 2);
                if (gt == std::string::npos) {
                    if (!final) return TOK_NEED_MORE;
                    out_pos = n;
                } else {
                    out_pos = gt + 1;
                }
                t.kind = TokKind::Comment;
                t.text = s.substr(lt + 2, (out_pos == n ? n : out_pos - 1) - (lt + 2));
                return TOK_OK;
            }
            size_t i = lt + 2;
            std::string name;
            while (i < n && !ascii_ws((uint8_t)s[i]) && s[i] != '/' && s[i] != '>') {
                name.push_back(ascii_lower(s[i]));
                ++i;
            }
            while (i < n && s[i] != '>') ++i;
            if (i >= n) {
                if (!final) return TOK_NEED_MORE;
                out_pos = n;
            } else {
                out_pos = i + 1;
            }
            t.kind = TokKind::EndTag;
            t.name = name;
            return TOK_OK;
        }

        if (c == '?') {
            size_t gt = s.find('>', lt + 2);
            if (gt == std::string::npos) {
                if (!final) return TOK_NEED_MORE;
                out_pos = n;
            } else {
                out_pos = gt + 1;
            }
            t.kind = TokKind::Comment;
            t.text = s.substr(lt + 2, (out_pos == n ? n : out_pos - 1) - (lt + 2));
            return TOK_OK;
        }

        if (!ascii_alpha((uint8_t)c)) {
            t.kind = TokKind::Text;
            t.text = "<";
            out_pos = lt + 1;
            return TOK_OK;
        }

        // Start tag.
        size_t i = lt + 1;
        std::string name;
        while (i < n && !ascii_ws((uint8_t)s[i]) && s[i] != '/' && s[i] != '>') {
            name.push_back(ascii_lower(s[i]));
            ++i;
        }
        bool self_closing = false;
        for (;;) {
            while (i < n && ascii_ws((uint8_t)s[i])) ++i;
            if (i >= n) {
                if (!final) return TOK_NEED_MORE;
                out_pos = n;
                break;
            }
            if (s[i] == '>') {
                ++i;
                out_pos = i;
                break;
            }
            if (s[i] == '/') {
                if (i + 1 < n && s[i + 1] == '>') {
                    self_closing = true;
                    i += 2;
                    out_pos = i;
                    break;
                }
                ++i;
                continue;
            }
            std::string an;
            while (i < n && !ascii_ws((uint8_t)s[i]) && s[i] != '=' && s[i] != '>' && s[i] != '/') {
                an.push_back(ascii_lower(s[i]));
                ++i;
            }
            if (an.empty()) {
                ++i;
                continue;
            }
            while (i < n && ascii_ws((uint8_t)s[i])) ++i;
            std::string av;
            if (i < n && s[i] == '=') {
                ++i;
                while (i < n && ascii_ws((uint8_t)s[i])) ++i;
                if (i < n && (s[i] == '"' || s[i] == '\'')) {
                    char q = s[i++];
                    size_t vs = i;
                    while (i < n && s[i] != q) ++i;
                    if (i >= n && !final) return TOK_NEED_MORE;
                    av = s.substr(vs, i - vs);
                    if (i < n) ++i;
                } else {
                    size_t vs = i;
                    while (i < n && !ascii_ws((uint8_t)s[i]) && s[i] != '>') ++i;
                    av = s.substr(vs, i - vs);
                }
                av = decode_entities(av, true);
            }
            bool duplicate = false;
            for (const Attr& existing : t.attrs) {
                if (existing.local == an && existing.ns.empty()) {
                    duplicate = true;
                    break;
                }
            }
            if (!duplicate) {
                Attr a;
                a.local = an;
                a.value = av;
                t.attrs.push_back(a);
            }
            if (i >= n && !final) return TOK_NEED_MORE;
        }
        t.kind = TokKind::StartTag;
        t.name = name;
        t.self_closing = self_closing;
        return TOK_OK;
    }

    // Assigns the tokenizer output position. Declared as a member so
    // next_token can report it without a second parameter.
    size_t token_out = 0;

    // ---- sink helpers ----------------------------------------------------

    Ref create_element(const std::string& name, const std::string& ns, const std::vector<Attr>& attrs,
                       bool context) {
        CreateElementCallback cb = context ? sink.create_context_element : sink.create_element;
        if (!cb) return nullptr;
        CAttributeIterator iter;
        iter.pos = 0;
        iter.attrs.reserve(attrs.size());
        for (const Attr& a : attrs) {
            InternalAttr ia;
            ia.prefix = a.prefix;
            ia.ns = a.ns;
            ia.local = a.local;
            ia.value = a.value;
            iter.attrs.push_back(std::move(ia));
        }
        CQualName q;
        q.prefix.tag = 0;
        std::memset(&q.prefix.value, 0, sizeof(q.prefix.value));
        q.ns = slice_of(ns);
        q.local = slice_of(name);
        Ref ref = cb(sink.ctx, (const void*)sink.document, q, &iter);
        if (!ref) failed = true;
        return ref;
    }

    void merge_attrs(Ref target, const std::vector<Attr>& attrs) {
        if (!sink.add_attrs || !target || attrs.empty()) return;
        CAttributeIterator iter;
        iter.pos = 0;
        iter.attrs.reserve(attrs.size());
        for (const Attr& a : attrs) {
            InternalAttr ia;
            ia.prefix = a.prefix;
            ia.ns = a.ns;
            ia.local = a.local;
            ia.value = a.value;
            iter.attrs.push_back(std::move(ia));
        }
        sink.add_attrs(sink.ctx, target, &iter);
    }

    Ref current() const {
        if (stack.empty()) return sink.document;
        const OpenEl& top = stack.back();
        return top.append_to ? top.append_to : top.ref;
    }

    bool current_is(const char* name) const {
        return !stack.empty() && stack.back().name == name;
    }

    void append_node(Ref parent, Ref child) {
        if (!sink.append || !parent || !child) return;
        CNodeOrText n;
        n.tag = 0;
        n.node = child;
        n.text.ptr = nullptr;
        n.text.len = 0;
        sink.append(sink.ctx, parent, n);
    }

    void append_text(Ref parent, const std::string& text) {
        if (!sink.append || !parent || text.empty()) return;
        CNodeOrText n;
        n.tag = 1;
        n.node = nullptr;
        n.text = slice_of(text);
        sink.append(sink.ctx, parent, n);
    }

    void append_text(const std::string& text) {
        append_text(current(), text);
    }

    void report_error(const char* msg) {
        if (!sink.parse_error) return;
        StringSlice sl;
        sl.ptr = reinterpret_cast<const uint8_t*>(msg);
        sl.len = std::strlen(msg);
        sink.parse_error(sink.ctx, sl);
    }

    void pop_element() {
        if (stack.empty()) return;
        OpenEl el = stack.back();
        stack.pop_back();
        if (sink.pop && el.ref) sink.pop(sink.ctx, el.ref);
    }

    void pop_through(const std::string& name) {
        for (size_t i = stack.size(); i > 0; --i) {
            if (stack[i - 1].name == name) {
                while (stack.size() >= i) pop_element();
                return;
            }
        }
    }

    bool find_open(const std::string& name) const {
        for (size_t i = stack.size(); i > 0; --i) {
            if (stack[i - 1].name == name) return true;
        }
        return false;
    }

    // List item scope: `ol`/`ul` bound the search, so a nested list never
    // closes its parent <li>.
    bool find_open_in_list_scope(const std::string& name) const {
        for (size_t i = stack.size(); i > 0; --i) {
            const std::string& n = stack[i - 1].name;
            if (n == name) return true;
            if (n == "ol" || n == "ul" || n == "button" || n == "table" || n == "html" ||
                n == "applet" || n == "marquee" || n == "object" || n == "template") {
                return false;
            }
        }
        return false;
    }

    bool find_open_in_scope(const std::string& name) const {
        for (size_t i = stack.size(); i > 0; --i) {
            const std::string& n = stack[i - 1].name;
            if (n == name) return true;
            if (n == "button" || n == "table" || n == "html" || n == "applet" ||
                n == "marquee" || n == "object") {
                return false;
            }
        }
        return false;
    }

    void close_p() {
        if (!find_open_in_scope("p")) return;
        pop_through("p");
    }

    void close_named(const char* name) {
        if (!find_open(name)) return;
        pop_through(name);
    }

    std::string child_ns() const {
        if (stack.empty()) return NS_HTML;
        const OpenEl& top = stack.back();
        if (top.ns == NS_SVG) {
            if (top.name == "foreignObject" || top.name == "desc" || top.name == "title") return NS_HTML;
            return NS_SVG;
        }
        if (top.ns == NS_MATHML) {
            if (top.name == "annotation-xml") return NS_HTML;
            return NS_MATHML;
        }
        return NS_HTML;
    }

    void push_element(const std::string& name, const std::string& ns, const std::vector<Attr>& attrs) {
        Ref ref = create_element(name, ns, attrs, false);
        if (!ref) return;
        append_node(current(), ref);
        OpenEl el;
        el.ref = ref;
        el.name = name;
        el.ns = ns;
        stack.push_back(el);
    }

    void push_element(const std::string& name) {
        static const std::vector<Attr> NONE;
        push_element(name, child_ns(), NONE);
    }

    void ensure_html(const std::vector<Attr>* attrs) {
        if (html_seen) return;
        static const std::vector<Attr> NONE;
        Ref ref = create_element("html", NS_HTML, attrs ? *attrs : NONE, false);
        if (!ref) return;
        append_node(sink.document, ref);
        OpenEl el;
        el.ref = ref;
        el.name = "html";
        el.ns = NS_HTML;
        stack.push_back(el);
        html_seen = true;
    }

    void ensure_head() {
        if (head_seen) return;
        if (!html_seen) ensure_html(nullptr);
        static const std::vector<Attr> NONE;
        Ref ref = create_element("head", NS_HTML, NONE, false);
        if (!ref) return;
        append_node(current(), ref);
        OpenEl el;
        el.ref = ref;
        el.name = "head";
        el.ns = NS_HTML;
        stack.push_back(el);
        head_seen = true;
    }

    void ensure_body() {
        if (body_seen) return;
        if (!head_seen) ensure_head();
        while (current_is("head")) pop_element();
        static const std::vector<Attr> NONE;
        Ref ref = create_element("body", NS_HTML, NONE, false);
        if (!ref) return;
        append_node(current(), ref);
        OpenEl el;
        el.ref = ref;
        el.name = "body";
        el.ns = NS_HTML;
        stack.push_back(el);
        body_seen = true;
    }

    void append_comment_to(Ref parent, const std::string& text) {
        if (!sink.create_comment || !parent) return;
        StringSlice sl = slice_of(text);
        Ref ref = sink.create_comment(sink.ctx, sl);
        if (ref) append_node(parent, ref);
    }

    void append_comment(const std::string& text) {
        append_comment_to(current(), text);
    }

    void append_pi(const std::string& target, const std::string& data) {
        if (!sink.create_pi) return;
        StringSlice ts = slice_of(target);
        StringSlice ds = slice_of(data);
        Ref ref = sink.create_pi(sink.ctx, ts, ds);
        if (ref) append_node(current(), ref);
    }

    void do_doctype(const Token& t) {
        if (!sink.append_doctype) return;
        StringSlice name = slice_of(t.name);
        StringSlice pub = slice_of(t.public_id);
        StringSlice sys = slice_of(t.system_id);
        sink.append_doctype(sink.ctx, name, pub, sys);
    }

    // ---- tree construction ----------------------------------------------

    void process(Token& t) {
        for (;;) {
            switch (mode) {
            case M_INITIAL:
                if (t.kind == TokKind::Doctype) {
                    do_doctype(t);
                    mode = M_BEFORE_HTML;
                    return;
                }
                if (t.kind == TokKind::Comment) {
                    append_comment(t.text);
                    return;
                }
                if (t.kind == TokKind::Text && is_all_ws(t.text)) {
                    if (fragment) {
                        append_text(t.text);
                    }
                    return;
                }
                if (fragment) {
                    mode = M_IN_BODY;
                    continue;
                }
                mode = M_BEFORE_HTML;
                continue;

            case M_BEFORE_HTML:
                if (t.kind == TokKind::Doctype) return;
                if (t.kind == TokKind::Comment) {
                    append_comment(t.text);
                    return;
                }
                if (t.kind == TokKind::Text && is_all_ws(t.text)) return;
                if (t.kind == TokKind::StartTag && t.name == "html") {
                    ensure_html(&t.attrs);
                    mode = M_BEFORE_HEAD;
                    return;
                }
                if (t.kind == TokKind::EndTag &&
                    (t.name == "head" || t.name == "body" || t.name == "html" || t.name == "br")) {
                    return;
                }
                ensure_html(nullptr);
                mode = M_BEFORE_HEAD;
                continue;

            case M_BEFORE_HEAD:
                if (t.kind == TokKind::Text && is_all_ws(t.text)) return;
                if (t.kind == TokKind::Comment) {
                    append_comment(t.text);
                    return;
                }
                if (t.kind == TokKind::Doctype) return;
                if (t.kind == TokKind::StartTag && t.name == "html") {
                    if (!stack.empty()) merge_attrs(stack[0].ref, t.attrs);
                    return;
                }
                if (t.kind == TokKind::StartTag && t.name == "head") {
                    static const std::vector<Attr> NONE;
                    Ref ref = create_element("head", NS_HTML, t.attrs, false);
                    if (!ref) return;
                    append_node(current(), ref);
                    OpenEl el;
                    el.ref = ref;
                    el.name = "head";
                    el.ns = NS_HTML;
                    stack.push_back(el);
                    head_seen = true;
                    mode = M_IN_HEAD;
                    return;
                }
                if (t.kind == TokKind::EndTag &&
                    (t.name == "head" || t.name == "body" || t.name == "html" || t.name == "br")) {
                    // handled by the implicit-head path below
                }
                ensure_head();
                mode = M_IN_HEAD;
                continue;

            case M_IN_HEAD:
                if (t.kind == TokKind::Text) {
                    if (is_all_ws(t.text)) {
                        append_text(t.text);
                        return;
                    }
                    while (current_is("head")) pop_element();
                    mode = M_AFTER_HEAD;
                    continue;
                }
                if (t.kind == TokKind::Comment) {
                    append_comment(t.text);
                    return;
                }
                if (t.kind == TokKind::Doctype) return;
                if (t.kind == TokKind::StartTag) {
                    const std::string& n = t.name;
                    if (n == "html") {
                        if (!stack.empty()) merge_attrs(stack[0].ref, t.attrs);
                        return;
                    }
                    if (n == "base" || n == "basefont" || n == "bgsound" || n == "link" || n == "meta") {
                        Ref ref = create_element(n, NS_HTML, t.attrs, false);
                        if (ref) append_node(current(), ref);
                        return;
                    }
                    if (n == "title" || n == "style" || n == "script" || n == "noscript" ||
                        n == "noframes" || n == "template") {
                        Ref ref = create_element(n, NS_HTML, t.attrs, false);
                        if (!ref) return;
                        append_node(current(), ref);
                        OpenEl el;
                        el.ref = ref;
                        el.name = n;
                        el.ns = NS_HTML;
                        stack.push_back(el);
                        if (n == "title") {
                            raw_mode = RAW_RCDATA;
                            raw_tag = n;
                            text_return_mode = M_IN_HEAD;
                            mode = M_TEXT;
                        } else if (n == "style" || n == "script" || n == "noframes") {
                            raw_mode = RAW_TEXT;
                            raw_tag = n;
                            text_return_mode = M_IN_HEAD;
                            mode = M_TEXT;
                        }
                        return;
                    }
                    if (n == "head") return;
                    while (current_is("head")) pop_element();
                    mode = M_AFTER_HEAD;
                    continue;
                }
                if (t.kind == TokKind::EndTag) {
                    if (t.name == "head") {
                        pop_through("head");
                        mode = M_AFTER_HEAD;
                        return;
                    }
                    if (t.name == "body" || t.name == "html" || t.name == "br") {
                        while (current_is("head")) pop_element();
                        mode = M_AFTER_HEAD;
                        continue;
                    }
                    return;
                }
                while (current_is("head")) pop_element();
                mode = M_AFTER_HEAD;
                continue;

            case M_AFTER_HEAD:
                if (t.kind == TokKind::Text && is_all_ws(t.text)) {
                    append_text(t.text);
                    return;
                }
                if (t.kind == TokKind::Comment) {
                    append_comment(t.text);
                    return;
                }
                if (t.kind == TokKind::Doctype) return;
                if (t.kind == TokKind::StartTag) {
                    if (t.name == "html") {
                        if (!stack.empty()) merge_attrs(stack[0].ref, t.attrs);
                        return;
                    }
                    if (t.name == "body") {
                        Ref ref = create_element("body", NS_HTML, t.attrs, false);
                        if (!ref) return;
                        append_node(current(), ref);
                        OpenEl el;
                        el.ref = ref;
                        el.name = "body";
                        el.ns = NS_HTML;
                        stack.push_back(el);
                        body_seen = true;
                        mode = M_IN_BODY;
                        return;
                    }
                    if (t.name == "head") return;
                }
                if (t.kind == TokKind::EndTag) {
                    if (t.name == "body" || t.name == "html" || t.name == "br") {
                        ensure_body();
                        mode = M_IN_BODY;
                        continue;
                    }
                    return;
                }
                ensure_body();
                mode = M_IN_BODY;
                continue;

            case M_IN_BODY:
                if (t.kind == TokKind::Text) {
                    append_text(t.text);
                    return;
                }
                if (t.kind == TokKind::Comment) {
                    append_comment(t.text);
                    return;
                }
                if (t.kind == TokKind::Doctype) return;
                if (t.kind == TokKind::PI) {
                    append_pi(t.name, t.text);
                    return;
                }
                if (t.kind == TokKind::EndTag) {
                    const std::string& n = t.name;
                    if (n == "body") {
                        mode = M_AFTER_BODY;
                        return;
                    }
                    if (n == "html") {
                        mode = M_AFTER_BODY;
                        continue;
                    }
                    if (n == "br") {
                        Ref ref = create_element("br", NS_HTML, t.attrs, false);
                        if (ref) append_node(current(), ref);
                        return;
                    }
                    if (n == "p" && !find_open_in_scope("p")) {
                        // Stray </p>: create and immediately close an empty <p>.
                        static const std::vector<Attr> NONE;
                        Ref ref = create_element("p", NS_HTML, NONE, false);
                        if (ref) {
                            append_node(current(), ref);
                            if (sink.pop) sink.pop(sink.ctx, ref);
                        }
                        return;
                    }
                    if (find_open(n)) {
                        pop_through(n);
                    }
                    return;
                }
                if (t.kind != TokKind::StartTag) return;
                {
                    const std::string& n = t.name;
                    if (n == "html") {
                        if (!stack.empty()) merge_attrs(stack[0].ref, t.attrs);
                        return;
                    }
                    if (n == "head") return;
                    if (n == "body") {
                        for (size_t i = 0; i < stack.size(); ++i) {
                            if (stack[i].name == "body") {
                                merge_attrs(stack[i].ref, t.attrs);
                                break;
                            }
                        }
                        return;
                    }
                    if (n == "li") {
                        if (find_open_in_list_scope("li")) pop_through("li");
                        close_p();
                        push_element("li", NS_HTML, t.attrs);
                        return;
                    }
                    if (n == "dd" || n == "dt") {
                        close_named("dd");
                        close_named("dt");
                        close_p();
                        push_element(n, NS_HTML, t.attrs);
                        return;
                    }
                    if (n == "option") {
                        if (current_is("option")) pop_element();
                        push_element("option", NS_HTML, t.attrs);
                        return;
                    }
                    if (n == "optgroup") {
                        if (current_is("option")) pop_element();
                        if (current_is("optgroup")) pop_element();
                        push_element("optgroup", NS_HTML, t.attrs);
                        return;
                    }
                    if (n == "tr") {
                        close_named("td");
                        close_named("th");
                        close_named("tr");
                        close_p();
                        push_element("tr", NS_HTML, t.attrs);
                        return;
                    }
                    if (n == "td" || n == "th") {
                        close_named("td");
                        close_named("th");
                        close_p();
                        push_element(n, NS_HTML, t.attrs);
                        return;
                    }
                    if (n == "thead" || n == "tbody" || n == "tfoot" || n == "caption" ||
                        n == "colgroup") {
                        close_named("td");
                        close_named("th");
                        close_named("tr");
                        close_named("thead");
                        close_named("tbody");
                        close_named("tfoot");
                        close_named("caption");
                        close_named("colgroup");
                        close_p();
                        push_element(n, NS_HTML, t.attrs);
                        return;
                    }
                    if (n == "col") {
                        Ref ref = create_element("col", NS_HTML, t.attrs, false);
                        if (ref) append_node(current(), ref);
                        return;
                    }
                    if (n.size() == 2 && n[0] == 'h' && n[1] >= '1' && n[1] <= '6') {
                        close_p();
                        if (!stack.empty() && stack.back().name.size() == 2 &&
                            stack.back().name[0] == 'h' && stack.back().name[1] >= '1' &&
                            stack.back().name[1] <= '6') {
                            pop_element();
                        }
                        push_element(n, NS_HTML, t.attrs);
                        return;
                    }
                    if (n == "button") {
                        close_named("button");
                        push_element("button", NS_HTML, t.attrs);
                        return;
                    }
                    if (n == "table") {
                        close_p();
                        push_element("table", NS_HTML, t.attrs);
                        return;
                    }
                    if (n == "select") {
                        push_element("select", NS_HTML, t.attrs);
                        return;
                    }
                    if (n == "template") {
                        Ref ref = create_element("template", NS_HTML, t.attrs, false);
                        if (!ref) return;
                        const std::string* mode = nullptr;
                        for (const Attr& a : t.attrs) {
                            if (a.local == "shadowrootmode") mode = &a.value;
                        }
                        bool attached = false;
                        if (sink.allow_declarative && sink.attach_shadow && mode &&
                            (*mode == "open" || *mode == "closed")) {
                            attached = sink.attach_shadow(sink.ctx, current(), ref, *mode == "open" ? 1 : 0) != 0;
                        }
                        // A declarative shadow root replaces the template: its
                        // content is the shadow tree, so the template itself is
                        // never inserted into the light DOM.
                        if (!attached) append_node(current(), ref);
                        // Template children live in the content fragment (the
                        // shadow tree when a declarative root was attached),
                        // never in the template element itself.
                        Ref content = sink.get_template ? sink.get_template(sink.ctx, ref) : nullptr;
                        OpenEl el;
                        el.ref = ref;
                        el.name = "template";
                        el.ns = NS_HTML;
                        el.append_to = content ? content : ref;
                        stack.push_back(el);
                        return;
                    }
                    bool rcdata = false;
                    if (raw_text_element(n, rcdata)) {
                        Ref ref = create_element(n, NS_HTML, t.attrs, false);
                        if (!ref) return;
                        append_node(current(), ref);
                        OpenEl el;
                        el.ref = ref;
                        el.name = n;
                        el.ns = NS_HTML;
                        stack.push_back(el);
                        raw_tag = n;
                        raw_mode = n == "plaintext" ? RAW_PLAINTEXT : (rcdata ? RAW_RCDATA : RAW_TEXT);
                        text_return_mode = M_IN_BODY;
                        mode = M_TEXT;
                        return;
                    }
                    if (void_element(n)) {
                        Ref ref = create_element(n, NS_HTML, t.attrs, false);
                        if (ref) append_node(current(), ref);
                        return;
                    }
                    if (n == "svg") {
                        close_p();
                        push_element("svg", NS_SVG, t.attrs);
                        return;
                    }
                    if (n == "math") {
                        close_p();
                        push_element("math", NS_MATHML, t.attrs);
                        return;
                    }
                    if (block_closes_p(n)) {
                        close_p();
                        push_element(n, NS_HTML, t.attrs);
                        return;
                    }
                    push_element(n, child_ns(), t.attrs);
                    return;
                }

            case M_TEXT:
                if (t.kind == TokKind::Text) {
                    append_text(t.text);
                    return;
                }
                if (t.kind == TokKind::EndTag) {
                    if (!stack.empty() && stack.back().name == raw_tag) {
                        pop_element();
                    } else if (find_open(raw_tag)) {
                        pop_through(raw_tag);
                    }
                    raw_mode = RAW_DATA;
                    raw_tag.clear();
                    mode = text_return_mode;
                    return;
                }
                if (t.kind == TokKind::Comment) {
                    append_comment(t.text);
                    return;
                }
                return;

            case M_AFTER_BODY:
                if (t.kind == TokKind::Comment) {
                    Ref html = sink.document;
                    for (const OpenEl& el : stack) {
                        if (el.name == "html") {
                            html = el.ref;
                            break;
                        }
                    }
                    append_comment_to(html, t.text);
                    return;
                }
                if (t.kind == TokKind::Text && is_all_ws(t.text)) {
                    append_text(t.text);
                    return;
                }
                if (t.kind == TokKind::EndTag && t.name == "html") {
                    mode = M_AFTER_AFTER_BODY;
                    return;
                }
                mode = M_IN_BODY;
                continue;

            case M_AFTER_AFTER_BODY:
                if (t.kind == TokKind::Comment) {
                    append_comment_to(sink.document, t.text);
                    return;
                }
                if (t.kind == TokKind::Text && is_all_ws(t.text)) return;
                if (t.kind == TokKind::Doctype) return;
                mode = M_IN_BODY;
                continue;
            }
        }
    }

    void finalize() {
        if (finalized) return;
        finalized = true;
        if (fragment) {
            while (stack.size() > 1) pop_element();
            return;
        }
        if (!html_seen) {
            ensure_html(nullptr);
        }
        if (!head_seen) {
            ensure_head();
        }
        if (!body_seen) {
            ensure_body();
        }
        while (!stack.empty()) pop_element();
    }
};

// ---------------------------------------------------------------------------
// XML parser
// ---------------------------------------------------------------------------

struct XmlNsFrame {
    std::string default_ns;
    std::vector<std::pair<std::string, std::string>> prefixes;
};

std::string xml_lookup_prefix(const std::vector<XmlNsFrame>& frames, const std::string& prefix) {
    if (prefix == "xml") return NS_XML;
    if (prefix == "xmlns") return NS_XMLNS;
    for (size_t i = frames.size(); i > 0; --i) {
        for (size_t k = frames[i - 1].prefixes.size(); k > 0; --k) {
            if (frames[i - 1].prefixes[k - 1].first == prefix) return frames[i - 1].prefixes[k - 1].second;
        }
    }
    return std::string();
}

bool xml_name_char(char c, bool first) {
    if (ascii_alpha((uint8_t)c) || c == '_' || c == ':') return true;
    if (!first && (c == '-' || c == '.' || (c >= '0' && c <= '9'))) return true;
    return false;
}

std::string xml_decode_entities(const std::string& in, bool& bad) {
    std::string out;
    size_t i = 0;
    while (i < in.size()) {
        if (in[i] != '&') {
            out.push_back(in[i++]);
            continue;
        }
        size_t semi = in.find(';', i + 1);
        if (semi == std::string::npos) {
            bad = true;
            out.append(in.substr(i));
            break;
        }
        std::string ref = in.substr(i + 1, semi - (i + 1));
        bool ok = false;
        if (!ref.empty() && ref[0] == '#') {
            bool hex = ref.size() > 1 && (ref[1] == 'x' || ref[1] == 'X');
            size_t k = hex ? 2 : 1;
            uint32_t cp = 0;
            bool digits = k < ref.size();
            for (; k < ref.size(); ++k) {
                char c = ref[k];
                int v;
                if (c >= '0' && c <= '9') v = c - '0';
                else if (hex && c >= 'a' && c <= 'f') v = c - 'a' + 10;
                else if (hex && c >= 'A' && c <= 'F') v = c - 'A' + 10;
                else { digits = false; break; }
                cp = cp * (hex ? 16 : 10) + (uint32_t)v;
            }
            if (digits) {
                if (cp == 0 || cp > 0x10FFFF || (cp >= 0xD800 && cp <= 0xDFFF)) {
                    bad = true;
                    cp = 0xFFFD;
                }
                append_utf8(out, cp);
                ok = true;
            }
        } else {
            for (const NamedEntity& e : NAMED_ENTITIES) {
                if (ref == e.name) {
                    append_utf8(out, e.cp);
                    ok = true;
                    break;
                }
            }
        }
        if (!ok) bad = true;
        if (!ok) out.append(in.substr(i, semi - i + 1));
        i = semi + 1;
    }
    return out;
}

struct XmlCallbacks {
    Sink* sink = nullptr;     // build mode
    ParseErrorCallback error = nullptr;
    Ref error_ctx = nullptr;
};

void xml_report(const XmlCallbacks& cb, const std::string& msg) {
    if (!cb.error) return;
    StringSlice sl;
    sl.ptr = reinterpret_cast<const uint8_t*>(msg.data());
    sl.len = msg.size();
    cb.error(cb.error_ctx, sl);
}

void run_xml(const uint8_t* data, size_t len, XmlCallbacks cb) {
    const std::string s(reinterpret_cast<const char*>(data), len);
    size_t n = s.size();
    size_t i = 0;
    std::vector<OpenEl> stack;
    std::vector<XmlNsFrame> ns_frames;
    bool build = cb.sink != nullptr;

    auto append_text = [&](const std::string& text) {
        if (!build || text.empty() || !cb.sink->append) return;
        CNodeOrText node;
        node.tag = 1;
        node.node = nullptr;
        node.text = slice_of(text);
        Ref parent = stack.empty() ? cb.sink->document : stack.back().ref;
        cb.sink->append(cb.sink->ctx, parent, node);
    };

    while (i < n) {
        if (s[i] != '<') {
            size_t j = i;
            while (j < n && s[j] != '<') ++j;
            bool bad = false;
            std::string text = xml_decode_entities(s.substr(i, j - i), bad);
            if (bad) xml_report(cb, "Invalid character reference");
            append_text(text);
            i = j;
            continue;
        }
        if (i + 1 >= n) {
            xml_report(cb, "Unexpected EOF");
            break;
        }
        char c = s[i + 1];
        if (c == '!') {
            if (s.compare(i, 4, "<!--") == 0) {
                size_t end = s.find("-->", i + 4);
                if (end == std::string::npos) {
                    xml_report(cb, "Unexpected EOF in comment");
                    i = n;
                    continue;
                }
                if (build && cb.sink->create_comment) {
                    std::string text = s.substr(i + 4, end - (i + 4));
                    StringSlice sl = slice_of(text);
                    Ref ref = cb.sink->create_comment(cb.sink->ctx, sl);
                    if (ref && cb.sink->append) {
                        CNodeOrText node;
                        node.tag = 0;
                        node.node = ref;
                        node.text.ptr = nullptr;
                        node.text.len = 0;
                        Ref parent = stack.empty() ? cb.sink->document : stack.back().ref;
                        cb.sink->append(cb.sink->ctx, parent, node);
                    }
                }
                i = end + 3;
                continue;
            }
            if (s.compare(i, 9, "<![CDATA[") == 0) {
                size_t end = s.find("]]>", i + 9);
                if (end == std::string::npos) {
                    xml_report(cb, "Unexpected EOF in CDATA section");
                    i = n;
                    continue;
                }
                append_text(s.substr(i + 9, end - (i + 9)));
                i = end + 3;
                continue;
            }
            // DOCTYPE and other declarations: skip to the matching '>'.
            size_t end = s.find('>', i + 2);
            if (end == std::string::npos) {
                xml_report(cb, "Unexpected EOF in declaration");
                i = n;
                continue;
            }
            i = end + 1;
            continue;
        }
        if (c == '?') {
            size_t end = s.find("?>", i + 2);
            if (end == std::string::npos) {
                xml_report(cb, "Unexpected EOF in processing instruction");
                i = n;
                continue;
            }
            if (build && cb.sink->create_pi) {
                size_t target_end = i + 2;
                while (target_end < end && !ascii_ws((uint8_t)s[target_end])) ++target_end;
                std::string target = s.substr(i + 2, target_end - (i + 2));
                size_t data_start = target_end;
                while (data_start < end && ascii_ws((uint8_t)s[data_start])) ++data_start;
                std::string pdata = s.substr(data_start, end - data_start);
                StringSlice ts = slice_of(target);
                StringSlice ds = slice_of(pdata);
                Ref ref = cb.sink->create_pi(cb.sink->ctx, ts, ds);
                if (ref && cb.sink->append) {
                    CNodeOrText node;
                    node.tag = 0;
                    node.node = ref;
                    node.text.ptr = nullptr;
                    node.text.len = 0;
                    Ref parent = stack.empty() ? cb.sink->document : stack.back().ref;
                    cb.sink->append(cb.sink->ctx, parent, node);
                }
            }
            i = end + 2;
            continue;
        }
        if (c == '/') {
            size_t k = i + 2;
            size_t name_start = k;
            while (k < n && xml_name_char(s[k], k == name_start)) ++k;
            std::string name = s.substr(name_start, k - name_start);
            while (k < n && ascii_ws((uint8_t)s[k])) ++k;
            if (k >= n || s[k] != '>') {
                xml_report(cb, "Invalid end tag");
                size_t gt = s.find('>', k);
                i = gt == std::string::npos ? n : gt + 1;
                continue;
            }
            i = k + 1;
            if (stack.empty() || stack.back().name != name) {
                xml_report(cb, "Unexpected end tag: " + name);
                for (size_t d = stack.size(); d > 0; --d) {
                    if (stack[d - 1].name == name) {
                        if (build) {
                            while (stack.size() >= d) {
                                if (cb.sink->pop) cb.sink->pop(cb.sink->ctx, stack.back().ref);
                                stack.pop_back();
                            }
                        } else {
                            stack.resize(d - 1);
                        }
                        if (ns_frames.size() >= d) ns_frames.resize(d - 1);
                        break;
                    }
                }
            } else {
                if (build && cb.sink->pop) cb.sink->pop(cb.sink->ctx, stack.back().ref);
                stack.pop_back();
                if (!ns_frames.empty()) ns_frames.pop_back();
            }
            continue;
        }
        // Start tag.
        size_t k = i + 1;
        size_t name_start = k;
        while (k < n && xml_name_char(s[k], k == name_start)) ++k;
        std::string raw_name = s.substr(name_start, k - name_start);
        if (raw_name.empty()) {
            xml_report(cb, "Invalid tag name");
            size_t gt = s.find('>', i + 1);
            i = gt == std::string::npos ? n : gt + 1;
            continue;
        }
        std::vector<Attr> attrs;
        bool self_closing = false;
        bool incomplete = false;
        for (;;) {
            while (k < n && ascii_ws((uint8_t)s[k])) ++k;
            if (k >= n) {
                incomplete = true;
                break;
            }
            if (s[k] == '>') {
                ++k;
                break;
            }
            if (s[k] == '/') {
                if (k + 1 < n && s[k + 1] == '>') {
                    self_closing = true;
                    k += 2;
                    break;
                }
                ++k;
                continue;
            }
            size_t an_start = k;
            while (k < n && xml_name_char(s[k], k == an_start)) ++k;
            std::string an = s.substr(an_start, k - an_start);
            if (an.empty()) {
                xml_report(cb, "Invalid attribute");
                ++k;
                continue;
            }
            while (k < n && ascii_ws((uint8_t)s[k])) ++k;
            std::string av;
            if (k < n && s[k] == '=') {
                ++k;
                while (k < n && ascii_ws((uint8_t)s[k])) ++k;
                if (k >= n) {
                    incomplete = true;
                    break;
                }
                if (s[k] == '"' || s[k] == '\'') {
                    char q = s[k++];
                    size_t vs = k;
                    while (k < n && s[k] != q) ++k;
                    if (k >= n) {
                        incomplete = true;
                        break;
                    }
                    av = s.substr(vs, k - vs);
                    ++k;
                } else {
                    size_t vs = k;
                    while (k < n && !ascii_ws((uint8_t)s[k]) && s[k] != '>') ++k;
                    av = s.substr(vs, k - vs);
                }
                bool bad = false;
                av = xml_decode_entities(av, bad);
                if (bad) xml_report(cb, "Invalid character reference");
            } else {
                xml_report(cb, "Attribute without value");
            }
            Attr a;
            a.prefix = "";
            a.ns = "";
            a.local = an;
            a.value = av;
            attrs.push_back(std::move(a));
        }
        if (incomplete) {
            xml_report(cb, "Unexpected EOF in start tag");
            i = n;
            break;
        }
        i = k;

        // Namespace declarations.
        XmlNsFrame frame;
        if (!ns_frames.empty()) frame.default_ns = ns_frames.back().default_ns;
        for (const Attr& a : attrs) {
            if (a.local == "xmlns") {
                frame.default_ns = a.value;
            } else if (a.local.rfind("xmlns:", 0) == 0) {
                frame.prefixes.push_back({a.local.substr(6), a.value});
            }
        }
        std::string prefix;
        std::string local = raw_name;
        size_t colon = raw_name.find(':');
        if (colon != std::string::npos) {
            prefix = raw_name.substr(0, colon);
            local = raw_name.substr(colon + 1);
        }
        std::string ns;
        if (prefix.empty()) {
            ns = frame.default_ns;
        } else {
            ns = xml_lookup_prefix(ns_frames, prefix);
            if (ns.empty()) {
                for (const auto& p : frame.prefixes) {
                    if (p.first == prefix) {
                        ns = p.second;
                        break;
                    }
                }
            }
        }
        ns_frames.push_back(frame);

        if (build) {
            if (!cb.sink->create_element) {
                i = n;
                break;
            }
            CAttributeIterator iter;
            iter.pos = 0;
            iter.attrs.reserve(attrs.size());
            for (const Attr& a : attrs) {
                InternalAttr ia;
                ia.prefix = a.prefix;
                ia.ns = a.ns;
                ia.local = a.local;
                ia.value = a.value;
                iter.attrs.push_back(std::move(ia));
            }
            CQualName q;
            q.prefix.tag = 0;
            std::memset(&q.prefix.value, 0, sizeof(q.prefix.value));
            if (!prefix.empty()) {
                q.prefix.tag = 1;
                q.prefix.value.ptr = reinterpret_cast<const uint8_t*>(raw_name.data());
                q.prefix.value.len = prefix.size();
            }
            std::string local_copy = local;
            q.ns = slice_of(ns);
            q.local = slice_of(local_copy);
            Ref ref = cb.sink->create_element(cb.sink->ctx, (const void*)cb.sink->document, q, &iter);
            if (ref && cb.sink->append) {
                CNodeOrText node;
                node.tag = 0;
                node.node = ref;
                node.text.ptr = nullptr;
                node.text.len = 0;
                Ref parent = stack.empty() ? cb.sink->document : stack.back().ref;
                cb.sink->append(cb.sink->ctx, parent, node);
            }
            if (ref && !self_closing) {
                OpenEl el;
                el.ref = ref;
                el.name = raw_name;
                el.ns = ns;
                stack.push_back(el);
            } else if (ref && self_closing && cb.sink->pop) {
                cb.sink->pop(cb.sink->ctx, ref);
            }
            if (!ref) {
                i = n;
                break;
            }
        } else {
            if (!self_closing) {
                OpenEl el;
                el.ref = nullptr;
                el.name = raw_name;
                el.ns = ns;
                stack.push_back(el);
            } else {
                ns_frames.pop_back();
            }
        }
        if (self_closing && build && !stack.empty() && stack.back().name == raw_name) {
            // handled above
        }
        if (self_closing && !build) {
            // frame already popped
        }
        if (self_closing && build) ns_frames.pop_back();
    }

    if (!stack.empty()) {
        xml_report(cb, "Unexpected EOF: unclosed <" + stack.back().name + ">");
        if (build) {
            while (!stack.empty()) {
                if (cb.sink->pop) cb.sink->pop(cb.sink->ctx, stack.back().ref);
                stack.pop_back();
            }
        }
    }
}

Sink make_sink(Ref document, Ref ctx, CreateElementCallback create_element,
               AppendCallback append, ParseErrorCallback parse_error, PopCallback pop,
               CreateCommentCallback create_comment, CreateProcessingInstruction create_pi,
               AppendDoctypeToDocumentCallback append_doctype, AddAttrsIfMissingCallback add_attrs,
               GetTemplateContentsCallback get_template, RemoveFromParentCallback remove_from_parent,
               ReparentChildrenCallback reparent_children, AppendBeforeSiblingCallback append_before_sibling,
               AppendBasedOnParentNodeCallback append_based_on_parent,
               AttachDeclarativeShadowCallback attach_shadow, bool allow_declarative_shadow) {
    Sink s;
    s.document = document;
    s.ctx = ctx;
    s.create_element = create_element;
    s.append = append;
    s.parse_error = parse_error;
    s.pop = pop;
    s.create_comment = create_comment;
    s.create_pi = create_pi;
    s.append_doctype = append_doctype;
    s.add_attrs = add_attrs;
    s.get_template = get_template;
    s.remove_from_parent = remove_from_parent;
    s.reparent_children = reparent_children;
    s.append_before_sibling = append_before_sibling;
    s.append_based_on_parent = append_based_on_parent;
    s.attach_shadow = attach_shadow;
    s.allow_declarative = allow_declarative_shadow;
    return s;
}

std::string decode_document_input(const uint8_t* data, size_t len, const uint8_t* charset, size_t charset_len,
                                  bool has_charset) {
    if (len == 0) return std::string();
    if (has_charset && charset && charset_len) {
        const EncTag* tag = find_encoding(charset, charset_len);
        if (tag) {
            std::string out;
            out.reserve(len * 2 + 8);
            size_t consumed = 0;
            bool errors = false;
            decode_into(tag->kind, data, len, out, (size_t)-1, consumed, errors, true);
            return out;
        }
    }
    if (len >= 3 && data[0] == 0xEF && data[1] == 0xBB && data[2] == 0xBF) {
        return std::string(reinterpret_cast<const char*>(data + 3), len - 3);
    }
    if (len >= 2 && data[0] == 0xFF && data[1] == 0xFE) {
        std::string out;
        out.reserve(len * 3 + 8);
        size_t consumed = 0;
        bool errors = false;
        decode_into(ENC_UTF16LE, data, len, out, (size_t)-1, consumed, errors, true);
        return out;
    }
    if (len >= 2 && data[0] == 0xFE && data[1] == 0xFF) {
        std::string out;
        out.reserve(len * 3 + 8);
        size_t consumed = 0;
        bool errors = false;
        decode_into(ENC_UTF16BE, data, len, out, (size_t)-1, consumed, errors, true);
        return out;
    }
    return std::string(reinterpret_cast<const char*>(data), len);
}

} // namespace

// ---------------------------------------------------------------------------
// Exported encoding API
// ---------------------------------------------------------------------------

EncodingInfo encoding_for_label(const uint8_t* label, size_t label_len) {
    EncodingInfo info;
    info.found = 0;
    info.handle = nullptr;
    info.name_len = 0;
    info.name_ptr = nullptr;
    if (!label || label_len == 0) return info;
    const EncTag* tag = find_encoding(label, label_len);
    if (!tag) return info;
    info.found = 1;
    info.handle = tag;
    info.name_len = std::strlen(tag->name);
    info.name_ptr = reinterpret_cast<const uint8_t*>(tag->name);
    return info;
}

size_t encoding_max_utf8_buffer_length(const void* handle, size_t input_len) {
    if (!handle) return 0;
    // A UTF-16 unit expands to at most 3 UTF-8 bytes; single-byte encodings to
    // 3 bytes (0x80..0x9F maps into the BMP); UTF-8 never grows.
    return input_len * 3 + 8;
}

DecodeResult encoding_decode(const void* handle, const uint8_t* input, size_t input_len, uint8_t* output,
                             size_t output_len, uint8_t is_last) {
    DecodeResult res;
    res.had_errors = 0;
    res.bytes_read = 0;
    res.bytes_written = 0;
    if (!handle || !output) return res;
    if (!input || input_len == 0) return res;
    const EncTag* tag = static_cast<const EncTag*>(handle);
    std::string tmp;
    tmp.reserve(output_len + 8);
    size_t consumed = 0;
    bool errors = false;
    decode_into(tag->kind, input, input_len, tmp, output_len, consumed, errors, is_last != 0);
    size_t written = std::min(tmp.size(), output_len);
    if (written) std::memcpy(output, tmp.data(), written);
    res.had_errors = errors ? 1 : 0;
    res.bytes_read = consumed;
    res.bytes_written = written;
    return res;
}

void* encoding_decoder_new(const void* handle) {
    return const_cast<void*>(handle);
}

DecodeResult encoding_decoder_decode(void* decoder_ptr, const uint8_t* input, size_t input_len, uint8_t* output,
                                     size_t output_len, uint8_t is_last) {
    return encoding_decode(decoder_ptr, input, input_len, output, output_len, is_last);
}

void encoding_decoder_free(void* decoder_ptr) {
    (void)decoder_ptr;
}

size_t encoding_max_encode_buffer_length(const void* handle, size_t input_len) {
    if (!handle) return 0;
    // Every UTF-8 byte can become at most a 12-byte NCR.
    return input_len * 12 + 8;
}

EncodeResult encoding_encode_with_ncr(const void* handle, const uint8_t* input, size_t input_len, uint8_t* output,
                                      size_t output_len) {
    EncodeResult res;
    res.had_errors = 0;
    res.bytes_read = 0;
    res.bytes_written = 0;
    if (!handle || !input || !output) return res;
    const EncTag* tag = static_cast<const EncTag*>(handle);
    std::string tmp;
    tmp.reserve(output_len + 16);
    size_t i = 0;
    bool errors = false;
    while (i < input_len) {
        uint8_t b = input[i];
        uint32_t cp = 0;
        size_t need = 0;
        if (b < 0x80) {
            cp = b;
            need = 1;
        } else if ((b & 0xE0) == 0xC0) {
            cp = b & 0x1F;
            need = 2;
        } else if ((b & 0xF0) == 0xE0) {
            cp = b & 0x0F;
            need = 3;
        } else if ((b & 0xF8) == 0xF0) {
            cp = b & 0x07;
            need = 4;
        } else {
            errors = true;
            cp = 0xFFFD;
            need = 1;
        }
        if (need > 1) {
            if (i + need > input_len) {
                errors = true;
                cp = 0xFFFD;
                need = 1;
            } else {
                bool ok = true;
                for (size_t k = 1; k < need; ++k) {
                    uint8_t c = input[i + k];
                    if ((c & 0xC0) != 0x80) {
                        ok = false;
                        break;
                    }
                    cp = (cp << 6) | (c & 0x3F);
                }
                if (!ok) {
                    errors = true;
                    cp = 0xFFFD;
                    need = 1;
                }
            }
        }
        encode_cp(tmp, tag->kind, cp);
        i += need;
    }
    size_t written = std::min(tmp.size(), output_len);
    if (written) std::memcpy(output, tmp.data(), written);
    res.had_errors = errors ? 1 : 0;
    res.bytes_read = input_len;
    res.bytes_written = written;
    return res;
}

// ---------------------------------------------------------------------------
// Exported attribute iterator and entity lookup
// ---------------------------------------------------------------------------

CNullable<CAttribute> html5ever_attribute_iterator_next(const void* c_iter) {
    CNullable<CAttribute> res;
    res.tag = 0;
    std::memset(&res.value, 0, sizeof(res.value));
    if (!c_iter) return res;
    CAttributeIterator* iter = const_cast<CAttributeIterator*>(static_cast<const CAttributeIterator*>(c_iter));
    if (iter->pos >= iter->attrs.size()) return res;
    const InternalAttr& a = iter->attrs[iter->pos++];
    res.tag = 1;
    res.value.name.local.ptr = reinterpret_cast<const uint8_t*>(a.local.data());
    res.value.name.local.len = a.local.size();
    res.value.name.ns.ptr = reinterpret_cast<const uint8_t*>(a.ns.data());
    res.value.name.ns.len = a.ns.size();
    if (!a.prefix.empty()) {
        res.value.name.prefix.tag = 1;
        res.value.name.prefix.value.ptr = reinterpret_cast<const uint8_t*>(a.prefix.data());
        res.value.name.prefix.value.len = a.prefix.size();
    } else {
        res.value.name.prefix.tag = 0;
        std::memset(&res.value.name.prefix.value, 0, sizeof(res.value.name.prefix.value));
    }
    res.value.value.ptr = reinterpret_cast<const uint8_t*>(a.value.data());
    res.value.value.len = a.value.size();
    return res;
}

size_t html5ever_attribute_iterator_count(const void* c_iter) {
    if (!c_iter) return 0;
    const CAttributeIterator* iter = static_cast<const CAttributeIterator*>(c_iter);
    return iter->attrs.size();
}

uint8_t html5ever_named_character_reference(const uint8_t* name, size_t len, uint32_t out[2]) {
    if (!name || len == 0) return 0;
    std::string s(reinterpret_cast<const char*>(name), len);
    size_t matched = 0;
    const NamedEntity* e = find_entity_prefix(s, matched);
    if (!e) return 0;
    if (matched == s.size()) {
        out[0] = e->cp;
        out[1] = 0;
        return 2;
    }
    return 1;
}

// ---------------------------------------------------------------------------
// Exported document / fragment / streaming parsers
// ---------------------------------------------------------------------------

void html5ever_parse_document(uint8_t* html, size_t len, Ref document, Ref ctx,
    CreateElementCallback create_element, AppendCallback append, ParseErrorCallback parse_error,
    PopCallback pop, CreateCommentCallback create_comment, CreateProcessingInstruction create_pi,
    AppendDoctypeToDocumentCallback append_doctype, AddAttrsIfMissingCallback add_attrs,
    GetTemplateContentsCallback get_template, RemoveFromParentCallback remove_from_parent,
    ReparentChildrenCallback reparent_children, AppendBeforeSiblingCallback append_before_sibling,
    AppendBasedOnParentNodeCallback append_based_on_parent, AttachDeclarativeShadowCallback attach_shadow,
    bool allow_declarative_shadow) {
    Sink sink = make_sink(document, ctx, create_element, append, parse_error, pop, create_comment,
                          create_pi, append_doctype, add_attrs, get_template, remove_from_parent,
                          reparent_children, append_before_sibling, append_based_on_parent,
                          attach_shadow, allow_declarative_shadow);
    std::string decoded = decode_document_input(html, len, nullptr, 0, false);
    HtmlParser parser;
    parser.sink = sink;
    parser.feed(reinterpret_cast<const uint8_t*>(decoded.data()), decoded.size(), true);
}

void html5ever_parse_document_with_encoding(uint8_t* html, size_t len, const uint8_t* charset, size_t charset_len,
    Ref document, Ref ctx,
    CreateElementCallback create_element, AppendCallback append, ParseErrorCallback parse_error,
    PopCallback pop, CreateCommentCallback create_comment, CreateProcessingInstruction create_pi,
    AppendDoctypeToDocumentCallback append_doctype, AddAttrsIfMissingCallback add_attrs,
    GetTemplateContentsCallback get_template, RemoveFromParentCallback remove_from_parent,
    ReparentChildrenCallback reparent_children, AppendBeforeSiblingCallback append_before_sibling,
    AppendBasedOnParentNodeCallback append_based_on_parent, AttachDeclarativeShadowCallback attach_shadow,
    bool allow_declarative_shadow) {
    Sink sink = make_sink(document, ctx, create_element, append, parse_error, pop, create_comment,
                          create_pi, append_doctype, add_attrs, get_template, remove_from_parent,
                          reparent_children, append_before_sibling, append_based_on_parent,
                          attach_shadow, allow_declarative_shadow);
    std::string decoded = decode_document_input(html, len, charset, charset_len, true);
    HtmlParser parser;
    parser.sink = sink;
    parser.feed(reinterpret_cast<const uint8_t*>(decoded.data()), decoded.size(), true);
}

void html5ever_parse_fragment(uint8_t* html, size_t len, const uint8_t* context_name, size_t context_name_len,
    uint8_t context_ns, Ref document, Ref ctx,
    CreateElementCallback create_element, CreateElementCallback create_context_element,
    AppendCallback append, ParseErrorCallback parse_error, PopCallback pop,
    CreateCommentCallback create_comment, CreateProcessingInstruction create_pi,
    AppendDoctypeToDocumentCallback append_doctype, AddAttrsIfMissingCallback add_attrs,
    GetTemplateContentsCallback get_template, RemoveFromParentCallback remove_from_parent,
    ReparentChildrenCallback reparent_children, AppendBeforeSiblingCallback append_before_sibling,
    AppendBasedOnParentNodeCallback append_based_on_parent, AttachDeclarativeShadowCallback attach_shadow,
    bool allow_declarative_shadow) {
    Sink sink = make_sink(document, ctx, create_element, append, parse_error, pop, create_comment,
                          create_pi, append_doctype, add_attrs, get_template, remove_from_parent,
                          reparent_children, append_before_sibling, append_based_on_parent,
                          attach_shadow, allow_declarative_shadow);
    sink.create_context_element = create_context_element;
    std::string name = context_name ? std::string(reinterpret_cast<const char*>(context_name), context_name_len)
                                    : std::string("div");
    std::string ns = context_ns == 1 ? NS_SVG : (context_ns == 2 ? NS_MATHML : NS_HTML);

    HtmlParser parser;
    parser.sink = sink;
    parser.fragment = true;
    parser.html_seen = true;
    parser.head_seen = true;
    parser.body_seen = true;
    parser.mode = M_IN_BODY;

    Ref root = parser.make_context_element(name, ns);
    if (!root) return;
    OpenEl el;
    el.ref = root;
    el.name = name;
    el.ns = ns;
    parser.stack.push_back(el);

    std::string decoded = decode_document_input(html, len, nullptr, 0, false);
    parser.feed(reinterpret_cast<const uint8_t*>(decoded.data()), decoded.size(), true);
}

void* html5ever_streaming_parser_create(Ref document, Ref ctx,
    CreateElementCallback create_element, AppendCallback append, ParseErrorCallback parse_error,
    PopCallback pop, CreateCommentCallback create_comment, CreateProcessingInstruction create_pi,
    AppendDoctypeToDocumentCallback append_doctype, AddAttrsIfMissingCallback add_attrs,
    GetTemplateContentsCallback get_template, RemoveFromParentCallback remove_from_parent,
    ReparentChildrenCallback reparent_children, AppendBeforeSiblingCallback append_before_sibling,
    AppendBasedOnParentNodeCallback append_based_on_parent, AttachDeclarativeShadowCallback attach_shadow,
    bool allow_declarative_shadow) {
    HtmlParser* parser = new HtmlParser();
    parser->sink = make_sink(document, ctx, create_element, append, parse_error, pop, create_comment,
                             create_pi, append_doctype, add_attrs, get_template, remove_from_parent,
                             reparent_children, append_before_sibling, append_based_on_parent,
                             attach_shadow, allow_declarative_shadow);
    return parser;
}

int32_t html5ever_streaming_parser_feed(void* parser_ptr, const uint8_t* html, size_t len) {
    if (!parser_ptr) return -1;
    HtmlParser* parser = static_cast<HtmlParser*>(parser_ptr);
    parser->feed(html, len, false);
    return parser->failed ? 1 : 0;
}

void html5ever_streaming_parser_finish(void* parser_ptr) {
    if (!parser_ptr) return;
    HtmlParser* parser = static_cast<HtmlParser*>(parser_ptr);
    parser->feed(nullptr, 0, true);
}

void html5ever_streaming_parser_destroy(void* parser_ptr) {
    delete static_cast<HtmlParser*>(parser_ptr);
}

// ---------------------------------------------------------------------------
// Exported XML parsers
// ---------------------------------------------------------------------------

void xml5ever_parse_document(uint8_t* xml, size_t len, Ref document, Ref ctx,
    CreateElementCallback create_element, AppendCallback append, ParseErrorCallback parse_error,
    PopCallback pop, CreateCommentCallback create_comment, CreateProcessingInstruction create_pi,
    AppendDoctypeToDocumentCallback append_doctype, AddAttrsIfMissingCallback add_attrs,
    GetTemplateContentsCallback get_template, RemoveFromParentCallback remove_from_parent,
    ReparentChildrenCallback reparent_children, AppendBeforeSiblingCallback append_before_sibling,
    AppendBasedOnParentNodeCallback append_based_on_parent, AttachDeclarativeShadowCallback attach_shadow,
    bool allow_declarative_shadow) {
    Sink sink = make_sink(document, ctx, create_element, append, parse_error, pop, create_comment,
                          create_pi, append_doctype, add_attrs, get_template, remove_from_parent,
                          reparent_children, append_before_sibling, append_based_on_parent,
                          attach_shadow, allow_declarative_shadow);
    XmlCallbacks cb;
    cb.sink = &sink;
    cb.error = parse_error;
    cb.error_ctx = ctx;
    run_xml(xml, len, cb);
}

void xml5ever_validate_document(uint8_t* xml, size_t len, Ref ctx, ParseErrorCallback parse_error) {
    XmlCallbacks cb;
    cb.sink = nullptr;
    cb.error = parse_error;
    cb.error_ctx = ctx;
    run_xml(xml, len, cb);
}
