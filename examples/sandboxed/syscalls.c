#include <errno.h>
#include <fcntl.h>
#include <linux/bpf.h>
#include <linux/io_uring.h>
#include <linux/perf_event.h>
#include <linux/userfaultfd.h>
#include <stdio.h>
#include <string.h>
#include <sys/syscall.h>
#include <unistd.h>

static int probe_bpf(void) {
    union bpf_attr attr;
    memset(&attr, 0, sizeof(attr));
    attr.map_type = BPF_MAP_TYPE_ARRAY;
    attr.key_size = 4;
    attr.value_size = 8;
    attr.max_entries = 1;
    return syscall(SYS_bpf, BPF_MAP_CREATE, &attr, sizeof(attr));
}

static int probe_perf(void) {
    struct perf_event_attr attr;
    memset(&attr, 0, sizeof(attr));
    attr.size = sizeof(attr);
    attr.type = PERF_TYPE_SOFTWARE;
    attr.config = PERF_COUNT_SW_CPU_CLOCK;
    return syscall(SYS_perf_event_open, &attr, -1, 0, -1, 0);
}

static int probe_uffd(void) {
    return syscall(SYS_userfaultfd, O_CLOEXEC | O_NONBLOCK);
}

static int probe_io_uring(void) {
    struct io_uring_params p;
    memset(&p, 0, sizeof(p));
    return syscall(SYS_io_uring_setup, 1, &p);
}

int main(int argc, char **argv) {
    int fd = -1;
    if (argc != 2) return 2;
    if (!strcmp(argv[1], "bpf")) fd = probe_bpf();
    else if (!strcmp(argv[1], "perf")) fd = probe_perf();
    else if (!strcmp(argv[1], "uffd")) fd = probe_uffd();
    else if (!strcmp(argv[1], "io_uring")) fd = probe_io_uring();
    else return 2;

    if (fd >= 0) {
        printf("ALLOWED\n");
        close(fd);
        return 0;
    }

    printf("DENIED errno=%d (%s)\n", errno, strerror(errno));
    return 1;
}
