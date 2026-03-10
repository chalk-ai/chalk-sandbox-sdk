# Chalk Sandbox SDK

Python SDK for the Chalk Sandbox gRPC service. Create sandboxes, execute commands, and stream output over bidirectional gRPC streams.

## Install

```
pip install grpcio protobuf
```

## Quick start

```python
from sandbox import SandboxClient

client = SandboxClient("localhost:50051")

# Create a sandbox
sandbox = client.create(image="ubuntu:latest", name="my-sandbox")

# Run a command
result = sandbox.exec("echo", "hello world")
print(result.stdout_text)  # "hello world"
print(result.exit_code)    # 0

# Clean up
sandbox.terminate()
```

## Connecting

```python
from sandbox import SandboxClient
import grpc

# Insecure (local dev)
client = SandboxClient("localhost:50051")

# With TLS
creds = grpc.ssl_channel_credentials()
client = SandboxClient("sandbox.example.com:443", credentials=creds)

# As a context manager
with SandboxClient("localhost:50051") as client:
    ...
```

## Sandbox lifecycle

```python
# Create with resource limits
sandbox = client.create(
    image="ubuntu:latest",
    name="dev-sandbox",
    cpu="2",
    memory="4Gi",
    env={"DEBIAN_FRONTEND": "noninteractive"},
)

# List all sandboxes
for info in client.list():
    print(f"{info.id} {info.state} {info.name}")

# Get a handle to an existing sandbox by ID
sandbox = client.get(id="550e8400-e29b-41d4-a716-446655440000")

# Fetch info from server
print(sandbox.info.state)
sandbox.refresh()  # force re-fetch

# Terminate
sandbox.terminate()
sandbox.terminate(grace_period_seconds=30)
```

## Executing commands

### Run and wait

```python
result = sandbox.exec("ls", "-la", "/tmp")
for line in result.stdout:
    print(line)
for line in result.stderr:
    print(f"ERR: {line}")
print(f"exit code: {result.exit_code}")

# Or get the full text at once
print(result.stdout_text)
print(result.stderr_text)
```

### Stream output in real time

```python
for event in sandbox.exec_stream("make", "build", workdir="/app"):
    if event.stdout:
        print(event.stdout, end="")
    if event.stderr:
        print(event.stderr, end="", file=sys.stderr)
    if event.is_exited:
        print(f"\nDone: exit code {event.exit_code}")
```

### Interactive processes (stdin + signals)

```python
process = sandbox.exec_start("bash")

process.write_stdin("echo hello\n")
process.write_stdin("exit\n")
process.close_stdin()

for event in process.output():
    if event.stdout:
        print(event.stdout, end="")
```

Send signals to running processes:

```python
import signal

process = sandbox.exec_start("sleep", "300")
process.send_signal(signal.SIGTERM)
result = process.wait()
```

### Options

All exec methods accept the same keyword arguments:

```python
result = sandbox.exec(
    "python", "train.py",
    workdir="/app",                     # working directory
    timeout_secs=3600,                  # kill after 1 hour
    env={"CUDA_VISIBLE_DEVICES": "0"},  # environment variables
)
```

## Examples

### Clone a GitHub repo into a sandbox

```python
from sandbox import SandboxClient

client = SandboxClient("localhost:50051")
sandbox = client.create(image="ubuntu:latest", name="repo-sandbox")

# Install git
sandbox.exec("apt-get", "update")
sandbox.exec("apt-get", "install", "-y", "git")

# Clone
result = sandbox.exec(
    "git", "clone", "https://github.com/chalk-ai/chalk.git", "/workspace/chalk"
)
if result.exit_code != 0:
    print(f"Clone failed: {result.stderr_text}")
else:
    # List what we got
    result = sandbox.exec("ls", "-la", "/workspace/chalk")
    for line in result.stdout:
        print(line)
```

### Spawn an OpenCode agent in a sandbox

[OpenCode](https://github.com/opencode-ai/opencode) is a terminal-based AI coding agent. You can run it inside a sandbox to give it an isolated environment to work in.

```python
from sandbox import SandboxClient

client = SandboxClient("localhost:50051")
sandbox = client.create(
    image="ubuntu:latest",
    name="opencode-agent",
    cpu="2",
    memory="4Gi",
    env={
        "ANTHROPIC_API_KEY": "sk-ant-...",
    },
)

# Install dependencies
sandbox.exec("apt-get", "update")
sandbox.exec("apt-get", "install", "-y", "git", "curl", "build-essential")

# Install Go (opencode is a Go binary)
sandbox.exec("bash", "-c", "curl -fsSL https://go.dev/dl/go1.24.1.linux-amd64.tar.gz | tar -C /usr/local -xz")
sandbox.exec("bash", "-c", "echo 'export PATH=$PATH:/usr/local/go/bin:/root/go/bin' >> /root/.bashrc")

# Install opencode
sandbox.exec("bash", "-c", "export PATH=$PATH:/usr/local/go/bin:/root/go/bin && go install github.com/opencode-ai/opencode@latest")

# Clone a repo to work on
sandbox.exec("git", "clone", "https://github.com/your-org/your-repo.git", "/workspace/repo")

# Run opencode non-interactively with a prompt
result = sandbox.exec(
    "bash", "-c",
    "export PATH=$PATH:/usr/local/go/bin:/root/go/bin && cd /workspace/repo && opencode -p 'fix the failing tests in pkg/auth'",
    timeout_secs=600,
)
print(result.stdout_text)

# Or run it interactively and feed it commands
process = sandbox.exec_start(
    "bash", "-c",
    "export PATH=$PATH:/usr/local/go/bin:/root/go/bin && cd /workspace/repo && opencode",
)

# Stream its output
for event in process.output():
    if event.stdout:
        print(event.stdout, end="")
    if event.stderr:
        print(event.stderr, end="", file=sys.stderr)
    if event.is_exited:
        break
```

### Long-running build with real-time output

```python
sandbox = client.create(image="node:22", name="build")

sandbox.exec("git", "clone", "https://github.com/your-org/frontend.git", "/app")
sandbox.exec("npm", "install", workdir="/app")

# Stream the build output as it happens
for event in sandbox.exec_stream("npm", "run", "build", workdir="/app"):
    if event.stdout:
        print(event.stdout, end="")
    if event.stderr:
        print(event.stderr, end="", file=sys.stderr)
    if event.is_exited and event.exit_code != 0:
        print(f"Build failed with exit code {event.exit_code}")

sandbox.terminate()
```

## CLI tools

### `sandbox_exec.py` - Run a command

```bash
python sandbox_exec.py --target localhost:50051 --sandbox-id <id> --exec "ls -la"
```

### `sandbox_stdout.py` - Interactive shell

```bash
echo "echo hello" | python sandbox_stdout.py --target localhost:50051 --sandbox-id <id> --exec "bash"
```

## Regenerating proto stubs

If the proto definition changes, regenerate the Python stubs:

```bash
pip install grpcio-tools
./generate.sh
```
