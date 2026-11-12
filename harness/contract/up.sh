#!/usr/bin/env bash
# Bring the contract cluster up, configure SmartStore, and wait until it is
# ready to test. Idempotent: run it again on a cluster that is already up and
# it only re-checks.
#
#   REG_CONTRACT_S3_ENDPOINT  S3 for SmartStore, as the containers reach it.
#                             Unset: start moto on this host, port 5000.
#   REG_CONTRACT_S3_BUCKET    default regulator-smartstore
#   REG_CONTRACT_PASSWORD     the admin password on every instance
set -euo pipefail
cd "$(dirname "$0")"

PASSWORD="${REG_CONTRACT_PASSWORD:-Regulator-contract-1}"
BUCKET="${REG_CONTRACT_S3_BUCKET:-regulator-smartstore}"
ACCESS_KEY="${REG_CONTRACT_S3_ACCESS_KEY:-contract}"
SECRET_KEY="${REG_CONTRACT_S3_SECRET_KEY:-contract}"
PYTHON="${PYTHON:-python3}"
STATE_DIR="${REG_CONTRACT_STATE_DIR:-.state}"
mkdir -p "$STATE_DIR"

if [ -z "${REG_CONTRACT_S3_ENDPOINT:-}" ]; then
    # A local S3 for SmartStore. moto, because it installs from PyPI and
    # needs no image pull.
    if ! curl -fs -o /dev/null http://127.0.0.1:5000/; then
        echo "starting a local S3 (moto) on :5000"
        nohup "$PYTHON" -m moto.server -H 0.0.0.0 -p 5000 >"$STATE_DIR/s3.log" 2>&1 &
        for _ in $(seq 1 30); do curl -fs -o /dev/null http://127.0.0.1:5000/ && break; sleep 1; done
    fi
    curl -fs -X PUT "http://127.0.0.1:5000/$BUCKET" -o /dev/null || true
    REG_CONTRACT_S3_ENDPOINT="http://host.docker.internal:5000"
fi

docker compose up -d

echo "waiting for every Splunk container to report healthy (first start takes several minutes)"
deadline=$((SECONDS + ${REG_CONTRACT_UP_TIMEOUT_S:-1500}))
for name in cm idx1 idx2 dep sh1 sh2 sh3; do
    while true; do
        state=$(docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' "$(docker compose ps -q "$name")")
        case "$state" in
            "running healthy") echo "  $name healthy"; break ;;
            exited*) echo "  $name exited"; docker compose logs --tail 40 "$name"; exit 1 ;;
        esac
        if [ $SECONDS -gt $deadline ]; then
            echo "  $name not healthy in time ($state)"; docker compose logs --tail 40 "$name"; exit 1
        fi
        sleep 10
    done
done

echo "configuring SmartStore on the cluster manager"
sed -e "s|@BUCKET@|$BUCKET|" -e "s|@ENDPOINT@|$REG_CONTRACT_S3_ENDPOINT|" \
    -e "s|@ACCESS_KEY@|$ACCESS_KEY|" -e "s|@SECRET_KEY@|$SECRET_KEY|" \
    smartstore/indexes.conf.tmpl > "$STATE_DIR/indexes.conf"
cm=$(docker compose ps -q cm)
docker exec -u splunk "$cm" bash -c 'for d in manager-apps master-apps; do [ -d /opt/splunk/etc/$d ] && echo $d && break; done' > "$STATE_DIR/apps_dir"
apps_dir=$(cat "$STATE_DIR/apps_dir")
docker exec -u splunk "$cm" mkdir -p "/opt/splunk/etc/$apps_dir/regulator_contract/local"
docker cp "$STATE_DIR/indexes.conf" "$cm:/opt/splunk/etc/$apps_dir/regulator_contract/local/indexes.conf"
docker exec -u root "$cm" chown -R splunk:splunk "/opt/splunk/etc/$apps_dir/regulator_contract"
docker exec -u splunk "$cm" /opt/splunk/bin/splunk apply cluster-bundle --answer-yes -auth "admin:$PASSWORD" || true

echo "waiting for the bundle to reach both indexers"
for _ in $(seq 1 60); do
    ok=$(docker exec -u splunk "$cm" /opt/splunk/bin/splunk show cluster-bundle-status -auth "admin:$PASSWORD" 2>/dev/null | grep -c "active_bundle" || true)
    if docker exec -u splunk "$(docker compose ps -q idx1)" test -f /opt/splunk/etc/peer-apps/regulator_contract/local/indexes.conf 2>/dev/null \
        || docker exec -u splunk "$(docker compose ps -q idx1)" test -f /opt/splunk/etc/slave-apps/regulator_contract/local/indexes.conf 2>/dev/null; then
        echo "  the indexers have it"; break
    fi
    sleep 10
done

echo "ready:"
echo "  search head cluster, through the balancer  https://127.0.0.1:18989"
echo "  sh1 / sh2 / sh3                             https://127.0.0.1:18389 / 18489 / 18589"
echo "  cluster manager                             https://127.0.0.1:18089"
echo "  idx1 / idx2 management, HEC                 https://127.0.0.1:18189, 18188 / 18289, 18288"
