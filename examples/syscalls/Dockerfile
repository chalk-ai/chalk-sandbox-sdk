FROM alpine:3.23

RUN apk update && apk add --no-cache \
  bash \
  linux-headers \
  build-base

WORKDIR /work
COPY linux_amd64_syscall_probe.c .
RUN gcc -O2 -Wall -static linux_amd64_syscall_probe.c -o linux_amd64_syscall_probe
RUN adduser -H -D user
USER user
USER 1000:1000

CMD ["./linux_amd64_syscall_probe"]
