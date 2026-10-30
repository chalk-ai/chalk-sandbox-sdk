#include <errno.h>
#include <fcntl.h>
#include <linux/bpf.h>
#include <linux/io_uring.h>
#include <linux/mount.h>
#include <linux/perf_event.h>
#include <linux/ptrace.h>
#include <linux/quota.h>
#include <linux/sched.h>
#include <sched.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/utsname.h>
#include <sys/wait.h>
#include <unistd.h>

#ifndef OPEN_TREE_CLOEXEC
#define OPEN_TREE_CLOEXEC O_CLOEXEC
#endif

#ifndef MAP_STACK
#define MAP_STACK 0
#endif

#ifndef MOVE_MOUNT_F_EMPTY_PATH
#define MOVE_MOUNT_F_EMPTY_PATH 0x00000004
#endif

#ifndef MNT_FORCE
#define MNT_FORCE 1
#endif

#ifndef MOUNT_ATTR_RDONLY
#define MOUNT_ATTR_RDONLY 0x00000001ULL
#endif

#define STACK_SIZE (1024 * 1024)

struct test_result {
    int rc;
    int err;
};

static int total_tests = 0;

static struct test_result run_syscall(long nr, long a1, long a2, long a3, long a4, long a5, long a6) {
    struct test_result result;
    errno = 0;
    result.rc = (int)syscall(nr, a1, a2, a3, a4, a5, a6);
    result.err = errno;
    return result;
}

static void print_result(const char *name, struct test_result result, const char *note) {
    printf("[%02d] %-18s rc=%d", ++total_tests, name, result.rc);
    if (result.rc < 0) {
        printf(" errno=%d (%s)", result.err, strerror(result.err));
    }
    if (note && *note) {
        printf("  %s", note);
    }
    putchar('\n');
}

static int child_main(void *arg) {
    (void)arg;
    _exit(0);
}

static void test_bpf(void) {
    union bpf_attr attr;
    memset(&attr, 0, sizeof(attr));
    struct test_result result = run_syscall(SYS_bpf, BPF_MAP_GET_NEXT_ID, (long)&attr, sizeof(attr), 0, 0, 0);
    print_result("bpf", result, "Unprivileged callers commonly get EPERM.");
}

static void test_clone(void) {
    void *stack = mmap(NULL, STACK_SIZE, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS | MAP_STACK, -1, 0);
    if (stack == MAP_FAILED) {
        perror("mmap for clone");
        return;
    }

    void *stack_top = (char *)stack + STACK_SIZE;
    struct test_result result = run_syscall(SYS_clone, SIGCHLD, (long)stack_top, 0, 0, 0, 0);
    if (result.rc == 0) {
        child_main(NULL);
    }
    print_result("clone", result, "With SIGCHLD only, this should behave like fork.");
    if (result.rc > 0) {
        waitpid(result.rc, NULL, 0);
    }
    munmap(stack, STACK_SIZE);
}

static void test_clone3(void) {
    struct clone_args args;
    memset(&args, 0, sizeof(args));
    args.exit_signal = SIGCHLD;
    struct test_result result = run_syscall(SYS_clone3, (long)&args, sizeof(args), 0, 0, 0, 0);
    if (result.rc == 0) {
        _exit(0);
    }
    print_result("clone3", result, "Older kernels may return ENOSYS.");
    if (result.rc > 0) {
        waitpid(result.rc, NULL, 0);
    }
}

static void test_mount_family(void) {
    struct test_result result;
    const char *target = "/tmp/syscall-probe-mount-target";
    mkdir(target, 0700);
    result = run_syscall(SYS_mount, (long)"none", (long)target, (long)"tmpfs", 0, 0, 0);
    print_result("mount", result, "Usually EPERM without CAP_SYS_ADMIN.");
    result = run_syscall(SYS_fsopen, (long)"tmpfs", 0, 0, 0, 0, 0);
    print_result("fsopen", result, "Success requires the new mount API and sufficient privilege.");
    int fsfd = result.rc;
    result = run_syscall(SYS_fsmount, fsfd, FSMOUNT_CLOEXEC, 0, 0, 0, 0);
    print_result("fsmount", result, "If fsopen failed, expect EBADF.");
    int mfd = result.rc;
    result = run_syscall(SYS_open_tree, AT_FDCWD, (long)"/tmp", OPEN_TREE_CLOEXEC, 0, 0, 0);
    print_result("open_tree", result, "May require CAP_SYS_ADMIN depending on flags/kernel.");
    int treefd = result.rc;
    struct mount_attr attr;
    memset(&attr, 0, sizeof(attr));
    attr.attr_set = MOUNT_ATTR_RDONLY;
    result = run_syscall(SYS_mount_setattr, AT_FDCWD, (long)"/tmp", 0, (long)&attr, sizeof(attr), 0);
    print_result("mount_setattr", result, "Usually EPERM on existing mounts.");
    result = run_syscall(SYS_move_mount, treefd, (long)"", AT_FDCWD, (long)target, MOVE_MOUNT_F_EMPTY_PATH, 0);
    print_result("move_mount", result, "If open_tree failed, expect EBADF.");

    if (treefd >= 0) {
        close(treefd);
    }
    if (mfd >= 0) {
        close(mfd);
    }
    if (fsfd >= 0) {
        close(fsfd);
    }
    rmdir(target);
}

static void test_perf_event_open(void) {
    struct perf_event_attr attr;
    memset(&attr, 0, sizeof(attr));
    attr.type = PERF_TYPE_SOFTWARE;
    attr.size = sizeof(attr);
    attr.config = PERF_COUNT_SW_CPU_CLOCK;
    attr.disabled = 1;
    struct test_result result = run_syscall(SYS_perf_event_open, (long)&attr, 0, -1, -1, 0, 0);
    print_result("perf_event_open", result, "Often blocked by perf_event_paranoid or seccomp.");
    if (result.rc >= 0) {
        close(result.rc);
    }
}

static void test_quotactl(void) {
    int cmd = QCMD(Q_GETFMT, USRQUOTA);
    struct test_result result = run_syscall(SYS_quotactl, cmd, (long)"/", 0, 0, 0, 0);
    print_result("quotactl", result, "Unsupported filesystems often return EINVAL/ENOTBLK/EPERM.");
}

static void test_domain_and_hostname(void) {
    struct utsname uts;
    struct test_result result;
    const char *domain = "probe.local";
    const char *hostname = "syscall-probe";

    if (uname(&uts) == 0 && strlen(uts.nodename) > 0) {
        hostname = uts.nodename;
    }
    result = run_syscall(SYS_setdomainname, (long)domain, strlen(domain), 0, 0, 0, 0);
    print_result("setdomainname", result, "This is privileged even when reusing the current domain.");
    result = run_syscall(SYS_sethostname, (long)hostname, strlen(hostname), 0, 0, 0, 0);
    print_result("sethostname", result, "This is privileged even when reusing the current hostname.");
}

static void test_syslog(void) {
    char buffer[64];
    memset(buffer, 0, sizeof(buffer));
    struct test_result result = run_syscall(SYS_syslog, 10, (long)buffer, sizeof(buffer), 0, 0, 0);
    print_result("syslog", result, "Kernel log reads are often blocked for unprivileged users.");
}

static void test_umount_family(void) {
    struct test_result result = run_syscall(SYS_umount2, (long)"/tmp/syscall-probe-not-mounted", 0, 0, 0, 0, 0);
    print_result("umount", result, "A nonexistent or unmounted target yields ENOENT/EINVAL.");

    result = run_syscall(SYS_umount2, (long)"/tmp/syscall-probe-not-mounted", MNT_FORCE, 0, 0, 0, 0);
    print_result("umount2", result, "Forced unmount is usually privileged.");
}

static void test_chroot(void) {
    struct test_result result = run_syscall(SYS_chroot, (long)"/", 0, 0, 0, 0, 0);
    print_result("chroot", result, "Even chroot to / is privileged.");
}

static void test_ptrace(void) {
    struct test_result result = run_syscall(SYS_ptrace, PTRACE_TRACEME, 0, 0, 0, 0, 0);
    print_result("ptrace", result, "PTRACE_TRACEME should succeed unless blocked by seccomp.");
}

static void test_io_uring(void) {
    struct test_result result;
    struct io_uring_params params;
    memset(&params, 0, sizeof(params));
    result = run_syscall(SYS_io_uring_setup, 2, (long)&params, 0, 0, 0, 0);
    print_result("io_uring_setup", result, "Older kernels may return ENOSYS; sandboxing may return EPERM.");
    int ringfd = result.rc;
    result = run_syscall(SYS_io_uring_register, ringfd, IORING_REGISTER_PROBE, 0, 0, 0, 0);
    print_result("io_uring_register", result, "If setup failed, expect EBADF.");
    result = run_syscall(SYS_io_uring_enter, ringfd, 0, 0, IORING_ENTER_GETEVENTS, 0, 0);
    print_result("io_uring_enter", result, "With no submissions, expect EAGAIN/EINVAL/EBADF.");

    if (ringfd >= 0) {
        close(ringfd);
    }
}

int main(void) {
    printf("linux/amd64 syscall probe\n");
    printf("This program reports raw syscall results; EPERM, EINVAL, and ENOSYS are often expected.\n\n");

    test_bpf();
    test_clone();
    test_clone3();
    test_mount_family();
    test_perf_event_open();
    test_quotactl();
    test_domain_and_hostname();
    test_syslog();
    test_umount_family();
    test_chroot();
    test_ptrace();
    test_io_uring();

    return 0;
}
