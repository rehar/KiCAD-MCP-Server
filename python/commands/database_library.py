"""KiCad database libraries (``.kicad_dbl``).

A database library is a ``(type "Database")`` row in a sym-lib-table whose URI
is a ``.kicad_dbl`` JSON file. That file names an ODBC source and one or more
tables; each table row is a part. A part's symbol name is
``<library name>/<key>`` -- ``Capacitors/120-00449`` is the row of the
"Capacitors" library whose key column holds ``120-00449`` -- and the row says
which ordinary library symbol draws it, which footprint it uses, and which
columns become fields.

KiCad itself only talks ODBC. Here the source is opened with ``pyodbc`` when it
is installed, which serves every driver KiCad does (DSN or connection string).
A ``Driver={SQLite3...}`` source is also opened directly with Python's
``sqlite3`` when ``pyodbc`` or the ODBC driver is missing, since that needs no
ODBC at all. Everything is read-only.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import quote

logger = logging.getLogger("kicad_interface")

_VAR_RE = re.compile(r"\$\{([^}]+)\}")


class DatabaseLibraryError(Exception):
    """The database library cannot be read; the message says why."""


@dataclass
class FieldMapping:
    """One ``fields[]`` entry: a column shown as a symbol field."""

    column: str
    name: str
    visible_on_add: bool = False
    show_name: bool = False


@dataclass
class DbTable:
    """One ``libraries[]`` entry of a ``.kicad_dbl``."""

    name: str
    table: str
    key: str
    symbols: str
    footprints: str
    fields: List[FieldMapping] = field(default_factory=list)
    #: KiCad's built-in properties -> column, e.g. {"description": "Description"}.
    properties: Dict[str, str] = field(default_factory=dict)


@dataclass
class DbPart:
    """Everything one table row contributes to a symbol."""

    #: Symbol name within the database library, ``<library>/<key>``.
    name: str
    #: The library symbol that draws the part, ``Lib:Symbol``.
    symbol: str
    footprints: List[str]
    #: (field name, value, visible, show_name), in the order the .kicad_dbl lists them.
    fields: List[Tuple[str, str, bool, bool]]
    description: str = ""
    keywords: str = ""


def _flag(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.lower() in ("true", "yes", "1"))


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _parse_connection_string(connection_string: str) -> Dict[str, str]:
    """``Key=Value;...`` pairs, keys lower-cased, ``{braced}`` values unwrapped."""
    pairs: Dict[str, str] = {}
    for m in re.finditer(r"\s*([^=;]+?)\s*=\s*(\{[^}]*\}|[^;]*)\s*(?:;|$)", connection_string):
        value = m.group(2).strip()
        if value.startswith("{") and value.endswith("}"):
            value = value[1:-1]
        pairs[m.group(1).strip().lower()] = value
    return pairs


class DatabaseLibrary:
    """A ``.kicad_dbl`` and the database behind it."""

    def __init__(self, dbl_path: Path, path_vars: Optional[Dict[str, str]] = None):
        import json

        self.path = Path(dbl_path)
        try:
            config = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise DatabaseLibraryError(f"Cannot read database library {self.path}: {e}") from e
        if not isinstance(config, dict):
            raise DatabaseLibraryError(f"{self.path} is not a KiCad database library")

        self.name: str = _text(config.get("name"))
        self.source: Dict[str, Any] = config.get("source") or {}
        self.tables: List[DbTable] = []
        for lib in config.get("libraries") or []:
            fields = [
                FieldMapping(
                    column=_text(f.get("column")),
                    name=_text(f.get("name")),
                    visible_on_add=_flag(f.get("visible_on_add")),
                    show_name=_flag(f.get("show_name")),
                )
                for f in lib.get("fields") or []
                if f.get("column") and f.get("name")
            ]
            self.tables.append(
                DbTable(
                    name=_text(lib.get("name")),
                    table=_text(lib.get("table")),
                    key=_text(lib.get("key")),
                    symbols=_text(lib.get("symbols")),
                    footprints=_text(lib.get("footprints")),
                    fields=fields,
                    properties={k: _text(v) for k, v in (lib.get("properties") or {}).items()},
                )
            )
        self._path_vars = dict(path_vars or {})

    # ------------------------------------------------------------------ names

    def split_name(self, symbol_name: str) -> Tuple[DbTable, str]:
        """``Capacitors/120-00449`` -> (the Capacitors table, ``120-00449``).

        Library names may contain spaces and keys may contain slashes, so the
        longest library name that prefixes *symbol_name* wins.
        """
        for table in sorted(self.tables, key=lambda t: len(t.name), reverse=True):
            prefix = table.name + "/"
            if symbol_name.startswith(prefix) and len(symbol_name) > len(prefix):
                return table, symbol_name[len(prefix) :]
        names = ", ".join(repr(t.name) for t in self.tables)
        raise DatabaseLibraryError(
            f"'{symbol_name}' is not a database symbol name: expected "
            f"'<library>/<key>' with <library> one of {names}"
        )

    # ------------------------------------------------------------- connection

    def _expand(self, text: str) -> str:
        """Expand ``${CWD}`` (the .kicad_dbl's directory) and path variables."""

        def substitute(m: re.Match) -> str:
            var = m.group(1)
            if var == "CWD":
                return str(self.path.parent)
            if var in os.environ:
                return os.environ[var]
            return self._path_vars.get(var, m.group(0))

        return _VAR_RE.sub(substitute, text)

    def _sqlite_file(self) -> Optional[Path]:
        """The database file of a SQLite source, or None for any other driver."""
        params = _parse_connection_string(self._expand(_text(self.source.get("connection_string"))))
        if "sqlite" not in params.get("driver", "").lower():
            return None
        database = params.get("database")
        if not database:
            return None
        path = Path(database)
        return path if path.is_absolute() else self.path.parent / path

    def source_files(self) -> List[Path]:
        """Local files whose change changes the library: the .kicad_dbl and a SQLite file."""
        files = [self.path]
        sqlite_file = self._sqlite_file()
        if sqlite_file is not None:
            files.append(sqlite_file)
        return files

    def _connect(self) -> Tuple[Any, str]:
        """An open read-only DB-API connection and its identifier quote character."""
        source_type = _text(self.source.get("type")).lower()
        if source_type != "odbc":
            raise DatabaseLibraryError(
                f"{self.path}: source type '{self.source.get('type')}' is not supported "
                "(KiCad database libraries use 'odbc')"
            )
        connection_string = self._expand(_text(self.source.get("connection_string")))
        if not connection_string:
            dsn = _text(self.source.get("dsn"))
            if not dsn:
                raise DatabaseLibraryError(
                    f"{self.path}: source has neither dsn nor connection_string"
                )
            connection_string = f"DSN={dsn}"
            if self.source.get("username"):
                connection_string += f";UID={self.source['username']}"
            if self.source.get("password"):
                connection_string += f";PWD={self.source['password']}"

        odbc_error: Optional[str] = None
        try:
            import pyodbc  # type: ignore[import-not-found]

            try:
                timeout = int(self.source.get("timeout_seconds") or 0)
                conn = pyodbc.connect(
                    connection_string, timeout=timeout, readonly=True, autocommit=True
                )
                quote_char = conn.getinfo(pyodbc.SQL_IDENTIFIER_QUOTE_CHAR) or '"'
                return conn, quote_char.strip() or '"'
            except pyodbc.Error as e:
                odbc_error = str(e)
        except ImportError:
            odbc_error = "pyodbc is not installed"

        sqlite_file = self._sqlite_file()
        if sqlite_file is not None:
            if not sqlite_file.is_file():
                raise DatabaseLibraryError(f"{self.path}: database file {sqlite_file} not found")
            uri = "file:" + quote(str(sqlite_file)) + "?mode=ro"
            return sqlite3.connect(uri, uri=True), '"'
        raise DatabaseLibraryError(
            f"Cannot open the database of {self.path}: {odbc_error}. Non-SQLite "
            "sources need pyodbc and the source's ODBC driver."
        )

    @staticmethod
    def _quote(identifier: str, quote_char: str) -> str:
        return quote_char + identifier.replace(quote_char, quote_char * 2) + quote_char

    def _rows(self, table: DbTable, key: Optional[str] = None) -> Iterator[Dict[str, Any]]:
        conn, quote_char = self._connect()
        try:
            sql = f"SELECT * FROM {self._quote(table.table, quote_char)}"
            args: Sequence[Any] = ()
            if key is not None:
                sql += f" WHERE {self._quote(table.key, quote_char)} = ?"
                args = (key,)
            cursor = conn.cursor()
            try:
                cursor.execute(sql, args)
            except Exception as e:
                raise DatabaseLibraryError(
                    f"{self.path}: query on table '{table.table}' failed: {e}"
                ) from e
            columns = [d[0] for d in cursor.description]
            for row in cursor.fetchall():
                yield dict(zip(columns, row))
        finally:
            conn.close()

    # ------------------------------------------------------------------ parts

    @staticmethod
    def _column(row: Dict[str, Any], column: str) -> Optional[Any]:
        """Value of *column*, matched case-insensitively as ODBC drivers differ."""
        if column in row:
            return row[column]
        lowered = column.lower()
        for name, value in row.items():
            if name.lower() == lowered:
                return value
        return None

    def _part(self, table: DbTable, row: Dict[str, Any]) -> DbPart:
        key = _text(self._column(row, table.key))
        footprints = [
            fp.strip() for fp in _text(self._column(row, table.footprints)).split(";") if fp.strip()
        ]
        fields = [
            (m.name, _text(self._column(row, m.column)), m.visible_on_add, m.show_name)
            for m in table.fields
            if self._column(row, m.column) is not None
        ]
        props = table.properties
        return DbPart(
            name=f"{table.name}/{key}",
            symbol=_text(self._column(row, table.symbols)).strip(),
            footprints=footprints,
            fields=fields,
            description=(
                _text(self._column(row, props["description"])) if props.get("description") else ""
            ),
            keywords=_text(self._column(row, props["keywords"])) if props.get("keywords") else "",
        )

    def part(self, symbol_name: str) -> DbPart:
        """The part named ``<library>/<key>``; raises DatabaseLibraryError if absent."""
        table, key = self.split_name(symbol_name)
        for row in self._rows(table, key):
            return self._part(table, row)
        raise DatabaseLibraryError(
            f"No row with {table.key} = '{key}' in table '{table.table}' of "
            f"database library {self.path.name}"
        )

    def parts(self) -> Iterator[DbPart]:
        """Every part of every table (for listing and search)."""
        for table in self.tables:
            for row in self._rows(table):
                yield self._part(table, row)
