FROM alpine:3.23

RUN apk update && apk add --no-cache \
  bash \
  linux-headers \
  build-base

WORKDIR /work
COPY linux_amd64_syscall_probe.c .
# RUN printf '#!/bin/bash\ngcc -O2 -Wall -static linux_amd64_syscall_probe.c -o linux_amd64_syscall_probe\n' >build.sh
# RUN chmod +x build.sh
# RUN ./build.sh
RUN gcc -O2 -Wall -static linux_amd64_syscall_probe.c -o linux_amd64_syscall_probe

# CMD ["sleep", "infinity"]
CMD ["./linux_amd64_syscall_probe"]
