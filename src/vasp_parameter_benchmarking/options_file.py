"""Read ``setup`` options from a ``key = value`` file (default: ``options.txt``).

An entire ``vasp-parameter-benchmarking setup`` invocation can be saved to a
file instead of being typed on the command line. When an ``options.txt`` exists
in the working directory it is picked up automatically; point at a differently
named file with ``--options``. Command-line flags override values from the file,
which in turn override the built-in defaults.

This file holds the *command-line options* of ``setup`` (mode, paths,
job-naming). The sweep itself - which INCAR tags to vary, and the memory table -
lives in the parameters file (``vasp_parameter_benchmarking_parameters.txt``),
exactly as before.

File format - one ``key = value`` per line::

    # blank lines and whole-line '#' comments are ignored
    mode       = oat
    vasp-files = VASP_Files
    root       = VASP_Parameter_Benchmarking
    name-jobs  = true

Keys are the long option names without the leading ``--`` (e.g. ``vasp-files``);
``-`` and ``_`` are interchangeable, and a leading ``--`` is tolerated. Any
surrounding quotes on a value are stripped. The boolean ``name-jobs`` key
replaces the ``--no-name-jobs`` flag (``name-jobs = false`` is the same as
passing ``--no-name-jobs``). The ``incar`` key may be repeated, one sweep spec
per line, mirroring the repeatable ``--incar`` flag::

    incar = ENCUT=300,400,500
    incar = SIGMA=0.05,0.1
"""

from __future__ import annotations

from pathlib import Path

DEFAULT_OPTIONS_FILE = "options.txt"

# Option keys accepted in the file, as their canonical hyphenated names. Keep in
# sync with the `setup` subparser and _SETUP_OPTIONS in cli.py. `name-jobs` is
# the boolean behind the --no-name-jobs flag.
KNOWN_OPTIONS = (
    "incar",
    "parameters",
    "mode",
    "vasp-files",
    "submit",
    "root",
    "name-jobs",
)

# Keys that may appear on several lines; their values accumulate into a list
# (like a repeatable CLI flag). All other keys must be unique.
REPEATABLE_OPTIONS = ("incar",)


def _canonical_key(key: str) -> str:
    """Normalise a file key to its canonical hyphenated form (a CLI flag name)."""
    return key.strip().lower().lstrip("-").replace("_", "-")


def parse_options_file(path: Path) -> dict[str, str | list[str]]:
    """Parse a ``key = value`` options file into a ``{dest: value}`` dict.

    Returned keys are argparse *dest* names (hyphens converted to underscores),
    ready to merge with parsed CLI arguments. Values are the raw strings (a list
    of them for repeatable keys such as ``incar``); type conversion (booleans,
    mode validation) is left to the caller so the file and the command line go
    through the same checks.

    Unknown keys, missing ``=`` separators, empty values and duplicated
    non-repeatable keys each raise ``ValueError`` (with the file name and line
    number) so mistakes surface instead of being silently ignored.
    """
    path = Path(path)
    options: dict[str, str | list[str]] = {}
    for lineno, raw_line in enumerate(path.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(
                f"{path}:{lineno}: expected 'key = value', got {line!r}"
            )

        key_part, value = line.split("=", 1)
        key = _canonical_key(key_part)
        value = value.strip()
        # Strip a single pair of surrounding quotes, if present.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]

        if key not in KNOWN_OPTIONS:
            valid = ", ".join(KNOWN_OPTIONS)
            raise ValueError(
                f"{path}:{lineno}: unknown option {key!r}. Valid keys are: {valid}"
            )
        if not value:
            raise ValueError(f"{path}:{lineno}: no value given for {key!r}")

        dest = key.replace("-", "_")
        if key in REPEATABLE_OPTIONS:
            options.setdefault(dest, []).append(value)  # type: ignore[union-attr]
        elif dest in options:
            raise ValueError(f"{path}:{lineno}: duplicate option {key!r}")
        else:
            options[dest] = value

    return options


def load_setup_options(
    explicit_path: str | None,
) -> tuple[dict[str, str | list[str]], Path | None]:
    """Load setup options from the options file.

    Returns ``(options, source)``, where ``options`` is a ``{dest: value}`` dict
    and ``source`` is the :class:`~pathlib.Path` read, or ``None`` when no file
    was found. With ``explicit_path`` set (from ``--options``) that file must
    exist; otherwise ``options.txt`` in the working directory is used when present
    and skipped when absent.
    """
    if explicit_path is not None:
        path = Path(explicit_path)
        if not path.is_file():
            raise FileNotFoundError(f"options file not found: {path}")
        return parse_options_file(path), path

    default_path = Path(DEFAULT_OPTIONS_FILE)
    if default_path.is_file():
        return parse_options_file(default_path), default_path
    return {}, None
