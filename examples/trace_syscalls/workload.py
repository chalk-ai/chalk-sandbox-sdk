"""Generate file I/O and a refused loopback connection inside the sandbox."""

import errno
import os
import socket
from pathlib import Path
from tempfile import TemporaryDirectory

with TemporaryDirectory() as directory:
    source = Path(directory) / "input"
    source.write_bytes(b"x" * 96)
    fd = os.open(source, os.O_RDONLY)
    data = os.read(fd, 128)
    os.close(fd)
    fd = os.open(Path(directory) / "output", os.O_WRONLY | os.O_CREAT, 0o600)
    os.write(fd, data[:32])
    os.close(fd)

# Reserve a loopback port without listening so the TCP connection is refused.
with socket.socket() as reserved, socket.socket() as client:
    reserved.bind(("127.0.0.1", 0))
    assert client.connect_ex(reserved.getsockname()) == errno.ECONNREFUSED
