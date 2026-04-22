import argparse
import logging
import os

import chalkcompute


logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Deploy orchestrate function")
    parser.add_argument(
        "--function-name",
        default="orchestrate",
        help="Name for the deployed function",
    )
    parser.add_argument(
        "--embed-function-name",
        default="embed",
        help="Name of the embed function to call",
    )
    args = parser.parse_args()

    @chalkcompute.function(
        name=args.function_name,
        image=chalkcompute.Image.debian_slim().pip_install(["chalkcompute==1.1.1"]),
        env={
            "CHALK_CLIENT_ID": os.getenv("CHALK_CLIENT_ID"),
            "CHALK_CLIENT_SECRET": os.getenv("CHALK_CLIENT_SECRET"),
            "CHALK_ENVIRONMENT_ID": os.getenv("CHALK_ENVIRONMENT_ID"),
            "CHALK_API_SERVER": os.getenv("CHALK_API_SERVER"),
        },
    )
    def orchestrate(texts: list[str]) -> list[list[float]]:
        import chalkcompute

        embed = chalkcompute.RemoteFunction.from_name(name=args.embed_function_name)
        embeddings = [embed(text) for text in texts]
        return embeddings


if __name__ == "__main__":
    main()
