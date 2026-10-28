"""tools/package_splunk_apps.py: what a release attaches is installable."""

from __future__ import annotations

import configparser
import importlib.util
import tarfile
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1] / "package_splunk_apps.py"
_spec = importlib.util.spec_from_file_location("package_splunk_apps", TOOL)
pkg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pkg)


def _conf(text):
    parser = configparser.RawConfigParser(strict=False)
    parser.read_string(text)
    return parser


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("dist")
    assert pkg.main(["--version", "v1.2.3", "--out", str(out)]) == 0
    return out


def test_every_shipped_app_is_packaged_under_its_id(built):
    names = sorted(p.name for p in built.glob("*.tgz"))
    ids = sorted(pkg.app_id(pkg.REPO / d) for d in pkg.APP_DIRS)
    assert names == ["%s-1.2.3.tgz" % i for i in ids]
    sums = (built / "SHA256SUMS").read_text().split("\n")
    assert len([s for s in sums if s]) == len(ids)


def test_a_package_is_one_folder_with_the_stamped_version(built):
    for path in built.glob("*.tgz"):
        app = path.name.rsplit("-", 1)[0]
        with tarfile.open(path) as tar:
            members = tar.getmembers()
            assert {m.name.split("/", 1)[0] for m in members} == {app}
            assert not any("/local/" in m.name or m.name.endswith("/local") for m in members)
            assert not any(m.name.endswith("local.meta") for m in members)
            for m in members:
                assert m.mode == (0o755 if m.isdir() else 0o644), m.name
                assert m.uid == 0 and m.gid == 0
            conf = _conf(tar.extractfile("%s/default/app.conf" % app).read().decode())
            assert conf.get("launcher", "version") == "1.2.3"
            assert conf.get("id", "name") == app
            assert conf.get("id", "version") == "1.2.3"
            views = [m for m in members if "/default/data/ui/views/" in m.name and m.isfile()]
            assert views, "%s packaged without its dashboards" % app


def test_the_same_version_gives_the_same_bytes(tmp_path):
    app = pkg.REPO / pkg.APP_DIRS[0]
    a = pkg.package(app, "0.0.1", tmp_path / "a")
    b = pkg.package(app, "0.0.1", tmp_path / "b")
    assert a.read_bytes() == b.read_bytes()


def test_the_source_app_conf_is_left_alone(tmp_path):
    conf = pkg.REPO / pkg.APP_DIRS[0] / "default" / "app.conf"
    before = conf.read_text()
    pkg.package(conf.parents[1], "9.9.9", tmp_path)
    assert conf.read_text() == before


def test_local_edits_and_dotfiles_stay_out(tmp_path):
    app = tmp_path / "src" / "demo_app"
    (app / "default").mkdir(parents=True)
    (app / "local").mkdir()
    (app / "metadata").mkdir()
    (app / "default" / "app.conf").write_text("# keep me\n[launcher]\nversion = 0.0.0\nauthor = x\n")
    (app / "local" / "app.conf").write_text("[ui]\nlabel = mine\n")
    (app / "metadata" / "local.meta").write_text("[]\n")
    (app / "metadata" / "default.meta").write_text("[]\nexport = system\n")
    (app / ".DS_Store").write_text("x")
    path = pkg.package(app, "2.0.0", tmp_path / "out")
    with tarfile.open(path) as tar:
        names = set(tar.getnames())
        text = tar.extractfile("demo_app/default/app.conf").read().decode()
    assert names == {"demo_app", "demo_app/default", "demo_app/default/app.conf",
                     "demo_app/metadata", "demo_app/metadata/default.meta"}
    assert text.startswith("# keep me\n[launcher]\nversion = 2.0.0\nauthor = x\n")
    assert "[id]\nname = demo_app\nversion = 2.0.0" in text


@pytest.mark.parametrize("bad", ["1.2", "1.2.3-rc1", "latest", ""])
def test_a_version_splunk_would_reject_fails_the_build(tmp_path, bad):
    assert pkg.main(["--version", bad, "--out", str(tmp_path)]) == 2
