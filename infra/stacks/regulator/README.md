# Regulator on Docker Swarm, through Portainer

The homelab path. One service, one named volume, deployed and updated with the
Portainer API exactly as Stoker is.

```bash
cd infra/stacks/regulator
cp .env.example .env        # fill in PORTAINER_HOST, PORTAINER_TOKEN, REG_ADMIN_PASSWORD
python deploy.py --dry-run  # see what would happen
python deploy.py            # create or update the stack
python deploy.py --status
```

The first deploy generates a Fernet master key, appends it to `.env` and creates
the `regulator_master_key` swarm secret from it. Keep `.env` safe: every stored
target credential is encrypted under that key.

What the stack does that the plain image does not:

- keeps the SQLite database, the imported scenarios and the run history on the
  `regulator_data` volume, pinned to `REGULATOR_NODE`;
- publishes the web interface on `REGULATOR_PORT` (8092 by default) and behind
  Traefik on `REGULATOR_HOST`;
- ships every run's telemetry to `REG_HEC_URL` when set, with the TLS flag that
  a self-signed collector needs;
- registers `REG_SEED_TARGET_*` at boot, so a rebuilt stack is runnable at once.

Browser scenarios do not run from this stack: the control-plane image carries no
Chromium. Run them from the browser worker image until the fleet lands:

```bash
docker run --rm -e REG_STANDALONE=1 -e REG_SCENARIO=dashboard-triage \
  -e REG_TARGET_URL=https://splunk:8089 -e REG_TARGET_WEB_URL=https://splunk:8000 \
  -e REG_TARGET_USERNAME=loadtest -e REG_TARGET_PASSWORD=... -e REG_TARGET_VERIFY_TLS=0 \
  ghcr.io/livehybrid/regulator-worker:browser
```
