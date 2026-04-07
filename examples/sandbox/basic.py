"""Basic Container example against a local Chalk instance.

Prerequisites — set these env vars before running:
    export CHALK_API_SERVER=http://localhost:3000
    export CHALK_CLIENT_ID=<your-client-id>
    export CHALK_CLIENT_SECRET=<your-client-secret>
    export CHALK_ENVIRONMENT_ID=<your-environment-id>

Run:
    uv run python examples/sandbox/basic.py
"""

from chalkcompute import Container

c = Container(image="ubuntu:latest", name="basic-example").run()
print(f"Container id: {c.info.id}")

# Basic exec — capture stdout
result = c.exec("echo", "hello world")
print(result.stdout_text)  # "hello world\n"
print(result.exit_code)    # 0

# Multi-step
c.exec("apt-get", "update", "-qq")
c.exec("apt-get", "install", "-y", "-qq", "curl")
result = c.exec("curl", "--version")
print(result.stdout_text.splitlines()[0])

c.stop()
print("Done.")
