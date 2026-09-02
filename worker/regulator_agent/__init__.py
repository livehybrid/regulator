"""Regulator worker agent.

Regulator drives search load against a Splunk cluster and measures what comes
back. This package is the worker: the thing that actually opens connections to
splunkd, dispatches searches, times them and reports.

It runs in two modes.

Standalone (``REG_STANDALONE=1``): everything comes from environment variables,
there is no control plane, and results are written to stdout as NDJSON and
optionally shipped to a Splunk HEC endpoint. This is the CI path and the
debugging path, and it is deliberately the mode that gets exercised first,
because a load generator you cannot run from a shell is a load generator you
cannot trust.

Managed (Phase 1): a control plane launches a fleet of these, hands each one a
slice of the virtual-user pool over a claim protocol, releases them all at a
shared wall-clock T0 so the aggregate load curve is meaningful, and collects
telemetry over heartbeats.
"""

__version__ = "0.1.0"
