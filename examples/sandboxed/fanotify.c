#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/fanotify.h>
#include <sys/types.h>
#include <unistd.h>

static void print_fd_path(int fd)
{
    char proc_path[64];
    char path[PATH_MAX];
    ssize_t len;

    snprintf(proc_path, sizeof(proc_path), "/proc/self/fd/%d", fd);
    len = readlink(proc_path, path, sizeof(path) - 1);
    if (len < 0) {
        perror("readlink");
        return;
    }

    path[len] = '\0';
    printf("path=%s\n", path);
}

int main(int argc, char **argv)
{
    const char *watch_path;
    int fan_fd;
    struct pollfd pfd;

    if (argc != 2) {
        fprintf(stderr, "usage: %s <path>\n", argv[0]);
        fprintf(stderr, "example: %s /tmp\n", argv[0]);
        return 1;
    }

    watch_path = argv[1];

    fan_fd = fanotify_init(FAN_CLASS_NOTIF | FAN_CLOEXEC, O_RDONLY | O_LARGEFILE);
    if (fan_fd < 0) {
        perror("fanotify_init");
        fprintf(stderr, "fanotify requires Linux and usually root or CAP_SYS_ADMIN.\n");
        return 1;
    }

    if (fanotify_mark(fan_fd,
                      FAN_MARK_ADD | FAN_MARK_MOUNT,
                      FAN_OPEN | FAN_EVENT_ON_CHILD,
                      AT_FDCWD,
                      watch_path) < 0) {
        perror("fanotify_mark");
        close(fan_fd);
        return 1;
    }

    printf("Watching %s for open events.\n", watch_path);
    printf("Press Ctrl+C to stop.\n");

    pfd.fd = fan_fd;
    pfd.events = POLLIN;

    for (;;) {
        char buffer[8192];
        ssize_t length;
        struct fanotify_event_metadata *metadata;

        if (poll(&pfd, 1, -1) < 0) {
            if (errno == EINTR) {
                continue;
            }

            perror("poll");
            break;
        }

        length = read(fan_fd, buffer, sizeof(buffer));
        if (length < 0) {
            if (errno == EINTR) {
                continue;
            }

            perror("read");
            break;
        }

        metadata = (struct fanotify_event_metadata *)buffer;
        while (FAN_EVENT_OK(metadata, length)) {
            if (metadata->vers != FANOTIFY_METADATA_VERSION) {
                fprintf(stderr, "fanotify metadata version mismatch.\n");
                close(fan_fd);
                return 1;
            }

            if (metadata->fd >= 0) {
                printf("pid=%d mask=0x%llx ", metadata->pid,
                       (unsigned long long)metadata->mask);
                print_fd_path(metadata->fd);
                close(metadata->fd);
            } else if (metadata->fd == FAN_NOFD) {
                printf("event without file descriptor, mask=0x%llx pid=%d\n",
                       (unsigned long long)metadata->mask, metadata->pid);
            }

            metadata = FAN_EVENT_NEXT(metadata, length);
        }
    }

    close(fan_fd);
    return 0;
}
