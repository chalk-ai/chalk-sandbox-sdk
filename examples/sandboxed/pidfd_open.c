#define _GNU_SOURCE
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/syscall.h>
#include <unistd.h>

#ifndef SYS_pidfd_open
#if defined(__x86_64__) || defined(__aarch64__)
#define SYS_pidfd_open 434
#else
#error "Unknown arch: define SYS_pidfd_open"
#endif
#endif

int main(void) {
    int fd = syscall(SYS_pidfd_open, getpid(), 0);
    if (fd >= 0) {
        puts("pidfd_open(self): SUCCESS");
        close(fd);
        return 0;
    }
    printf("pidfd_open(self): %s (%d)\n", strerror(errno), errno);
    return 1;
}