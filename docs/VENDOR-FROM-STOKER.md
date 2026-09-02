# Vendored from Stoker

**Phase 0 status: nothing has been copied yet. Every row in the table below is
planned, not present.** This file exists ahead of the code so that the first
copy is recorded at the moment it happens rather than reconstructed afterwards.

## Why copy at all

Regulator and [Stoker](https://github.com/livehybrid/stoker) are the same shape
of system pointed at different halves of a Splunk benchmark. Stoker generates
data load over HEC. Regulator generates search load over the splunkd REST API
(and, from Phase 2, a headless browser). Both are a control plane that owns
runs, a fleet of stateless workers that claim work and report back, and a
scheduler that has to start all of those workers at the same instant or the
measurement is meaningless.

Stoker's answer to that second problem is not obvious and was not cheap. It has
been through real multi-node runs on Docker Swarm and Kubernetes, and it has
had the failure modes beaten out of it: the leases that stop two workers
claiming one slice, the shared T0 release that makes N workers start together
despite unsynchronised container start times, the heartbeat channel that
doubles as the command path so a stop is delivered without an inbound
connection to the worker, the execution drivers that abstract "start N things
somewhere" across Swarm, Kubernetes and a fake, the Fernet-at-rest wrapping of
target credentials, the per-run JWT that scopes a worker to exactly one run,
and the Alembic-on-boot migration so a fresh container comes up with a correct
schema and no operator step.

None of that is about events. None of it is about searches either. It is a
domain-free distributed-execution skeleton that happens to live inside Stoker,
and writing a second one from scratch would mean rediscovering the same traps
with a worse test suite behind us. So Regulator copies it.

## Why record it

Copying is the right call now and the wrong call permanently. Two divergent
forks of the same skeleton is how you end up fixing a lease race in one repo
and not the other. The intended end state is a small shared package that both
projects depend on.

That extraction is only cheap if we know, per file, exactly what was taken and
from which Stoker commit. With this table it is a mechanical diff. Without it,
somebody has to read both trees line by line and guess which differences were
deliberate. Hence the rules:

- Add a row **when you copy**, not later.
- Record the **short Stoker commit you copied from**, not "latest". If you
  later re-sync a file, update its commit and say so in Notes.
- If you modified the copy, say `Yes` and say **what** in Notes. "Renamed
  identifiers" and "replaced the HEC target with a search target" are very
  different kinds of change and the extraction cares about the difference.
- Anything you write from scratch does **not** belong in this table. It is an
  inventory of borrowed code, not a file listing.

## Reference commit

Stoker `e367ea1` (`e367ea148e8ad35d07622c7408ffd2ac8d877ca9`, 2026-09-02,
"Stop boot reconciliation destroying a run launched during startup"). This is
the commit the Phase 1 plan was written against and the default source for
every planned row below. Source tree on the build host:
`/opt/aios/apps/stoker`.

## Inventory

| Regulator path | Stoker source | Stoker commit | Modified? | Notes |
| --- | --- | --- | --- | --- |
| `server/drivers/base.py` | `server/drivers/base.py` | `e367ea1` | Planned, not yet copied | The driver interface (start N workers, poll, stop, reap). Domain-free already: it deals in replica counts and environment, not in events. Expected to copy near-verbatim. |
| `server/drivers/fake.py` | `server/drivers/fake.py` | `e367ea1` | Planned, not yet copied | In-memory driver that makes the whole lifecycle testable with no container runtime. This is what keeps CI free of Docker and is the reason the control-plane suite is fast. |
| `server/drivers/swarm.py` | `server/drivers/swarm.py` | `e367ea1` | Planned, not yet copied | Docker Swarm service driver. Carries the hard-won bits: global vs replicated mode, placement constraints and the reap path that does not orphan services when a run is killed mid-start. |
| `server/drivers/k8s.py` | `server/drivers/k8s.py` | `e367ea1` | Planned, not yet copied | Kubernetes Job driver. Same contract as the Swarm one, different object model. |
| `server/crypto.py` | `server/crypto.py` | `e367ea1` | Planned, not yet copied | Fernet-at-rest for target credentials. Regulator stores splunkd search credentials rather than HEC tokens, which changes what is encrypted but not how. |
| `server/auth.py` | `server/auth.py` | `e367ea1` | Planned, not yet copied | Per-run JWT minting and verification. A worker's token is scoped to one run and expires with it, so a leaked token from an old run cannot claim new work. |
| `server/lifecycle.py` (lease + T0 logic only) | `server/lifecycle.py` | `e367ea1` | Planned, not yet copied | **Partial copy.** Take the slice-lease acquisition and renewal, the shared T0 release barrier and the heartbeat command channel. Leave behind everything that knows what a slice contains: Regulator slices are search workloads, not event streams. Expect this to be the row with the most divergence and the one that most wants extracting into a shared package. |
| `server/db.py` and the migrations bootstrap | `server/db.py`, `server/migrations/env.py`, `alembic.ini` | `e367ea1` | Planned, not yet copied | Session handling plus run-Alembic-on-boot so a fresh container self-migrates and there is no separate operator step to forget. Models and migration versions are Regulator's own and are not copied. |
| UI shell | `ui/` (Vite, React, Tailwind scaffolding: `package.json`, config files, layout and API client skeleton) | `e367ea1` | Planned, not yet copied | The shell only: build config, routing, auth wiring, the polling API client. Every Stoker-specific view is rewritten, so do not treat this row as licence to copy `ui/src` wholesale. |

## Licensing

Both projects are Apache-2.0 and share a copyright holder, so copying carries
no attribution obligation beyond keeping the licence headers intact. Copied
files keep whatever header they arrived with. `NOTICE` at the repository root
covers third-party source, which this is not: it is first-party code moving
between two of our own repositories. If a copied file ever brings third-party
code with it, that goes in `NOTICE` as well as here.
