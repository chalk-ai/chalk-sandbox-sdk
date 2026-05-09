#include <errno.h>
#include <fcntl.h>
#include <linux/kexec.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <unistd.h>

static void usage(const char *progname)
{
    fprintf(stderr,
            "Usage: %s <kernel-file> [initramfs-file] [cmdline] [flags]\n"
            "\n"
            "Loads a kernel with kexec_file_load(2).\n"
            "\n"
            "  kernel-file     Kernel image file to load.\n"
            "  initramfs-file  Optional initramfs image. Use - for none.\n"
            "  cmdline         Optional kernel command line. Use - for empty.\n"
            "  flags           Optional extra kexec flags. Defaults to 0.\n"
            "\n"
            "Examples:\n"
            "  %s /boot/vmlinuz-lts\n"
            "  %s /boot/vmlinuz-lts /boot/initramfs-lts 'console=ttyS0 reboot=k'\n"
            "\n"
            "Notes:\n"
            "  - This version assumes Alpine Linux on x86_64.\n"
            "  - You normally need CAP_SYS_BOOT and a kernel built with CONFIG_KEXEC_FILE.\n",
            progname, progname, progname);
}

static int open_ro(const char *path)
{
    int fd;

    fd = open(path, O_RDONLY);
    if (fd < 0) {
        fprintf(stderr, "open %s failed: %s\n", path, strerror(errno));
    }

    return fd;
}

int main(int argc, char **argv)
{
    const char *kernel_path;
    const char *initramfs_path = NULL;
    const char *cmdline = "";
    const char *flags_text = NULL;
    int kernel_fd;
    int initramfs_fd = -1;
    unsigned long flags = 0;
    char *end = NULL;
    long rc;

    if (argc < 2 || argc > 5) {
        usage(argv[0]);
        return 1;
    }

    kernel_path = argv[1];
    if (argc >= 3 && strcmp(argv[2], "-") != 0) {
        initramfs_path = argv[2];
    }
    if (argc >= 4 && strcmp(argv[3], "-") != 0) {
        cmdline = argv[3];
    }
    if (argc == 5) {
        flags_text = argv[4];
    }

    if (flags_text != NULL) {
        errno = 0;
        flags = strtoul(flags_text, &end, 0);
        if (errno != 0 || end == flags_text || *end != '\0') {
            fprintf(stderr, "failed to parse flags\n");
            return 1;
        }
    }

    kernel_fd = open_ro(kernel_path);
    if (kernel_fd < 0) {
        return 1;
    }

    if (initramfs_path != NULL) {
        initramfs_fd = open_ro(initramfs_path);
        if (initramfs_fd < 0) {
            close(kernel_fd);
            return 1;
        }
    }

    rc = syscall(SYS_kexec_file_load,
                 kernel_fd,
                 initramfs_fd,
                 (unsigned long)strlen(cmdline),
                 cmdline,
                 flags);
    if (rc < 0) {
        fprintf(stderr, "kexec_file_load failed: %s\n", strerror(errno));
        if (initramfs_fd >= 0) {
            close(initramfs_fd);
        }
        close(kernel_fd);
        return 1;
    }

    printf("kexec kernel loaded successfully.\n");
    printf("kernel=%s\n", kernel_path);
    if (initramfs_path != NULL) {
        printf("initramfs=%s\n", initramfs_path);
    } else {
        printf("initramfs=(none)\n");
    }
    printf("cmdline=%s\n", cmdline[0] == '\0' ? "(empty)" : cmdline);
    printf("You can trigger it with: kexec -e\n");

    if (initramfs_fd >= 0) {
        close(initramfs_fd);
    }
    close(kernel_fd);
    return 0;
}
