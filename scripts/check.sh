#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}/.."

if [[ ! -f ".venv/bin/activate" ]]; then
  echo "Error: .venv not found. Create it first: python -m venv .venv"
  exit 1
fi

source ".venv/bin/activate"
if [[ -z "${SKIP_INSTALL_DEPENDENCIES:-}" ]]; then
  python -m pip install --upgrade pip
  python -m pip install -r requirements.txt -r requirements-dev.txt
else
  echo "SKIP_INSTALL_DEPENDENCIES set; assuming required packages are already installed."
fi

FILES=(app.py auth.py database.py)

printf "\n==> Running compilation\n"
python -m compileall "${FILES[@]}"

printf "\n==> Running black\n"
black --check "${FILES[@]}"

printf "\n==> Running ruff\n"
ruff check "${FILES[@]}"

printf "\n==> Running mypy\n"
mypy "${FILES[@]}"
