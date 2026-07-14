from __future__ import annotations

import ast
import importlib.util
import re
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_PATH = REPO_ROOT / "scripts" / "validate_probe_framework.py"

spec = importlib.util.spec_from_file_location("validate_probe_framework", VALIDATOR_PATH)
validator = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(validator)


def profile_variable(text: str, name: str) -> str:
    match = re.search(rf"^\s*variable_{re.escape(name)}\s*:\s*(.*?)\s*$", text, re.M)
    assert match is not None, f"missing variable_{name}"
    return str(ast.literal_eval(validator.strip_inline_comment(match.group(1)).strip()))


def startprint_actions(text: str) -> tuple[str, ...]:
    return ast.literal_eval(profile_variable(text, "startprint_actions"))


MINIMAL_MACHINE_CFG = """[virtual_sdcard]
on_error_gcode:
    _PROBE_RESET_CONTACT_GUARD
"""

MINIMAL_VIRTUAL_Z_PROBE_CFG = """[gcode_macro _PROBE_RESET_CONTACT_GUARD]
gcode:

[gcode_macro _PROBE_CONTACT_Z_HOME]
gcode:
    _PROBE_ENTER_CONTACT_GUARD OPERATION=contact_z_home SOURCE=manual
    _PROBE_EXIT_CONTACT_GUARD
"""

MINIMAL_GENERIC_PROBE_CFG = """[gcode_macro ACTIVATE_PROBE]
gcode:
    _PROBE_RESET_CONTACT_GUARD
    _PROBE_ENTER_CONTACT_GUARD OPERATION=activate_probe SOURCE=manual

[gcode_macro DEACTIVATE_PROBE]
gcode:
    _PROBE_EXIT_CONTACT_GUARD
"""


def minimal_lifecycle_macro(name: str) -> str:
    return f"[gcode_macro {name}]\ngcode:\n    _PROBE_RESET_CONTACT_GUARD\n"


def make_minimal_repo(tmp_path: Path) -> Path:
    repo = tmp_path
    (repo / "README.md").write_text("", encoding="utf-8")
    (repo / "config").mkdir()
    (repo / "config" / "machine.cfg").write_text(MINIMAL_MACHINE_CFG, encoding="utf-8")
    (repo / "config" / "hardware").mkdir()
    (repo / "config" / "hardware" / "probes").mkdir()
    (repo / "macros").mkdir()
    (repo / "macros" / "base").mkdir()
    (repo / "macros" / "base" / "probing").mkdir()
    (repo / "macros" / "base" / "probing" / "hooks").mkdir()
    (repo / "macros" / "base" / "probing" / "virtual_z_probe.cfg").write_text(
        MINIMAL_VIRTUAL_Z_PROBE_CFG, encoding="utf-8"
    )
    (repo / "macros" / "base" / "probing" / "generic_probe.cfg").write_text(
        MINIMAL_GENERIC_PROBE_CFG, encoding="utf-8"
    )
    (repo / "macros" / "base" / "start_print.cfg").write_text(
        minimal_lifecycle_macro("START_PRINT"), encoding="utf-8"
    )
    (repo / "macros" / "base" / "cancel_print.cfg").write_text(
        minimal_lifecycle_macro("CANCEL_PRINT"), encoding="utf-8"
    )
    (repo / "macros" / "base" / "end_print.cfg").write_text(
        minimal_lifecycle_macro("END_PRINT"), encoding="utf-8"
    )
    (repo / "scripts").mkdir()
    return repo


def write_cartographer_hook(repo: Path, *, include_z_home: bool = True) -> None:
    z_home_macro = ""
    if include_z_home:
        z_home_macro = """
[gcode_macro _PROBE_HOOK_CONTACT_Z_HOME]
gcode:
    CARTOGRAPHER_TOUCH_HOME
"""

    (repo / "macros" / "base" / "probing" / "hooks" / "cartographer_touch.cfg").write_text(
        f"""{z_home_macro}
[gcode_macro _PROBE_HOOK_CONTACT_ACTIVATE]
gcode:

[gcode_macro _PROBE_HOOK_CONTACT_DEACTIVATE]
gcode:
""",
        encoding="utf-8",
    )


def write_probe_profile(repo: Path, text: str) -> None:
    (repo / "config" / "hardware" / "probes" / "cartographer_touch.cfg").write_text(
        text,
        encoding="utf-8",
    )


class ProbeFrameworkValidatorTest(unittest.TestCase):
    def test_startprint_hook_mode_allows_standard_g28_homing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = make_minimal_repo(Path(tmpdir))
            write_cartographer_hook(repo)
            write_probe_profile(
                repo,
                """
[gcode_macro _USER_VARIABLES]
variable_probe_type_enabled: "cartographer_touch"
variable_probe_needs_contact_temp_guard: True
variable_probe_hook_family: "cartographer_touch"
variable_probe_contact_z_home_mode: "none"
variable_probe_contact_z_home_startprint_mode: "hook"
variable_probe_contact_auto_calibrate_mode: "none"
gcode:

[include ../../../macros/base/probing/hooks/cartographer_touch.cfg]
""",
            )

            self.assertEqual([], validator.validate(repo))

    def test_startprint_hook_mode_requires_contact_z_home_hook(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = make_minimal_repo(Path(tmpdir))
            write_cartographer_hook(repo, include_z_home=False)
            write_probe_profile(
                repo,
                """
[gcode_macro _USER_VARIABLES]
variable_probe_type_enabled: "cartographer_touch"
variable_probe_needs_contact_temp_guard: True
variable_probe_hook_family: "cartographer_touch"
variable_probe_contact_z_home_mode: "none"
variable_probe_contact_z_home_startprint_mode: "hook"
variable_probe_contact_auto_calibrate_mode: "none"
gcode:

[include ../../../macros/base/probing/hooks/cartographer_touch.cfg]
""",
            )

            self.assertTrue(
                any(
                    "contact start-print Z home uses hook mode but _PROBE_HOOK_CONTACT_Z_HOME is missing" in error
                    for error in validator.validate(repo)
                ),
            )


class ContactGuardCleanupTest(unittest.TestCase):
    def test_compliant_minimal_repo_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = make_minimal_repo(Path(tmpdir))

            self.assertEqual([], validator.validate(repo))

    def test_lifecycle_entry_points_must_clear_stale_guard(self) -> None:
        for relative_path in validator.GUARD_CLEANUP_CALLERS:
            with self.subTest(relative_path=relative_path), tempfile.TemporaryDirectory() as tmpdir:
                repo = make_minimal_repo(Path(tmpdir))
                path = repo / relative_path
                path.write_text(
                    path.read_text(encoding="utf-8").replace("_PROBE_RESET_CONTACT_GUARD", "G4 P0"),
                    encoding="utf-8",
                )

                self.assertTrue(
                    any(
                        "must call _PROBE_RESET_CONTACT_GUARD" in error and relative_path in error
                        for error in validator.validate(repo)
                    ),
                )

    def test_reset_macro_must_be_defined(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = make_minimal_repo(Path(tmpdir))
            (repo / "macros" / "base" / "probing" / "virtual_z_probe.cfg").write_text(
                "[gcode_macro _PROBE_CONTACT_Z_HOME]\ngcode:\n", encoding="utf-8"
            )

            self.assertTrue(
                any(
                    "_PROBE_RESET_CONTACT_GUARD is not defined" in error
                    for error in validator.validate(repo)
                ),
            )

    def test_guarded_wrapper_must_exit_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = make_minimal_repo(Path(tmpdir))
            (repo / "macros" / "base" / "probing" / "virtual_z_probe.cfg").write_text(
                MINIMAL_VIRTUAL_Z_PROBE_CFG.replace("    _PROBE_EXIT_CONTACT_GUARD\n", ""),
                encoding="utf-8",
            )

            self.assertTrue(
                any(
                    "[gcode_macro _PROBE_CONTACT_Z_HOME] enters the contact guard but never exits it" in error
                    for error in validator.validate(repo)
                ),
            )

    def test_activate_probe_must_reset_before_entering_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = make_minimal_repo(Path(tmpdir))
            (repo / "macros" / "base" / "probing" / "generic_probe.cfg").write_text(
                MINIMAL_GENERIC_PROBE_CFG.replace("    _PROBE_RESET_CONTACT_GUARD\n", ""),
                encoding="utf-8",
            )

            self.assertTrue(
                any(
                    "ACTIVATE_PROBE must clear a stale contact guard before entering a new one" in error
                    for error in validator.validate(repo)
                ),
            )

    def test_repo_satisfies_guard_cleanup_contracts(self) -> None:
        self.assertEqual([], validator.validate_guard_cleanup(REPO_ROOT))


class CartographerTouchProfileTest(unittest.TestCase):
    def test_profile_uses_scan_g28_then_startprint_touch_home(self) -> None:
        profile = (REPO_ROOT / "config" / "hardware" / "probes" / "cartographer_touch.cfg").read_text(
            encoding="utf-8",
        )
        actions = startprint_actions(profile)

        self.assertEqual("cartographer_touch", profile_variable(profile, "probe_hook_family"))
        self.assertEqual("none", profile_variable(profile, "probe_contact_z_home_mode"))
        self.assertEqual("hook", profile_variable(profile, "probe_contact_z_home_startprint_mode"))
        self.assertNotIn("z_offset", actions)
        self.assertLess(actions.index("extruder_preheating"), actions.index("chamber_soak"))
        self.assertLess(actions.index("extruder_preheating"), actions.index("tilt_calib"))
        self.assertLess(actions.index("extruder_preheating"), actions.index("contact_z_home"))
        self.assertLess(actions.index("tilt_calib"), actions.index("contact_z_home"))
        self.assertLess(actions.index("contact_z_home"), actions.index("bedmesh"))


if __name__ == "__main__":
    unittest.main()
