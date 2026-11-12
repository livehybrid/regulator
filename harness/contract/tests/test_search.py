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
