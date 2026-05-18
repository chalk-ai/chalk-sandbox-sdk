"""The investigator image: a Debian slim base with forensics CLIs and Python
libraries baked in. This is the "fraud workbench" the agent gets a shell into —
adding a new check is a matter of installing a tool in the image, not writing
a new function-tool in the agent harness."""

import chalkcompute


def investigator_image() -> chalkcompute.Image:
    return (
        chalkcompute.Image.debian_slim("3.12")
        .run_commands(
            "apt-get update",
            "apt-get install -y --no-install-recommends "
            "tesseract-ocr libimage-exiftool-perl poppler-utils jq",
            "rm -rf /var/lib/apt/lists/*",
        )
        .pip_install(["pypdf>=4.0", "pillow>=10.0", "exifread>=3.0"])
    )
