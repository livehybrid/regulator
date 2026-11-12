#!/usr/bin/env bash
# Run the contract tests against the cluster up.sh started (or the one the
# REG_CONTRACT_* variables name). Extra arguments go to pytest.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=contract.env
source "$here/contract.env"
cd "$here/../.."
PYTHON="${PYTHON:-.venv/bin/python}"
# With no test named, the whole suite.
if [ $# -eq 0 ] || [[ "${1:-}" == -* ]]; then set -- harness/contract/tests "$@"; fi
exec "$PYTHON" -m pytest -p no:cacheprovider --timeout=900 -rA "$@"
