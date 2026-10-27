"""Real schema introspection over a SQLite database.

Nothing here is hardcoded to a particular database: every table, column,
type, and foreign key is read back from SQLite's own metadata
(``PRAGMA table_info`` / ``PRAGMA foreign_key_list`` and ``sqlite_master``),
so this module works against any SQLite file handed to it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field


@dataclass
class ColumnInfo:
    name: str
    type: str
    not_null: bool
    primary_key: bool


@dataclass
class ForeignKey:
    from_table: str
    from_column: str
    to_table: str
    to_column: str


@dataclass
class TableInfo:
    name: str
    columns: dict[str, ColumnInfo] = field(default_factory=dict)
    foreign_keys: list[ForeignKey] = field(default_factory=list)

    @property
    def column_names(self) -> list[str]:
        return list(self.columns.keys())

    @property
    def text_columns(self) -> list[str]:
        return [c.name for c in self.columns.values() if "CHAR" in c.type.upper() or "TEXT" in c.type.upper()]

    @property
    def numeric_columns(self) -> list[str]:
        upper_types = ("INT", "REAL", "NUM", "FLOAT", "DOUBLE")
        return [c.name for c in self.columns.values() if any(t in c.type.upper() for t in upper_types)]


class Schema:
    """Introspected metadata for a SQLite database, plus a cache of distinct
    values for text columns, used by the parser to ground entity mentions.
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.tables: dict[str, TableInfo] = {}
        self._value_cache: dict[tuple[str, str], list[str]] = {}
        self._introspect()

    # -- introspection -----------------------------------------------------

    def _introspect(self) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
        table_names = [row[0] for row in cur.fetchall()]

        for tname in table_names:
            table = TableInfo(name=tname)
            for row in self.conn.execute(f"PRAGMA table_info('{tname}')"):
                # row: cid, name, type, notnull, dflt_value, pk
                _, col_name, col_type, notnull, _, pk = row
                table.columns[col_name] = ColumnInfo(
                    name=col_name, type=col_type or "", not_null=bool(notnull), primary_key=bool(pk)
                )
            for row in self.conn.execute(f"PRAGMA foreign_key_list('{tname}')"):
                # row: id, seq, table, from, to, on_update, on_delete, match
                _, _, to_table, from_col, to_col, *_ = row
                table.foreign_keys.append(
                    ForeignKey(from_table=tname, from_column=from_col, to_table=to_table, to_column=to_col)
                )
            self.tables[tname] = table

    # -- lookups -------------------------------------------------------------

    def table_names(self) -> list[str]:
        return list(self.tables.keys())

    def all_columns(self) -> list[tuple[str, str]]:
        """Returns (table, column) pairs for every column in the schema."""
        return [(t.name, c) for t in self.tables.values() for c in t.column_names]

    def find_table(self, name: str) -> TableInfo | None:
        return self.tables.get(name)

    def columns_named(self, column: str) -> list[str]:
        """Tables that have a column with this exact name."""
        return [t.name for t in self.tables.values() if column in t.columns]

    def distinct_values(self, table: str, column: str, limit: int = 2000) -> list[str]:
        """Cached distinct string values of a text column, for fuzzy entity
        matching (e.g. matching "electronics" in a question to the actual
        row value "Electronics" in categories.name).
        """
        key = (table, column)
        if key in self._value_cache:
            return self._value_cache[key]
        cur = self.conn.execute(
            f'SELECT DISTINCT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL LIMIT ?',
            (limit,),
        )
        values = [str(row[0]) for row in cur.fetchall()]
        self._value_cache[key] = values
        return values

    def foreign_key_path(self, table_a: str, table_b: str) -> ForeignKey | None:
        """A direct foreign key relationship between two tables, in either
        direction, if one exists.
        """
        for fk in self.tables.get(table_a, TableInfo(table_a)).foreign_keys:
            if fk.to_table == table_b:
                return fk
        for fk in self.tables.get(table_b, TableInfo(table_b)).foreign_keys:
            if fk.to_table == table_a:
                return fk
        return None

    def join_path(self, tables: list[str]) -> list[ForeignKey]:
        """Given a set of tables that must appear together in a query,
        finds a connected set of foreign-key edges (a spanning tree) linking
        them, using a real graph search over the actual FK graph rather than
        any assumption about which tables "should" relate.

        Raises ValueError if the tables are not connected by any FK path
        (direct or indirect, through an unlisted intermediate table).
        """
        if len(tables) <= 1:
            return []

        edges: list[ForeignKey] = []
        for t in self.tables.values():
            edges.extend(t.foreign_keys)

        # Build adjacency over ALL tables (so paths can route through an
        # intermediate table not explicitly requested, e.g. order_items
        # between orders and products).
        adjacency: dict[str, list[tuple[str, ForeignKey]]] = {}
        for fk in edges:
            adjacency.setdefault(fk.from_table, []).append((fk.to_table, fk))
            adjacency.setdefault(fk.to_table, []).append((fk.from_table, fk))

        start = tables[0]
        # Standard BFS shortest-path tree from `start` over the WHOLE graph
        # (not just the needed tables), so distance ties resolve correctly:
        # e.g. from "orders", both "customers" and "order_items" are one
        # hop away, but that must never make order_items' path route
        # through customers.
        predecessor: dict[str, tuple[str, ForeignKey] | None] = {start: None}
        queue = [start]
        while queue:
            node = queue.pop(0)
            for neighbor, fk in adjacency.get(node, []):
                if neighbor not in predecessor:
                    predecessor[neighbor] = (node, fk)
                    queue.append(neighbor)

        missing = [t for t in tables[1:] if t not in predecessor]
        if missing:
            raise ValueError(f"No foreign-key path connects: {set(missing)}")

        used_edges: list[ForeignKey] = []
        seen_edge_keys: set[tuple] = set()
        for target in tables[1:]:
            node = target
            while node != start:
                parent, fk = predecessor[node]
                key = (fk.from_table, fk.from_column, fk.to_table, fk.to_column)
                if key not in seen_edge_keys:
                    seen_edge_keys.add(key)
                    used_edges.append(fk)
                node = parent

        return used_edges


def load_schema(db_path: str) -> Schema:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    return Schema(conn)
