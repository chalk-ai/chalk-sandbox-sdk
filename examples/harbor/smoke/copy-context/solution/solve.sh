#!/bin/bash
set -euo pipefail
tr '[:lower:]' '[:upper:]' < /opt/assets/words.txt > /app/upper.txt
