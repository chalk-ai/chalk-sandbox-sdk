"""Image definition for the ChalkDF projection example container."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from image import Image

image = (
    Image.debian_slim().pip_install(["chalkdf", "pyarrow", "chalkpy"]).workdir("/app")
)
