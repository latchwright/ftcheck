/* A C dependency of the kind that is everywhere and documented, somewhere, as
 * "not thread-safe". The static buffer is the whole problem. */
#include <stdio.h>

static char buf[64];

const char *legacy_format(unsigned long v) {
    snprintf(buf, sizeof buf, "value=%lu", v);
    return buf;
}
