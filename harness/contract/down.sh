#!/usr/bin/env bash
# Tear the contract cluster down, volumes and all, and stop the local S3 if
# up.sh started one.
set -uo pipefail
cd "$(dirname "$0")"
docker compose down -v --remove-orphans
pkill -f "moto.server -H 0.0.0.0 -p 5000" 2>/dev/null || true
rm -rf "${REG_CONTRACT_STATE_DIR:-.state}"
