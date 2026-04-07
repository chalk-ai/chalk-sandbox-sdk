import logging
from chalkcompute import Container, Image

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


# Create a sandbox from a pre-built image
c = Container(image="ubuntu:latest").run()
log.info("Created container: %s", c.id)

# Run a command
result = c.exec("echo", "hello world")
log.info("stdout: %s", result.stdout_text.strip())
log.info("exit_code: %s", result.exit_code)
