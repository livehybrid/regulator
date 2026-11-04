"""Which concurrent-search limit binds first."""

from __future__ import annotations

from conftest import run_async
from regulator_agent.ceilings import search_ceilings
from regulator_agent.engines.base import TargetCapabilities


def caps(cores=16):
    return TargetCapabilities(
        server_name="sh1", cpu_count=cores, base_max_searches=6, max_searches_per_cpu=1, max_searches_perc=50
    )


class Stub:
    def __init__(self, entries):
        self._entries = entries

    async def entries(self, path, context, params=None):
        return self._entries.get(path, [])


MEMBERS = [
    {"name": "m1", "mgmt_uri": "https://sh1.example:8089"},
    {"name": "m2", "mgmt_uri": "https://sh2.example:8089"},
    {"name": "m3", "mgmt_uri": "https://sh3.example:8089"},
]


def ceilings(entries, url="https://splunk.example:8089", cores=16):
    return run_async(search_ceilings(Stub(entries), url, caps(cores)))


def test_a_single_instance_is_its_own_ad_hoc_limit():
    out = ceilings({})
    assert out["search_head"]["ad_hoc"] == 22
    assert out["search_head"]["scheduler_share"] == 11
    assert out["cluster"] is None
    assert out["effective"] == {"limit": "search head ad hoc limit", "value": 22}


def test_a_load_balanced_cluster_multiplies_the_member_limit():
    out = ceilings({"/services/shcluster/member/members": MEMBERS})
    assert out["cluster"]["routing"] == "load_balancer"
    assert out["cluster"]["ad_hoc_total"] == 66
    assert out["effective"]["value"] == 66
    assert "3 members" in out["effective"]["note"]


def test_a_target_naming_one_member_only_gets_that_member():
    out = ceilings({"/services/shcluster/member/members": MEMBERS}, url="https://SH2.example:8089")
    assert out["cluster"]["routing"] == "direct"
    assert out["effective"]["value"] == 22
    assert "one cluster member" in out["effective"]["note"]


def test_the_role_quota_binds_before_the_instance_limit():
    out = ceilings({
        "/services/shcluster/member/members": MEMBERS,
        "/services/authentication/current-context": [{"username": "loadtest", "roles": ["power", "user"]}],
        "/services/authorization/roles/power": [{"srchJobsQuota": "10", "cumulativeSrchJobsQuota": "200"}],
        "/services/authorization/roles/user": [{"srchJobsQuota": "3"}],
    })
    assert out["account"]["srch_jobs_quota"] == 10
    assert out["account"]["cumulative_quota"] == 200
    # 10 per member across three members, against 66 for the cluster.
    assert out["effective"]["value"] == 30
    assert "role quota" in out["effective"]["limit"]


def test_a_zero_quota_is_unlimited():
    out = ceilings({
        "/services/authentication/current-context": [{"username": "admin", "roles": ["admin", "user"]}],
        "/services/authorization/roles/admin": [{"srchJobsQuota": "0"}],
        "/services/authorization/roles/user": [{"srchJobsQuota": "3"}],
    })
    assert out["account"]["srch_jobs_quota"] is None
    assert out["effective"]["limit"] == "search head ad hoc limit"


def test_unreadable_parts_say_so():
    out = run_async(search_ceilings(Stub({}), "https://x:8089", TargetCapabilities(cpu_count=8)))
    assert out["effective"] is None
    assert any("list_settings" in n for n in out["notes"])
    assert any("current-context" in n for n in out["notes"])


def test_indexer_cores_are_totalled_when_every_peer_reports_them():
    out = ceilings({"/services/search/distributed/peers": [
        {"name": "idx1", "status": "Up", "numberOfVirtualCores": "16"},
        {"name": "idx2", "status": "Up", "numberOfVirtualCores": "16"},
    ]})
    assert out["indexers"] == {"peers": 2, "up": 2, "cores_total": 32}
