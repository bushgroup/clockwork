"""The instrument directory beside the lab directory, a library of several directories, and
the project a report names (lab record, task 158).

`conftest.no_instrument_directory` points `$CLOCKWORK_INSTRUMENT` at an empty folder for
every test; the tests here that want an instrument set it to a tree of their own.
"""

from __future__ import annotations

import io
import os
import socket

import pytest

import clockwork
from clockwork import envelope
from clockwork.app.methodlib import scan_library
from clockwork.mcp import Toolbox, ToolFailure, server
from clockwork.owner import RemoteOwner
from clockwork.report import Report, project_of
from test_daemon import Daemon
from test_mcp import LABELS, TEMPLATE, fake_owner, limits_text  # noqa: F401 -- a fixture

PROJECT_TEMPLATE = TEMPLATE.replace('name = "mcp-test"', 'name = "project-test"')
"""A template of a project's own: another name, so another hash."""

DOCUMENT = 'schema_version = 1\n\n[instrument]\nname = "TEST1"\n'


def instrument_tree(root, *, golden: bool = True, routines: bool = False) -> str:
    os.makedirs(root, exist_ok=True)
    (root / clockwork.INSTRUMENT_FILE).write_text(DOCUMENT, encoding="utf-8")
    if golden:
        (root / "golden").mkdir()
        (root / "golden" / "line-b.toml").write_text(TEMPLATE, encoding="utf-8")
    if routines:
        (root / "routines").mkdir()
    return str(root)


# --- resolution ------------------------------------------------------------------------


def test_the_variable_names_the_instrument_directory_and_its_parts(tmp_path, monkeypatch):
    root = instrument_tree(tmp_path / "slimphony", routines=True)
    monkeypatch.setenv(clockwork.INSTRUMENT_ENV, root)
    assert clockwork.instrument_dir() == root
    assert clockwork.instrument_dir("golden") == os.path.join(root, "golden")
    assert clockwork.instrument_dir("routines") == os.path.join(root, "routines")
    assert clockwork.instrument_file() == os.path.join(root, clockwork.INSTRUMENT_FILE)


def test_a_missing_part_is_none_and_an_unknown_one_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv(clockwork.INSTRUMENT_ENV, instrument_tree(tmp_path / "i"))
    assert clockwork.instrument_dir("routines") is None
    for name in ("console", "tasks", "limits"):
        with pytest.raises(ValueError, match="unknown instrument directory"):
            clockwork.instrument_dir(name)


def test_a_project_repository_is_not_an_instrument_directory(tmp_path, monkeypatch):
    """A project has a task system too; only the instrument document marks the root."""
    project = tmp_path / "slimphony-demo"
    (project / "tasks").mkdir(parents=True)
    (project / "tasks" / "README.md").write_text("# tasks\n", encoding="utf-8")
    (project / "templates").mkdir()
    monkeypatch.setenv(clockwork.INSTRUMENT_ENV, str(project))
    assert clockwork.instrument_dir() is None and clockwork.instrument_file() is None


def test_a_set_variable_that_names_nothing_is_not_passed_over_for_the_sibling(
        tmp_path, monkeypatch):
    sibling = tmp_path / "slimphony"
    instrument_tree(sibling)
    monkeypatch.setattr(clockwork, "ROOT", str(tmp_path / "clockwork"))
    monkeypatch.delenv(clockwork.INSTRUMENT_ENV)
    assert clockwork.instrument_dir() == str(sibling)
    monkeypatch.setenv(clockwork.INSTRUMENT_ENV, str(tmp_path / "elsewhere"))
    assert clockwork.instrument_dir() is None


def test_with_neither_present_nothing_resolves(tmp_path, monkeypatch):
    monkeypatch.setattr(clockwork, "ROOT", str(tmp_path / "clockwork"))
    monkeypatch.delenv(clockwork.INSTRUMENT_ENV)
    assert clockwork.instrument_dir() is None
    assert clockwork.instrument_dir("golden") is None
    assert clockwork.instrument_file() is None


def test_the_lab_directory_keeps_the_console_and_never_the_routines():
    with pytest.raises(ValueError, match="unknown lab directory"):
        clockwork.lab_dir("routines")
    found = clockwork.lab_dir("console")
    assert found is None or os.path.isdir(found)


def test_a_library_setting_is_several_directories_in_order_each_once(tmp_path):
    one, two = str(tmp_path / "one"), str(tmp_path / "two")
    setting = os.pathsep.join([one, "", two, one, " "])
    assert clockwork.library_dirs(setting) == (one, two)
    assert clockwork.library_dirs("") == ()
    assert clockwork.library_setting(clockwork.library_dirs(setting)) == \
        os.pathsep.join([one, two])


# --- several library directories --------------------------------------------------------


@pytest.fixture
def two_libraries(tmp_path):
    """The instrument's library and a project's `templates/`, as one setting."""
    instrument = instrument_tree(tmp_path / "slimphony", routines=True)
    templates = tmp_path / "slimphony-demo" / "templates"
    (templates / "kv").mkdir(parents=True)
    (templates / "kv" / "template.toml").write_text(PROJECT_TEMPLATE, encoding="utf-8")
    golden = os.path.join(instrument, "golden")
    return golden, str(templates), os.pathsep.join([golden, str(templates)])


def test_the_window_scan_names_the_library_each_document_came_from(two_libraries):
    golden, templates, setting = two_libraries
    found = {entry.name: entry.library for entry in scan_library(setting)}
    assert found == {"mcp-test": golden, "project-test": templates}


def test_list_templates_reports_each_templates_library(two_libraries, tmp_path):
    golden, templates, setting = two_libraries
    toolbox = Toolbox(object(), library=setting, output=str(tmp_path / "runs"))
    listed = {entry["name"]: entry for entry in toolbox.call("list_templates")["templates"]}
    assert listed["mcp-test"]["library"] == golden
    assert listed["mcp-test"]["path"] == "line-b.toml"
    assert listed["project-test"]["library"] == templates
    assert listed["project-test"]["path"] == "kv/template.toml"
    assert not any("duplicate" in entry for entry in listed.values())
    rendered = toolbox.call("render_template", {"template": "kv/template.toml",
                                                "labels": LABELS})
    assert rendered["ok"] and rendered["name"] == "project-test"


def test_the_routines_are_beside_the_first_library_and_a_new_file_lands_in_the_last(
        two_libraries, tmp_path):
    golden, templates, setting = two_libraries
    toolbox = Toolbox(object(), library=setting, output=str(tmp_path / "runs"))
    assert toolbox.routines == os.path.join(os.path.dirname(golden), "routines")
    written = toolbox.call("render_template", {"template": "line-b.toml", "labels": LABELS,
                                               "to": "rendered/b-default"})["written"]
    assert written == os.path.join(templates, "rendered", "b-default.toml")
    assert not os.path.exists(os.path.join(golden, "rendered"))


def test_a_name_in_two_libraries_is_refused_naming_both(two_libraries, tmp_path):
    golden, templates, setting = two_libraries
    copy = os.path.join(templates, "line-b.toml")
    with open(copy, "w", encoding="utf-8") as handle:
        handle.write(TEMPLATE)
    toolbox = Toolbox(object(), library=setting, output=str(tmp_path / "runs"))
    original = os.path.join(golden, "line-b.toml")
    with pytest.raises(ToolFailure, match="more than one library") as refused:
        toolbox.call("render_template", {"template": "line-b.toml", "labels": LABELS})
    assert original in str(refused.value) and copy in str(refused.value)
    listed = [entry for entry in toolbox.call("list_templates")["templates"]
              if entry["path"] == "line-b.toml"]
    assert [entry["library"] for entry in listed] == [golden, templates]
    assert all(entry["duplicate"] == [original, copy] for entry in listed)
    # By its full path it is one file, and renders.
    assert toolbox.call("render_template", {"template": copy, "labels": LABELS})["ok"]


def test_a_project_template_the_limits_do_not_name_is_refused_as_any_unlisted_one(
        two_libraries, tmp_path, fake_owner):  # noqa: F811
    """The standing limits stay the one list of what may run, wherever it lives."""
    golden, _, setting = two_libraries
    path = tmp_path / "limits.toml"
    path.write_text(limits_text(golden), encoding="utf-8")
    toolbox = Toolbox(fake_owner, library=setting, output=str(tmp_path / "runs"),
                      limits=envelope.load(str(path)))
    listed = {entry["name"]: entry for entry in toolbox.call("list_templates")["templates"]}
    assert listed["mcp-test"]["limits"]["allowed"]
    assert listed["project-test"]["limits"] == {"allowed": False, "problems": []}
    checked = toolbox.call("validate_method", {"template": "kv/template.toml",
                                               "labels": LABELS})
    assert not checked["ok"]
    assert any("not in the standing limits" in line for line in checked["limits"])


# --- the defaults ------------------------------------------------------------------------


def test_the_daemon_serves_the_instrument_library_when_none_is_named(tmp_path, monkeypatch):
    root = instrument_tree(tmp_path / "slimphony")
    monkeypatch.setenv(clockwork.INSTRUMENT_ENV, root)
    made = Daemon(tmp_path / "runs")
    client = RemoteOwner(made.endpoint, timeout=10)
    try:
        assert client.hello().library == os.path.join(root, "golden")
    finally:
        made.stop(client)
        client.close()


def test_a_named_library_is_the_daemons_and_none_resolves_to_none(tmp_path):
    named = os.pathsep.join([str(tmp_path / "a"), str(tmp_path / "b")])
    for library, expected in ((named, named), ("", "")):
        made = Daemon(tmp_path / "runs", library=library)
        client = RemoteOwner(made.endpoint, timeout=10)
        try:
            assert client.hello().library == expected
        finally:
            made.stop(client)
            client.close()


def test_the_mcp_server_reads_the_instrument_document_by_default(tmp_path, monkeypatch):
    """Read before any daemon is asked for, so a document that does not load is the
    sentence back, naming the file the default found."""
    root = instrument_tree(tmp_path / "slimphony")
    (tmp_path / "slimphony" / clockwork.INSTRUMENT_FILE).write_text(
        "schema_version = 1\nnot_a_key = 1\n", encoding="utf-8")
    monkeypatch.setenv(clockwork.INSTRUMENT_ENV, root)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    said = io.StringIO()
    assert server.run(endpoint=f"tcp://127.0.0.1:{port}", stream=said) == 1
    assert os.path.join(root, clockwork.INSTRUMENT_FILE) in said.getvalue()


# --- the project in a report --------------------------------------------------------------


@pytest.mark.parametrize(("folder", "project"), [
    ("D:/repos/slimphony-ubiquitin-dynamics-data/261109-figure5", "slimphony-ubiquitin-dynamics"),
    ("D:/repos/slimphony-ubiquitin-dynamics-data", "slimphony-ubiquitin-dynamics"),
    ("D:/repos/slimphony-data/261109-beam-check", "slimphony"),
    ("D:/repos/slimphony-x-data/261109-calibration-data", "slimphony-x"),
    ("D:/data/Bruce/261103", ""),
    ("D:/repos/-data/261109", ""),
    ("", ""),
])
def test_the_project_is_read_off_the_data_folder(folder, project):
    assert project_of(folder) == project


def test_a_report_names_its_project_and_one_without_names_none():
    def block(made: Report) -> str:
        from urllib.parse import parse_qs, urlsplit

        return parse_qs(urlsplit(made.url()).query)["attachments"][0]

    assert "- Project: slimphony-demo" in block(Report(project="slimphony-demo"))
    assert "Project" not in block(Report())
