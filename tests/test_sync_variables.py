import importlib.util
import pathlib
import pytest

_MOD = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "sync_variables.py"
_spec = importlib.util.spec_from_file_location("sync_variables", _MOD)
sv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sv)


def _vals(text):
    return sv.parse_effective(text)["gcode_macro _U"]


def test_hash_truncates_unconditionally_even_inside_quotes():
    assert _vals('[gcode_macro _U]\nvariable_x: "rack#2"\ngcode:\n')["variable_x"] == '"rack'


def test_semicolon_requires_preceding_whitespace():
    v = _vals('[gcode_macro _U]\nvariable_a: 5;x\nvariable_b: 5 ;x\ngcode:\n')
    assert v["variable_a"] == "5;x"
    assert v["variable_b"] == "5"


def test_blank_line_does_not_end_a_multiline_value():
    text = "[gcode_macro _U]\nvariable_m: {\n    'A': 1,\n\n    'B': 2\n    }\ngcode:\n"
    assert _vals(text)["variable_m"] == "{\n'A': 1,\n\n'B': 2\n}"


def test_option_names_are_case_folded_and_last_wins():
    v = _vals('[gcode_macro _U]\nvariable_L: 1\nvariable_l: 2\ngcode:\n')
    assert v["variable_l"] == "2"


def test_crlf_is_normalised_and_bom_is_rejected():
    assert sv.decode(b"[s]\r\na: 1\r\n") == "[s]\na: 1\n"
    with pytest.raises(sv.SyncError, match="byte order mark"):
        sv.decode(b"\xef\xbb\xbf[s]\n")
    with pytest.raises(sv.SyncError, match="not valid UTF-8"):
        sv.decode(b"\xff\xfe[s]\n")


def test_literal_check_matches_gcode_macro_and_exempts_gcode():
    sv.check_literal("variable_x", "5")
    sv.check_literal("variable_p", '"bed_soak", "extruder_preheating"')
    sv.check_literal("gcode", "")          # exempt: literal_eval("") would raise
    with pytest.raises(sv.SyncError, match="not a valid literal"):
        sv.check_literal("variable_x", '"rack')


def _opts(text, section="gcode_macro _U"):
    doc = sv.scan_document(text)
    sect = [s for s in doc.sections if s.name == section][0]
    return {o.name: o for o in sect.options}


def test_line_kind_classifies_before_indentation():
    assert sv.line_kind("  ; note") == "semi"
    assert sv.line_kind("  # note") == "blank"     # '#' truncation leaves whitespace
    assert sv.line_kind("") == "blank"
    assert sv.line_kind("variable_x: 1") == "content"
    assert sv.line_kind("x: 1 ; trailing") == "content"


def test_equally_indented_keys_are_separate_options():
    o = _opts("[gcode_macro _U]\n  variable_a: 1\n  variable_b: 2\ngcode:\n")
    assert set(o) == {"variable_a", "variable_b", "gcode"}
    assert o["variable_a"].span_end == o["variable_a"].key_line


def test_deeper_indented_key_is_continuation_not_an_option():
    o = _opts("[gcode_macro _U]\nvariable_a: 1\n  variable_b: 2\ngcode:\n")
    assert set(o) == {"variable_a", "gcode"}
    assert o["variable_a"].span_end == 2


def test_semicolon_line_between_continuations_does_not_end_the_span():
    text = (
        "[gcode_macro _U]\nvariable_d: {\n    'a': 1,\n; note\n    'b': 2,\n"
        "    }\ngcode:\n"
    )
    assert _opts(text)["variable_d"].span_end == 5
    assert sv.parse_effective(text)["gcode_macro _U"]["variable_d"] == (
        "{\n'a': 1,\n'b': 2,\n}"
    )


def test_hash_line_between_continuations_also_does_not_end_the_span():
    text = (
        "[gcode_macro _U]\nvariable_d: {\n    'a': 1,\n# note\n    'b': 2,\n"
        "    }\ngcode:\n"
    )
    assert _opts(text)["variable_d"].span_end == 5


def test_section_names_are_case_sensitive():
    doc = sv.scan_document("[gcode_macro _U]\nvariable_a: 1\ngcode:\n[GCODE_MACRO _U]\ngcode:\n")
    assert [s.name for s in doc.sections] == ["gcode_macro _U", "GCODE_MACRO _U"]


def test_equals_delimiter_is_recognised():
    o = _opts("[gcode_macro _U]\nvariable_c = 1\ngcode:\n")
    assert o["variable_c"].delimiter == "="


def test_indented_section_header_is_still_a_header():
    doc = sv.scan_document("[gcode_macro _U]\n    variable_a: 1\n  [save_variables]\n  filename: ~/x.cfg\n")
    assert [s.name for s in doc.sections] == ["gcode_macro _U", "save_variables"]


def test_section_header_deeper_than_its_key_is_continuation_text():
    doc = sv.scan_document("[gcode_macro _U]\nvariable_a: 1\n    [foo]\ngcode:\n")
    assert [s.name for s in doc.sections] == ["gcode_macro _U"]


def test_stray_line_at_base_indent_aborts():
    with pytest.raises(sv.SyncError, match="not an option or a section header"):
        sv.scan_document("[gcode_macro _U]\nvariable_a: 1\nnot an option line\n")


_HEAD = "[gcode_macro _USER_VARIABLES]\n"


def _check(body, extra=""):
    text = _HEAD + body + "gcode:\n" + extra
    sv.validate_target(sv.scan_document(text), sv.parse_effective(text))


def test_valid_target_passes():
    _check("variable_a: 1\n")


def test_duplicate_names_abort_case_insensitively():
    with pytest.raises(sv.SyncError, match="appears more than once"):
        _check("variable_A: 1\nvariable_a: 2\n")


def test_duplicate_message_names_the_option_not_always_a_variable():
    # A duplicated description: is refused by the same check as a duplicated
    # variable_*, and the message must say "option", not always "variable".
    with pytest.raises(sv.SyncError, match=r"option 'description' appears more than once"):
        _check("description: a\ndescription: b\n")


def test_missing_gcode_aborts():
    text = _HEAD + "variable_a: 1\n"
    with pytest.raises(sv.SyncError, match="no 'gcode:' option"):
        sv.validate_target(sv.scan_document(text), sv.parse_effective(text))


def test_gcode_must_be_the_final_option():
    text = _HEAD + "gcode:\nvariable_a: 1\n"
    with pytest.raises(sv.SyncError, match="must be the last option"):
        sv.validate_target(sv.scan_document(text), sv.parse_effective(text))


def test_uppercase_gcode_marker_is_accepted():
    text = _HEAD + "variable_a: 1\nGCODE:\n"
    sv.validate_target(sv.scan_document(text), sv.parse_effective(text))


def test_unrecognised_section_aborts():
    with pytest.raises(sv.SyncError, match="unrecognised section"):
        _check("variable_a: 1\n", extra="[fan_generic foo]\nmax_power: 1\n")


def test_save_variables_section_is_permitted():
    _check("variable_a: 1\n", extra="[save_variables]\nfilename: ~/x.cfg\n")


def test_invalid_literal_aborts():
    with pytest.raises(sv.SyncError, match="not a valid literal"):
        _check('variable_a: "rack#2"\n')


def test_repeated_owned_section_aborts():
    text = _HEAD + "variable_a: 1\ngcode:\n" + _HEAD + "variable_b: 2\ngcode:\n"
    with pytest.raises(sv.SyncError, match="appears 2 times"):
        sv.validate_target(sv.scan_document(text), sv.parse_effective(text))


def test_missing_owned_section_aborts():
    text = "[save_variables]\nfilename: ~/x.cfg\n"
    with pytest.raises(sv.SyncError, match="no \\[gcode_macro _USER_VARIABLES\\]"):
        sv.validate_target(sv.scan_document(text), sv.parse_effective(text))


@pytest.fixture
def tables(monkeypatch):
    def apply(renames, removed):
        monkeypatch.setattr(sv, "RENAMES", dict(renames))
        monkeypatch.setattr(sv, "REMOVED", set(removed))
    return apply


def test_removed_applied_before_renames_and_before_custom(tables):
    tables({}, {"variable_gone"})
    mapping, custom = sv.classify(["variable_gone", "variable_keep"], {"variable_keep"})
    assert mapping == {"variable_keep": "variable_keep"}
    assert custom == []


def test_rename_carries_the_value_and_does_not_also_bank_it(tables):
    tables({"variable_old": "variable_new"}, set())
    mapping, custom = sv.classify(["variable_old"], {"variable_new"})
    assert mapping == {"variable_old": "variable_new"}
    assert custom == []


def test_unknown_names_become_custom(tables):
    tables({}, set())
    mapping, custom = sv.classify(["variable_mine"], {"variable_a"})
    assert mapping == {}
    assert custom == ["variable_mine"]


def test_rename_collision_with_existing_new_name_aborts(tables):
    tables({"variable_old": "variable_new"}, set())
    with pytest.raises(sv.SyncError, match="both '.*' and its replacement"):
        sv.classify(["variable_old", "variable_new"], {"variable_new"})


@pytest.mark.parametrize("renames,removed,message", [
    ({"variable_old": "variable_absent"}, set(), "destination .* is not in the template"),
    ({"variable_a": "variable_new"}, set(), "source .* is still in the template"),
    ({}, {"variable_a"}, "removed name .* is still in the template"),
    ({"variable_old": "variable_new"}, {"variable_old"}, "both a rename source and removed"),
    ({"variable_old": "variable_new", "variable_new": "variable_a"}, set(), "chained"),
    ({"variable_old": "variable_new"}, {"variable_new"}, "destination .* is removed"),
    ({"variable_o1": "variable_new", "variable_o2": "variable_new"}, set(), "share the destination"),
    ({"variable_Old": "variable_new"}, set(), "not canonical"),
])
def test_migration_table_invariants(tables, renames, removed, message):
    tables(renames, removed)
    with pytest.raises(sv.SyncError, match=message):
        sv.validate_migrations({"variable_a", "variable_new"})


def _one(text, name="variable_x"):
    doc = sv.scan_document(text)
    option = {o.name: o for o in doc.sections[0].options}[name]
    return sv.reemit_span(doc, option)


def test_key_line_value_drops_both_comment_kinds():
    assert _one("[gcode_macro _U]\nvariable_x: 5 # note\ngcode:\n") == ["5"]
    assert _one("[gcode_macro _U]\nvariable_x: 5 ; note\ngcode:\n") == ["5"]


def test_semicolon_line_in_span_is_dropped_hash_line_becomes_blank():
    semi = _one("[gcode_macro _U]\nvariable_x: {\n    'a': 1,\n    ; note\n    'b': 2\n    }\ngcode:\n")
    assert semi == ["{", "    'a': 1,", "    'b': 2", "    }"]
    hashed = _one("[gcode_macro _U]\nvariable_x: {\n    'a': 1,\n    # note\n    'b': 2\n    }\ngcode:\n")
    assert hashed == ["{", "    'a': 1,", "", "    'b': 2", "    }"]


def test_reemission_preserves_the_effective_value():
    # Deliberately unbalanced: two ';' lines against one '#' line. With one of
    # each the dropped and blanked counts cancel, and the test passes even with
    # the asymmetry swapped - laundering the exact bug it exists to catch.
    text = (
        "[gcode_macro _U]\nvariable_x: {\n    'a': 1, # keep\n    ; drop one\n"
        "    ; drop two\n    # becomes blank\n    'b': 2\n    }\ngcode:\n"
    )
    doc = sv.scan_document(text)
    option = {o.name: o for o in doc.sections[0].options}["variable_x"]
    lines = sv.reemit_span(doc, option)
    rebuilt = "[gcode_macro _U]\nvariable_x: %s\ngcode:\n" % "\n".join(lines)
    assert (
        sv.parse_effective(rebuilt)["gcode_macro _U"]["variable_x"]
        == sv.parse_effective(text)["gcode_macro _U"]["variable_x"]
    )


def test_inline_comment_extraction():
    assert sv.inline_comment("variable_x: 5 # note") == "# note"
    assert sv.inline_comment("variable_x: 5") == ""


FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "variables"
TEMPLATE = (FIXTURES / "template.cfg").read_text()


def _render(user_text):
    tdoc = sv.scan_document(TEMPLATE)
    tnames = {o.name for o in sv.find_section(tdoc, sv.OWNED_SECTION).options}
    udoc = sv.scan_document(user_text)
    uvalues = sv.parse_effective(user_text)
    unames = [o.name for o in sv.find_section(udoc, sv.OWNED_SECTION).options]
    mapping, custom = sv.classify(unames, tnames)
    return sv.render(tdoc, sv.parse_effective(TEMPLATE), udoc, uvalues, mapping, custom)


def test_missing_variable_is_added_with_its_comment():
    out = _render("[gcode_macro _USER_VARIABLES]\nvariable_travel_speed: 350 # mm/s\ngcode:\n")
    assert "variable_z_drop_speed: 15        # aligned comment" in out
    assert "variable_travel_speed: 350 # mm/s" in out


def test_changed_value_wins_but_template_comment_replaces_the_user_comment():
    out = _render(
        "[gcode_macro _USER_VARIABLES]\nvariable_travel_speed: 120 # my slow printer\ngcode:\n"
    )
    assert "variable_travel_speed: 120 # mm/s" in out
    assert "my slow printer" not in out


def test_custom_variables_are_banked_before_gcode():
    out = _render(
        "[gcode_macro _USER_VARIABLES]\n# my note\nvariable_mine: 7\ngcode:\n"
    )
    assert "## Custom variables" in out
    assert "# my note" in out
    assert out.index("variable_mine: 7") < out.index("gcode:")


def test_default_save_variables_is_dropped_and_custom_one_is_kept_last():
    default = (
        "[gcode_macro _USER_VARIABLES]\ngcode:\n[save_variables]\n"
        "filename: ~/printer_data/config/save_variables.cfg\n"
    )
    assert "[save_variables]" not in _render(default)
    custom = (
        "[gcode_macro _USER_VARIABLES]\ngcode:\n[save_variables]\nfilename: ~/elsewhere.cfg\n"
    )
    out = _render(custom)
    assert out.endswith("filename: ~/elsewhere.cfg\n")


def test_save_variables_with_an_extra_option_is_kept_even_at_the_default_filename():
    user = (
        "[gcode_macro _USER_VARIABLES]\ngcode:\n[save_variables]\n"
        "filename: %s\nunused: 1\n" % sv.SAVE_VARIABLES_DEFAULT
    )
    udoc = sv.scan_document(user)
    kept = sv.save_variables_kept(udoc, sv.parse_effective(user))
    assert len(kept) == 1
    out = _render(user)
    assert "[save_variables]" in out
    assert "unused: 1" in out


def test_two_save_variables_sections_are_both_kept():
    user = (
        "[gcode_macro _USER_VARIABLES]\ngcode:\n"
        "[save_variables]\nfilename: ~/one.cfg\n"
        "[save_variables]\nfilename: ~/two.cfg\n"
    )
    udoc = sv.scan_document(user)
    kept = sv.save_variables_kept(udoc, sv.parse_effective(user))
    assert len(kept) == 2
    out = _render(user)
    assert out.count("[save_variables]") == 2
    assert "filename: ~/one.cfg" in out
    assert "filename: ~/two.cfg" in out


def test_section_end_trims_trailing_blanks_so_repeated_syncs_do_not_grow():
    user = (
        "[gcode_macro _USER_VARIABLES]\ngcode:\n[save_variables]\n"
        "filename: ~/elsewhere.cfg\n\n\n"
    )
    udoc = sv.scan_document(user)
    section = sv.find_section(udoc, sv.SAVE_SECTION)
    end = sv._section_end(udoc, section)
    assert udoc.lines[end].strip() != ""

    first = _render(user)
    second = _render(first)
    assert second == first


def test_non_empty_user_gcode_body_survives_and_marker_is_normalised():
    out = _render(
        "[gcode_macro _USER_VARIABLES]\nGCODE:\n    RESPOND MSG=\"hi\" # diagnostic\n"
    )
    assert 'RESPOND MSG="hi" # diagnostic' in out
    assert "GCODE:" not in out


def test_unchanged_value_re_emits_the_template_line_verbatim():
    # Multi-space alignment is the point: with a single space the rebuild path
    # produces a byte-identical line, and an implementation that ALWAYS rebuilds
    # passes. This line can only survive by being copied.
    template_line = "variable_z_drop_speed: 15        # aligned comment"
    out = _render("[gcode_macro _USER_VARIABLES]\n%s\ngcode:\n" % template_line)
    assert template_line in out


def test_a_kept_save_variables_section_keeps_its_trailing_comment():
    out = _render(
        "[gcode_macro _USER_VARIABLES]\ngcode:\n[save_variables]\n"
        "filename: ~/elsewhere.cfg\n# keep this note\n"
    )
    assert "# keep this note" in out


def test_custom_variable_after_a_multiline_value_is_carried_intact():
    # The line above such a variable is a more-indented content line - the
    # previous value's last continuation. That is ordinary, so validate_custom_span
    # must not treat a non-comment predecessor as unpreservable.
    out = _render(
        "[gcode_macro _USER_VARIABLES]\nvariable_material_parameters: {\n"
        "        'PLA': {'pressure_advance': 0.04}\n    }\nvariable_mine: 7\ngcode:\n"
    )
    assert "variable_mine: 7" in out
    assert "'PLA': {'pressure_advance': 0.04}" in out


def test_both_comment_characters_are_carried_above_a_custom_variable():
    out = _render(
        "[gcode_macro _USER_VARIABLES]\n# hash note\n; semi note\n"
        "variable_mine: 7\ngcode:\n"
    )
    assert "# hash note" in out
    assert "; semi note" in out


def test_custom_variable_after_the_templates_trailing_comment_is_not_duplicated():
    # The user's custom variable sits directly under the same comment line the
    # template itself owns just above gcode:. That line must be emitted only
    # once - where the template puts it - not also copied into the custom block.
    user = (
        "[gcode_macro _USER_VARIABLES]\n## Do not remove the next line\n"
        "variable_mine: 7\ngcode:\n"
    )
    out = _render(user)
    assert out.count("## Do not remove the next line") == 1
    assert "variable_mine: 7" in out
    # Idempotent: syncing the rendered result again changes nothing further.
    again = _render(out)
    assert again == out


def test_a_user_comment_above_a_template_matching_comment_survives():
    # The walk must collect the whole contiguous comment run first and then
    # filter out template-owned lines, not stop at the first template-owned
    # line it meets - or a user's own note directly above it is lost.
    user = (
        "[gcode_macro _USER_VARIABLES]\n# User-specific note\n"
        "## Do not remove the next line\nvariable_mine: 7\ngcode:\n"
    )
    out = _render(user)
    assert "# User-specific note" in out
    assert out.count("## Do not remove the next line") == 1
    assert "variable_mine: 7" in out
    # Idempotent: syncing the rendered result again changes nothing further.
    again = _render(out)
    assert again == out


def test_custom_variable_preceded_by_a_stray_line_aborts():
    # The constraint is enforced by scan_document: a column-zero line that is
    # neither an option nor a header cannot reach the renderer at all.
    with pytest.raises(sv.SyncError, match="not an option or a section header"):
        _render("[gcode_macro _USER_VARIABLES]\nnot a comment\nvariable_mine: 7\ngcode:\n")


def test_custom_variable_with_header_like_continuation_stays_one_value():
    out = _render(
        "[gcode_macro _USER_VARIABLES]\nvariable_mine: {\n    '[foo]': 1\n    }\ngcode:\n"
    )
    assert "'[foo]': 1" in out
    assert sv.parse_effective(out)[sv.OWNED_SECTION]["variable_mine"] == "{\n'[foo]': 1\n}"


def test_multiline_value_keeps_its_shape():
    user = (
        "[gcode_macro _USER_VARIABLES]\nvariable_material_parameters: {\n"
        "        'PLA': {'pressure_advance': 0.0400}\n    }\ngcode:\n"
    )
    out = _render(user)
    assert "'PLA': {'pressure_advance': 0.0400}" in out
    assert "0.0525" not in out


def test_indented_user_section_still_yields_a_parseable_gcode_option():
    out = _render(
        "[gcode_macro _USER_VARIABLES]\n    variable_travel_speed: 120\n"
        "    gcode:\n        G28\n"
    )
    assert "gcode" in sv.parse_effective(out)[sv.OWNED_SECTION]
    sv.validate_target(sv.scan_document(out), sv.parse_effective(out))


def test_syncing_a_pristine_copy_reports_no_change():
    rendered, changed = sv.plan_sync(TEMPLATE, TEMPLATE)
    assert changed is False
    assert rendered == TEMPLATE


def test_sync_is_idempotent():
    user = "[gcode_macro _USER_VARIABLES]\nvariable_travel_speed: 120\ngcode:\n"
    once, changed = sv.plan_sync(TEMPLATE, user)
    assert changed is True
    twice, changed_again = sv.plan_sync(TEMPLATE, once)
    assert twice == once
    assert changed_again is False


def test_missing_target_renders_the_template_as_is():
    rendered, changed = sv.plan_sync(TEMPLATE, None)
    assert rendered == TEMPLATE
    assert changed is True


def test_render_validation_catches_a_lost_value():
    intended = dict(sv.parse_effective(TEMPLATE)[sv.OWNED_SECTION])
    intended["variable_travel_speed"] = "999"
    with pytest.raises(sv.SyncError, match="lost variable_travel_speed"):
        sv.validate_render(TEMPLATE, {sv.OWNED_SECTION: intended})


def test_render_validation_rejects_an_unintended_variable():
    rendered = TEMPLATE.replace(
        "gcode:\n", "variable_never_intended: 99\ngcode:\n", 1
    )
    intended = dict(sv.parse_effective(TEMPLATE)[sv.OWNED_SECTION])
    with pytest.raises(sv.SyncError, match="variable_never_intended"):
        sv.validate_render(rendered, {sv.OWNED_SECTION: intended})


def test_render_validation_catches_an_unexpected_section():
    # [save_variables] is the only section that can reach this branch: any
    # other name is refused by validate_target first, and matching the owned
    # section's intended map to the template's own values keeps its checks a
    # no-op here.
    rendered = TEMPLATE + "\n[save_variables]\nfilename: ~/x.cfg\n"
    intended = dict(sv.parse_effective(TEMPLATE)[sv.OWNED_SECTION])
    with pytest.raises(sv.SyncError, match="unexpected section"):
        sv.validate_render(rendered, {sv.OWNED_SECTION: intended})


def test_one_line_gcode_body_is_preserved_and_proved(tmp_path):
    user = '[gcode_macro _USER_VARIABLES]\ngcode: RESPOND MSG="hi"\n'
    rendered, _ = sv.plan_sync(TEMPLATE, user)
    assert 'gcode: RESPOND MSG="hi"' in rendered


def test_validation_rejects_a_corrupted_gcode_payload():
    user = '[gcode_macro _USER_VARIABLES]\ngcode:\n    RESPOND MSG="hi"\n'
    with pytest.raises(sv.SyncError, match="gcode body verbatim"):
        sv.validate_render(TEMPLATE, {sv.OWNED_SECTION: {}},
                           gcode=sv.gcode_payload(sv.scan_document(user)))


def test_a_preserved_gcode_body_is_proved_by_validation():
    user = "[gcode_macro _USER_VARIABLES]\ngcode:\n    RESPOND MSG=\"hi\"\n"
    rendered, _ = sv.plan_sync(TEMPLATE, user)
    assert sv.parse_effective(rendered)[sv.OWNED_SECTION]["gcode"] == (
        sv.parse_effective(user)[sv.OWNED_SECTION]["gcode"]
    )


def test_comment_only_gcode_is_not_treated_as_a_body():
    for user in ('[gcode_macro _USER_VARIABLES]\ngcode: # note\n',
                 '[gcode_macro _USER_VARIABLES]\ngcode:\n    ; note\n'):
        assert sv.has_gcode_body(sv.scan_document(user)) is False
    assert sv.has_gcode_body(
        sv.scan_document('[gcode_macro _USER_VARIABLES]\ngcode: M117 hi\n')
    ) is True


def test_save_variables_emitted_before_the_owned_section_is_rejected():
    rendered = "[save_variables]\nfilename: ~/x.cfg\n\n" + TEMPLATE
    with pytest.raises(sv.SyncError, match="emitted before"):
        sv.validate_render(rendered, {sv.OWNED_SECTION: {}, sv.SAVE_SECTION: {}})


def test_validation_rejects_a_corrupted_save_block():
    user = ("[gcode_macro _USER_VARIABLES]\ngcode:\n[save_variables]\n"
            "filename: ~/elsewhere.cfg\n")
    udoc = sv.scan_document(user)
    kept = sv.save_variables_kept(udoc, sv.parse_effective(user))
    expected = [udoc.lines[k.header_line:sv._section_end(udoc, k) + 1] for k in kept]
    rendered = TEMPLATE + "\n[save_variables]\nfilename: ~/somewhere_else.cfg\n"
    with pytest.raises(sv.SyncError, match=r"preserve \[save_variables\] verbatim"):
        sv.validate_render(rendered, {sv.OWNED_SECTION: {}, sv.SAVE_SECTION: {}},
                           saves=expected)


def test_validation_rejects_a_dropped_section():
    intended = dict(sv.parse_effective(TEMPLATE)[sv.OWNED_SECTION])
    with pytest.raises(sv.SyncError, match=r"dropped the \[save_variables\] section"):
        sv.validate_render(TEMPLATE, {sv.OWNED_SECTION: intended, sv.SAVE_SECTION: {}})


def test_every_user_value_survives_a_real_sync():
    user = (
        "[gcode_macro _USER_VARIABLES]\nvariable_travel_speed: 120\n"
        "variable_material_parameters: {\n        'PLA': {'pressure_advance': 0.04}\n    }\n"
        "variable_mine: 7\ngcode:\n"
    )
    rendered, _ = sv.plan_sync(TEMPLATE, user)
    after = sv.parse_effective(rendered)[sv.OWNED_SECTION]
    before = sv.parse_effective(user)[sv.OWNED_SECTION]
    for name, value in before.items():
        assert after[name] == value


import errno
import os
import stat


STAMP = "2026_09_08-120000"


def test_backup_is_named_for_its_content(tmp_path):
    path = sv.write_backup(b"hello", directory=tmp_path, stamp=STAMP)
    assert path.read_bytes() == b"hello"
    assert path.name.startswith("variables.cfg.")
    assert len(path.name.rsplit("-", 1)[1]) == 12


def test_identical_bytes_reuse_the_existing_backup(tmp_path):
    first = sv.write_backup(b"hello", directory=tmp_path, stamp=STAMP)
    second = sv.write_backup(b"hello", directory=tmp_path, stamp=STAMP)
    assert first == second
    assert len(list(tmp_path.iterdir())) == 1


def test_a_truncated_predecessor_is_never_trusted(tmp_path):
    real = sv.write_backup(b"hello", directory=tmp_path, stamp=STAMP)
    real.write_bytes(b"hel")                      # simulate an interrupted run
    again = sv.write_backup(b"hello", directory=tmp_path, stamp=STAMP)
    assert again != real
    assert again.name.endswith(".1")
    assert again.read_bytes() == b"hello"
    assert real.read_bytes() == b"hel"            # untouched, not overwritten


def test_fallback_is_used_only_for_link_unsupported_errnos(tmp_path, monkeypatch):
    def unsupported(src, dst):
        raise OSError(errno.EOPNOTSUPP, "nope")
    monkeypatch.setattr(sv.os, "link", unsupported)
    path = sv.write_backup(b"hello", directory=tmp_path, stamp=STAMP)
    assert path.read_bytes() == b"hello"
    # The name matters as much as the bytes: switching to the fallback must not
    # skip the canonical candidate and leave every backup carrying a suffix.
    digest = __import__("hashlib").sha256(b"hello").hexdigest()[:12]
    assert path.name == "variables.cfg.%s-%s" % (STAMP, digest)

    def no_space(src, dst):
        raise OSError(errno.ENOSPC, "full")
    monkeypatch.setattr(sv.os, "link", no_space)
    with pytest.raises(sv.SyncError, match="could not create backup"):
        sv.write_backup(b"other", directory=tmp_path, stamp=STAMP)


def test_no_temporary_file_is_left_behind(tmp_path):
    sv.write_backup(b"hello", directory=tmp_path, stamp=STAMP)
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".")]


def test_a_symlink_is_never_accepted_as_a_backup(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_bytes(b"hello")
    digest = __import__("hashlib").sha256(b"hello").hexdigest()[:12]
    (tmp_path / ("variables.cfg.%s-%s" % (STAMP, digest))).symlink_to(elsewhere)
    with pytest.raises(sv.SyncError, match="not a regular file"):
        sv.write_backup(b"hello", directory=tmp_path, stamp=STAMP)


def test_lock_identity_follows_the_resolved_path(tmp_path, monkeypatch):
    monkeypatch.setattr(sv, "BACKUP_ROOT", tmp_path / "backups")
    real = tmp_path / "variables.cfg"
    real.write_text("x")
    link = tmp_path / "link.cfg"
    link.symlink_to(real)
    assert sv.lock_path_for(link) == sv.lock_path_for(real)
    assert sv.lock_path_for(tmp_path / "other.cfg") != sv.lock_path_for(real)
    assert sv.lock_path_for(real).parent == sv.BACKUP_ROOT


def test_replacement_refuses_when_recorded_bytes_no_longer_match(tmp_path):
    # This proves the comparison, which is what closes the common case. The
    # residual window between the comparison and os.replace is accepted and
    # documented in the spec; no portable test can pin it.
    target = tmp_path / "variables.cfg"
    target.write_bytes(b"original")
    target.write_bytes(b"someone else wrote this")
    with pytest.raises(sv.SyncError, match="changed while the sync was running"):
        sv.replace_atomically(target, b"new", b"original")
    assert target.read_bytes() == b"someone else wrote this"


def test_replacement_writes_and_leaves_no_temporary(tmp_path):
    target = tmp_path / "variables.cfg"
    target.write_bytes(b"original")
    sv.replace_atomically(target, b"new", b"original")
    assert target.read_bytes() == b"new"
    assert list(tmp_path.iterdir()) == [target]


def test_replacement_refuses_a_target_that_appeared_after_the_check(tmp_path):
    # original=None means "there was no file". If one exists by the time we
    # replace, someone created it underneath us: fail closed, never clobber.
    target = tmp_path / "variables.cfg"
    target.write_bytes(b"appeared from nowhere")
    with pytest.raises(sv.SyncError, match="changed while the sync was running"):
        sv.replace_atomically(target, b"new", None)
    assert target.read_bytes() == b"appeared from nowhere"


def test_lock_is_taken_exclusively_and_blocking(tmp_path, monkeypatch):
    # A regression to LOCK_NB would turn "wait for the other sync" into
    # "fail because another sync is running", which no other test would catch.
    monkeypatch.setattr(sv, "BACKUP_ROOT", tmp_path / "backups")
    seen = []
    real = sv.fcntl.flock
    monkeypatch.setattr(sv.fcntl, "flock", lambda fd, op: (seen.append(op), real(fd, op))[1])
    with sv.target_lock(tmp_path / "variables.cfg"):
        pass
    assert seen == [sv.fcntl.LOCK_EX]
    assert not seen[0] & sv.fcntl.LOCK_NB


def test_lock_failure_is_reported_as_a_sync_error(tmp_path, monkeypatch):
    monkeypatch.setattr(sv, "BACKUP_ROOT", tmp_path / "backups")

    def refuse(fd, op):
        raise OSError(errno.EWOULDBLOCK, "busy")

    monkeypatch.setattr(sv.fcntl, "flock", refuse)
    with pytest.raises(sv.SyncError, match="could not lock"):
        with sv.target_lock(tmp_path / "variables.cfg"):
            pass


def test_creating_a_new_target_needs_no_original(tmp_path):
    target = tmp_path / "variables.cfg"
    sv.replace_atomically(target, b"fresh", None)
    assert target.read_bytes() == b"fresh"


def test_replacement_preserves_an_existing_0644_target(tmp_path):
    target = tmp_path / "variables.cfg"
    target.write_bytes(b"original")
    os.chmod(target, 0o644)
    sv.replace_atomically(target, b"new", b"original")
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o644


def test_replacement_preserves_an_existing_0600_target(tmp_path):
    target = tmp_path / "variables.cfg"
    target.write_bytes(b"original")
    os.chmod(target, 0o600)
    sv.replace_atomically(target, b"new", b"original")
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600


def test_a_newly_created_target_is_not_0600_by_accident(tmp_path):
    target = tmp_path / "variables.cfg"
    old_umask = os.umask(0o022)
    try:
        sv.replace_atomically(target, b"fresh", None)
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o644


def test_lock_directory_failure_is_reported(tmp_path, monkeypatch):
    blocker = tmp_path / "backups"
    blocker.write_text("not a directory")
    monkeypatch.setattr(sv, "BACKUP_ROOT", blocker / "variables")
    with pytest.raises(sv.SyncError, match="could not prepare"):
        with sv.target_lock(tmp_path / "variables.cfg"):
            pass


import subprocess
import sys

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "sync_variables.py"


def _run(tmp_path, user_text, *args):
    template = tmp_path / "template.cfg"
    template.write_text(TEMPLATE)
    target = tmp_path / "variables.cfg"
    if user_text is not None:
        target.write_text(user_text)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--template", str(template),
         "--target", str(target),
         "--backup-root", str(tmp_path / "backups"), *args],
        capture_output=True, text=True,
    )
    return result, target


def test_cli_never_writes_outside_its_backup_root(tmp_path, monkeypatch):
    # A second, fake $HOME proves the DEFAULT root itself gains nothing: an
    # implementation that wrote to both the injected root and the default
    # would otherwise still pass by only checking the injected one.
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    default_root = fake_home / "klippain_config_backups" / "variables"

    _run(tmp_path, "[gcode_macro _USER_VARIABLES]\nvariable_travel_speed: 120\ngcode:\n")

    backups = list((tmp_path / "backups").iterdir())
    assert [p for p in backups if p.name.startswith("variables.cfg.")]
    assert [p for p in backups if p.name.startswith(".lock-")]
    assert not default_root.exists() or not list(default_root.iterdir())


def test_cli_syncing_through_a_symlink_updates_the_real_file(tmp_path):
    template = tmp_path / "template.cfg"
    template.write_text(TEMPLATE)
    real = tmp_path / "real.cfg"
    real.write_text("[gcode_macro _USER_VARIABLES]\nvariable_travel_speed: 120\ngcode:\n")
    link = tmp_path / "link.cfg"
    link.symlink_to(real)

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--template", str(template),
         "--target", str(link), "--backup-root", str(tmp_path / "backups")],
        capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stderr
    assert link.is_symlink()
    assert os.path.realpath(str(link)) == str(real)
    assert "variable_z_drop_speed: 15" in real.read_text()
    assert "variable_travel_speed: 120" in real.read_text()


def test_cli_locks_the_resolved_path_not_the_symlink(tmp_path, monkeypatch):
    # Regression: the lock and the read/write path must be derived from a
    # single realpath() call. Recording what lock_path_for is actually called
    # with, and comparing that to os.path.realpath(link), catches a
    # reintroduced split where the lock guards the link while the work
    # operates on a separately (and possibly differently) resolved path.
    monkeypatch.setattr(sv, "BACKUP_ROOT", tmp_path / "backups")
    template = tmp_path / "template.cfg"
    template.write_text(TEMPLATE)
    real = tmp_path / "real.cfg"
    real.write_text("[gcode_macro _USER_VARIABLES]\nvariable_travel_speed: 120\ngcode:\n")
    link = tmp_path / "link.cfg"
    link.symlink_to(real)

    seen = []
    real_lock_path_for = sv.lock_path_for

    def recording_lock_path_for(target):
        seen.append(target)
        return real_lock_path_for(target)

    monkeypatch.setattr(sv, "lock_path_for", recording_lock_path_for)

    rc = sv.main([
        "--template", str(template),
        "--target", str(link),
        "--backup-root", str(tmp_path / "backups"),
    ])

    assert rc == 0
    assert seen == [pathlib.Path(os.path.realpath(str(link)))]
    assert link.is_symlink()
    assert os.path.realpath(str(link)) == str(real)
    assert "variable_z_drop_speed: 15" in real.read_text()
    assert "variable_travel_speed: 120" in real.read_text()


def test_cli_adds_missing_variables(tmp_path):
    result, target = _run(
        tmp_path, "[gcode_macro _USER_VARIABLES]\nvariable_travel_speed: 120\ngcode:\n"
    )
    assert result.returncode == 0, result.stderr
    assert "variable_z_drop_speed: 15" in target.read_text()
    assert "variable_travel_speed: 120" in target.read_text()


def test_up_to_date_target_is_not_rewritten(tmp_path):
    synced, target = _run(
        tmp_path, "[gcode_macro _USER_VARIABLES]\nvariable_travel_speed: 120\ngcode:\n"
    )
    assert synced.returncode == 0
    before = target.stat().st_mtime_ns
    again, _ = _run(tmp_path, None)      # None leaves the existing target alone
    assert again.returncode == 0
    assert target.stat().st_mtime_ns == before


def test_diff_writes_nothing(tmp_path):
    before = "[gcode_macro _USER_VARIABLES]\nvariable_travel_speed: 120\ngcode:\n"
    result, target = _run(tmp_path, before, "--diff")
    assert result.returncode == 0
    assert "variable_z_drop_speed" in result.stdout
    assert target.read_text() == before


def test_check_exits_non_zero_when_out_of_date(tmp_path):
    result, _ = _run(
        tmp_path, "[gcode_macro _USER_VARIABLES]\nvariable_travel_speed: 120\ngcode:\n",
        "--check",
    )
    assert result.returncode == 1


def test_opt_out_is_honoured_and_force_overrides_it(tmp_path):
    opted = (
        "[gcode_macro _USER_VARIABLES]\n"
        "variable_klippain_variables_autoupdate: False\ngcode:\n"
    )
    result, target = _run(tmp_path, opted)
    assert result.returncode == 0
    assert target.read_text() == opted
    result, target = _run(tmp_path, opted, "--force")
    assert result.returncode == 0
    assert "variable_travel_speed" in target.read_text()
    assert "variable_klippain_variables_autoupdate: False" in target.read_text()


def test_malformed_target_exits_non_zero_and_writes_nothing(tmp_path):
    broken = "[gcode_macro _USER_VARIABLES]\nvariable_a: 1\nvariable_A: 2\ngcode:\n"
    result, target = _run(tmp_path, broken)
    assert result.returncode == 2
    assert target.read_text() == broken
    assert "appears more than once" in result.stderr


def test_crlf_target_syncs_and_backs_up_the_original_bytes(tmp_path):
    # A CRLF target must not be compared, backed up, or reported as "changed
    # while the sync was running" against its LF-decoded text: replace_atomically
    # and write_backup must both see the user's actual bytes.
    body = "[gcode_macro _USER_VARIABLES]\nvariable_travel_speed: 120\ngcode:\n"
    crlf_body = body.replace("\n", "\r\n")
    target = tmp_path / "variables.cfg"
    target.write_bytes(crlf_body.encode("utf-8"))
    template = tmp_path / "template.cfg"
    template.write_text(TEMPLATE)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--template", str(template),
         "--target", str(target), "--backup-root", str(tmp_path / "backups")],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr

    backups = [p for p in (tmp_path / "backups").iterdir()
               if p.name.startswith("variables.cfg.")]
    assert len(backups) == 1
    assert backups[0].read_bytes() == crlf_body.encode("utf-8")

    written = target.read_bytes()
    assert b"\r\n" not in written
    assert "variable_travel_speed: 120" in written.decode("utf-8")


REPO = pathlib.Path(__file__).resolve().parent.parent
SHIPPED = REPO / "user_templates" / "variables.cfg"


def test_shipped_template_holds_exactly_one_section():
    doc = sv.scan_document(sv.decode(SHIPPED.read_bytes()))
    assert [s.name for s in doc.sections] == [sv.OWNED_SECTION]


def test_shipped_template_is_internally_valid():
    text = sv.decode(SHIPPED.read_bytes())
    sv.validate_target(sv.scan_document(text), sv.parse_effective(text))


def test_every_shipped_value_is_a_valid_literal():
    text = sv.decode(SHIPPED.read_bytes())
    for name, value in sv.parse_effective(text)[sv.OWNED_SECTION].items():
        sv.check_literal(name, value)


def test_shipped_template_ships_the_opt_out_enabled():
    text = sv.decode(SHIPPED.read_bytes())
    values = sv.parse_effective(text)[sv.OWNED_SECTION]
    assert values[sv.AUTOUPDATE_VARIABLE] == "True"


def test_shipped_template_round_trips_unchanged():
    text = sv.decode(SHIPPED.read_bytes())
    rendered, changed = sv.plan_sync(text, text)
    assert changed is False
    assert rendered == text


def test_machine_cfg_owns_save_variables():
    machine = (REPO / "config" / "machine.cfg").read_text()
    assert "[save_variables]" in machine
    assert sv.SAVE_VARIABLES_DEFAULT in machine
