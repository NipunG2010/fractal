"""Implements ``Database`` class."""

from __future__ import annotations

import contextlib
import pathlib
import sqlite3
from collections.abc import Iterator
from typing import Any, Optional

__all__ = ['Database']


class Database:
    """Thin SQLite wrapper for the tree's central database.

    Provides generic table operations -- ``init``, ``read``,
    ``write``, ``update``, ``merge``, ``delete``, ``exists``,
    and ``count`` -- with no domain-specific logic. Multi-statement
    transitions run inside :meth:`transaction`, passing its connection
    to the table operations via ``connection=``.
    """

    _timeout = 30

    def __init__(
        self: Database,
        path: pathlib.Path,
        schema: pathlib.Path,
        **kwargs: Any,
    ) -> None:
        """Initialize ``Database``.

        Args:
            path: Path to the ``.db`` file.
            schema: Path to the ``.sql`` schema executed by ``init``.
            **kwargs: Additional configuration retained on the instance.

        """
        self._path = pathlib.Path(path).resolve()
        self._schema = pathlib.Path(schema).resolve()
        self._config = kwargs

    def init(self: Database) -> None:
        """Create the database and tables from the schema.

        Idempotent -- safe to call on an existing database.
        """
        sql = self._schema.read_text(encoding='utf-8')
        connection = self._connect()
        try:
            connection.executescript(sql)
        finally:
            connection.close()

    def _connect(
        self: Database,
        read_only: bool = False,
    ) -> sqlite3.Connection:
        """Open a database connection.

        Sets WAL journal mode and enables foreign keys, and gives every handle a
        30-second busy timeout (overriding ``sqlite3.connect``'s 5-second
        default) so a contending writer waits for the lock under wide node
        fan-out instead of failing fast and aborting the loop. A caller may
        override the timeout via the ``Database`` kwargs. Each caller is
        responsible for closing the returned connection.

        Args:
            read_only: Open in read-only mode.

        Returns:
            Database connection.

        """
        # default the busy timeout to 30s (the fleet contends on one DB under
        # fan-out); a caller's explicit timeout in self._config still wins
        config = {'timeout': self._timeout, **self._config}
        # create connection
        if read_only:
            # a read-only open of a missing file yields a cryptic "unable to open
            # database file" -- raise an actionable error pointing at fractal init
            if not self._path.exists():
                raise FileNotFoundError(
                    f'No database at {self._path}; run `fractal init` at the repo root.'
                )
            uri = f'file:{self._path}?mode=ro'
            connection = sqlite3.connect(uri, uri=True, **config)
        else:
            connection = sqlite3.connect(f'{self._path}', **config)
        # configure connection -- journal_mode is a write, so only set it on a
        # writable handle (a read-only connection reads a WAL db fine without it,
        # and setting it errors when no -wal/-shm sidecar exists)
        if not read_only:
            connection.execute('PRAGMA journal_mode = WAL')
        connection.execute('PRAGMA foreign_keys = ON')
        connection.row_factory = sqlite3.Row
        return connection

    @contextlib.contextmanager
    def transaction(self: Database) -> Iterator[sqlite3.Connection]:
        """One ``BEGIN IMMEDIATE`` transaction over a dedicated connection.

        Yields a connection holding the database write lock for the whole
        block, so a read-decide-write transition commits or rolls back as
        one atomic unit; the table operations join it via ``connection=``.
        Every operation inside the block must pass it: a write without
        ``connection=`` deadlocks against the block's own lock until the
        busy timeout, and a read without it silently sees the
        pre-transaction snapshot. ``BEGIN IMMEDIATE`` takes the lock up
        front -- two concurrent transactions serialize (bounded by the
        busy timeout) instead of failing mid-block. Commits on clean
        exit; rolls back on any exception (re-raised).

        Yields:
            The transaction's connection.

        """
        connection = self._connect()
        # explicit BEGIN/COMMIT -- disable the sqlite3 module's implicit
        # transaction management for this handle
        connection.isolation_level = None
        try:
            connection.execute('BEGIN IMMEDIATE')
            yield connection
            connection.commit()
        except BaseException:
            # BEGIN itself may be what failed (busy timeout), leaving no
            # transaction to roll back
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @contextlib.contextmanager
    def _handle(
        self: Database,
        connection: Optional[sqlite3.Connection],
        *,
        read_only: bool = False,
    ) -> Iterator[sqlite3.Connection]:
        """Yield ``connection`` if given, else an owned one-shot handle.

        An owned handle commits on clean exit and always closes; a caller's
        connection is yielded untouched -- its transaction owns commit and
        close, and reads on it see the transaction's uncommitted writes.

        Args:
            connection: Transaction to run inside (from
                :meth:`transaction`), or ``None`` for an owned handle.
            read_only: Open an owned handle read-only (no commit).

        Yields:
            The connection the operation runs on.

        """
        if connection is not None:
            yield connection
            return
        owned = self._connect(read_only=read_only)
        try:
            yield owned
            if not read_only:
                owned.commit()
        finally:
            owned.close()

    def read(
        self: Database,
        table: Optional[str] = None,
        *,
        query: Optional[str] = None,
        params: tuple[Any, ...] = (),
        where: Optional[dict[str, Any]] = None,
        limit: Optional[int] = None,
        connection: Optional[sqlite3.Connection] = None,
    ) -> list[dict[str, Any]]:
        """Read rows from a table.

        Either ``query`` or ``table`` must be provided.
        ``query`` is mutually exclusive with ``where``
        and ``limit``. When ``query`` is given, it is
        executed as raw read-only SQL with ``params`` bound
        positionally to its ``?`` placeholders. Otherwise, a
        ``SELECT`` is built from ``table``, ``where``, and
        ``limit``.

        Built ``SELECT``s are ordered by ``rowid DESC`` -- the
        true write order (a monotonic alias of the
        ``INTEGER PRIMARY KEY``) -- so the most recently written row is
        first, without assuming any particular timestamp
        column (tables name their start ``started_at`` or
        ``created_at``). A raw ``query`` keeps its own ordering
        (none is imposed); ``limit=0`` is valid and returns no rows.

        Args:
            table: Table name.
            query: Raw SQL query (mutually exclusive with
                ``where`` and ``limit``).
            params: Parameters bound to the ``query``'s ``?``
                placeholders.
            where: Column filters (AND-joined equality).
            limit: Maximum rows to return.
            connection: Transaction to run inside (from
                :meth:`transaction`).

        Returns:
            List of row dicts.

        """
        # validate arguments
        if query is not None and (where is not None or limit is not None):
            raise ValueError('Cannot combine query with where or limit.')
        if query is None and table is None:
            raise ValueError('Either table or query is required.')
        if limit is not None and limit < 0:
            raise ValueError(f'limit must be non-negative, got {limit}.')
        # build query -- the table/where path binds its own params
        # (query and where are mutually exclusive, so the caller's
        # params are untouched there)
        if query is None:
            query = f'SELECT * FROM {table}'
            if where:
                where_clause, where_params = self._where_clause(where)
                query += ' WHERE ' + where_clause
                params += where_params
            query += ' ORDER BY rowid DESC'
            if limit is not None:
                query += f' LIMIT {limit}'
        # execute query
        with self._handle(connection, read_only=True) as handle:
            cursor = handle.execute(query, params)
            result = [dict(row) for row in cursor.fetchall()]
        return result

    def write(
        self: Database,
        data: dict[str, Any],
        table: str,
        *,
        connection: Optional[sqlite3.Connection] = None,
    ) -> int:
        """Insert a row into a table.

        Args:
            data: Column values.
            table: Table name.
            connection: Transaction to run inside (from
                :meth:`transaction`).

        Returns:
            Row ID of the inserted row.

        """
        # build statement
        columns = ', '.join(data.keys())
        placeholders = ', '.join('?' for _ in data)
        statement = f'INSERT INTO {table} ({columns}) VALUES ({placeholders})'
        # execute statement
        with self._handle(connection) as handle:
            cursor = handle.execute(statement, tuple(data.values()))
            row_id = cursor.lastrowid
        return row_id

    def update(
        self: Database,
        data: dict[str, Any],
        table: str,
        *,
        where: dict[str, Any],
        connection: Optional[sqlite3.Connection] = None,
    ) -> int:
        """Update rows in a table.

        Args:
            data: Column values to set.
            table: Table name.
            where: Column filters (AND-joined equality).
            connection: Transaction to run inside (from
                :meth:`transaction`).

        Returns:
            Number of rows updated -- the observable outcome of a
            compare-and-swap ``where`` (0 means another writer won).

        """
        # build statement
        set_clause = ', '.join(f'{key} = ?' for key in data)
        where_clause, params = self._where_clause(where)
        statement = f'UPDATE {table} SET {set_clause} WHERE {where_clause}'
        params = tuple(data.values()) + params
        # execute statement
        with self._handle(connection) as handle:
            cursor = handle.execute(statement, params)
            count = cursor.rowcount
        return count

    def merge(
        self: Database,
        data: dict[str, Any],
        table: str,
        *,
        conflict: Optional[list[str]] = None,
        connection: Optional[sqlite3.Connection] = None,
    ) -> int:
        """Insert a row, or upsert it on a unique conflict.

        With ``conflict`` (the unique column(s) identifying an existing row), an
        existing row is updated in place for **only** the columns in ``data`` --
        every other column keeps its stored value. This is a real upsert: a
        partial merge (e.g. just ``status``) preserves the row's other columns
        rather than wiping them the way a whole-row ``INSERT OR REPLACE`` would.
        A partial merge must still supply every ``NOT NULL`` column that lacks a
        SQL default: the base ``INSERT``'s constraints are checked before the
        conflict routes to the update, so omitting one fails (it is not
        backfilled). Without ``conflict`` it falls back to ``INSERT OR REPLACE``
        (a full-row insert).

        Args:
            data: Column values.
            table: Table name.
            conflict: Unique column(s) to upsert on. ``None`` does
                a whole-row insert-or-replace.
            connection: Transaction to run inside (from
                :meth:`transaction`).

        Returns:
            Row ID of the inserted, updated, or (on a no-op) existing row.

        """
        # build statement
        columns = ', '.join(data.keys())
        placeholders = ', '.join('?' for _ in data)
        if conflict:
            targets = ', '.join(conflict)
            # update only the provided non-conflict columns so
            # unprovided columns keep their stored values
            updates = ', '.join(
                f'{key} = excluded.{key}' for key in data if key not in conflict
            )
            if updates:
                statement = (
                    f'INSERT INTO {table} ({columns}) VALUES ({placeholders})'
                    f' ON CONFLICT({targets}) DO UPDATE SET {updates}'
                    ' RETURNING rowid'
                )
            else:
                # nothing to update -- a pure "insert if new, else leave alone";
                # INSERT OR IGNORE skips the conflicting row rather than failing;
                # ON CONFLICT DO NOTHING would still evaluate the base INSERT's
                # NOT NULL checks first and raise on a conflict-only row
                statement = (
                    f'INSERT OR IGNORE INTO {table} ({columns})'
                    f' VALUES ({placeholders}) RETURNING rowid'
                )
        else:
            statement = (
                f'INSERT OR REPLACE INTO {table} ({columns}) VALUES ({placeholders})'
            )
        # execute statement
        with self._handle(connection) as handle:
            cursor = handle.execute(statement, tuple(data.values()))
            if conflict:
                # RETURNING yields the affected row on insert/update, but nothing
                # on a no-op (the row already exists and was left alone) -- so fall
                # back to resolving the existing row's id by the conflict key(s)
                returned = cursor.fetchone()
                if returned is not None:
                    row_id = returned[0]
                else:
                    where = ' AND '.join(f'{key} = ?' for key in conflict)
                    found = handle.execute(
                        f'SELECT rowid FROM {table} WHERE {where}',
                        tuple(data[key] for key in conflict),
                    ).fetchone()
                    row_id = found[0] if found else 0
            else:
                row_id = cursor.lastrowid
        return row_id

    def delete(
        self: Database,
        table: str,
        *,
        where: dict[str, Any],
        connection: Optional[sqlite3.Connection] = None,
    ) -> None:
        """Delete rows from a table.

        Args:
            table: Table name.
            where: Column filters (AND-joined equality).
            connection: Transaction to run inside (from
                :meth:`transaction`).

        """
        # build statement
        where_clause, params = self._where_clause(where)
        statement = f'DELETE FROM {table} WHERE {where_clause}'
        # execute statement
        with self._handle(connection) as handle:
            handle.execute(statement, params)

    def exists(
        self: Database,
        table: str,
        *,
        where: dict[str, Any],
        connection: Optional[sqlite3.Connection] = None,
    ) -> bool:
        """Check whether a matching row exists.

        Args:
            table: Table name.
            where: Column filters (AND-joined equality).
            connection: Transaction to run inside (from
                :meth:`transaction`).

        Returns:
            Whether a matching row exists.

        """
        # build query
        where_clause, params = self._where_clause(where)
        query = f'SELECT 1 FROM {table} WHERE {where_clause} LIMIT 1'
        # execute query
        with self._handle(connection, read_only=True) as handle:
            cursor = handle.execute(query, params)
            result = cursor.fetchone() is not None
        return result

    def count(
        self: Database,
        table: str,
        *,
        where: Optional[dict[str, Any]] = None,
        connection: Optional[sqlite3.Connection] = None,
    ) -> int:
        """Count rows in a table.

        Args:
            table: Table name.
            where: Column filters (AND-joined equality).
            connection: Transaction to run inside (from
                :meth:`transaction`).

        Returns:
            Row count.

        """
        # build query
        query = f'SELECT COUNT(*) FROM {table}'
        params = ()
        if where:
            where_clause, where_params = self._where_clause(where)
            query += ' WHERE ' + where_clause
            params += where_params
        # execute query
        with self._handle(connection, read_only=True) as handle:
            cursor = handle.execute(query, params)
            result, *_ = cursor.fetchone()
        return result

    @staticmethod
    def _where_clause(where: dict[str, Any]) -> tuple[str, tuple[Any, ...]]:
        """Build an AND-joined WHERE clause.

        Renders a ``None`` filter value as ``col IS NULL``,
        binding no parameter. SQL never matches ``col = NULL``
        (the comparison is always unknown), so equality cannot
        stand in for a null check.

        Args:
            where: Column filters (AND-joined equality).

        Returns:
            The clause text (without the ``WHERE`` keyword) and
            the parameter tuple for the non-null comparisons,
            positionally aligned with it.

        """
        clauses = []
        params = []
        for key, value in where.items():
            if value is None:
                clauses.append(f'{key} IS NULL')
            else:
                clauses.append(f'{key} = ?')
                params.append(value)
        return ' AND '.join(clauses), tuple(params)
