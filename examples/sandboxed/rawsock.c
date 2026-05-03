#include <arpa/inet.h>
#include <errno.h>
#include <linux/if_ether.h>
#include <linux/if_packet.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

int main(void) {
    int fd = socket(AF_PACKET, SOCK_RAW, htons(ETH_P_ALL));
    if (fd >= 0) {
        puts("AF_PACKET raw socket: SUCCESS");
        close(fd);
        return 0;
    }
    printf("AF_PACKET raw socket: %s (%d)\n", strerror(errno), errno);
    return 1;
}
