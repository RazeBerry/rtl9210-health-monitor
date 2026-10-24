#ifndef RTL_CANCELLATION_H
#define RTL_CANCELLATION_H
#include <signal.h>
#include <string.h>
static volatile sig_atomic_t rtl_cancel_signal = 0;
static void rtl_request_cancel(int signum) { rtl_cancel_signal = signum; }
static int rtl_install_cancellation(void)
{
    struct sigaction action;
    memset(&action, 0, sizeof action);
    action.sa_handler = rtl_request_cancel;
    sigemptyset(&action.sa_mask);
    const int signals[] = {SIGINT, SIGTERM, SIGHUP};
    for (unsigned i = 0; i < sizeof(signals) / sizeof(signals[0]); ++i)
        if (sigaction(signals[i], &action, NULL) != 0) return -1;
    return 0;
}
#endif
