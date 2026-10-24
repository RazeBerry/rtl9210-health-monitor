/* USB-free harness: only libc, production signal handler, and fake resource counters. */
#include "cancellation.h"
#include <stdlib.h>
#include <stdio.h>
#include <unistd.h>
int main(int argc, char **argv)
{
    if (argc != 3) return 2;
    int signum = atoi(argv[1]), phase = atoi(argv[2]);
    int acquired = 0, released = 0, commands = 0;
    if (rtl_install_cancellation() != 0) return 3;
    for (int step = 0; step < 4; ++step) {
        if (step == phase) {
            if (raise(signum) != 0) return 4;
            /* The signal handler must return; it must not release resources itself. */
            if (released != 0 || rtl_cancel_signal != signum) return 5;
        }
        if (rtl_cancel_signal) goto cleanup;
        if (step == 0) acquired = 1; /* fake capture */
        else ++commands;            /* fake configure / transport */
    }
cleanup:
    if (acquired) ++released; /* normal-context cleanup, no device APIs */
    if (released != (phase > 0) || commands != (phase > 0 ? phase-1 : 0)) return 6;
    puts("cancel observed; fake cleanup completed; no subsequent operation");
    return 0;
}
