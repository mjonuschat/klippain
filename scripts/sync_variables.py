#!/usr/bin/env python3
"""Regenerate a user's variables.cfg from the Klippain template.

Adds variables introduced since the file was created, while preserving every
value the user changed and every variable they added themselves. Refuses to
write rather than guess: see the abort conditions in validate_target.
"""

import ast
import configparser
import contextlib
import errno
import fcntl
import hashlib
import io
import json
import os
import pathlib
import re
import stat
import tempfile
import time
from dataclasses import dataclass, field


class SyncError(Exception):
    """Any condition that must abort the sync without writing."""


# --- Klipper's pipeline (klippy/configfile.py) -----------------------------

def decode(raw):
    if raw.startswith(b"\xef\xbb\xbf"):
        raise SyncError(
            "file starts with a UTF-8 byte order mark, which stops Klipper "
            "recognising the first section header"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SyncError("file is not valid UTF-8: %s" % exc)
    return text.replace("\r\n", "\n")


def strip_hash(line):
    pos = line.find("#")
    return line if pos < 0 else line[:pos]


def strip_semi(line):
    for i, char in enumerate(line):
        if char == ";" and (i == 0 or line[i - 1].isspace()):
            return line[:i]
    return line


def parse_effective(text):
    lines = [strip_hash(line) for line in text.split("\n")]
    parser = configparser.RawConfigParser(
        strict=False, inline_comment_prefixes=(";", "#")
    )
    try:
        parser.read_file(io.StringIO("\n".join(lines)), "variables.cfg")
    except configparser.Error as exc:
        raise SyncError("cannot parse the file: %s" % exc)
    return {name: dict(parser[name]) for name in parser.sections()}


def check_literal(name, value):
    """Apply gcode_macro.py's own check to the options it applies it to."""
    if not name.startswith("variable_"):
        return
    try:
        json.dumps(ast.literal_eval(value), separators=(",", ":"))
    except (SyntaxError, TypeError, ValueError) as exc:
        raise SyncError("%s is not a valid literal: %s" % (name, exc))


# --- Layout scanner -------------------------------------------------------

SECTION_RE = configparser.RawConfigParser.SECTCRE
OPTION_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<name>[^\s:=\[][^:=]*?)\s*(?P<delim>[:=])")


@dataclass
class Option:
    name: str
    key_line: int
    span_end: int
    base_indent: int
    delimiter: str


@dataclass
class Section:
    name: str
    header_line: int
    options: list = field(default_factory=list)


@dataclass
class Document:
    lines: list
    sections: list


def line_kind(line):
    """Classify a line before any indentation test.

    ';' lines are discarded by configparser and '#' lines are blanked by
    Klipper, so neither can end a value span - but only the '#' case leaves a
    blank line behind as part of the value.
    """
    if line.strip().startswith(";"):
        return "semi"
    if not strip_hash(line).strip():
        return "blank"
    return "content"


def scan_document(text):
    lines = text.split("\n")
    doc = Document(lines=lines, sections=[])
    section = None
    option = None
    for index, line in enumerate(lines):
        if line_kind(line) != "content":
            continue
        indent = len(line) - len(line.lstrip())
        if option is not None and indent > option.base_indent:
            option.span_end = index
            continue
        stripped = strip_hash(line)
        # configparser strips a line before matching SECTCRE, so an indented
        # header is still a header once the continuation test above has passed.
        header = SECTION_RE.match(stripped.strip())
        if header is not None and header.group("header"):
            section = Section(name=header.group("header"), header_line=index)
            doc.sections.append(section)
            option = None
            continue
        match = OPTION_RE.match(stripped)
        if match is None or section is None:
            raise SyncError(
                "line %d is not an option or a section header: %r"
                % (index + 1, line)
            )
        option = Option(
            name=match.group("name").strip().lower(),
            key_line=index,
            span_end=index,
            base_indent=indent,
            delimiter=match.group("delim"),
        )
        section.options.append(option)
    return doc


# --- Target validation ---------------------------------------------------

OWNED_SECTION = "gcode_macro _USER_VARIABLES"
SAVE_SECTION = "save_variables"


def find_section(doc, name):
    for section in doc.sections:
        if section.name == name:
            return section
    return None


def validate_target(doc, values):
    owned = [s for s in doc.sections if s.name == OWNED_SECTION]
    if not owned:
        raise SyncError("file has no [gcode_macro _USER_VARIABLES] section")
    if len(owned) > 1:
        raise SyncError(
            "[gcode_macro _USER_VARIABLES] appears %d times; merge them into one"
            % len(owned)
        )
    section = owned[0]

    for other in doc.sections:
        if other.name not in (OWNED_SECTION, SAVE_SECTION):
            raise SyncError(
                "unrecognised section [%s]; move it to overrides.cfg and run "
                "the sync again" % other.name
            )

    seen = {}
    for option in section.options:
        if option.name in seen:
            raise SyncError(
                "option '%s' appears more than once, on lines %d and %d; "
                "delete the one you do not want"
                % (option.name, seen[option.name] + 1, option.key_line + 1)
            )
        seen[option.name] = option.key_line

    gcodes = [o for o in section.options if o.name == "gcode"]
    if not gcodes:
        raise SyncError("section has no 'gcode:' option")
    # A repeated gcode: needs no branch of its own - the duplicate-name check
    # above has already refused it, with a message that names both lines.
    if section.options[-1].name != "gcode":
        raise SyncError("'gcode:' must be the last option in the section")

    for name, value in values.get(OWNED_SECTION, {}).items():
        check_literal(name, value)


# --- Migration tables and classification -------------------------------------

NAME_RE = re.compile(r"^[a-z0-9_]+$")

# Add an entry here when a variable is renamed or dropped, so existing users
# keep their value instead of accumulating a dead one in ## Custom variables.
RENAMES = {}
REMOVED = set()


def validate_migrations(template_names):
    for name in list(RENAMES) + list(RENAMES.values()) + list(REMOVED):
        if not NAME_RE.match(name):
            raise SyncError("migration name %r is not canonical" % name)
    for source, dest in RENAMES.items():
        if dest not in template_names:
            raise SyncError("rename destination '%s' is not in the template" % dest)
        if source in template_names:
            raise SyncError("rename source '%s' is still in the template" % source)
        if source in REMOVED:
            raise SyncError("'%s' is both a rename source and removed" % source)
        if dest in REMOVED:
            raise SyncError("rename destination '%s' is removed" % dest)
        if dest in RENAMES:
            raise SyncError("rename '%s' -> '%s' is chained" % (source, dest))
    for name in REMOVED:
        if name in template_names:
            raise SyncError("removed name '%s' is still in the template" % name)
    seen = {}
    for source, dest in RENAMES.items():
        if dest in seen:
            raise SyncError(
                "'%s' and '%s' share the destination '%s'" % (seen[dest], source, dest)
            )
        seen[dest] = source


def classify(user_names, template_names):
    """Classify every user name exactly once, in a fixed order.

    Order matters: applying renames after the custom bucket would emit a
    renamed variable twice, once under each name.
    """
    mapping = {}
    custom = []
    for name in user_names:
        if name in REMOVED:
            continue
        target = RENAMES.get(name)
        if target is not None:
            if target in user_names:
                raise SyncError(
                    "file contains both '%s' and its replacement '%s'; delete "
                    "the obsolete one" % (name, target)
                )
            mapping[name] = target
        elif name in template_names:
            mapping[name] = name
        else:
            custom.append(name)
    return mapping, custom


def inline_comment(line):
    pos = line.find("#")
    return "" if pos < 0 else line[pos:].rstrip()


def reemit_span(doc, option):
    """Emit an option's span with Klipper's own per-line comment handling.

    The two comment characters are not interchangeable inside a value: a ';'
    line is discarded by configparser, while a '#' line is blanked by Klipper
    and the resulting blank line is part of the value.
    """
    key = doc.lines[option.key_line]
    match = OPTION_RE.match(key)
    out = [strip_semi(strip_hash(key[match.end():])).strip()]
    for index in range(option.key_line + 1, option.span_end + 1):
        line = doc.lines[index]
        kind = line_kind(line)
        if kind == "semi":
            continue
        if kind == "blank":
            out.append("")
            continue
        out.append(strip_semi(strip_hash(line)).rstrip())
    return out


SAVE_VARIABLES_DEFAULT = "~/printer_data/config/save_variables.cfg"


def _option_at(section, index):
    for option in section.options:
        if option.key_line == index:
            return option
    return None


def _is_comment_line(line):
    stripped = line.strip()
    return stripped.startswith("#") or stripped.startswith(";")


def _comment_block_above(doc, option, skip=()):
    start = option.key_line
    while start > 0 and _is_comment_line(doc.lines[start - 1]):
        start -= 1
    return [line for line in doc.lines[start:option.key_line] if line not in skip]


def _emit_option(name, value_lines, comment=""):
    head = "%s: %s" % (name, value_lines[0])
    if comment:
        head = "%s %s" % (head.rstrip(), comment)
    return [head.rstrip()] + value_lines[1:]


def _custom_block(doc, options, custom, skip=()):
    if not custom:
        return []
    out = ["", "## Custom variables", "## Added by you; Klippain does not ship these.", ""]
    for name in custom:
        option = options[name]
        # No span check is needed here: scan_document only extends a span with
        # lines indented deeper than the key, and refuses anything at or below
        # it that is neither an option nor a section header.
        out.extend(_comment_block_above(doc, option, skip))
        out.extend(_emit_option(name, reemit_span(doc, option)))
    return out


def gcode_payload(doc):
    """Everything of a gcode option except its name: the text after the
    delimiter, then every continuation line."""
    section = find_section(doc, OWNED_SECTION)
    options = [o for o in section.options if o.name == "gcode"] if section else []
    if not options:
        raise SyncError("file has no gcode: option to read a payload from")
    option = options[0]
    key = doc.lines[option.key_line]
    return ([key[OPTION_RE.match(key).end():]]
            + doc.lines[option.key_line + 1:option.span_end + 1])


def has_gcode_body(doc):
    """True when the gcode option has a body Klipper would actually see.

    Raw text is the wrong test: `gcode: # note` and a continuation holding only
    a comment look non-empty but reduce to nothing, and preserving them instead
    of falling back to the shipped bare `gcode:` would carry noise forward.
    """
    return any(
        strip_semi(strip_hash(part)).strip() for part in gcode_payload(doc)
    )


def _gcode_lines(tdoc, toption, udoc, options):
    option = options.get("gcode")
    if option is None or not has_gcode_body(udoc):
        return tdoc.lines[toption.key_line:toption.span_end + 1]
    key = udoc.lines[option.key_line]
    match = OPTION_RE.match(key)
    # Take the name and the indentation from the template, the delimiter, spacing
    # and value from the user. Keeping the user's indentation would emit a key
    # deeper than the option above it, and Klipper would read the whole gcode
    # option as that option's continuation text and lose it.
    tkey = tdoc.lines[toption.key_line]
    tmatch = OPTION_RE.match(tkey)
    head = tkey[:tmatch.start("name")] + "gcode" + key[match.end("name"):]
    return [head] + udoc.lines[option.key_line + 1:option.span_end + 1]


def _section_end(doc, section):
    """Last line of a section: everything up to the next header, or EOF."""
    starts = sorted(s.header_line for s in doc.sections if s.header_line > section.header_line)
    end = (starts[0] - 1) if starts else (len(doc.lines) - 1)
    while end > section.header_line and not doc.lines[end].strip():
        end -= 1          # trim trailing blanks so repeated syncs cannot grow the file
    return end


def save_variables_kept(udoc, uvalues):
    sections = [s for s in udoc.sections if s.name == SAVE_SECTION]
    if not sections:
        return []
    if len(sections) == 1 and len(sections[0].options) == 1:
        only = sections[0].options[0]
        if only.name == "filename":
            if uvalues[SAVE_SECTION]["filename"] == SAVE_VARIABLES_DEFAULT:
                return []
    return sections


def _save_variables_lines(udoc, uvalues):
    sections = save_variables_kept(udoc, uvalues)
    if not sections:
        return []
    out = [""]
    for section in sections:
        out.extend(udoc.lines[section.header_line:_section_end(udoc, section) + 1])
    # _section_end trims trailing blanks, so restore the one that carries the
    # file's final newline. Trim-then-restore keeps repeated syncs idempotent.
    return out + [""]


def render(tdoc, tvalues, udoc, uvalues, mapping, custom):
    tsection = find_section(tdoc, OWNED_SECTION)
    usection = find_section(udoc, OWNED_SECTION)
    uoptions = {o.name: o for o in usection.options}
    by_shipped = {shipped: user for user, shipped in mapping.items()}
    towned = tvalues[OWNED_SECTION]
    uowned = uvalues[OWNED_SECTION]

    out = []
    index = 0
    while index < len(tdoc.lines):
        option = _option_at(tsection, index)
        if option is None:
            out.append(tdoc.lines[index])
            index += 1
            continue
        if option.name == "gcode":
            # A custom block must not swallow the template's own trailing
            # comment block (e.g. "## Do not remove the next line"), which is
            # emitted separately just below - or that block would be rendered
            # twice.
            template_skip = _comment_block_above(tdoc, option)
            out.extend(_custom_block(udoc, uoptions, custom, template_skip))
            out.extend(_gcode_lines(tdoc, option, udoc, uoptions))
        else:
            source = by_shipped.get(option.name)
            # An unchanged value re-emits the template's own line. Some shipped
            # lines align their comments with several spaces, and rebuilding
            # those would make a no-op sync look like a change.
            if source is None or uowned[source] == towned[option.name]:
                out.extend(tdoc.lines[option.key_line:option.span_end + 1])
            else:
                out.extend(_emit_option(
                    option.name,
                    reemit_span(udoc, uoptions[source]),
                    inline_comment(tdoc.lines[option.key_line]),
                ))
        index = option.span_end + 1
    out.extend(_save_variables_lines(udoc, uvalues))
    return "\n".join(out)


def _save_blocks(doc):
    return [doc.lines[s.header_line:_section_end(doc, s) + 1]
            for s in doc.sections if s.name == SAVE_SECTION]


def validate_render(rendered, intended, gcode=None, saves=None):
    """Prove the render against the merge's intent.

    `intended` maps a section name to the effective values it must produce.
    `gcode` and `saves` are compared against the rendered file's own gcode
    option and save sections - anchored to their location, not searched for
    anywhere in the text, since identical lines elsewhere would otherwise
    satisfy the check while the real block was altered.
    """
    doc = scan_document(rendered)
    values = parse_effective(rendered)
    validate_target(doc, values)
    if gcode is not None and gcode_payload(doc) != list(gcode):
        raise SyncError("render did not preserve the gcode body verbatim")
    if saves is not None and _save_blocks(doc) != [list(b) for b in saves]:
        raise SyncError("render did not preserve [save_variables] verbatim")
    # A save section before the owned one would put every variable after it in
    # the wrong section. Checking the headers is enough: a header ends the owned
    # section, so gcode - which validate_target proves is its last option -
    # necessarily precedes any save header that follows the owned header.
    owned_header = find_section(doc, OWNED_SECTION).header_line
    for section in doc.sections:
        if section.name == SAVE_SECTION and section.header_line < owned_header:
            raise SyncError(
                "[save_variables] is emitted before [gcode_macro _USER_VARIABLES]"
            )
    for section, options in intended.items():
        if section not in values:
            raise SyncError("render dropped the [%s] section" % section)
        produced = values.get(section, {})
        extra = set(produced) - set(options)
        if extra:
            raise SyncError(
                "render of [%s] added unintended variable(s): %s"
                % (section, ", ".join(sorted(extra)))
            )
        missing = set(options) - set(produced)
        if missing:
            raise SyncError(
                "render of [%s] is missing variable(s): %s"
                % (section, ", ".join(sorted(missing)))
            )
        for name, value in options.items():
            if produced.get(name) != value:
                raise SyncError(
                    "render lost %s: expected %r, produced %r"
                    % (name, value, produced.get(name))
                )
    for section in values:
        if section not in intended:
            raise SyncError("render produced an unexpected section [%s]" % section)


def plan_sync(template_text, target_text):
    tdoc = scan_document(template_text)
    tvalues = parse_effective(template_text)
    tsection = find_section(tdoc, OWNED_SECTION)
    if tsection is None:
        raise SyncError("template has no [gcode_macro _USER_VARIABLES] section")
    tnames = {o.name for o in tsection.options}
    validate_migrations(tnames - {"gcode"})

    if target_text is None:
        validate_render(template_text, {OWNED_SECTION: tvalues[OWNED_SECTION]})
        return template_text, True

    udoc = scan_document(target_text)
    uvalues = parse_effective(target_text)
    validate_target(udoc, uvalues)
    usection = find_section(udoc, OWNED_SECTION)
    unames = [o.name for o in usection.options if o.name != "gcode"]
    mapping, custom = classify(unames, tnames)

    owned = uvalues[OWNED_SECTION]
    variables = dict(tvalues[OWNED_SECTION])
    for source, shipped in mapping.items():
        variables[shipped] = owned[source]
    for name in custom:
        variables[name] = owned[name]
    # A user's macro body is preserved, so it - not the template's empty
    # gcode: - is what the render must reproduce.
    variables["gcode"] = owned.get("gcode", tvalues[OWNED_SECTION].get("gcode", ""))

    intended = {OWNED_SECTION: variables}
    # Only assert the gcode payload when the user actually has a body: an empty
    # one falls back to the template's line, which may differ in trailing space.
    expected_gcode = gcode_payload(udoc) if has_gcode_body(udoc) else None
    kept = save_variables_kept(udoc, uvalues)
    expected_saves = None
    if kept:
        intended[SAVE_SECTION] = uvalues[SAVE_SECTION]
        expected_saves = [udoc.lines[k.header_line:_section_end(udoc, k) + 1]
                          for k in kept]

    rendered = render(tdoc, tvalues, udoc, uvalues, mapping, custom)
    validate_render(rendered, intended, expected_gcode, expected_saves)
    return rendered, rendered != target_text


DEFAULT_BACKUP_ROOT = pathlib.Path.home() / "klippain_config_backups" / "variables"
BACKUP_ROOT = DEFAULT_BACKUP_ROOT
LINK_FALLBACK_ERRNOS = {errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS}
MAX_BACKUP_SUFFIXES = 100


def backup_dir():
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    os.chmod(BACKUP_ROOT, 0o700)
    return BACKUP_ROOT


def _write_exclusive(candidate, data):
    handle = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(handle, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _fsync_dir(directory):
    handle = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


def _matches_existing(candidate, data):
    """True only for a regular file holding exactly `data`.

    The type check and the read go through one descriptor opened O_NOFOLLOW,
    so the entry cannot be swapped for a symlink between checking and reading.
    """
    try:
        handle = os.open(str(candidate), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        # O_NOFOLLOW reports a symlink as ELOOP on Linux and EMLINK on some
        # BSDs; both mean the same thing here.
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise SyncError(
                "%s exists but is not a regular file; move it aside" % candidate
            )
        raise SyncError(
            "%s exists but could not be verified: %s" % (candidate, exc)
        )
    try:
        if not stat.S_ISREG(os.fstat(handle).st_mode):
            raise SyncError(
                "%s exists but is not a regular file; move it aside" % candidate
            )
        try:
            with os.fdopen(os.dup(handle), "rb") as stream:
                return stream.read() == data
        except OSError as exc:
            raise SyncError("could not read %s: %s" % (candidate, exc))
    finally:
        os.close(handle)


def write_backup(data, directory=None, stamp=None):
    directory = pathlib.Path(directory) if directory else backup_dir()
    digest = hashlib.sha256(data).hexdigest()[:12]
    stamp = stamp or time.strftime("%Y_%m_%d-%H%M%S")
    base = directory / ("variables.cfg.%s-%s" % (stamp, digest))
    # A unique temporary name: two syncs of different targets share this
    # directory and are not serialised by the per-target lock.
    handle, temp_name = tempfile.mkstemp(dir=str(directory), prefix=".tmp-")
    temp = pathlib.Path(temp_name)
    use_link = True
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        index = 0
        while index < MAX_BACKUP_SUFFIXES:
            candidate = base if index == 0 else pathlib.Path("%s.%d" % (base, index))
            try:
                if use_link:
                    os.link(temp, candidate)
                else:
                    _write_exclusive(candidate, data)
            except FileExistsError:
                if _matches_existing(candidate, data):
                    return candidate
                index += 1
                continue
            except OSError as exc:
                if use_link and exc.errno in LINK_FALLBACK_ERRNOS:
                    # Switch to the fallback and retry THIS candidate. Advancing
                    # the index here would skip the canonical name entirely, so
                    # every backup on a link-less filesystem would carry a
                    # spurious suffix.
                    use_link = False
                    continue
                raise SyncError("could not create backup %s: %s" % (candidate, exc))
            _fsync_dir(directory)
            if candidate.read_bytes() != data:
                raise SyncError("backup %s does not match the source" % candidate)
            return candidate
        raise SyncError("could not find a free backup name after %d attempts"
                        % MAX_BACKUP_SUFFIXES)
    except OSError as exc:
        raise SyncError("could not write a backup in %s: %s" % (directory, exc))
    finally:
        if temp.exists():
            temp.unlink()


def lock_path_for(target):
    key = hashlib.sha256(
        os.path.realpath(str(target)).encode("utf-8")
    ).hexdigest()[:12]
    return BACKUP_ROOT / (".lock-%s" % key)


@contextlib.contextmanager
def target_lock(target):
    """Serialise syncs of the same file.

    Advisory, so it binds other syncs but not an editor or a web UI; the
    pre-replacement comparison in replace_atomically narrows that window
    without closing it.
    """
    try:
        backup_dir()
        path = lock_path_for(target)
        handle = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        raise SyncError("could not prepare the lock for %s: %s" % (target, exc))
    try:
        # O_CREAT's mode applies only when the file is created, so an existing
        # lock left with wider permissions is narrowed explicitly.
        os.fchmod(handle, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
        except OSError as exc:
            raise SyncError("could not lock %s: %s" % (path, exc))
        yield
    finally:
        os.close(handle)


def replace_atomically(target, data, original):
    try:
        current = target.read_bytes() if target.exists() else None
    except OSError as exc:
        raise SyncError("could not re-read %s: %s" % (target, exc))
    if current != original:
        raise SyncError(
            "%s changed while the sync was running; nothing was written" % target
        )
    handle, temp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp-")
    temp = pathlib.Path(temp_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # mkstemp creates the file 0600; without this, os.replace would carry
        # that mode over the target and silently tighten every existing file.
        if original is not None:
            os.chmod(temp, stat.S_IMODE(os.stat(target).st_mode))
        else:
            # A fixed mode, matching what install.sh's `cp` produces under a
            # default umask, rather than briefly mutating process-global state
            # (os.umask has no read-only form) just to learn one.
            os.chmod(temp, 0o644)
        os.replace(str(temp), str(target))
    except OSError as exc:
        raise SyncError("could not write %s: %s" % (target, exc))
    finally:
        if temp.exists():
            temp.unlink()


import argparse
import difflib
import sys

AUTOUPDATE_VARIABLE = "variable_klippain_variables_autoupdate"
DEFAULT_TEMPLATE = pathlib.Path.home() / "klippain_config" / "user_templates" / "variables.cfg"
DEFAULT_TARGET = pathlib.Path.home() / "printer_data" / "config" / "variables.cfg"
FALSEY = {"false", "0", "no", "off"}


def _read(path):
    if not path.exists():
        return None, None
    raw = path.read_bytes()
    return raw, decode(raw)


def _opted_out(target_text):
    if target_text is None:
        return False
    values = parse_effective(target_text).get(OWNED_SECTION, {})
    return values.get(AUTOUPDATE_VARIABLE, "True").strip().lower() in FALSEY


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", type=pathlib.Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--target", type=pathlib.Path, default=DEFAULT_TARGET)
    parser.add_argument("--diff", action="store_true", help="show changes, write nothing")
    parser.add_argument("--check", action="store_true", help="exit 1 if out of date")
    parser.add_argument("--force", action="store_true", help="ignore the opt-out variable")
    parser.add_argument(
        "--backup-root", type=pathlib.Path, default=None,
        help="where backups and lock files live (default: "
             "~/klippain_config_backups/variables; used by the tests)",
    )
    args = parser.parse_args(argv)

    global BACKUP_ROOT
    BACKUP_ROOT = (
        args.backup_root if args.backup_root is not None else DEFAULT_BACKUP_ROOT
    )

    try:
        _, template_text = _read(args.template)
        if template_text is None:
            raise SyncError("template not found: %s" % args.template)
        # Resolved once, before the lock is taken, so the lock and every step
        # below agree on the same path: resolving separately for the lock and
        # for the work leaves a window where the two could disagree if the
        # target is a symlink that gets retargeted in between.
        resolved_target = pathlib.Path(os.path.realpath(str(args.target)))
        with target_lock(resolved_target):
            # Every read of the target happens under the lock, the opt-out
            # check included, so a concurrent sync cannot change the decision
            # between reading it and acting on it.
            original, target_text = _read(resolved_target)
            if _opted_out(target_text) and not args.force:
                print("[SYNC] %s is set to False; leaving variables.cfg alone"
                      % AUTOUPDATE_VARIABLE)
                return 0
            rendered, changed = plan_sync(template_text, target_text)
            if not changed:
                if not args.diff:
                    print("[SYNC] variables.cfg is already up to date")
                return 0
            if args.diff:
                sys.stdout.writelines(difflib.unified_diff(
                    (target_text or "").splitlines(keepends=True),
                    rendered.splitlines(keepends=True),
                    fromfile=str(args.target), tofile="%s (synced)" % args.target,
                ))
                return 0
            if args.check:
                return 1
            data = rendered.encode("utf-8")
            # `original` is the target's raw bytes (CRLF and all), never the
            # LF-normalised text: it is what replace_atomically compares
            # against the file on disk, and what the backup preserves.
            if original is not None:
                backup = write_backup(original)
                print("[SYNC] previous file saved as %s" % backup)
            replace_atomically(resolved_target, data, original)
            print("[SYNC] variables.cfg updated from the Klippain template")
            return 0
    except SyncError as exc:
        print("[SYNC] %s" % exc, file=sys.stderr)
        return 2
    except OSError as exc:
        # Anything the filesystem raises reaches the user as the documented
        # abort, never as a traceback with a different exit code.
        print("[SYNC] %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
