"""Pane text to a schema-2 method and back, and comments as strings.

The two golden trainee files are the fixtures that matter here: everything else
is a case they do not cover. They are lab material, so every test that reads one
skips in a public clone.
"""

import os

import pytest

import clockwork
from clockwork import method as method_module
from clockwork.method import BoxMethod, RfChannel, Step, is_comment
from clockwork.method import text as pane_text

LABELS = {"MIPS A": "auklet", "MIPS B": "bufflehead", "MIPS C": "cormorant"}
"""The trainee files' box labels, from the lab record's instrument map.

Passed in rather than built in: which letter is which box is a fact about one
lab's instrument, and this package holds none.
"""

TRAINEE_FILES = {
    "detection-response": "Tables_DetectionResponse.txt",
    "bradykinin-clock": "Tables_BradykininCLOCK.txt",
}


def golden(name: str, filename: str) -> str:
    directory = clockwork.lab_dir("golden")
    if directory is None:
        pytest.skip("the golden experiments are lab material and this is a public clone")
    path = os.path.join(directory, name, filename)
    if not os.path.isfile(path):
        pytest.skip(f"no golden file at {name}/{filename}")
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def golden_method(name: str):
    return method_module.loads(golden(name, "method.toml"))


def commands(strings):
    """A phase's strings with the comments taken out, which is what goes on a wire."""
    return [string for string in strings if not is_comment(string)]


# --- comments as strings (task 48 step 1) ---------------------------------------------


@pytest.mark.parametrize("string,expected", [
    ("# a note", True),
    ("#no space", True),
    ("   # indented", True),
    ("\t#after a tab", True),
    ("##", True),
    ("#", True),
    ("SWFREQ,1,15000", False),
    ("STBLDAT;0:A:1[A:1,20:];", False),
    ("SARBCTBL,J10[HRsm1CD12m1ND4.0272r]100", False),
    ("Ion Mobility Scans = 5000 #number of pushes in frame", False),
    ("TARBTRG #sent to MIPS B", False),
])
def test_is_comment_is_the_first_non_blank_character(string, expected):
    assert is_comment(string) is expected


def test_a_comment_loads_as_a_string_in_the_phase_it_precedes():
    loaded = method_module.from_dict({
        "schema_version": 2,
        "start": [["box1", "TBLSTRT"]],
        "metadata": {"name": "m", "created": _date(), "description": ""},
        "acquisition": {"frames": 1, "scans": 10, "accumulations": 1,
                        "file_stem": "m"},
        "boxes": [{"name": "box1", "port": "COM3",
                   "setup": ["# the travelling wave", "SWFREQ,1,15000"],
                   "load": ["# the table", "STBLDAT;0:A:1[A:1,10:];"],
                   "arm": ["SMOD,TBL"]}],
    })
    box = loaded.box("box1")
    assert box.setup == ("# the travelling wave", "SWFREQ,1,15000")
    assert box.load == ("# the table", "STBLDAT;0:A:1[A:1,10:];")
    assert loaded.warnings == ()


def test_a_comments_indentation_is_stripped_without_a_warning():
    """The warning exists because invisible whitespace reaches a box. A comment's
    does not, so repairing it silently is the whole of what is owed."""
    loaded = method_module.from_dict(_document(setup=["   # indented", "  SWFREQ,1,1  "]))
    assert loaded.box("box1").setup == ("# indented", "SWFREQ,1,1")
    assert len(loaded.warnings) == 1
    assert "SWFREQ" in loaded.warnings[0]


def test_a_load_phase_of_only_comments_is_not_something_to_load():
    with pytest.raises(method_module.MethodError) as raised:
        method_module.from_dict(_document(load=["# nothing here"]))
    assert any("(a comment is not one)" in problem
               for problem in raised.value.problems)


def test_a_start_list_of_only_comments_starts_nothing():
    with pytest.raises(method_module.MethodError) as raised:
        method_module.from_dict(_document(start=[["box1", "# about to start"]]))
    assert any("a comment starts nothing" in problem for problem in raised.value.problems)


def test_a_comment_is_hashed_into_the_stamp():
    """A comment is the trainee's record of what they meant, so two methods that
    differ only in one are two different methods."""
    plain = method_module.from_dict(_document(setup=["SWFREQ,1,1"]))
    noted = method_module.from_dict(_document(setup=["# the wave", "SWFREQ,1,1"]))
    assert method_module.stamp(plain)["method_hash"] \
        != method_module.stamp(noted)["method_hash"]


def test_a_method_carrying_comments_round_trips_through_toml():
    loaded = method_module.from_dict(_document(setup=["# the wave", "SWFREQ,1,1"]))
    assert method_module.loads(method_module.dumps(loaded)) == loaded


def _date():
    import datetime

    return datetime.date(2026, 9, 16)


def _document(*, setup=("SWFREQ,1,1",), load=("STBLDAT;0:A:1[A:1,10:];",),
              arm=("SMOD,TBL",), start=(["box1", "TBLSTRT"],)):
    return {
        "schema_version": 2,
        "start": [list(step) for step in start],
        "metadata": {"name": "m", "created": _date(), "description": ""},
        "acquisition": {"frames": 1, "scans": 10, "accumulations": 1, "file_stem": "m"},
        "boxes": [{"name": "box1", "port": "COM3", "setup": list(setup),
                   "load": list(load), "arm": list(arm)}],
    }


# --- the classifier (task 48 step 2) --------------------------------------------------


@pytest.mark.parametrize("command,phase", [
    ("STBLDAT;0:A:1[A:1,20000:];", "load"),
    ("SARBCTBL,J10[HRsm1CD12m1ND4.0272r]100", "load"),
    ("SMOD,TBL", "arm"),
    ("smod,tbl", "arm"),
    ("SMOD,ONCE", "arm"),
    ("SMOD,5", "arm"),
    ("SMOD,LOC", "setup"),
    ("TBLSTRT", "start"),
    ("TARBTRG", "start"),
    ("SWFREQ,1,15000", "setup"),
    ("ARBSYNC", "setup"),
    ("SDCB,15,0.00", "setup"),
    ("SNEVERHEARDOFIT,1", "setup"),
    ("# a note", "comment"),
])
def test_classify_reads_the_command_word(command, phase):
    assert pane_text.classify(command) == phase


@pytest.mark.parametrize("line,expected", [
    ("SWFREQ,1,15000", True),
    ("ARBSYNC", True),
    ("STBLDAT;0:A:1[A:1,20:];", True),
    ("SARBCTBL,J10[HRsm1CD12m1ND4.0272r]100\t", True),
    ("Ion Mobility Scans = 5000 #number of pushes in frame", False),
    ("Accumulations = 100 #number of repeats", False),
    ("File name = YYMMDD_ION_xxx #sets the name", False),
    ("These commands were used to acquire 260904_BK_094.uimf, which was", False),
    ("TARBTRG #sent to MIPS B. Executes the compression table.", False),
    ("# a note", False),
    ("", False),
])
def test_is_command_like_keeps_prose_off_the_wire(line, expected):
    assert pane_text.is_command_like(line) is expected


def test_prose_in_a_pane_is_unplaced_rather_than_sent_as_setup():
    """The rule that an unknown word is `setup` would otherwise send a trainee's
    note about FALKOR's settings to a box."""
    result = pane_text.parse_pane(
        "SWFREQ,1,15000\nIon Mobility Scans = 5000\nSMOD,TBL", "box1")
    assert result.setup == ("SWFREQ,1,15000",)
    assert result.arm == ("SMOD,TBL",)
    assert [line.text for line in result.unplaced] == ["Ion Mobility Scans = 5000"]


def test_whitespace_is_stripped_and_reported_by_line():
    result = pane_text.parse_pane("SWFREQ,1,15000  \n\tSMOD,TBL", "box1")
    assert result.setup == ("SWFREQ,1,15000",)
    assert result.arm == ("SMOD,TBL",)
    assert len(result.warnings) == 2
    assert result.warnings[0].startswith("line 1: stripped")
    assert result.warnings[1].startswith("line 2: stripped")


def test_every_line_gets_a_tag_for_the_margin():
    result = pane_text.parse_pane(
        "# the wave\nSWFREQ,1,15000\n\nSTBLDAT;0:A:1[A:1,10:];\nwhat is this", "box1")
    assert [(line.number, line.tag, line.phase) for line in result.lines] == [
        (1, "comment", "setup"), (2, "setup", "setup"), (3, "blank", ""),
        (4, "load", "load"), (5, "unplaced", ""),
    ]
    assert [line.sent for line in result.lines] == [False, True, False, True, False]


def test_a_comment_labels_the_strings_under_it_not_the_ones_above():
    result = pane_text.parse_pane(
        "SWFREQ,1,15000\n\n# the table\nSTBLDAT;0:A:1[A:1,10:];", "box1")
    assert result.setup == ("SWFREQ,1,15000",)
    assert result.load == ("# the table", "STBLDAT;0:A:1[A:1,10:];")


def test_a_trailing_comment_keeps_company_with_what_it_follows():
    result = pane_text.parse_pane(
        "STBLDAT;0:A:1[A:1,10:];\n\n# relevant files: 260904_BK_094.uimf", "box1")
    assert result.load == ("STBLDAT;0:A:1[A:1,10:];", "# relevant files: 260904_BK_094.uimf")


# --- the reset rule and the manual tag (task 48 step 3) -------------------------------


def test_an_untagged_smod_loc_after_the_start_is_the_reset_group():
    result = pane_text.parse_pane(
        "STBLDAT;0:A:1[A:1,10:];\n\nSMOD,TBL\n\nTBLSTRT\n\nSMOD,LOC", "box1")
    assert result.arm == ("SMOD,TBL",)
    assert [step.command for step in result.reset] == ["SMOD,LOC", "SMOD,TBL"]


def test_an_smod_loc_before_the_start_is_an_ordinary_setup_string():
    """Which is what it is in every other context: the guard `send_phases` puts in
    front of a LOC-only command is the same string and resets nothing."""
    result = pane_text.parse_pane(
        "SMOD,LOC\nSTBLCLK,EXT\n\nSTBLDAT;0:A:1[A:1,10:];\n\nSMOD,TBL\n\nTBLSTRT", "box1")
    assert result.setup == ("SMOD,LOC", "STBLCLK,EXT")
    assert result.reset == ()


def test_a_reset_that_already_arms_the_box_is_not_completed_twice():
    result = pane_text.parse_pane(
        "STBLDAT;0:A:1[A:1,10:];\n\nSMOD,TBL\n\nTBLSTRT\n\nSMOD,LOC\nSMOD,TBL", "box1")
    assert [step.command for step in result.reset] == ["SMOD,LOC", "SMOD,TBL"]


def test_a_box_with_nothing_to_arm_gets_no_completion():
    result = pane_text.parse_pane(
        "SARBCTBL,J10[HRr]1\n\nTARBTRG\n\n# clockwork: reset\nSMOD,LOC", "box1")
    assert [step.command for step in result.reset] == ["SMOD,LOC"]


def test_the_tag_forces_a_group_and_is_not_kept_as_a_string():
    result = pane_text.parse_pane(
        "STBLDAT;0:A:1[A:1,10:];\n\n# clockwork: setup\nSARBCTBL,J10[HRr]1", "box1")
    assert result.setup == ("SARBCTBL,J10[HRr]1",)
    assert result.load == ("STBLDAT;0:A:1[A:1,10:];",)
    assert [line.tag for line in result.lines] == ["load", "blank", "directive", "setup"]
    assert [line.phase for line in result.lines] == ["load", "", "", "setup"]


@pytest.mark.parametrize("written", [
    "# clockwork: reset", "#clockwork:reset", "#  CLOCKWORK : Reset  ",
])
def test_the_tag_is_written_the_way_a_trainee_would_write_it(written):
    result = pane_text.parse_pane(
        f"STBLDAT;0:A:1[A:1,10:];\n\nTBLSTRT\n\n{written}\nSMOD,LOC", "box1")
    assert [step.command for step in result.reset] == ["SMOD,LOC"]


def test_a_tag_naming_no_phase_stays_an_ordinary_comment():
    """A note, not an error: the window design's decision 8 (lab record, task 50)
    says a line the classifier cannot place gets a manual tag and never a refusal."""
    result = pane_text.parse_pane("# clockwork: someday\nSWFREQ,1,15000", "box1")
    assert result.setup == ("# clockwork: someday", "SWFREQ,1,15000")


# --- the renderer (task 48 step 4) ----------------------------------------------------


def test_render_pane_writes_the_phases_in_order_with_groups_between():
    box = BoxMethod(name="box1", port="COM3",
                    setup=("# the wave", "SWFREQ,1,15000"),
                    load=("STBLDAT;0:A:1[A:1,10:];",), arm=("SMOD,TBL",))
    rendered = pane_text.render_pane(
        box, (Step("box1", "TBLSTRT"),),
        (Step("box1", "SMOD,LOC"), Step("box1", "SMOD,TBL")))
    assert rendered == (
        "# the wave\n"
        "SWFREQ,1,15000\n"
        "\n"
        "STBLDAT;0:A:1[A:1,10:];\n"
        "\n"
        "SMOD,TBL\n"
        "\n"
        "TBLSTRT\n"
        "\n"
        "# clockwork: reset\n"
        "SMOD,LOC\n"
        "SMOD,TBL"
    )


def test_render_pane_gives_another_boxs_steps_to_that_box():
    box = BoxMethod(name="box1", port="COM3", load=("STBLDAT;0:A:1[A:1,10:];",))
    rendered = pane_text.render_pane(
        box, (Step("box2", "TARBTRG"), Step("box1", "TBLSTRT")))
    assert rendered.splitlines() == ["STBLDAT;0:A:1[A:1,10:];", "", "TBLSTRT"]


def test_declared_dc_bias_and_rf_render_as_nothing():
    """They are fields the window edits in a form, not strings a trainee types."""
    box = BoxMethod(name="box1", port="COM3", load=("STBLDAT;0:A:1[A:1,10:];",),
                    dc_bias=((15, 0.0), (16, 5.0)),
                    rf=(RfChannel(1, frequency_hz=943000, drive_pct=50.0),))
    assert pane_text.render_pane(box) == "STBLDAT;0:A:1[A:1,10:];"


@pytest.mark.parametrize("name", list(TRAINEE_FILES))
def test_a_golden_method_round_trips_through_its_pane_text(name):
    loaded = golden_method(name)
    for box in loaded.boxes:
        rendered = pane_text.render_pane(box, loaded.start, loaded.reset)
        back = pane_text.parse_pane(rendered, box.name)
        assert back.setup == box.setup, box.name
        assert back.load == box.load, box.name
        assert back.arm == box.arm, box.name
        assert back.start == tuple(s for s in loaded.start if s.box == box.name), box.name
        assert back.reset == tuple(s for s in loaded.reset if s.box == box.name), box.name


@pytest.mark.parametrize("name", list(TRAINEE_FILES))
def test_a_golden_methods_panes_rebuild_it(name):
    """The whole loop the window runs on Open and Save: TOML to panes to TOML."""
    loaded = golden_method(name)
    panes = {box.name: pane_text.parse_pane(
        pane_text.render_pane(box, loaded.start, loaded.reset), box.name)
        for box in loaded.boxes}
    rebuilt = method_module.from_dict({
        **method_module.to_dict(loaded),
        "start": [[s.box, s.command] for s in pane_text.start_order(panes)],
        "reset": [[s.box, s.command] for box in loaded.boxes
                  for s in panes[box.name].reset],
        "boxes": [
            {**{key: value for key, value in raw.items()
                if key not in ("setup", "load", "arm")},
             "setup": list(panes[raw["name"]].setup),
             "load": list(panes[raw["name"]].load),
             "arm": list(panes[raw["name"]].arm)}
            for raw in method_module.to_dict(loaded)["boxes"]
        ],
    })
    assert rebuilt == loaded
    assert method_module.stamp(rebuilt)["method_hash"] \
        == method_module.stamp(loaded)["method_hash"]


# --- the cross-box start order (task 48 step 5) ---------------------------------------


@pytest.mark.parametrize("name", list(TRAINEE_FILES))
def test_start_order_equals_the_golden_start_list(name):
    loaded = golden_method(name)
    panes = {box.name: pane_text.parse_pane(
        pane_text.render_pane(box, loaded.start, loaded.reset), box.name)
        for box in loaded.boxes}
    assert pane_text.start_order(panes) == loaded.start


def test_start_order_puts_every_compression_table_before_the_sequencer():
    """Box order within each command word, `TARBTRG` before `TBLSTRT` across them,
    whatever order the panes happen to be in."""
    panes = {
        name: pane_text.parse_pane(text, name)
        for name, text in (("box1", "TBLSTRT"), ("box2", "TARBTRG"),
                           ("box3", "TARBTRG"))
    }
    assert pane_text.start_order(panes) == (
        Step("box2", "TARBTRG"), Step("box3", "TARBTRG"), Step("box1", "TBLSTRT"))


def test_start_order_carries_each_commands_own_comments():
    panes = {
        "box1": pane_text.parse_pane("# last, it starts everything\nTBLSTRT", "box1"),
        "box2": pane_text.parse_pane("# first, it must be waiting\nTARBTRG", "box2"),
    }
    assert [step.command for step in pane_text.start_order(panes)] == [
        "# first, it must be waiting", "TARBTRG",
        "# last, it starts everything", "TBLSTRT",
    ]


# --- the legacy import (task 48 step 6) -----------------------------------------------


def test_the_clock_trainee_file_splits_into_its_three_panes():
    """The one multi-box paste file that names its boxes. Every phase equals the
    transcription's except where the transcription declares what the trainee file
    does not say, which is stated here rather than hidden:

    - AUKLET's `STBLCLK,EXT` and `STBLTRG,SW` appear in no trainee file and were
      added on 2026-09-15 and 2026-09-16 (lab record, tasks 41 and 42).
    - the travelling-wave frequency and amplitude block is given once, unlabelled,
      for two boxes, so the split leaves it unattributed rather than guessing.
    """
    text = golden("bradykinin-clock", TRAINEE_FILES["bradykinin-clock"])
    loaded = golden_method("bradykinin-clock")
    panes = pane_text.split_trainee_file(text, LABELS)
    assert set(panes) == {None, "auklet", "bufflehead", "cormorant"}

    parsed = {box.name: pane_text.parse_pane(panes[box.name], box.name)
              for box in loaded.boxes}
    undeclared = ["STBLCLK,EXT", "STBLTRG,SW"]
    common = [f"SWFREQ,{channel},15000" for channel in (1, 2, 3, 4)] \
        + [f"SWFVRNG,{channel},15" for channel in (1, 2, 3, 4)]
    for box in loaded.boxes:
        missing = undeclared if box.name == "auklet" else common
        assert commands(parsed[box.name].setup) \
            == [string for string in box.setup if string not in missing], box.name
        assert commands(parsed[box.name].load) == list(box.load), box.name
        assert commands(parsed[box.name].arm) == list(box.arm), box.name

    assert commands(pane_text.parse_pane(panes[None], "?").setup) == common
    assert tuple(step for step in pane_text.start_order(parsed)
                 if not is_comment(step.command)) == loaded.start
    assert tuple(step for name in parsed for step in parsed[name].reset
                 if not is_comment(step.command)) == loaded.reset


def test_the_clock_files_falkor_settings_are_unplaced_not_sent():
    text = golden("bradykinin-clock", TRAINEE_FILES["bradykinin-clock"])
    panes = pane_text.split_trainee_file(text, LABELS)
    unplaced = [line.text for line in pane_text.parse_pane(panes[None], "?").unplaced]
    assert [line.split(" #")[0] for line in unplaced] == [
        "Ion Mobility Scans = 5000",
        "Accumulations = 100",
        "File name = YYMMDD_ION_xxx",
    ]


def test_a_commands_trailing_comment_moves_onto_its_own_line():
    split = pane_text.split_trainee_file(
        "#Then, the following are sent to MIPS to run the experiment.\n"
        "TARBTRG #sent to MIPS B. Executes the compression table.\n"
        "TBLSTRT #sent to MIPS A. Executes the pulse sequence.\n",
        LABELS,
    )
    assert split["bufflehead"].splitlines()[-1] == "TARBTRG"
    assert split["auklet"].splitlines()[-1] == "TBLSTRT"
    assert "Executes the compression table." in split["bufflehead"]


def test_a_file_that_names_no_box_is_left_whole_for_the_clipboard():
    """The detection-response file attributes nothing, which is the honest answer
    rather than a guess: it is one experiment's strings for three boxes with no
    box named anywhere in it."""
    text = golden("detection-response", TRAINEE_FILES["detection-response"])
    panes = pane_text.split_trainee_file(text, LABELS)
    assert list(panes) == [None]


def test_the_detection_response_lines_classify_as_the_transcription_files_them():
    """The file names no box, so this is the check that can be made of it: every
    command line lands in the phase the transcription puts it in."""
    text = golden("detection-response", TRAINEE_FILES["detection-response"])
    loaded = golden_method("detection-response")
    declared = {
        string: phase
        for box in loaded.boxes
        for phase in ("setup", "load", "arm")
        for string in getattr(box, phase)
    }
    declared.update({step.command: "start" for step in loaded.start})
    seen = 0
    for line in pane_text.parse_pane(text, "?").lines:
        if not line.sent:
            continue
        assert line.text in declared, line.text
        assert line.tag == declared[line.text], line.text
        seen += 1
    assert seen == 11


def test_splitting_without_a_map_keeps_the_labels_the_file_used():
    split = pane_text.split_trainee_file(
        "#Pulse sequence. Send to MIPS A.\nSTBLDAT;0:A:1[A:1,10:];\n")
    assert list(split) == ["MIPS A"]
