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


def make_minimal_repo(tmp_path: Path) -> Path:
    repo = tmp_path
    (repo / "README.md").write_text("", encoding="utf-8")
    (repo / "config").mkdir()
    (repo / "config" / "machine.cfg").write_text("", encoding="utf-8")
    (repo / "config" / "hardware").mkdir()
    (repo / "config" / "hardware" / "probes").mkdir()
    (repo / "macros").mkdir()
    (repo / "macros" / "base").mkdir()
    (repo / "macros" / "base" / "probing").mkdir()
    (repo / "macros" / "base" / "probing" / "hooks").mkdir()
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
