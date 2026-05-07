"""Pull text out of a PDF. PDFs are a common malware vector, so it's nice that
this runs inside an isolated sandbox — bad PDFs only blow up the sandbox."""

import json
import sys

from pypdf import PdfReader


def main(path: str) -> int:
    reader = PdfReader(path)
    pages = [p.extract_text() or "" for p in reader.pages]
    text = "\n".join(pages)
    print(json.dumps({"page_count": len(pages), "text": text}, indent=2))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: parse_pdf.py <path>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
