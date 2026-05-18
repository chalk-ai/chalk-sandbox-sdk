"""Stop a kept-alive investigator container.

    uv run python -m demo_tools.stop <container_id>
"""

import sys

from dotenv import load_dotenv

load_dotenv()

from chalkcompute import Container  # noqa: E402  (import after load_dotenv)


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: uv run python -m demo_tools.stop <container_id>", file=sys.stderr)
        sys.exit(2)
    container_id = sys.argv[1]
    print(f"stopping container {container_id}...")
    Container.from_id(container_id).stop()
    print("done")


if __name__ == "__main__":
    main()
