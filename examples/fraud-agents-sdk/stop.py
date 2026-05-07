"""Stop and clean up a kept-alive investigator container.

    uv run python stop.py <container_id>

Used after a `--keep` run when you're done inspecting the container in the
dashboard. Loads .env first so the chalkcompute client can authenticate.
"""

import sys

from dotenv import load_dotenv

load_dotenv()

from chalkcompute import Container  # noqa: E402  (import after load_dotenv)


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: uv run python stop.py <container_id>", file=sys.stderr)
        sys.exit(2)
    container_id = sys.argv[1]
    print(f"stopping container {container_id}...")
    Container.from_id(container_id).stop()
    print("done")


if __name__ == "__main__":
    main()
