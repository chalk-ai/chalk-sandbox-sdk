"""Surface EXIF metadata + a few cheap consistency flags. Selfies usually have
camera EXIF; ID photos that have been re-saved through editors usually don't."""

import json
import sys

import exifread


def main(path: str) -> int:
    with open(path, "rb") as f:
        tags = exifread.process_file(f, details=False)

    summary = {k: str(v) for k, v in tags.items()}
    flags = []
    if "Image Make" not in tags and "Image Model" not in tags:
        flags.append("no_camera_metadata")
    if "EXIF DateTimeOriginal" not in tags:
        flags.append("no_capture_date")

    print(json.dumps({"flags": flags, "tags": summary}, indent=2))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: check_exif.py <path>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
