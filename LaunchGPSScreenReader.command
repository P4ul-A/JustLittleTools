#!/bin/zsh

set -e

SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR"

if ! command -v python3.12 >/dev/null 2>&1; then
  echo "Python 3.12 is required. Install it with: brew install python@3.12"
  read -k 1 "?Press any key to close..."
  exit 1
fi

if ! command -v tesseract >/dev/null 2>&1; then
  echo "Tesseract OCR is required. Install it with: brew install tesseract"
  read -k 1 "?Press any key to close..."
  exit 1
fi

if [[ ! -x .venv/bin/python ]]; then
  python3.12 -m venv .venv
fi

.venv/bin/python -m pip install -q -r requirements.txt

set +e
.venv/bin/python gps_screen_reader.py "$@"
STATUS=$?
set -e

echo
read -k 1 "?Finished. Press any key to close..."
exit $STATUS
