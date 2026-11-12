/* Pinned Brotli cleanup-on-OOM can consume input after ring-buffer allocation
 * fails and return success for an empty final block. Keep upstream untouched;
 * expose failure before Core publishes any output from that call. */
#define BrotliEncoderCompressStream lp_brotli_encoder_compress_unchecked
#include "encode.c"
#undef BrotliEncoderCompressStream

BROTLI_BOOL BrotliEncoderCompressStream(
    BrotliEncoderState *state, BrotliEncoderOperation operation,
    size_t *available_in, const uint8_t **next_in,
    size_t *available_out, uint8_t **next_out, size_t *total_out) {
  BROTLI_BOOL result = lp_brotli_encoder_compress_unchecked(
      state, operation, available_in, next_in, available_out, next_out, total_out);
  return BROTLI_IS_OOM(&state->memory_manager_) ? BROTLI_FALSE : result;
}
