#!/usr/bin/env python3
"""Generate one scenario per Stoker pack, in Splunk's own search format.

Each scenario is a directory holding a ``savedsearches.conf`` written the way
a Splunk admin would write it (cron schedules, dispatch time ranges, nothing
Regulator-specific in the file) and a ``scenario.yaml`` that points at it and
classifies each search. The searches are written against the fields each
Stoker pack actually emits, checked against the pack samples, so a Stoker fill
followed by the matching Regulator scenario is a working benchmark on day one.

Run from the repository root::

    python tools/build_pack_scenarios.py

It rewrites ``scenarios/pack-*`` and ``scenarios/stoker-scheduler`` and is
idempotent. Hand edits go in this file, not in the generated directories.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "scenarios"
sys.path.insert(0, str(ROOT / "worker"))

from regulator_agent import savedsearches as ss  # noqa: E402
from regulator_agent.scenario import is_advice, lint, load_scenario  # noqa: E402

METRIC_INDEX = "stoker_metrics"


@dataclass
class Search:
    name: str
    search: str
    cron: str
    earliest: str
    klass: str
    latest: str = "now"
    description: str = ""
    result_count: Optional[int] = None


@dataclass
class Pack:
    slug: str
    stoker_pack: str
    sourcetype: str
    seed: int
    description: str
    searches: List[Search]
    tags: List[str] = field(default_factory=list)
    metric: bool = False
    index: str = "main"
    virtual_users: int = 10


# Common field extractions for the packs Splunk has no built-in extractions
# for. Written once here so every search in the pack agrees.
S3_REX = (
    'rex "^\\S+ (?<bucket>\\S+) \\[[^\\]]+\\] (?<remote_ip>\\S+) (?<requester>\\S+) '
    '(?<request_id>\\S+) (?<operation>\\S+) (?<key>\\S+) \\"(?<request>[^\\"]*)\\" '
    '(?<http_status>\\d+) (?<error_code>\\S+) (?<bytes_sent>\\S+) (?<object_size>\\S+) '
    '(?<total_time>\\S+)"'
)
ALB_REX = (
    'rex "^(?<type>\\S+) \\S+ (?<elb>\\S+) (?<client_ip>[\\d.]+):\\d+ (?<target>\\S+) '
    '(?<request_processing_time>\\S+) (?<target_processing_time>\\S+) '
    '(?<response_processing_time>\\S+) (?<elb_status_code>\\d+) (?<target_status_code>\\S+) '
    '(?<received_bytes>\\d+) (?<sent_bytes>\\d+) \\"(?<verb>\\S+) (?<url>\\S+) '
    '(?<proto>[^\\"]+)\\" \\"(?<user_agent>[^\\"]*)\\""'
)
SECURE_REX = 'rex "(?:for (?:invalid user )?(?<user>\\S+) )?from (?<src_ip>\\d+\\.\\d+\\.\\d+\\.\\d+)"'
SYSMON_REX = (
    'rex "<EventID>(?<EventID>\\d+)</EventID>" | rex "<Computer>(?<Computer>[^<]+)</Computer>"'
)


def event_packs() -> List[Pack]:
    st = "sourcetype"
    return [
        Pack(
            slug="web-access",
            stoker_pack="web-access",
            sourcetype="access_combined",
            seed=1101,
            tags=["web", "access_combined"],
            description="A website's access log: status mix, top paths, errors, bots and scanner probes, the way an operations team watches it",
            searches=[
                Search("Web status codes every 5 minutes", f"index=main {st}=access_combined | timechart span=5m count by status", "*/5 * * * *", "-1h", "dense"),
                Search("Top paths by requests", f"index=main {st}=access_combined | stats count, sum(bytes) as bytes by uri_path | sort - count | head 50", "0 * * * *", "-60m@m", "dense"),
                Search("5xx responses by path", f"index=main {st}=access_combined status>=500 | stats count by uri_path, status | sort - count", "*/15 * * * *", "-15m", "sparse"),
                Search("Bot share of traffic", f"index=main {st}=access_combined | eval kind=if(match(useragent, \"(?i)bot|crawler|spider\"), \"bot\", \"human\") | stats count by kind", "0 */6 * * *", "-24h@h", "dense"),
                Search("Scanner probes on paths that do not exist", f"index=main {st}=access_combined (uri_path=\"*.php\" OR uri_path=\"*wp-admin*\" OR uri_path=\"*.env\") | stats count by clientip, uri_path | sort - count | head 50", "0 * * * *", "-4h", "rare"),
                Search("Daily traffic report", f"index=main {st}=access_combined | timechart span=1h count, avg(bytes) as avg_bytes, dc(clientip) as clients", "0 6 * * *", "-24h@h", "dense", latest="@h"),
                Search("Top talkers", f"index=main {st}=access_combined | top limit=20 clientip", "*/30 * * * *", "-30m", "dense"),
            ],
        ),
        Pack(
            slug="apigw",
            stoker_pack="apigw",
            sourcetype="stoker:apigw",
            seed=1102,
            tags=["api", "gateway"],
            description="An API gateway's access log: latency percentiles, upstream errors, cache effectiveness and key abuse, the SRE's five-minute loop",
            searches=[
                Search("API latency percentiles by path", f"index=main {st}=stoker:apigw | stats perc50(dur_ms) as p50, perc95(dur_ms) as p95, count by path | sort - p95 | head 20", "*/5 * * * *", "-15m", "dense"),
                Search("API errors by upstream", f"index=main {st}=stoker:apigw status>=500 | stats count by upstream, status | sort - count", "*/5 * * * *", "-15m", "sparse"),
                Search("Gateway throughput by edge", f"index=main {st}=stoker:apigw | timechart span=1m count by gw", "*/10 * * * *", "-1h", "dense"),
                Search("API keys over their fair share", f"index=main {st}=stoker:apigw apikey_id=* | stats count by apikey_id | where count > 500 | sort - count", "0 * * * *", "-1h", "rare"),
                Search("Cache effectiveness", f"index=main {st}=stoker:apigw | stats count by cache | eventstats sum(count) as total | eval pct=round(100*count/total, 1)", "0 * * * *", "-60m@m", "dense"),
                Search("Requests slower than two seconds", f"index=main {st}=stoker:apigw dur_ms>2000 | table _time, path, dur_ms, upstream, request_id | sort - dur_ms | head 100", "*/15 * * * *", "-15m", "sparse"),
                Search("Daily requests per client and region", f"index=main {st}=stoker:apigw | stats count, avg(dur_ms) as avg_ms by client_id, region | sort - count", "0 7 * * *", "-24h@h", "dense"),
            ],
        ),
        Pack(
            slug="aws-cloudtrail",
            stoker_pack="aws-cloudtrail",
            sourcetype="aws:cloudtrail",
            seed=1103,
            tags=["aws", "cloudtrail", "security"],
            description="CloudTrail as a security team schedules it: console logins, S3 data events, policy changes, role assumption and one correlation across event types",
            searches=[
                Search("Console logins by identity and source", f"index=main {st}=aws:cloudtrail eventName=ConsoleLogin | stats count by userIdentity.arn, sourceIPAddress | sort - count", "*/15 * * * *", "-1h", "sparse"),
                Search("S3 data events by bucket", f"index=main {st}=aws:cloudtrail eventSource=s3.amazonaws.com | stats count by eventName, requestParameters.bucketName | sort - count | head 50", "*/10 * * * *", "-30m", "dense"),
                Search("Bucket policy and bucket creation", f"index=main {st}=aws:cloudtrail (eventName=PutBucketPolicy OR eventName=CreateBucket OR eventName=GetBucketAcl) | table _time, eventName, userIdentity.arn, sourceIPAddress, awsRegion", "*/5 * * * *", "-7d@d", "rare"),
                Search("Security group ingress changes", f"index=main {st}=aws:cloudtrail eventName=AuthorizeSecurityGroupIngress | table _time, userIdentity.arn, sourceIPAddress, awsRegion, requestParameters.groupId", "0 * * * *", "-24h", "rare"),
                Search("Role assumption by role", f"index=main {st}=aws:cloudtrail eventName=AssumeRole | stats count by requestParameters.roleArn, sourceIPAddress | sort - count", "0 * * * *", "-60m@m", "dense"),
                Search("Activity by region", f"index=main {st}=aws:cloudtrail | timechart span=1h count by awsRegion", "0 6 * * *", "-24h@h", "dense"),
                Search("KMS usage by identity", f"index=main {st}=aws:cloudtrail eventSource=kms.amazonaws.com | stats count by eventName, userIdentity.arn | sort - count", "*/30 * * * *", "-30m", "dense"),
                Search("Sources active across many event types", f"index=main {st}=aws:cloudtrail [ search index=main {st}=aws:cloudtrail eventName=ConsoleLogin | stats count by sourceIPAddress | fields sourceIPAddress | head 100 ] | stats dc(eventName) as kinds, count by sourceIPAddress | where kinds > 3 | sort - kinds", "0 * * * *", "-24h", "subsearch"),
            ],
        ),
        Pack(
            slug="aws-s3-access",
            stoker_pack="aws-s3-access",
            sourcetype="aws:s3:accesslogs",
            seed=1104,
            tags=["aws", "s3", "storage"],
            description="S3 server access logs without the AWS add-on installed: every search extracts its own fields, which is how most of these get written in the field",
            searches=[
                Search("S3 operations by bucket", f"index=main {st}=aws:s3:accesslogs | {S3_REX} | stats count by bucket, operation | sort - count", "*/10 * * * *", "-1h", "dense"),
                Search("S3 client and server errors", f"index=main {st}=aws:s3:accesslogs | {S3_REX} | search http_status>=400 | stats count by bucket, http_status, error_code | sort - count", "*/15 * * * *", "-15m", "sparse"),
                Search("S3 deletes", f"index=main {st}=aws:s3:accesslogs (\"REST.DELETE.OBJECT\" OR \"MULTI_OBJECT_DELETE\") | {S3_REX} | table _time, bucket, operation, key, requester, remote_ip", "0 * * * *", "-24h", "rare"),
                Search("Bytes served per bucket", f"index=main {st}=aws:s3:accesslogs | {S3_REX} | eval bytes_sent=if(bytes_sent=\"-\", 0, bytes_sent) | stats sum(bytes_sent) as bytes, count by bucket | sort - bytes", "0 6 * * *", "-24h@h", "dense"),
                Search("Top keys by requests", f"index=main {st}=aws:s3:accesslogs | {S3_REX} | stats count by key | sort - count | head 50", "0 * * * *", "-60m@m", "dense"),
                Search("Slow S3 requests", f"index=main {st}=aws:s3:accesslogs | {S3_REX} | where total_time > 1000 | table _time, bucket, operation, key, total_time | sort - total_time | head 100", "*/15 * * * *", "-15m", "sparse"),
            ],
        ),
        Pack(
            slug="aws-elb-alb",
            stoker_pack="aws-elb-alb",
            sourcetype="aws:elb:accesslogs",
            seed=1105,
            tags=["aws", "alb", "loadbalancer"],
            description="Application Load Balancer access logs: status mix per minute, 5xx by target, target latency percentiles, traffic by host and client top talkers",
            searches=[
                Search("ALB status codes per minute", f"index=main {st}=aws:elb:accesslogs | {ALB_REX} | timechart span=1m count by elb_status_code", "*/5 * * * *", "-15m", "dense"),
                Search("ALB 5xx by target", f"index=main {st}=aws:elb:accesslogs | {ALB_REX} | search elb_status_code>=500 | stats count by target, target_status_code | sort - count", "*/5 * * * *", "-15m", "sparse"),
                Search("Target latency percentiles by URL", f"index=main {st}=aws:elb:accesslogs | {ALB_REX} | rex field=url \"^https?://(?<host>[^:/]+)\" | stats perc50(target_processing_time) as p50, perc95(target_processing_time) as p95, count by host | sort - p95", "*/10 * * * *", "-30m", "dense"),
                Search("Traffic by host", f"index=main {st}=aws:elb:accesslogs | {ALB_REX} | rex field=url \"^https?://(?<host>[^:/]+)\" | timechart span=5m count by host", "0 * * * *", "-60m@m", "dense"),
                Search("Client top talkers", f"index=main {st}=aws:elb:accesslogs | {ALB_REX} | stats count, sum(sent_bytes) as bytes by client_ip | sort - count | head 50", "*/30 * * * *", "-30m", "dense"),
                Search("Requests that never reached a target", f"index=main {st}=aws:elb:accesslogs | {ALB_REX} | search target=\"-\" OR target_processing_time=\"-1\" | table _time, elb_status_code, verb, url, client_ip", "0 * * * *", "-24h", "rare"),
                Search("Daily bytes in and out", f"index=main {st}=aws:elb:accesslogs | {ALB_REX} | timechart span=1h sum(received_bytes) as in_bytes, sum(sent_bytes) as out_bytes", "0 6 * * *", "-24h@h", "dense"),
            ],
        ),
        Pack(
            slug="splunk-tutorial-web",
            stoker_pack="splunk-tutorial-web",
            sourcetype="access_combined_wcookie",
            seed=1106,
            tags=["splunk", "tutorial", "buttercup"],
            description="The Search Tutorial's Buttercup Games shop: purchases by product, categories, sessions with transaction, cart abandonment and errors",
            searches=[
                Search("Purchases by product", f"index=main {st}=access_combined_wcookie action=purchase status=200 | stats count by productId | sort - count | head 20", "*/10 * * * *", "-1h", "sparse"),
                Search("Views by category", f"index=main {st}=access_combined_wcookie categoryId=* | stats count by categoryId | sort - count", "*/5 * * * *", "-15m", "dense"),
                Search("Sessions by page count", f"index=main {st}=access_combined_wcookie | transaction JSESSIONID maxspan=30m | stats avg(duration) as avg_session_s, avg(eventcount) as avg_pages, count as sessions", "0 * * * *", "-60m@m", "heavy"),
                Search("Cart activity", f"index=main {st}=access_combined_wcookie uri_path=\"/cart.do\" | stats count by action | sort - count", "*/15 * * * *", "-15m", "sparse"),
                Search("Errors by page", f"index=main {st}=access_combined_wcookie status>=400 | stats count by uri_path, status | sort - count", "*/15 * * * *", "-15m", "sparse"),
                Search("Products added to cart but not bought", f"index=main {st}=access_combined_wcookie action=addtocart NOT [ search index=main {st}=access_combined_wcookie action=purchase | stats count by JSESSIONID | fields JSESSIONID | head 1000 ] | stats count by productId | sort - count | head 20", "0 * * * *", "-24h", "subsearch"),
                Search("Daily revenue proxy by category", f"index=main {st}=access_combined_wcookie action=purchase | timechart span=1h count by categoryId", "0 6 * * *", "-24h@h", "dense"),
            ],
        ),
        Pack(
            slug="splunk-tutorial-secure",
            stoker_pack="splunk-tutorial-secure",
            sourcetype="linux_secure",
            seed=1107,
            tags=["splunk", "tutorial", "linux", "auth"],
            description="sshd authentication as a SOC schedules it: failed logins by source, brute force, invalid users, the rare accepted login and a correlation of success after failure",
            searches=[
                Search("Failed logins by source", f"index=main {st}=linux_secure \"Failed password\" | {SECURE_REX} | stats count by src_ip | sort - count | head 50", "*/5 * * * *", "-15m", "sparse"),
                Search("Brute force sources", f"index=main {st}=linux_secure \"Failed password\" | {SECURE_REX} | stats count, dc(user) as users by src_ip | where count > 20 | sort - count", "*/5 * * * *", "-15m", "sparse"),
                Search("Invalid user attempts", f"index=main {st}=linux_secure \"Invalid user\" | {SECURE_REX} | stats count by user | sort - count | head 50", "*/15 * * * *", "-1h", "dense"),
                Search("Accepted logins", f"index=main {st}=linux_secure \"Accepted password\" | {SECURE_REX} | table _time, host, user, src_ip", "*/10 * * * *", "-24h", "rare"),
                Search("Failures per hour by user", f"index=main {st}=linux_secure \"Failed password\" | {SECURE_REX} | timechart span=1h count by user limit=10", "0 * * * *", "-24h@h", "dense"),
                Search("Success after repeated failure", f"index=main {st}=linux_secure \"Accepted password\" | {SECURE_REX} | search [ search index=main {st}=linux_secure \"Failed password\" | {SECURE_REX} | stats count by src_ip | where count > 10 | fields src_ip | head 500 ] | table _time, user, src_ip", "0 * * * *", "-24h", "subsearch"),
            ],
        ),
        Pack(
            slug="splunk-tutorial-vendor-sales",
            stoker_pack="splunk-tutorial-vendor-sales",
            sourcetype="vendor_sales",
            seed=1108,
            tags=["splunk", "tutorial", "sales"],
            description="The Search Tutorial's vendor sales: sales by code and vendor, distinct accounts, a rare high-vendor check and a vendors-above-average correlation",
            searches=[
                Search("Sales by product code", f"index=main {st}=vendor_sales | stats count by Code | sort - count", "*/10 * * * *", "-1h", "dense"),
                Search("Top vendors", f"index=main {st}=vendor_sales | stats count by VendorID | sort - count | head 20", "0 * * * *", "-60m@m", "dense"),
                Search("Distinct accounts buying", f"index=main {st}=vendor_sales | stats dc(AcctID) as accounts, count as sales", "*/15 * * * *", "-15m", "dense"),
                Search("Sales from the highest vendor ids", f"index=main {st}=vendor_sales VendorID>=9990 | table _time, VendorID, Code, AcctID", "0 * * * *", "-24h", "rare"),
                Search("Vendors above the average", f"index=main {st}=vendor_sales | stats count by VendorID | eventstats avg(count) as mean | where count > mean | sort - count | head 50", "0 6 * * *", "-24h@h", "dense"),
                Search("Sales per hour", f"index=main {st}=vendor_sales | timechart span=1h count by Code", "0 7 * * *", "-24h@h", "dense"),
            ],
        ),
        Pack(
            slug="flatline",
            stoker_pack="flatline",
            sourcetype="stoker:flatline",
            seed=1109,
            tags=["baseline", "web"],
            description="The constant-rate baseline pack: request counts by service, warnings and errors, latency percentiles and slow calls, so a search-side baseline matches the data-side one",
            searches=[
                Search("Requests by service per minute", f"index=main {st}=stoker:flatline | timechart span=1m count by svc", "*/5 * * * *", "-15m", "dense"),
                Search("Warnings and errors by service", f"index=main {st}=stoker:flatline (level=WARN OR level=ERROR) | stats count by svc, level, msg | sort - count", "*/5 * * * *", "-15m", "sparse"),
                Search("Latency percentiles by service", f"index=main {st}=stoker:flatline | stats perc50(dur_ms) as p50, perc95(dur_ms) as p95 by svc", "*/10 * * * *", "-30m", "dense"),
                Search("Slow calls", f"index=main {st}=stoker:flatline dur_ms>300 | table _time, host, svc, msg, dur_ms | sort - dur_ms | head 100", "*/15 * * * *", "-15m", "sparse"),
                Search("Daily volume by host", f"index=main {st}=stoker:flatline | timechart span=1h count by host", "0 6 * * *", "-24h@h", "dense"),
            ],
        ),
        Pack(
            slug="attack-replay",
            stoker_pack="attack-replay",
            sourcetype="XmlWinEventLog",
            seed=1110,
            tags=["security", "windows", "sysmon"],
            description="Sysmon and Windows Security events from the attack_data replay, searched without the Windows add-on: event ids and computers are extracted inline",
            searches=[
                Search("Events by id", f"index=main {st}=XmlWinEventLog | {SYSMON_REX} | stats count by EventID | sort - count", "*/5 * * * *", "-15m", "dense"),
                Search("Process creations by computer", f"index=main {st}=XmlWinEventLog \"<EventID>1</EventID>\" | {SYSMON_REX} | stats count by Computer | sort - count", "*/10 * * * *", "-1h", "sparse"),
                Search("Network connections by computer", f"index=main {st}=XmlWinEventLog \"<EventID>3</EventID>\" | {SYSMON_REX} | stats count by Computer | sort - count", "*/10 * * * *", "-1h", "sparse"),
                Search("LSASS access", f"index=main {st}=XmlWinEventLog \"<EventID>10</EventID>\" lsass.exe | {SYSMON_REX} | table _time, Computer, _raw", "*/5 * * * *", "-24h", "rare"),
                Search("Logon failures", f"index=main {st}=XmlWinEventLog \"<EventID>4625</EventID>\" | {SYSMON_REX} | stats count by Computer | sort - count", "*/15 * * * *", "-1h", "rare"),
                Search("Hourly event volume by computer", f"index=main {st}=XmlWinEventLog | {SYSMON_REX} | timechart span=1h count by Computer", "0 6 * * *", "-24h@h", "dense"),
            ],
        ),
    ]


def metric_packs() -> List[Pack]:
    idx = METRIC_INDEX

    def m(name: str, search: str, cron: str, earliest: str) -> Search:
        return Search(name, search, cron, earliest, "accelerated")

    return [
        Pack(
            slug="host-infra-metrics", stoker_pack="host-infra-metrics", sourcetype="stoker:metric", seed=1201, metric=True,
            tags=["metrics", "infrastructure"], index=idx,
            description="Host metrics as an infrastructure dashboard schedules them: CPU by host, memory ceilings, disk growth, load and network, all over mstats",
            searches=[
                m("CPU by host", f"| mstats avg(host.cpu.usage.pct) as cpu WHERE index={idx} span=1m BY host", "*/5 * * * *", "-1h"),
                m("Memory ceiling by host", f"| mstats max(host.memory.used.pct) as mem WHERE index={idx} span=5m BY host, region", "*/10 * * * *", "-6h"),
                m("Disk growth", f"| mstats latest(host.disk.used.pct) as disk WHERE index={idx} span=1h BY host", "0 * * * *", "-24h@h"),
                m("Load average peaks", f"| mstats max(host.load.avg1m) as load WHERE index={idx} span=10m BY host | where load > 3", "*/15 * * * *", "-2h"),
                m("Network throughput by region", f"| mstats sum(host.net.throughput.mbps) as mbps WHERE index={idx} span=5m BY region", "*/10 * * * *", "-1h"),
                m("Every host metric, daily", f"| mstats avg(host.*) WHERE index={idx} span=1h BY host", "0 6 * * *", "-24h@h"),
            ],
        ),
        Pack(
            slug="api-service-red-metrics", stoker_pack="api-service-red-metrics", sourcetype="stoker:metric", seed=1202, metric=True,
            tags=["metrics", "red", "services"], index=idx,
            description="RED metrics per service: request rate, 5xx errors and latency percentiles, the SLO dashboard's own searches",
            searches=[
                m("Request rate by service", f"| mstats sum(http.requests.total) as requests WHERE index={idx} span=1m BY service", "*/5 * * * *", "-1h"),
                m("Error rate by service", f"| mstats sum(http.errors.5xx.total) as errors, sum(http.requests.total) as requests WHERE index={idx} span=5m BY service | eval error_pct=round(100*errors/requests, 3)", "*/5 * * * *", "-1h"),
                m("Latency p95 by service and method", f"| mstats avg(http.latency.p95.ms) as p95 WHERE index={idx} span=5m BY service, method", "*/10 * * * *", "-2h"),
                m("Slowest services", f"| mstats max(http.latency.p99.ms) as p99 WHERE index={idx} span=1h BY service | sort - p99", "0 * * * *", "-24h@h"),
                m("Daily error budget", f"| mstats sum(http.errors.5xx.total) as errors, sum(http.requests.total) as requests WHERE index={idx} span=1d BY service", "0 6 * * *", "-7d@d"),
            ],
        ),
        Pack(
            slug="k8s-workload-metrics", stoker_pack="k8s-workload-metrics", sourcetype="stoker:metric", seed=1203, metric=True,
            tags=["metrics", "kubernetes"], index=idx,
            description="Kubernetes workload metrics: container CPU and memory, restarts, ready replicas and network, by namespace and workload",
            searches=[
                m("Container CPU by namespace", f"| mstats avg(k8s.container.cpu.cores) as cores WHERE index={idx} span=1m BY namespace, workload", "*/5 * * * *", "-1h"),
                m("Memory by workload", f"| mstats max(k8s.container.memory.mb) as mb WHERE index={idx} span=5m BY namespace, workload", "*/10 * * * *", "-6h"),
                m("Pod restarts", f"| mstats sum(k8s.pod.restarts.total) as restarts WHERE index={idx} span=1h BY namespace | where restarts > 0", "*/15 * * * *", "-24h"),
                m("Ready replicas", f"| mstats min(k8s.workload.replicas.ready) as ready WHERE index={idx} span=5m BY namespace, workload", "*/5 * * * *", "-30m"),
                m("Network receive by namespace, daily", f"| mstats avg(k8s.container.net.rx.kbps) as kbps WHERE index={idx} span=1h BY namespace", "0 6 * * *", "-24h@h"),
            ],
        ),
        Pack(
            slug="database-metrics", stoker_pack="database-metrics", sourcetype="stoker:metric", seed=1204, metric=True,
            tags=["metrics", "database"], index=idx,
            description="Database metrics: connections, queries per second, cache hit ratio, replication lag and slow queries, by cluster and role",
            searches=[
                m("Connections by cluster", f"| mstats avg(db.connections.active) as connections WHERE index={idx} span=1m BY cluster, role", "*/5 * * * *", "-1h"),
                m("Queries per second", f"| mstats avg(db.queries.per_sec) as qps WHERE index={idx} span=5m BY cluster", "*/5 * * * *", "-1h"),
                m("Cache hit ratio floor", f"| mstats min(db.cache.hit_ratio.pct) as hit WHERE index={idx} span=10m BY cluster | where hit < 90", "*/10 * * * *", "-2h"),
                m("Replication lag on replicas", f"| mstats max(db.replication.lag.seconds) as lag WHERE index={idx} AND role=replica span=1m BY cluster", "*/5 * * * *", "-30m"),
                m("Slow queries, daily", f"| mstats sum(db.slow_queries.total) as slow WHERE index={idx} span=1h BY cluster", "0 6 * * *", "-24h@h"),
            ],
        ),
        Pack(
            slug="message-queue-metrics", stoker_pack="message-queue-metrics", sourcetype="stoker:metric", seed=1205, metric=True,
            tags=["metrics", "messaging"], index=idx,
            description="Message queue metrics: publish and consume rates, consumer lag and queue depth, by topic and consumer group",
            searches=[
                m("Publish and consume rates", f"| mstats sum(mq.messages.published.per_sec) as published, sum(mq.messages.consumed.per_sec) as consumed WHERE index={idx} span=1m BY topic", "*/5 * * * *", "-1h"),
                m("Consumer lag by group", f"| mstats max(mq.consumer.lag.messages) as lag WHERE index={idx} span=5m BY topic, consumer_group", "*/5 * * * *", "-1h"),
                m("Queue depth peaks", f"| mstats max(mq.queue.depth.messages) as depth WHERE index={idx} span=10m BY topic | where depth > 10000", "*/10 * * * *", "-6h"),
                m("Dead letter volume", f"| mstats sum(mq.messages.published.per_sec) as published WHERE index={idx} AND topic=deadletter span=1h", "0 * * * *", "-24h@h"),
                m("Daily throughput by topic", f"| mstats sum(mq.messages.consumed.per_sec) as consumed WHERE index={idx} span=1h BY topic", "0 6 * * *", "-24h@h"),
            ],
        ),
        Pack(
            slug="network-interface-metrics", stoker_pack="network-interface-metrics", sourcetype="stoker:metric", seed=1206, metric=True,
            tags=["metrics", "network"], index=idx,
            description="Network interface metrics: throughput in and out, utilisation, errors and discards, by device and interface",
            searches=[
                m("Throughput by device", f"| mstats avg(net.if.in.mbps) as in_mbps, avg(net.if.out.mbps) as out_mbps WHERE index={idx} span=1m BY device", "*/5 * * * *", "-1h"),
                m("Utilisation over 80 percent", f"| mstats max(net.if.utilization.pct) as util WHERE index={idx} span=5m BY device, interface | where util > 80", "*/5 * * * *", "-1h"),
                m("Interface errors", f"| mstats sum(net.if.errors.per_sec) as errors WHERE index={idx} span=10m BY device, interface | where errors > 0", "*/10 * * * *", "-2h"),
                m("Discards by device", f"| mstats sum(net.if.discards.per_sec) as discards WHERE index={idx} span=1h BY device", "0 * * * *", "-24h@h"),
                m("Daily utilisation by interface", f"| mstats avg(net.if.utilization.pct) as util WHERE index={idx} span=1h BY device, interface", "0 6 * * *", "-24h@h"),
            ],
        ),
        Pack(
            slug="web-store-metrics", stoker_pack="web-store-metrics", sourcetype="stoker:metric", seed=1207, metric=True,
            tags=["metrics", "business"], index=idx,
            description="Web store KPIs as metrics: requests and errors by service and region, CPU and checkout latency, the business dashboard's searches",
            searches=[
                m("Store requests by service", f"| mstats sum(store.requests) as requests WHERE index={idx} span=1m BY service, region", "*/5 * * * *", "-1h"),
                m("Store errors", f"| mstats sum(store.errors) as errors WHERE index={idx} span=5m BY service | where errors > 0", "*/5 * * * *", "-1h"),
                m("Checkout latency", f"| mstats avg(store.checkout.latency.ms) as latency WHERE index={idx} span=5m BY region", "*/10 * * * *", "-2h"),
                m("Host CPU behind the store", f"| mstats max(host.cpu.usage) as cpu WHERE index={idx} span=10m BY service", "*/15 * * * *", "-6h"),
                m("Daily requests by region", f"| mstats sum(store.requests) as requests WHERE index={idx} span=1h BY region", "0 6 * * *", "-24h@h"),
            ],
        ),
    ]


def conf_for(pack: Pack) -> str:
    stanzas: Dict[str, Dict[str, str]] = {}
    for search in pack.searches:
        stanza: Dict[str, str] = {}
        if search.description:
            stanza["description"] = search.description
        stanza["search"] = search.search
        stanza["dispatch.earliest_time"] = search.earliest
        stanza["dispatch.latest_time"] = search.latest
        stanza["cron_schedule"] = search.cron
        stanza["enableSched"] = "1"
        stanza["request.ui_dispatch_app"] = "search"
        stanzas[search.name] = stanza
    header = (
        f"Regulator scenario pack-{pack.slug}: searches written against the Stoker pack "
        f"'{pack.stoker_pack}' (sourcetype {pack.sourcetype}).\n"
        "Plain savedsearches.conf: drop it into an app's local/ directory and Splunk will\n"
        "schedule these searches itself. The classes Regulator uses are in scenario.yaml.\n"
        "Generated by tools/build_pack_scenarios.py; edit that file rather than this one."
    )
    return ss.render_conf(stanzas, header=header)


def yaml_for(pack: Pack, schedule: bool = False) -> Dict[str, object]:
    name = f"pack-{pack.slug}"
    load: Dict[str, object]
    if schedule:
        load = {"model": "schedule", "duration": "1800s"}
    else:
        load = {
            "model": "closed",
            "virtual_users": pack.virtual_users,
            "ramp": [{"to": pack.virtual_users, "over_s": 60}, {"hold_s": 540}],
            "duration": "600s",
        }
    corpus: Dict[str, object] = {
        "requires_packs": [pack.stoker_pack],
        "index": "main",
        "sourcetypes": [pack.sourcetype] if not pack.metric else [],
    }
    if pack.metric:
        corpus["metric_index"] = pack.index
    return {
        "name": name,
        "engine": "api",
        "seed": pack.seed,
        "description": pack.description,
        "tags": ["stoker-pack", pack.stoker_pack] + list(pack.tags),
        "corpus": corpus,
        "time_policy": {"mode": "rolling", "window": "24h", "jitter": "30m", "align": "1m"},
        "searches": {
            "file": "savedsearches.conf",
            "only_enabled": True,
            "time_from_saved": "derived",
            # Classes live here so the conf stays a plain Splunk file.
            "classes": {search.name: search.klass for search in pack.searches},
        },
        "personas": [
            {
                "name": "analyst",
                "weight": 100,
                "think_time": {"dist": "lognormal", "median_s": 20, "sigma": 0.5, "min_s": 2, "max_s": 180},
                "steps_from": "saved",
                "weight_by": "cron",
                "walk": "sample",
            }
        ],
        "load": load,
        "abort_if": {"error_rate_pct": 20, "p95_ms": 120000, "generator_drift_ms": 2000},
    }


HEADER = """# Generated by tools/build_pack_scenarios.py from the Stoker pack '{pack}'.
#
# The searches live in savedsearches.conf next to this file, in Splunk's own
# format, so the same file can be installed into a Splunk app and scheduled by
# Splunk itself. This file says how Regulator applies them as load: which class
# each belongs to, how the population behaves and how hard to push.
#
# Two ways to run it. As shipped, a closed population of virtual users draws
# one search per iteration, weighted by how often each is scheduled. Change
# load.model to `schedule` (see scenarios/stoker-scheduler) and every search
# fires on its own cron instead, which is what Splunk's scheduler would do.
"""


def write_pack(pack: Pack) -> Path:
    directory = SCENARIOS / f"pack-{pack.slug}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "savedsearches.conf").write_text(conf_for(pack), encoding="utf-8")
    (directory / "scenario.yaml").write_text(
        HEADER.format(pack=pack.stoker_pack) + yaml.safe_dump(yaml_for(pack), sort_keys=False, width=100),
        encoding="utf-8",
    )
    return directory


def write_scheduler(packs: List[Pack]) -> Path:
    """Every event pack's searches in one file, fired on their crons."""
    directory = SCENARIOS / "stoker-scheduler"
    directory.mkdir(parents=True, exist_ok=True)
    stanzas: Dict[str, Dict[str, str]] = {}
    classes: Dict[str, str] = {}
    sourcetypes: List[str] = []
    for pack in packs:
        if pack.metric:
            continue
        sourcetypes.append(pack.sourcetype)
        for search in pack.searches:
            name = f"{pack.stoker_pack}: {search.name}"
            stanzas[name] = {
                "search": search.search,
                "dispatch.earliest_time": search.earliest,
                "dispatch.latest_time": search.latest,
                "cron_schedule": search.cron,
                "enableSched": "1",
                "request.ui_dispatch_app": "search",
            }
            classes[name] = search.klass
    header = (
        "Regulator scenario stoker-scheduler: every event pack's scheduled searches in one\n"
        "file, so a run replays the whole scheduler the way Splunk would run it.\n"
        "Generated by tools/build_pack_scenarios.py."
    )
    (directory / "savedsearches.conf").write_text(ss.render_conf(stanzas, header=header), encoding="utf-8")
    document = {
        "name": "stoker-scheduler",
        "engine": "api",
        "seed": 1300,
        "description": (
            "Every event pack's scheduled searches fired on their own cron, as Splunk's scheduler "
            "would: the top-of-the-hour burst is the point, because that is where a real cluster queues"
        ),
        "tags": ["stoker-pack", "schedule", "scheduler"],
        "corpus": {
            "requires_packs": [p.stoker_pack for p in packs if not p.metric],
            "index": "main",
            "sourcetypes": sourcetypes,
        },
        "time_policy": {"mode": "rolling", "window": "24h", "jitter": "30m", "align": "1m"},
        "searches": {"file": "savedsearches.conf", "only_scheduled": True, "classes": classes},
        "personas": [
            {
                "name": "scheduler",
                "weight": 100,
                "think_time": {"dist": "fixed", "value_s": 0},
                "steps_from": "saved",
                "weight_by": "cron",
            }
        ],
        # Start the virtual clock just before the hour so the first burst
        # lands early in the run rather than up to 59 minutes in.
        "load": {"model": "schedule", "duration": "1800s", "schedule_start": "08:55"},
        "abort_if": {"error_rate_pct": 25, "p95_ms": 300000, "generator_drift_ms": 3000},
    }
    (directory / "scenario.yaml").write_text(
        "# Generated by tools/build_pack_scenarios.py. The searches are in savedsearches.conf.\n"
        "# Splunk would skip searches over its concurrency limit; Regulator dispatches them all\n"
        "# and records the queueing, since seeing it is the point.\n"
        + yaml.safe_dump(document, sort_keys=False, width=100),
        encoding="utf-8",
    )
    return directory


def main() -> int:
    packs = event_packs() + metric_packs()
    written = [write_pack(pack) for pack in packs]
    written.append(write_scheduler(packs))
    problems = 0
    for directory in written:
        scenario = load_scenario(directory)
        blocking = [line for line in lint(scenario) if not is_advice(line)]
        selected = len(scenario.saved_selected)
        if blocking:
            problems += 1
            print(f"{directory.name}: {selected} searches, LINT PROBLEMS: {'; '.join(blocking)}")
        else:
            print(f"{directory.name}: {selected} searches, {len(scenario.steps)} steps, lint clean")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
