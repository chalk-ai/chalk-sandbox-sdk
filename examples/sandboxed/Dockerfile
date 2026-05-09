FROM alpine:3.23

RUN apk update && apk add --no-cache \
  bash \
  build-base \
  e2fsprogs \
  findutils \
  iproute2 \
  jq \
  keyutils \
  kubectl \
  libcap-utils \
  linux-headers \
  netcat-openbsd \
  procps \
  quota-tools \
  strace \
  tree \
  util-linux

WORKDIR /work
COPY rawsock.c pidfd_open.c io_uring.c syscalls.c fanotify.c kexec_load.c .
COPY probe.sh .
RUN chmod +x probe.sh
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
