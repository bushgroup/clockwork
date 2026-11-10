"""The instrument directory beside the lab directory, a library of several directories, and
the project a report names (lab record, task 158).

`conftest.no_instrument_directory` points `$CLOCKWORK_INSTRUMENT` at an empty folder for
every test; the tests here that want an instrument set it to a tree of their own.
"""

from __future__ import annotations

import argparse
import ctypes
import io
import os
import socket
import sys

import pytest

import clockwork
from clockwork import envelope
from clockwork.app.methodlib import scan_library
from clockwork.mcp import Toolbox, ToolFailure, cli, server
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
    ("D:/repos/slimphony-demo-data/261109-sitting", "slimphony-demo"),
    ("D:/repos/slimphony-demo-data", "slimphony-demo"),
    ("D:/repos/slimphony-data/261109-beam-check", "slimphony"),
    ("D:/repos/slimphony-x-data/261109-calibration-data", "slimphony-x"),
    ("D:/data/someone/261103", ""),
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


# --- follow-ups to the split (lab record, task 160) --------------------------------------


@pytest.mark.parametrize("written", ['"{}"', "'{}'", "  {}  ", '" {} " '])
def test_a_quoted_or_padded_variable_names_the_directory_it_meant(tmp_path, monkeypatch,
                                                                written):
    root = instrument_tree(tmp_path / "slimphony")
    monkeypatch.setenv(clockwork.INSTRUMENT_ENV, written.format(root))
    assert clockwork.instrument_dir() == root
    assert clockwork.instrument_unresolved() == ""


def test_a_variable_that_names_nothing_is_said_by_name_and_by_value(tmp_path, monkeypatch):
    missing = str(tmp_path / "typo")
    monkeypatch.setenv(clockwork.INSTRUMENT_ENV, missing)
    said = clockwork.instrument_unresolved(when_unset=False)
    assert clockwork.INSTRUMENT_ENV in said and repr(missing) in said
    assert "is not a folder" in said
    (tmp_path / "typo").mkdir()
    assert f"holds no {clockwork.INSTRUMENT_FILE}" in clockwork.instrument_unresolved()


def test_no_instrument_at_all_is_said_only_where_it_is_asked_for(tmp_path, monkeypatch):
    monkeypatch.setattr(clockwork, "ROOT", str(tmp_path / "clockwork"))
    monkeypatch.delenv(clockwork.INSTRUMENT_ENV)
    assert clockwork.instrument_unresolved(when_unset=False) == ""
    assert clockwork.INSTRUMENT_ENV in clockwork.instrument_unresolved()
    assert clockwork.INSTRUMENT_ENV in envelope.NO_LIMITS
    toolbox = Toolbox(object(), library="", output=str(tmp_path / "runs"))
    with pytest.raises(ToolFailure, match=clockwork.INSTRUMENT_ENV):
        toolbox.call("render_template", {"template": "line-b.toml"})
    assert clockwork.INSTRUMENT_ENV in toolbox.call("list_routines")["problem"]


def test_the_daemon_logs_a_variable_that_names_nothing_whatever_library_it_was_given(
        tmp_path, monkeypatch):
    monkeypatch.setenv(clockwork.INSTRUMENT_ENV, str(tmp_path / "typo"))
    for library in ("", str(tmp_path / "a-library")):
        made = Daemon(tmp_path / "runs", library=library)
        try:
            logged = made.log.getvalue()
        finally:
            made.stop()
        assert clockwork.INSTRUMENT_ENV in logged and "typo" in logged


def test_the_daemon_says_no_instrument_only_when_it_has_no_library(tmp_path, monkeypatch):
    monkeypatch.setattr(clockwork, "ROOT", str(tmp_path / "clockwork"))
    monkeypatch.delenv(clockwork.INSTRUMENT_ENV)
    for library, said in (("", True), (str(tmp_path / "a-library"), False)):
        made = Daemon(tmp_path / "runs", library=library)
        try:
            logged = made.log.getvalue()
        finally:
            made.stop()
        assert (clockwork.INSTRUMENT_ENV in logged) is said


def test_a_verb_says_a_variable_that_names_nothing_and_a_rehearsal_does_not(
        tmp_path, monkeypatch, capsys):
    """Against the instrument the verb looked for the document and found none; a
    rehearsal's simulated boxes never look."""
    from clockwork.app import main
    from clockwork.mips import Discovery

    monkeypatch.setenv(clockwork.INSTRUMENT_ENV, str(tmp_path / "typo"))
    for options, said in (({}, False),
                          ({"fake": False, "discover": lambda *_, **__: Discovery(()),
                            "clear_port": False, "start_console": False}, True)):
        made = Daemon(tmp_path / "runs", **options)
        try:
            capsys.readouterr()
            assert main(["list-templates", "--endpoint", made.endpoint]) == 0
            err = capsys.readouterr().err
        finally:
            made.stop()
        assert (clockwork.INSTRUMENT_ENV in err) is said, err


def test_the_mcp_server_leaves_the_instrument_document_to_the_instrument(
        tmp_path, monkeypatch):
    """Over `clockwork serve --fake` the server is a rehearsal too: the instrument's own
    document and the limits beside it are not what its simulated boxes run under."""
    root = instrument_tree(tmp_path / "slimphony")
    (tmp_path / "slimphony" / envelope.LIMITS_NAME).write_text(
        limits_text(os.path.join(root, "golden")), encoding="utf-8")
    monkeypatch.setenv(clockwork.INSTRUMENT_ENV, root)
    built: dict[str, object] = {}

    class Served:
        def run(self, transport: str) -> None:
            built["transport"] = transport

    def build(owner, library, output, **given):
        built.update(given, library=library)
        return Served()

    monkeypatch.setattr(server, "build_server", build)
    made = Daemon(tmp_path / "runs")
    try:
        said = io.StringIO()
        assert server.run(endpoint=made.endpoint, stream=said) == 0
    finally:
        made.stop()
    assert built["transport"] == "stdio"
    assert built["instrument"] is server.SIMULATED and built["instrument_path"] == ""
    assert built["limits"] is None and "limits (none)" in said.getvalue()
    assert built["library"] == os.path.join(root, "golden")


# --- where a rendering lands ---------------------------------------------------------------


def render_to(toolbox: Toolbox, to: str, *, template: str = "line-b.toml",
              overwrite: bool = False) -> str:
    return toolbox.call("render_template", {"template": template, "labels": LABELS,
                                            "to": to, "overwrite": overwrite})["written"]


def test_a_relative_render_is_never_written_into_the_instruments_library(
        two_libraries, tmp_path):
    """Served alone, which is the default, or named last: the instrument's library is
    kept by its people, and a session's render goes to a path it names in full."""
    golden, templates, _ = two_libraries
    before = sorted(os.listdir(golden))
    for setting in (golden, os.pathsep.join([templates, golden])):
        toolbox = Toolbox(object(), library=setting, output=str(tmp_path / "runs"))
        with pytest.raises(ToolFailure, match="instrument's own") as refused:
            render_to(toolbox, "rendered/b-default")
        assert "absolute path" in str(refused.value)
        assert sorted(os.listdir(golden)) == before
    # In full, it is written where it says, the instrument's library included.
    toolbox = Toolbox(object(), library=golden, output=str(tmp_path / "runs"))
    target = str(tmp_path / "task" / "b-default")
    assert render_to(toolbox, target) == target + ".toml"
    assert os.path.isfile(target + ".toml")


def test_a_library_that_is_not_an_instruments_takes_a_relative_render(tmp_path):
    """A folder of methods with no instrument document above it is nobody's but the
    person's who named it."""
    library = tmp_path / "library"
    library.mkdir()
    (library / "line-b.toml").write_text(TEMPLATE, encoding="utf-8")
    toolbox = Toolbox(object(), library=str(library), output=str(tmp_path / "runs"))
    assert render_to(toolbox, "rendered/b") == str(library / "rendered" / "b.toml")


def test_a_render_neither_replaces_nor_shadows_a_file_in_an_earlier_library(
        two_libraries, tmp_path):
    golden, templates, setting = two_libraries
    toolbox = Toolbox(object(), library=setting, output=str(tmp_path / "runs"))
    original = os.path.join(golden, "line-b.toml")
    # Without the suffix it is the same file, and is looked up as the file it would be.
    for to in ("line-b.toml", "line-b"):
        for overwrite in (False, True):
            with pytest.raises(ToolFailure, match="another library directory") as refused:
                render_to(toolbox, to, template="kv/template.toml", overwrite=overwrite)
            assert original in str(refused.value)
    with open(original, encoding="utf-8") as handle:
        assert handle.read() == TEMPLATE
    assert not os.path.exists(os.path.join(templates, "line-b.toml"))
    # The last directory's own file is replaced when asked, and only then.
    written = render_to(toolbox, "kv/out")
    with pytest.raises(ToolFailure, match="already exists"):
        render_to(toolbox, "kv/out")
    assert render_to(toolbox, "kv/out", overwrite=True) == written


def test_a_mistyped_last_library_is_refused_and_not_made(two_libraries, tmp_path):
    golden, templates, _ = two_libraries
    typo = templates + "-typo"
    toolbox = Toolbox(object(), library=os.pathsep.join([golden, typo]),
                      output=str(tmp_path / "runs"))
    with pytest.raises(ToolFailure, match="does not exist"):
        render_to(toolbox, "x")
    assert not os.path.exists(typo)


def test_routines_that_are_not_there_say_where_they_were_looked_for(two_libraries, tmp_path):
    """A project's templates named first point at a `routines` the project does not
    have; the answer names the folder and which library it was beside."""
    golden, templates, _ = two_libraries
    toolbox = Toolbox(object(), library=os.pathsep.join([templates, golden]),
                      output=str(tmp_path / "runs"))
    looked = os.path.join(os.path.dirname(templates), "routines")
    listed = toolbox.call("list_routines")
    assert listed["routines"] == [] and looked in listed["problem"]
    assert templates in listed["problem"] and "first" in listed["problem"]
    with pytest.raises(ToolFailure, match="no routine called 'beam-check'") as refused:
        toolbox._routine("beam-check")
    assert looked in str(refused.value)
    # The instrument's library first finds them, and says nothing.
    toolbox = Toolbox(object(), library=os.pathsep.join([golden, templates]),
                      output=str(tmp_path / "runs"))
    assert "problem" not in toolbox.call("list_routines")


def test_a_document_that_is_not_in_the_library_is_one_sentence(two_libraries, tmp_path):
    golden, templates, setting = two_libraries
    toolbox = Toolbox(object(), library=setting, output=str(tmp_path / "runs"))
    for name, arguments in (("render_template", {"template": "no-such.toml"}),
                            ("validate_method", {"method": "no-such.toml"}),
                            ("load_method", {"method": "no-such.toml"})):
        with pytest.raises(ToolFailure, match="there is no (template|method) 'no-such.toml'") \
                as refused:
            toolbox.call(name, arguments)
        assert golden in str(refused.value) and templates in str(refused.value)
        assert "Errno" not in str(refused.value)


# --- duplicates ----------------------------------------------------------------------------


def test_list_methods_marks_a_path_two_libraries_hold(two_libraries, tmp_path):
    golden, templates, setting = two_libraries
    toolbox = Toolbox(object(), library=setting, output=str(tmp_path / "runs"))
    first = render_to(Toolbox(object(), library=templates, output=str(tmp_path / "runs")),
                      "copy", template=os.path.join(golden, "line-b.toml"))
    second = os.path.join(golden, "copy.toml")
    with open(first, encoding="utf-8") as source, \
            open(second, "w", encoding="utf-8") as target:
        target.write(source.read())
    listed = [entry for entry in toolbox.call("list_methods")["methods"]
              if entry["path"] == "copy.toml"]
    assert sorted(entry["library"] for entry in listed) == sorted([golden, templates])
    assert all(entry["duplicate"] == [second, first] for entry in listed)
    with pytest.raises(ToolFailure, match="more than one library"):
        toolbox.call("load_method", {"method": "copy.toml"})


def test_a_duplicate_is_listed_however_its_name_is_cased(two_libraries, tmp_path):
    """The lookup that refuses it is the file system's, which on Windows ignores case;
    the listing agrees with the refusal."""
    golden, templates, setting = two_libraries
    if not os.path.exists(os.path.join(golden, "LINE-B.toml")):
        pytest.skip("this file system tells line-b.toml from LINE-B.toml")
    copy = os.path.join(templates, "LINE-B.toml")
    with open(copy, "w", encoding="utf-8") as handle:
        handle.write(TEMPLATE)
    toolbox = Toolbox(object(), library=setting, output=str(tmp_path / "runs"))
    listed = {entry["path"]: entry for entry in toolbox.call("list_templates")["templates"]}
    both = [os.path.join(golden, "line-b.toml"), copy]
    assert listed["line-b.toml"]["duplicate"] == both
    assert listed["LINE-B.toml"]["duplicate"] == both
    assert "duplicate" not in listed["kv/template.toml"]


def test_one_directory_named_two_ways_is_one_library(tmp_path):
    """A long name and its 8.3 short name are one folder; as two libraries every
    document in it would be its own duplicate."""
    if sys.platform != "win32":
        pytest.skip("8.3 short names are Windows's")
    long = tmp_path / "a library with a long name"
    long.mkdir()
    buffer = ctypes.create_unicode_buffer(1024)
    ctypes.windll.kernel32.GetShortPathNameW(str(long), buffer, 1024)
    short = buffer.value
    if not short or os.path.normcase(short) == os.path.normcase(str(long)):
        pytest.skip("this volume makes no short names")
    assert clockwork.library_dirs(os.pathsep.join([str(long), short])) == (str(long),)


# --- the limits ----------------------------------------------------------------------------


def test_status_tells_a_template_not_served_from_limits_that_cannot_be_applied(
        two_libraries, tmp_path, fake_owner):  # noqa: F811
    """One instrument's limits name several projects' templates and a daemon serves one
    project's: the others' entries are not problems, and a real one must not hide
    among them."""
    golden, _, setting = two_libraries
    elsewhere = ('[templates.another-projects]\nhash = "0123456789ab"\n')
    path = tmp_path / "limits.toml"
    path.write_text(limits_text(golden) + elsewhere, encoding="utf-8")
    toolbox = Toolbox(fake_owner, library=setting, output=str(tmp_path / "runs"),
                      limits=envelope.load(str(path)))
    found = toolbox.call("status")["limits"]
    assert found["problems"] == [] and found["not_served"] == ["another-projects"]
    # A bound wider than the template's own is a problem, and is the only one listed.
    path.write_text(limits_text(golden).replace("max = 450", "max = 100000") + elsewhere,
                    encoding="utf-8")
    toolbox = Toolbox(fake_owner, library=setting, output=str(tmp_path / "runs"),
                      limits=envelope.load(str(path)))
    found = toolbox.call("status")["limits"]
    assert len(found["problems"]) == 1 and "never widen" in found["problems"][0]
    assert found["not_served"] == ["another-projects"]
    # `against` still answers both for a caller that means the whole library.
    assert len(toolbox.limits.against(toolbox._templates())) == 2


def test_an_unlisted_template_is_told_who_admits_it(two_libraries, tmp_path,
                                                    fake_owner):  # noqa: F811
    golden, _, setting = two_libraries
    path = tmp_path / "limits.toml"
    path.write_text(limits_text(golden), encoding="utf-8")
    toolbox = Toolbox(fake_owner, library=setting, output=str(tmp_path / "runs"),
                      limits=envelope.load(str(path)))
    [line] = toolbox.call("validate_method", {"template": "kv/template.toml",
                                              "labels": LABELS})["limits"]
    assert "not in the standing limits" in line and str(path) in line
    assert "[templates] entry" in line and "never by a session" in line


# --- the help ------------------------------------------------------------------------------


def test_the_help_says_the_variable_is_not_a_first_try(capsys):
    """Set, the variable is the only place looked; "then ../slimphony" read as a
    fallback it does not have."""
    parser = argparse.ArgumentParser()
    cli.add_verbs(parser.add_subparsers())
    with pytest.raises(SystemExit):
        parser.parse_args(["status", "--help"])
    said = " ".join(capsys.readouterr().out.split())
    assert "$CLOCKWORK_INSTRUMENT when that is set" in said
    assert "then in ../slimphony" not in said and "or ../slimphony" not in said
