"""The investigator image: a Debian slim base with a fixed set of forensics CLIs
and Python helpers baked in. This is the "fraud workbench" the agent gets a
shell into — adding a new check is a matter of adding a script to
``tools_in_image/`` and rebuilding."""

from pathlib import Path

import chalkcompute

_TOOLS_DIR = Path(__file__).parent / "tools_in_image"

_SCRIPTS = [
    "ocr.py",
    "parse_pdf.py",
    "check_exif.py",
    "compare_fields.py",
]


def investigator_image() -> chalkcompute.Image:
    img = (
        chalkcompute.Image.debian_slim("3.12")
        .run_commands(
            "apt-get update",
            "apt-get install -y --no-install-recommends "
            "tesseract-ocr libimage-exiftool-perl poppler-utils jq",
            "rm -rf /var/lib/apt/lists/*",
        )
        .pip_install(["pypdf>=4.0", "pillow>=10.0", "exifread>=3.0"])
    )
    for name in _SCRIPTS:
        img = img.add_local_file(str(_TOOLS_DIR / name), f"/opt/tools/{name}")
    return img
