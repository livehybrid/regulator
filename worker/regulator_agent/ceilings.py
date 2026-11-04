"""Which concurrent-search limit a run against this target will hit first.

The single number Regulator used to show, ``base_max_searches +
max_searches_per_cpu x cores``, is the ad hoc limit of the one splunkd the
management URL reached. Three other limits sit beside it, and on a real
cluster one of them is usually the one that binds:

* **The search head cluster.** The ad hoc limit is per member. Load spread
  across members by a load balancer can use the sum; load sent straight to one
  member cannot. Which one this target is, is read off the member list: a
  management URL that names a member goes to that member only.
* **The account's role quota.** ``srchJobsQuota`` caps concurrent historical
  searches per user. Regulator runs every virtual user as one account, so this
  is often far below the instance limit, and when it binds searches queue in a
  way that looks exactly like a slow cluster. 0 means unlimited. With
  ``enable_cumulative_quota`` on, ``cumulativeSrchJobsQuota`` caps the role
  as a whole; Splunk documents that one as per member in a search head
  cluster, with no cluster-wide accounting, and the per-user quota is treated
  the same way here.
* **The indexers.** They apply no concurrent-search cap to distributed
  searches by default (workload management can add one). Each search runs a
  process on every peer it touches, so their CPU is the real ceiling. Shown as
  context, never as a number to compare against.

Everything is best effort, and each part says why it is missing.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from .splunk import SplunkClient


def _int(value: Any) -> Optional[int]:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _hostport(url: str) -> str:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = (parsed.hostname or "").lower()
    return f"{host}:{parsed.port or 8089}"


async def search_ceilings(client: SplunkClient, target_url: str, caps: Any) -> Dict[str, Any]:
    """The limits in force for a run against ``target_url``. Never raises."""
    notes: List[str] = []
    out: Dict[str, Any] = {"notes": notes}

    # ------------------------------------------------- this search head
    ad_hoc = getattr(caps, "max_hist_searches", None)
    base = getattr(caps, "base_max_searches", None)
    per_cpu = getattr(caps, "max_searches_per_cpu", None)
    cores = getattr(caps, "cpu_count", 0) or None
    perc = getattr(caps, "max_searches_perc", None)
    out["search_head"] = {
        "server_name": getattr(caps, "server_name", "") or None,
        "ad_hoc": ad_hoc,
        "base_max_searches": base,
        "max_searches_per_cpu": per_cpu,
        "cores": cores,
        "scheduler_share": int(ad_hoc * perc / 100) if ad_hoc is not None and perc is not None else None,
        "max_searches_perc": perc,
    }
    if ad_hoc is None:
        notes.append(
            "this search head's limit is unknown: limits.conf [search] was not readable "
            "(the account needs list_settings) or server/info reported no core count"
        )

    # ------------------------------------------------- search head cluster
    members = await _entries(client, "/services/shcluster/member/members", "search head cluster members")
    routing = "single instance"
    cluster: Optional[Dict[str, Any]] = None
    if members:
        ours = _hostport(target_url)
        member_hosts = {_hostport(str(m.get("mgmt_uri") or "")) for m in members if m.get("mgmt_uri")}
        routing = "direct" if ours in member_hosts else "load_balancer"
        cluster = {
            "members": len(members),
            "routing": routing,
            # Every member is assumed to be sized like the one that answered:
            # other members' server/info is not reachable through a balancer.
            "ad_hoc_total": ad_hoc * len(members) if ad_hoc is not None else None,
        }
    out["cluster"] = cluster
    spread = cluster["members"] if cluster and routing == "load_balancer" else 1

    # ------------------------------------------------- the account
    account = await _account(client, notes)
    out["account"] = account

    # ------------------------------------------------- indexers
    peers = await _entries(client, "/services/search/distributed/peers", "distributed peers")
    if peers:
        up = [p for p in peers if str(p.get("status") or "").lower() in ("up", "")]
        core_values = [_int(p.get("numberOfVirtualCores") or p.get("numberOfCores")) for p in up]
        out["indexers"] = {
            "peers": len(peers),
            "up": len(up),
            "cores_total": sum(c for c in core_values if c) if core_values and all(core_values) else None,
        }
    else:
        out["indexers"] = None

    # ------------------------------------------------- what binds first
    candidates: List[Dict[str, Any]] = []
    if ad_hoc is not None:
        candidates.append({
            "limit": "search head cluster ad hoc limit" if spread > 1 else "search head ad hoc limit",
            "value": ad_hoc * spread,
        })
    if account and account.get("srch_jobs_quota"):
        candidates.append({
            "limit": f"role quota for {account.get('user') or 'this account'}",
            "value": int(account["srch_jobs_quota"]) * spread,
        })
    # A role's cumulative quota is shown but never chosen: it only applies with
    # limits.conf enable_cumulative_quota = true, which is not readable here.
    out["effective"] = min(candidates, key=lambda c: c["value"]) if candidates else None
    if out["effective"] and spread > 1:
        out["effective"]["note"] = (
            f"across {spread} members, assuming the load balancer spreads searches evenly "
            "and every member is sized like the one that answered"
        )
    elif out["effective"] and cluster and routing == "direct":
        out["effective"]["note"] = (
            "this target names one cluster member, so the run uses that member's limits only; "
            "point it at the load balancer to use the cluster"
        )
    return out


async def _entries(client: SplunkClient, path: str, context: str) -> List[Dict[str, Any]]:
    try:
        return await client.entries(path, context)
    except Exception:  # noqa: BLE001 - every part of this is optional
        return []


async def _account(client: SplunkClient, notes: List[str]) -> Optional[Dict[str, Any]]:
    """The account's own search quotas, from its roles."""
    context = await _entries(client, "/services/authentication/current-context", "current user")
    if not context:
        notes.append("the account's role quota is unknown: authentication/current-context was not readable")
        return None
    user = context[0].get("username") or context[0].get("name")
    roles = context[0].get("roles") or []
    if isinstance(roles, str):
        roles = [roles]
    quotas: List[int] = []
    cumulative: List[tuple] = []
    unread: List[str] = []
    for role in roles:
        entry = await _entries(client, f"/services/authorization/roles/{role}", f"role {role}")
        if not entry:
            unread.append(str(role))
            continue
        content = entry[0]
        for key in ("srchJobsQuota", "imported_srchJobsQuota"):
            value = _int(content.get(key))
            if value is not None:
                quotas.append(value)
        value = _int(content.get("cumulativeSrchJobsQuota"))
        if value:
            cumulative.append((value, str(role)))
    if unread:
        notes.append(
            "could not read role(s) " + ", ".join(unread) + " (the account needs to list roles), "
            "so its search quota may be higher than shown"
        )
    if not quotas:
        return {"user": user, "roles": roles, "srch_jobs_quota": None, "cumulative_quota": None}
    # 0 is "no limit"; otherwise the largest quota of any role the user has.
    quota = None if 0 in quotas else max(quotas)
    largest = max(cumulative) if cumulative else None
    return {
        "user": user,
        "roles": roles,
        "srch_jobs_quota": quota,
        # Only in force with limits.conf enable_cumulative_quota = true, which
        # is not readable here; shown when the role sets one.
        "cumulative_quota": largest[0] if largest else None,
        "cumulative_role": largest[1] if largest else None,
    }
