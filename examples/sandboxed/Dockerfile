FROM alpine:3.23

RUN apk update && apk add --no-cache \
  bash \
  build-base \
  e2fsprogs \
  eza \
  findutils \
  iproute2 \
  keyutils \
  kubectl \
  libcap-utils \
  linux-headers \
  netcat-openbsd \
  procps \
  quota-tools \
  strace \
  util-linux

WORKDIR /work
COPY rawsock.c pidfd_open.c io_uring.c syscalls.c fanotify.c kexec_load.c .
RUN for src in \
      rawsock \
      pidfd_open \
      io_uring \
      syscalls \
      fanotify \
      kexec_load; do \
      gcc -O2 -Wall "/work/${src}.c" -o "/work/${src}"; \
    done

CMD ["sleep", "infinity"]
