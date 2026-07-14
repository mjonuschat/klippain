#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path


VALID_MODES = {"none", "standard_g28", "hook"}
HOOK_MACROS = {
    "contact_z_home": "_PROBE_HOOK_CONTACT_Z_HOME",
    "contact_auto_calibrate": "_PROBE_HOOK_CONTACT_AUTO_CALIBRATE",
    "contact_activate": "_PROBE_HOOK_CONTACT_ACTIVATE",
    "contact_deactivate": "_PROBE_HOOK_CONTACT_DEACTIVATE",
}
BANNED_NAMES = {"cartographer_survey.cfg", "PZ Probe.cfg"}
REQUIRED_REPO_PATHS = {
    "README.md": "file",
    "config/machine.cfg": "file",
    "config/hardware/probes": "dir",
    "macros/base/probing": "dir",
    "macros/base/probing/generic_probe.cfg": "file",
    "macros/base/probing/virtual_z_probe.cfg": "file",
    "macros/base/start_print.cfg": "file",
    "macros/base/cancel_print.cfg": "file",
    "macros/base/end_print.cfg": "file",
    "scripts": "dir",
}
GUARD_RESET_MACRO = "_PROBE_RESET_CONTACT_GUARD"
GUARD_ENTER_MACRO = "_PROBE_ENTER_CONTACT_GUARD"
GUARD_EXIT_MACRO = "_PROBE_EXIT_CONTACT_GUARD"
# Print lifecycle entry points that must clear a contact guard leaked by an
# interrupted guarded operation
GUARD_CLEANUP_CALLERS = (
    "macros/base/start_print.cfg",
    "macros/base/cancel_print.cfg",
    "macros/base/end_print.cfg",
    "config/machine.cfg",
)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def variable(text: str, name: str, default: str = "") -> str:
    match = re.search(rf"^\s*variable_{re.escape(name)}\s*:\s*(.*?)\s*$", text, re.M)
    if not match:
        return default
    value = strip_inline_comment(match.group(1)).strip()
    try:
        return str(ast.literal_eval(value))
    except (SyntaxError, ValueError):
        return value


def strip_inline_comment(value: str) -> str:
    in_single_quote = False
    in_double_quote = False
    for index, char in enumerate(value):
        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
        elif char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
        elif char == "#" and not in_single_quote and not in_double_quote:
            return value[:index].strip()
    return value.strip()


def includes_hook(text: str, hook_family: str) -> bool:
    if not hook_family:
        return False
    return hook_family in hook_includes(text)


def hook_includes(text: str) -> list[str]:
    return re.findall(r"^\s*\[include\s+[^\]]*hooks/([A-Za-z0-9_-]+)\.cfg\s*\]\s*(?:#.*)?$", text, re.M)


def has_macro(text: str, macro: str) -> bool:
    return re.search(rf"^\s*\[gcode_macro\s+{re.escape(macro)}\]\s*(?:#.*)?$", text, re.M) is not None


def calls_macro(text: str, macro: str) -> bool:
    return re.search(rf"^\s*{re.escape(macro)}\b", text, re.M) is not None


def macro_bodies(text: str) -> dict[str, str]:
    bodies: dict[str, str] = {}
    headers = list(re.finditer(r"^\s*\[gcode_macro\s+([^\]]+)\]\s*(?:#.*)?$", text, re.M))
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        bodies[header.group(1).strip()] = text[header.end():end]
    return bodies


def validate_guard_cleanup(repo: Path) -> list[str]:
    errors: list[str] = []

    framework_file = repo / "macros" / "base" / "probing" / "virtual_z_probe.cfg"
    framework_text = read(framework_file)
    if not has_macro(framework_text, GUARD_RESET_MACRO):
        errors.append(f"{framework_file}: {GUARD_RESET_MACRO} is not defined")

    for relative_path in GUARD_CLEANUP_CALLERS:
        path = repo / relative_path
        if not calls_macro(read(path), GUARD_RESET_MACRO):
            errors.append(f"{path}: must call {GUARD_RESET_MACRO} to clear a stale contact guard")

    # Guarded wrappers in the framework must pair enter with exit in the same macro.
    # generic_probe.cfg is the one sanctioned cross-macro pair: ACTIVATE_PROBE enters
    # and DEACTIVATE_PROBE exits.
    for name, body in macro_bodies(framework_text).items():
        if calls_macro(body, GUARD_ENTER_MACRO) and not calls_macro(body, GUARD_EXIT_MACRO):
            errors.append(f"{framework_file}: [gcode_macro {name}] enters the contact guard but never exits it")

    generic_file = repo / "macros" / "base" / "probing" / "generic_probe.cfg"
    activate_body = macro_bodies(read(generic_file)).get("ACTIVATE_PROBE", "")
    enter_call = re.search(rf"^\s*{GUARD_ENTER_MACRO}\b", activate_body, re.M)
    reset_call = re.search(rf"^\s*{GUARD_RESET_MACRO}\b", activate_body, re.M)
    if enter_call and (reset_call is None or reset_call.start() > enter_call.start()):
        errors.append(f"{generic_file}: ACTIVATE_PROBE must clear a stale contact guard before entering a new one")

    return errors


def validate_repo_layout(repo: Path) -> list[str]:
    errors: list[str] = []
    if not repo.is_dir():
        return [f"repository root does not exist or is not a directory: {repo}"]

    for relative_path, expected_type in REQUIRED_REPO_PATHS.items():
        path = repo / relative_path
        if expected_type == "dir" and not path.is_dir():
            errors.append(f"required directory is missing: {path}")
        elif expected_type == "file" and not path.is_file():
            errors.append(f"required file is missing: {path}")

    return errors


def validate(repo: Path) -> list[str]:
    errors = validate_repo_layout(repo)
    if errors:
        return errors

    errors.extend(validate_guard_cleanup(repo))

    profiles_dir = repo / "config" / "hardware" / "probes"
    hooks_dir = repo / "macros" / "base" / "probing" / "hooks"

    for banned in BANNED_NAMES:
        if (profiles_dir / banned).exists():
            errors.append(f"unmerged PR compatibility alias must not exist: {profiles_dir / banned}")

    for profile in sorted(profiles_dir.glob("*.cfg")):
        text = read(profile)
        hook_family = variable(text, "probe_hook_family")
        needs_contact_guard = variable(text, "probe_needs_contact_temp_guard", "False").lower() == "true"
        z_home_mode = variable(text, "probe_contact_z_home_mode", "none")
        startprint_home_mode = variable(text, "probe_contact_z_home_startprint_mode", z_home_mode)
        auto_cal_mode = variable(text, "probe_contact_auto_calibrate_mode", "none")
        declared_hook_includes = hook_includes(text)

        for mode_name, mode in {
            "probe_contact_z_home_mode": z_home_mode,
            "probe_contact_z_home_startprint_mode": startprint_home_mode,
            "probe_contact_auto_calibrate_mode": auto_cal_mode,
        }.items():
            if mode not in VALID_MODES:
                errors.append(f"{profile}: {mode_name} has invalid value {mode!r}; valid values are {sorted(VALID_MODES)}")

        if hook_family and z_home_mode == "none" and startprint_home_mode == "none" and auto_cal_mode == "none":
            errors.append(f"{profile}: hook family {hook_family!r} is set but both contact operation modes are none")

        if len(declared_hook_includes) > 1:
            errors.append(f"{profile}: includes multiple hook families {declared_hook_includes}; include exactly one hook family per profile")

        if hook_family and not includes_hook(text, hook_family):
            errors.append(f"{profile}: hook family {hook_family!r} is set but hooks/{hook_family}.cfg is not included")

        for included_hook in declared_hook_includes:
            included_hook_file = hooks_dir / f"{included_hook}.cfg"
            if not included_hook_file.exists():
                errors.append(f"{profile}: includes hooks/{included_hook}.cfg but {included_hook_file} does not exist")
            if hook_family != included_hook:
                errors.append(f"{profile}: includes hooks/{included_hook}.cfg but probe_hook_family is {hook_family!r}")

        hook_file = hooks_dir / f"{hook_family}.cfg" if hook_family else None
        hook_text = read(hook_file) if hook_file and hook_file.exists() else ""

        if z_home_mode == "hook":
            if not hook_file or not hook_file.exists():
                errors.append(f"{profile}: contact Z home uses hook mode but {hook_file} does not exist")
            elif not has_macro(hook_text, HOOK_MACROS["contact_z_home"]):
                errors.append(f"{profile}: contact Z home uses hook mode but {HOOK_MACROS['contact_z_home']} is missing")

        if startprint_home_mode == "hook":
            if not hook_file or not hook_file.exists():
                errors.append(f"{profile}: contact start-print Z home uses hook mode but {hook_file} does not exist")
            elif not has_macro(hook_text, HOOK_MACROS["contact_z_home"]):
                errors.append(f"{profile}: contact start-print Z home uses hook mode but {HOOK_MACROS['contact_z_home']} is missing")

        if auto_cal_mode == "hook":
            if not hook_file or not hook_file.exists():
                errors.append(f"{profile}: contact auto-calibrate uses hook mode but {hook_file} does not exist")
            elif not has_macro(hook_text, HOOK_MACROS["contact_auto_calibrate"]):
                errors.append(f"{profile}: contact auto-calibrate uses hook mode but {HOOK_MACROS['contact_auto_calibrate']} is missing")

        if hook_family and needs_contact_guard:
            if not hook_file or not hook_file.exists():
                errors.append(f"{profile}: contact guard uses hook family {hook_family!r} but {hook_file} does not exist")
            else:
                for lifecycle_name in ("contact_activate", "contact_deactivate"):
                    lifecycle_macro = HOOK_MACROS[lifecycle_name]
                    if not has_macro(hook_text, lifecycle_macro):
                        errors.append(f"{profile}: contact guard uses hook family {hook_family!r} but {lifecycle_macro} is missing")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Klippain virtual-Z probe framework contracts.")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="Repository root")
    args = parser.parse_args()

    errors = validate(args.repo.resolve())
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("virtual-Z probe framework contracts OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
