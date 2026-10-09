"""Resolve a library nickname the way KiCad does.

KiCad looks a nickname up in the project's library table first and the global
one second. Within a table the first row with that name wins, and a
``(type "Table")`` row stands for every row of the table it points at, read in
its place. KiCad 10's own global table is a single such row, and companies put
their libraries behind one too, so a lookup that does not follow them finds
none of those libraries.

URIs are expanded with the same variables ``list_library_table`` uses: KiCad's
built-in library directories, the path variables in ``kicad_common.json``, the
environment, and ``${KIPRJMOD}``.

``DynamicSymbolLoader`` resolves symbol libraries through ``find_library``; the
library managers use ``watched_files`` and ``stamp`` to notice an edited table
and reload instead of serving the libraries they read at startup.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, NamedTuple, Optional, Set, Tuple

from commands.library_tables import (
    _TABLES,
    _VAR_RE,
    _is_table_ref,
    _parse_entries,
    _path_vars,
    _resolve_uri,
    global_table_path,
)
from utils.file_io import read_text_preserve_newline
from utils.platform_helper import PlatformHelper

logger = logging.getLogger("kicad_interface")


class ResolvedLibrary(NamedTuple):
    """One library row, with its URI expanded."""

    name: str
    #: The row's ``(type ...)``: "KiCad", "Database", "Legacy", ...
    lib_type: str
    #: The URI as the table writes it.
    uri: str
    #: The URI with every known variable expanded.
    path: Path
    #: Whether *path* exists on disk.
    exists: bool
    #: The table the row is in (a nested table for a row behind a Table row).
    table: Path
    #: Variables in the URI that nothing defines, e.g. ["VM_LIBS"].
    unresolved_vars: List[str]

    @property
    def is_database(self) -> bool:
        return self.lib_type.strip().lower() == "database"


def _top_tables(table_type: str, project_dir: Optional[Path]) -> List[Path]:
    """The project table (when there is one), then the global table."""
    tables: List[Path] = []
    if project_dir is not None:
        project_table = Path(project_dir) / _TABLES[table_type][0]
        if project_table.is_file():
            tables.append(project_table)
    global_table = global_table_path(table_type)
    if global_table is not None:
        tables.append(global_table)
    return tables


def _walk(
    table_path: Path,
    project_dir: Optional[Path],
    path_vars: Dict[str, str],
    seen: Set[str],
) -> Iterator[ResolvedLibrary]:
    """Every library row of *table_path* in table order, Table rows followed."""
    real = os.path.realpath(table_path)
    if real in seen:
        logger.warning(f"Library table {table_path} includes itself; not read again")
        return
    seen.add(real)
    try:
        text, _newline = read_text_preserve_newline(table_path)
    except OSError as e:
        logger.warning(f"Cannot read library table {table_path}: {e}")
        return
    for entry in _parse_entries(text):
        if "(disabled" in text[entry["start"] : entry["end"]]:
            continue
        expanded, exists = _resolve_uri(entry["uri"], table_path, path_vars, project_dir)
        if _is_table_ref(entry):
            if exists and Path(expanded).is_file():
                yield from _walk(Path(expanded), project_dir, path_vars, seen)
            else:
                logger.warning(
                    f"Table row '{entry['name']}' in {table_path} points at "
                    f"{entry['uri']}, which does not resolve to a file"
                )
            continue
        yield ResolvedLibrary(
            name=entry["name"],
            lib_type=entry["type"],
            uri=entry["uri"],
            path=Path(expanded),
            exists=exists,
            table=table_path,
            unresolved_vars=_VAR_RE.findall(expanded),
        )


def iter_libraries(
    table_type: str,
    project_dir: Optional[Path] = None,
    path_vars: Optional[Dict[str, str]] = None,
) -> Iterator[ResolvedLibrary]:
    """Every library KiCad would see, project rows first, in lookup order.

    A nickname can occur more than once; the first occurrence is the one KiCad
    uses. *table_type* is "symbol" or "footprint".
    """
    variables = path_vars if path_vars is not None else _path_vars(table_type)
    project = Path(project_dir) if project_dir is not None else None
    seen: Set[str] = set()
    for table in _top_tables(table_type, project):
        yield from _walk(table, project, variables, seen)


def find_library(
    name: str, table_type: str, project_dir: Optional[Path] = None
) -> Optional[ResolvedLibrary]:
    """The row KiCad would use for nickname *name*, or None if no table has it.

    The row is returned even when its file is missing or a variable in its URI
    is undefined, so the caller can say which; check ``exists``.
    """
    for library in iter_libraries(table_type, project_dir):
        if library.name == name:
            return library
    return None


def describe_unusable(library: ResolvedLibrary) -> str:
    """Why a row that ``find_library`` returned cannot be opened."""
    where = f"Library '{library.name}' is registered in {library.table} as {library.uri}"
    if library.unresolved_vars:
        names = ", ".join("${" + v + "}" for v in library.unresolved_vars)
        return (
            f"{where}, but {names} is not defined (Preferences > Configure Paths, "
            "or the environment)"
        )
    return f"{where}, but {library.path} does not exist"


def _kicad_common_path() -> Optional[Path]:
    for config_dir in PlatformHelper.kicad_config_dirs():
        candidate = config_dir / "kicad_common.json"
        if candidate.is_file():
            return candidate
    return None


def watched_files(
    table_file: str, project_dir: Optional[Path], tables_read: Iterable[str]
) -> List[Path]:
    """Every file a library list loaded from *tables_read* depends on.

    That is ``kicad_common.json`` (the path variables), the project table, and
    every table that was read, nested ones included. The project table is
    listed even before it exists, so creating one counts as a change.
    *table_file* is "sym-lib-table" or "fp-lib-table".
    """
    files: List[Path] = []
    common = _kicad_common_path()
    if common is not None:
        files.append(common)
    if project_dir is not None:
        files.append(Path(project_dir) / table_file)
    for real in sorted(tables_read):
        if Path(real) not in files:
            files.append(Path(real))
    return files


def stamp(files: List[Path]) -> Tuple[Optional[int], ...]:
    """Modification times of *files*; equal stamps mean nothing was edited."""

    def mtime(path: Path) -> Optional[int]:
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return None

    return tuple(mtime(p) for p in files)
