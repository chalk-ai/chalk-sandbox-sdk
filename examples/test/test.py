import logging
from chalkcompute import Container, Image
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


# Create a container from a pre-built image
c = Container(image="ubuntu:latest").run()
log.info("Created container: %s", c.id)

# Run a command
result = c.exec("echo", "hello world")
log.info("stdout: %s", result.stdout_text.strip())
log.info("exit_code: %s", result.exit_code)

"""
Notes:
- Bumped your pyproject.toml to use python 3.11 since chalkcompute requires it.
- Then installed chalkcompute with `uv pip install chalkcompute`.
- `chalk login --api-host https://api.staging.chalk.ai`
- `chalk config` to get values for env vars in next step
- CHALK_API_SERVER="https://api.staging.chalk.ai" CHALK_CLIENT_ID="client-ddd82536d2470e4122e56f51b113ac12" CHALK_CLIENT_SECRET="secret-574d48682de2d142386df2d9912563d7f977930c95cc08d82754626f434a01a8" CHALK_ENVIRONMENT_ID="tmnmwqtcbscbd" python test_container.py
- View containers here: https://staging.chalk.dev/projects/tmnmau5shs3qe/environments/tmnmwqtcbscbd/containers
"""
