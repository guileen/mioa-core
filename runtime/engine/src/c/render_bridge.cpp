// C-ABI Bridge for the Canvas2D / WebGL interface.
//
// Document layout and rasterization live in the Desktop-owned Zig kernel
// (`mioa-desktop/src/render_kernel`, called through `render_kernel` in
// `browser/native_layout.zig` / `browser/screenshot.zig`). What remains here is
// the canvas/WebGL boundary, which is still a stub: commands are accepted and
// pixels stay zero. The PNG encoder is real and serves `canvas.toDataURL()`.

#include <cstdint>
#include <cstddef>
#include <cstring>
#include <cstdlib>
#include <cstdio>
#include <string>
#include <vector>
#include <algorithm>

namespace render_bridge_detail {

// Encodes straight-alpha RGBA as a PNG (colour type 6, 8 bits, no
// interlace). Returns false when the image is empty.
bool encode_rgba_png(const uint8_t* pixels, uint32_t width, uint32_t height, std::string& out);

} // namespace render_bridge_detail

extern "C" {

typedef bool (*WriteCallback)(void* ctx, const uint8_t* data, size_t len);

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
// PNG encoder
//
// Stored (uncompressed) deflate: deterministic, dependency-free, and the canvas
// readback path is not hot. Screenshot PNGs come from the Zig kernel's own
// encoder.
// ---------------------------------------------------------------------------

namespace render_bridge_detail {

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
