"""Search behaviour Regulator models: cluster job visibility and OR precedence.

Rows 4 and 6 of issue 16's table.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest

from conftest import INDEX, MEMBER_URLS, SH_URL, client_for, oneshot, run


@pytest.mark.skipif(len(MEMBER_URLS) < 2, reason="needs two search head cluster members")
def test_only_the_member_that_dispatched_an_ad_hoc_job_knows_it():
    """The assumption behind REG_UNKNOWN_SID_GRACE_MS, as it turned out.

    It was written as "a member that has not heard of a job yet answers 404,
    and hears within seconds". On 10.6 the other members never hear: they
    answer 404 for the job's whole life, on every jobs path. So the grace now
    runs from the last time any member answered for the job (engines/api.py),
    and the fake's shc_members mode behaves like this.
    """
    from regulator_agent.splunk import SplunkHttpError
    from regulator_agent.timepolicy import TimeWindow

    origin, others = MEMBER_URLS[0], MEMBER_URLS[1:]

    async def go():
        a = client_for(origin)
        await a.start()
        clients = [client_for(url) for url in others]
        for c in clients:
            await c.start()
        try:
            sid = f"reg_contract_{uuid.uuid4().hex[:10]}"
            await a.create_job("| makeresults count=1", TimeWindow(earliest=time.time() - 60, latest=time.time()), sid=sid)
            began = time.perf_counter()
            answers = []
            while time.perf_counter() - began < 15:
                for c in clients:
                    try:
                        await c.job_status(sid)
                        answers.append("known")
                    except SplunkHttpError as exc:
                        answers.append(exc.status)
                await asyncio.sleep(0.5)
            origin_knows = (await a.job_status(sid)).sid == sid
            return answers, origin_knows
        finally:
            for c in [a, *clients]:
                await c.close()

    answers, origin_knows = run(go())
    print(f"the other members, {len(answers)} times over 15 s:", sorted(set(map(str, answers))))
    assert origin_knows
    assert set(answers) == {404}, answers


def test_or_binds_tighter_than_the_implicit_and(corpus):
    """splscope.py reads `a OR b c` as `(a OR b) c`. If Splunk read it as
    `a OR (b c)` the forecast would miss indexes a search reads."""
    tag = corpus["tag"]
    spl = f"search {tag} sourcetype=contract:a OR index=main index={INDEX} | stats count by index"
    rows = run(oneshot(SH_URL, spl, earliest=corpus["earliest"], latest=corpus["latest"]))
    found = {r["index"]: int(r["count"]) for r in rows}
    print(spl, "->", found)
    # (sourcetype=contract:a OR index=main) AND index=contract: the contract
    # index only. AND first would also return main's contract:a events.
    assert set(found) == {INDEX}, found

    from regulator_agent.splscope import read_scope

    read = {i for clause in read_scope(spl).clauses for i in clause.indexes}
    # The model may over-read (it unions the terms) but must not under-read.
    assert set(found) <= read, (found, read)


@pytest.mark.skipif(len(MEMBER_URLS) < 2, reason="needs two search head cluster members")
def test_the_audit_trail_names_the_member_that_served_each_search():
    """Issue 18's open question: which member served a search behind a balancer.

    The client never learns it. The answer Regulator relies on is the host of
    the search's own _audit records, written by the member that dispatched it
    (the only one that ever knows the job, per the test above). So each search
    here is dispatched on a known member directly, and both the per-sid lookup
    (sut.members_for_sids) and the per-step, per-member probe must name that
    member, read back through the balanced address like a real run.
    """
    from regulator_agent.params import apply_cache_bust, cache_bust_marker, sid_prefix_for
    from regulator_agent.sut import build_probes, marker_prefix_for, members_for_sids
    from regulator_agent.timepolicy import TimeWindow

    label = f"contract-{uuid.uuid4().hex[:8]}"
    prefix = sid_prefix_for(label)
    began = time.time() - 5

    async def dispatch():
        names, served = {}, {}
        for n, url in enumerate(MEMBER_URLS):
            client = client_for(url)
            await client.start()
            try:
                info = await client.server_info()
                names[url] = {str(info.get("host") or ""), str(info.get("serverName") or "")} - {""}
                for i in range(2):
                    sid = f"{prefix}{n}_{i}"
                    spl = apply_cache_bust("| makeresults count=1", cache_bust_marker(label, n, i, f"member{n}"))
                    await client.create_job(spl, TimeWindow(earliest=time.time() - 60, latest=time.time()), sid=sid)
                    served[sid] = url
            finally:
                await client.close()
        return names, served

    names, served = run(dispatch())
    print("member names, by url:", names)

    async def lookup():
        client = client_for(SH_URL)
        await client.start()
        try:
            window = TimeWindow(earliest=began, latest=time.time() + 120)
            return await members_for_sids(client, window, sorted(served))
        finally:
            await client.close()

    found = wait_for(
        lambda: (lambda f: f if len(f[0]) == len(served) else None)(run(lookup())),
        timeout_s=180, every_s=10, what="the audit trail to hold every search",
    )
    members, queries, reason = found
    print("audit trail:", members, "in", queries, "search(es)", reason or "")
    for sid, url in served.items():
        assert members[sid] in names[url], (sid, members[sid], names[url])
    # Different members, told apart: not every search on one host.
    assert len(set(members.values())) == len(MEMBER_URLS)

    probe = next(p for p in build_probes(marker_prefix_for(label), prefix) if p.name == "our_searches_by_member")
    rows = run(oneshot(SH_URL, probe.spl, earliest=began, count=probe.max_rows))
    print("by step and member:", [(r.get("step"), r.get("member"), r.get("searches")) for r in rows])
    placed = {(r["step"], r["member"]): int(float(r["searches"])) for r in rows}
    for n, url in enumerate(MEMBER_URLS):
        step = f"member{n}"
        on_member = {member: c for (s, member), c in placed.items() if s == step}
        assert set(on_member) <= names[url] and sum(on_member.values()) == 2, (step, on_member, names[url])
