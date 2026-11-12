/* Independent decoder: no encoder/parser code from mioa is linked here.
 * Preserve stored RGBA8 samples: deliberately do not request gamma conversion,
 * alpha compositing, or background replacement. Exit 3 is a libpng rejection;
 * exit 1 is adapter/resource/I/O failure, and exit 2 is invalid CLI usage. */
#include <png.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

struct decoder {
    FILE *input;
    FILE *output;
    png_structp png;
    png_infop info;
    unsigned char *pixels;
    png_bytep *rows;
};

static int release(struct decoder *d, int status) {
    if (d->output && fclose(d->output) != 0) status = 1;
    if (d->input) fclose(d->input);
    free(d->rows);
    free(d->pixels);
    if (d->png) png_destroy_read_struct(&d->png, d->info ? &d->info : NULL, NULL);
    free(d);
    return status;
}

int main(int argc, char **argv) {
    if (argc != 3) {
        fprintf(stderr, "usage: libpng_decoder input.png output.rgba\n");
        return 2;
    }
    /* Heap state remains defined when libpng longjmps on a decoding error. */
    struct decoder *d = calloc(1, sizeof *d);
    if (!d) return 1;
    d->input = fopen(argv[1], "rb");
    if (!d->input) return release(d, 1);
    d->png = png_create_read_struct(PNG_LIBPNG_VER_STRING, NULL, NULL, NULL);
    if (!d->png) return release(d, 1);
    d->info = png_create_info_struct(d->png);
    if (!d->info) return release(d, 1);
    if (setjmp(png_jmpbuf(d->png))) return release(d, 3);
    png_init_io(d->png, d->input);
    png_set_crc_action(d->png, PNG_CRC_ERROR_QUIT, PNG_CRC_ERROR_QUIT);
    png_set_user_limits(d->png, 32768, 32768);
    png_read_info(d->png, d->info);
    png_uint_32 width = png_get_image_width(d->png, d->info);
    png_uint_32 height = png_get_image_height(d->png, d->info);
    int depth = png_get_bit_depth(d->png, d->info);
    int color = png_get_color_type(d->png, d->info);
    if (color == PNG_COLOR_TYPE_PALETTE) png_set_palette_to_rgb(d->png);
    if (color == PNG_COLOR_TYPE_GRAY && depth < 8) png_set_expand_gray_1_2_4_to_8(d->png);
    int transparency = png_get_valid(d->png, d->info, PNG_INFO_tRNS) != 0;
    if (transparency) png_set_tRNS_to_alpha(d->png);
    if (depth == 16) png_set_strip_16(d->png);
    if (color == PNG_COLOR_TYPE_GRAY || color == PNG_COLOR_TYPE_GRAY_ALPHA) png_set_gray_to_rgb(d->png);
    if (!(color & PNG_COLOR_MASK_ALPHA) && !transparency) png_set_add_alpha(d->png, 255, PNG_FILLER_AFTER);
    (void)png_set_interlace_handling(d->png);
    png_read_update_info(d->png, d->info);
    size_t stride = png_get_rowbytes(d->png, d->info);
    if (width == 0 || height == 0 || stride != (size_t)width * 4 ||
        (size_t)height > (268435456u / stride)) return release(d, 1);
    size_t size = stride * height;
    d->pixels = malloc(size);
    d->rows = malloc((size_t)height * sizeof *d->rows);
    if (!d->pixels || !d->rows) return release(d, 1);
    for (png_uint_32 y = 0; y < height; ++y) d->rows[y] = d->pixels + (size_t)y * stride;
    png_read_image(d->png, d->rows);
    png_read_end(d->png, NULL);
    d->output = fopen(argv[2], "wb");
    if (!d->output || fwrite(d->pixels, 1, size, d->output) != size) return release(d, 1);
    if (release(d, 0) != 0) return 1;
    if (printf("{\"width\":%u,\"height\":%u,\"format\":\"RGBA8\"}\n", width, height) < 0) return 1;
    return 0;
}
