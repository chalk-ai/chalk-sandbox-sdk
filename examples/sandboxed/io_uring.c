#define _GNU_SOURCE
#include <errno.h>
#include <linux/io_uring.h>
#include <stdio.h>
#include <string.h>
#include <sys/syscall.h>
#include <unistd.h>

#ifndef SYS_io_uring_setup
#if defined(__x86_64__) || defined(__aarch64__)
#define SYS_io_uring_setup 425
#else
#error "Unknown arch: define SYS_io_uring_setup"
#endif
#endif

int main(void) {
    struct io_uring_params p;
    memset(&p, 0, sizeof(p));
    int fd = syscall(SYS_io_uring_setup, 1, &p);
    if (fd >= 0) {
        puts("io_uring_setup: SUCCESS");
        close(fd);
        return 0;
    }
    printf("io_uring_setup: %s (%d)\n", strerror(errno), errno);
    return 1;
}