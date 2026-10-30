#!/usr/bin/env -S uv run

"""
Test whether a selection of privileged syscalls can be invoked
"""

from pathlib import Path
from uuid import uuid4

from chalkcompute import Container, Image
from dotenv import load_dotenv

load_dotenv()

IMAGE = "us-central1-docker.pkg.dev/chalk-develop/base-images/scauligi-syscalls:latest"
container = Container(image=IMAGE, name=f"syscalls-{uuid4()}")
container.run()

result = container.exec("./linux_amd64_syscall_probe")
print(result.stdout_text)

container.stop()
