#ifndef MIOA_PROCESS_V1_H
#define MIOA_PROCESS_V1_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum mioa_process_status_v1 {
    MIOA_PROCESS_OK = 0,
    MIOA_PROCESS_INVALID = 1,
    MIOA_PROCESS_NO_MEMORY = 2,
    MIOA_PROCESS_START_FAILED = 3,
    MIOA_PROCESS_WAIT_FAILED = 4,
    MIOA_PROCESS_CHILD_FAILED = 5,
    MIOA_PROCESS_NOT_FOUND = 6,
    MIOA_PROCESS_CAPTURE_FAILED = 7
} mioa_process_status_v1;

typedef struct mioa_process_capture_result_v1 {
    size_t stdout_length;
    size_t stderr_length;
    int32_t exit_code;
    uint8_t exited;
} mioa_process_capture_result_v1;

/* Execute argv directly, inheriting the Host environment and stdout/stderr.
 * stdin receives input[0..input_length] and is closed before waiting for the
 * child. argv/input are borrowed until return; input may be NULL iff length 0.
 * The provider owns and reaps the child. A closed stdin pipe is not by itself a
 * failure; a normal exit with status 0 returns OK. */
mioa_process_status_v1 mioa_process_run_with_stdin_v1(
    const char *const argv[], size_t argc,
    const unsigned char *input, size_t input_length);
/* Capture argv directly with Host environment. stdout and stderr are exact byte
 * slices, independently bounded by the caller-provided disjoint buffers.
 * Result lengths and exited are zero on capture failure; exit_code is valid
 * only when exited is nonzero. Child output is not NUL-terminated. */
mioa_process_status_v1 mioa_process_capture_v1(
    const char *const argv[], size_t argc,
    unsigned char *stdout_buffer, size_t stdout_capacity,
    unsigned char *stderr_buffer, size_t stderr_capacity,
    mioa_process_capture_result_v1 *result);

/* A nonzero or abnormal child exit returns CHILD_FAILED with captured lengths
 * and termination facts populated. Output overflow or another capture failure
 * returns CAPTURE_FAILED with result lengths zero. */
/* Flush the Host's shared I/O instance and return to the caller. */
void mioa_process_flush_v1(void);

/* Terminate immediately with the exact 8-bit status; this function does not
 * flush buffered output and does not return. */
#ifdef __cplusplus
[[noreturn]] void mioa_process_exit_v1(uint8_t status);
#else
_Noreturn void mioa_process_exit_v1(uint8_t status);
#endif

/* Return the current process identifier. */
int64_t mioa_process_pid_v1(void);

/* Send a signal to a process, or request termination. Returns MIOA_PROCESS_OK on success. */
mioa_process_status_v1 mioa_process_kill_v1(int64_t pid, int32_t signal);

#ifdef __cplusplus
}
#endif
#endif
