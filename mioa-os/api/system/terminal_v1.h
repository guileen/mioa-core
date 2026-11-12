#ifndef MIOA_TERMINAL_V1_H
#define MIOA_TERMINAL_V1_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Current Host stderr terminal width in columns; 0 means unavailable. */
uint32_t mioa_terminal_stderr_columns_v1(void);

/* Caller-owned, zero-initialized local terminal mode state; do not copy while active. */
typedef struct { uint64_t opaque[32]; } mioa_terminal_mode_v1;
/* Return 0 on success. Failure leaves the state unchanged. */
int32_t mioa_terminal_stdin_raw_enter_v1(mioa_terminal_mode_v1 *state);
/* On failure the state remains active and may be retried. */
int32_t mioa_terminal_stdin_raw_restore_v1(mioa_terminal_mode_v1 *state);

#ifdef __cplusplus
}
#endif
#endif
