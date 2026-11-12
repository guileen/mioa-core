#include "api/system/terminal_v1.h"

#include <string.h>
#if defined(_WIN32)
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#elif defined(__unix__) || defined(__APPLE__)
#include <sys/ioctl.h>
#include <termios.h>
#include <unistd.h>
#endif

uint32_t mioa_terminal_stderr_columns_v1(void) {
#if defined(_WIN32)
    HANDLE stderr_handle = GetStdHandle(STD_ERROR_HANDLE);
    CONSOLE_SCREEN_BUFFER_INFO info;
    if (stderr_handle == NULL || stderr_handle == INVALID_HANDLE_VALUE ||
        !GetConsoleScreenBufferInfo(stderr_handle, &info))
        return 0;
    const int32_t columns = (int32_t)info.srWindow.Right -
        (int32_t)info.srWindow.Left + 1;
    return columns > 0 ? (uint32_t)columns : 0;
#elif (defined(__unix__) || defined(__APPLE__)) && defined(TIOCGWINSZ) && defined(STDERR_FILENO)
    struct winsize size;
    if (ioctl(STDERR_FILENO, TIOCGWINSZ, &size) != 0 || size.ws_col == 0)
        return 0;
    return size.ws_col;
#else
    return 0;
#endif
}

int32_t mioa_terminal_stdin_raw_enter_v1(mioa_terminal_mode_v1 *state) {
#if defined(__unix__) || defined(__APPLE__)
    _Static_assert(sizeof(struct termios) <= sizeof(state->opaque) - sizeof(state->opaque[0]),
                   "terminal state storage too small");
    if (!state || state->opaque[0]) return -1;
    struct termios original;
    if (tcgetattr(STDIN_FILENO, &original) != 0) return -1;
    struct termios raw = original;
    raw.c_iflag &= ~(BRKINT | ICRNL | INPCK | ISTRIP | IXON);
    raw.c_oflag &= ~OPOST;
    raw.c_cflag = (raw.c_cflag & ~CSIZE) | CS8;
    raw.c_lflag &= ~(ECHO | ICANON | IEXTEN | ISIG);
    raw.c_cc[VMIN] = 0;
    raw.c_cc[VTIME] = 1;
    if (tcsetattr(STDIN_FILENO, TCSAFLUSH, &raw) != 0) return -1;
    memcpy(&state->opaque[1], &original, sizeof(original));
    state->opaque[0] = 1;
    return 0;
#else
    (void)state;
    return -1;
#endif
}

int32_t mioa_terminal_stdin_raw_restore_v1(mioa_terminal_mode_v1 *state) {
#if defined(__unix__) || defined(__APPLE__)
    if (!state || state->opaque[0] != 1) return -1;
    struct termios original;
    memcpy(&original, &state->opaque[1], sizeof(original));
    if (tcsetattr(STDIN_FILENO, TCSAFLUSH, &original) != 0) return -1;
    memset(state, 0, sizeof(*state));
    return 0;
#else
    (void)state;
    return -1;
#endif
}
