#!/bin/bash
set -e
echo "Running pre-commit checks..."
cd zenith
ruff check tests/test_dispatcher.py
ruff format tests/test_dispatcher.py
MYPYPATH=src mypy tests/test_dispatcher.py
python -m pytest -v tests/test_dispatcher.py
