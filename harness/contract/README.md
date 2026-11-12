# Contract tests

Regulator's assumptions about Splunk, checked against a real deployment
(issue 16). See "Contract tests against a real cluster" in the top-level
README for what the suite checks and what it found.

```
make contract-up      # or harness/contract/up.sh
make contract-test    # or harness/contract/test.sh [pytest args]
make contract-down    # or harness/contract/down.sh
```

- `docker-compose.yml`: cluster manager and two indexers, a deployer and a
  three-member search head cluster, and HAProxy round-robin in front of the
  members with no stickiness. Every Splunk container accepts Splunk's licence
  and general terms and runs on the image's trial licence.
- `default.yml`: settings applied at first boot. The free-disk floor and
  SmartStore's eviction padding are lowered so eight instances fit on one
  small disk; at the defaults every search stops, and every SmartStore
  download is refused.
- `smartstore/indexes.conf.tmpl`: the `contract` index on remote storage,
  with small buckets, pushed from the cluster manager by `up.sh`.
- `up.sh` starts a local S3 (moto) on port 5000 unless
  `REG_CONTRACT_S3_ENDPOINT` names one.
- `contract.env`: where the tests look. Point the variables at another
  cluster to test that one; every test is skipped without
  `REG_CONTRACT_SH_URL`.

Sizing: about 16 GB of memory and 25 GB of disk for the eight instances.
