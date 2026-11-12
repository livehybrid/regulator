"""The REST paths chaos actions (issue 15) would restart through. Row 5 of issue 16.

A GET to each answers 400 "requires POST" where a path that does not exist
answers 404, so this proves the handlers are there. It cannot prove the
action names: the cluster manager's handler refuses a GET before it looks at
the action, so a wrong action name answers the same. Confirming those means
restarting the cluster, which belongs with the chaos actions themselves.
"""

from __future__ import annotations

import urllib.error

import pytest

from conftest import CM_URL, SH_URL, rest


@pytest.mark.parametrize(
    "where, path",
    [
        ("sh", "/services/server/control/restart"),
        ("cm", "/services/cluster/manager/control/default/restart"),
        ("sh", "/services/shcluster/captain/control/default/restart"),
    ],
)
def test_the_restart_handler_exists_and_wants_a_post(where, path):
    url = {"sh": SH_URL, "cm": CM_URL}[where]
    if not url:
        pytest.skip(f"no {where} URL")
    with pytest.raises(urllib.error.HTTPError) as caught:
        rest(url, path)
    assert caught.value.code == 400, caught.value.code
    assert "POST" in caught.value.read().decode("utf-8", "replace")


def test_a_path_that_does_not_exist_is_a_404_so_the_check_means_something():
    with pytest.raises(urllib.error.HTTPError) as caught:
        rest(SH_URL, "/services/server/no_such_handler/restart")
    assert caught.value.code == 404
