"""Test the ``fractal.core.db`` module."""

from __future__ import annotations

import pathlib
import threading

import pytest

from fractal.core.db import Database

__all__ = [
    'test_schema_tables_exist',
    'test_init_is_idempotent_on_a_populated_db',
    'test_crud_workflow',
    'test_merge_upsert',
    'test_raw_sql_query',
    'test_read_validation',
    'test_read_orders_by_write_order',
    'test_where_none_filters_match_null_columns',
    'test_connect_sets_generous_busy_timeout',
    'test_concurrent_writers_serialize',
    'test_transaction_commits_and_rolls_back',
    'test_update_reports_rowcount_for_compare_and_swap',
]

# runs.started_at is NOT NULL with no SQL default (the Python writers supply it)
# and every row table carries its owning node, so direct db.write() inserts in
# these generic tests go through a minimal-valid-row helper
_STARTED = '2026-01-01T00:00:00.000Z'
_NODE = 'main'


def _run(**extra: object) -> dict:
    """A minimal valid ``runs`` row (override or extend via ``extra``)."""
    return {'node': _NODE, 'status': 'active', 'started_at': _STARTED, **extra}


def test_schema_tables_exist(database: Database) -> None:
    """All expected tables are created by ``init()``."""
    tables = database.read(
        query="SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
    )
    names = [row['name'] for row in tables]
    assert 'channels' in names
    assert 'events' in names
    assert 'iters' in names
    assert 'messages' in names
    assert 'nodes' in names
    assert 'reads' in names
    assert 'reacts' in names
    assert 'runs' in names
    assert 'signals' in names
    assert 'steps' in names
    assert 'subs' in names


def test_init_is_idempotent_on_a_populated_db(
    tmp_path: pathlib.Path,
    schema: pathlib.Path,
) -> None:
    """Re-running ``init`` on an already-populated database is a safe no-op.

    ``init`` is the documented idempotent recovery for a stranded tree, so a
    second call on a database that already holds rows must not raise and must
    leave the existing data intact -- ``CREATE TABLE IF NOT EXISTS`` skips the
    tables and the rows survive.
    """
    path = tmp_path / '.db'
    db = Database(path, schema)
    db.init()
    db.write(_run(metadata='seed'), 'runs')

    # the idempotent recovery re-runs init without error and keeps the row
    db.init()
    rows = db.read('runs', where={'node': _NODE})
    assert len(rows) == 1
    assert rows[0]['metadata'] == 'seed'


def test_crud_workflow(database: Database) -> None:
    """Write, read, update, exists, count, and delete in sequence."""
    # write rows
    id_1 = database.write(_run(metadata='first'), 'runs')
    id_2 = database.write(_run(metadata='second'), 'runs')
    assert id_1 != id_2

    # read all rows (most-recently-written first)
    rows = database.read('runs')
    assert len(rows) == 2
    assert rows[0]['run_id'] == id_2

    # read with where filter
    rows = database.read('runs', where={'run_id': id_1})
    assert len(rows) == 1
    assert rows[0]['metadata'] == 'first'

    # read with limit
    rows = database.read('runs', limit=1)
    assert len(rows) == 1

    # update
    database.update({'status': 'completed'}, 'runs', where={'run_id': id_1})
    rows = database.read('runs', where={'run_id': id_1})
    assert rows[0]['status'] == 'completed'

    # exists
    assert database.exists('runs', where={'run_id': id_1})
    assert not database.exists('runs', where={'run_id': 999})

    # count
    assert database.count('runs') == 2
    assert database.count('runs', where={'status': 'active'}) == 1

    # delete
    database.delete('runs', where={'run_id': id_1})
    assert database.count('runs') == 1
    assert not database.exists('runs', where={'run_id': id_1})


def test_merge_upsert(database: Database) -> None:
    """Merge inserts, replaces, partial-upserts, returns the id, and no-ops."""
    # insert via merge
    database.merge({'node': 'task.a', 'status': 'idle', 'max_depth': 2}, 'nodes')
    rows = database.read('nodes', where={'node': 'task.a'})
    assert len(rows) == 1
    assert rows[0]['max_depth'] == 2

    # replace via merge (same unique key)
    database.merge({'node': 'task.a', 'status': 'idle', 'max_depth': 5}, 'nodes')
    rows = database.read('nodes', where={'node': 'task.a'})
    assert len(rows) == 1
    assert rows[0]['max_depth'] == 5

    # conflict upsert: a partial merge updates only the given columns and
    # preserves the rest (a whole-row replace would have wiped max_depth)
    database.merge({'node': 'task.a', 'status': 'active'}, 'nodes', conflict=['node'])
    rows = database.read('nodes', where={'node': 'task.a'})
    assert len(rows) == 1
    assert rows[0]['status'] == 'active'
    assert rows[0]['max_depth'] == 5

    # the conflict upsert returns the affected row's real id (not 0: lastrowid is
    # 0 on the ON CONFLICT update path because no INSERT happens)
    node_id = rows[0]['node_id']
    returned = database.merge(
        {'node': 'task.a', 'status': 'idle'},
        'nodes',
        conflict=['node'],
    )
    assert returned == node_id

    # a conflict-only merge (no non-conflict columns) is a no-op, not a crash --
    # ON CONFLICT DO NOTHING would trip the NOT NULL on nodes.status first
    again = database.merge({'node': 'task.a'}, 'nodes', conflict=['node'])
    assert again == node_id
    rows = database.read('nodes', where={'node': 'task.a'})
    assert rows[0]['status'] == 'idle'  # unchanged, no exception raised


def test_raw_sql_query(database: Database) -> None:
    """Read with raw SQL query returns expected results."""
    database.write(_run(), 'runs')
    database.write(_run(status='completed'), 'runs')
    rows = database.read(
        query="SELECT COUNT(*) AS n FROM runs WHERE status = 'active'",
    )
    assert rows[0]['n'] == 1


def test_read_validation(database: Database) -> None:
    """Read raises ``ValueError`` for invalid argument combinations."""
    # query + where is invalid
    with pytest.raises(ValueError):
        database.read('runs', query='SELECT 1', where={'status': 'active'})

    # no table and no query is invalid
    with pytest.raises(ValueError):
        database.read()


def test_read_orders_by_write_order(database: Database) -> None:
    """Built SELECTs return rows most-recently-written first (``rowid DESC``).

    The order is the true insertion order (a monotonic alias of the INTEGER
    PRIMARY KEY), so it holds without any ``created_at`` column and is stable
    regardless of timestamp values.
    """
    # write three rows sharing one start timestamp -- order must still be stable
    id_1 = database.write(_run(), 'runs')
    id_2 = database.write(_run(), 'runs')
    id_3 = database.write(_run(), 'runs')

    # newest first, descending by write order
    rows = database.read('runs')
    assert [row['run_id'] for row in rows] == [id_3, id_2, id_1]
    assert database.read('runs', limit=1)[0]['run_id'] == id_3


def test_where_none_filters_match_null_columns(database: Database) -> None:
    """``where`` filters with ``None`` match SQL NULL via ``IS NULL``.

    A row written without ``exit_code`` stores NULL; filtering by
    ``exit_code=None`` must find it (``col IS NULL``) rather than silently
    matching nothing (``col = NULL`` is never true in SQL).
    """
    # write one row with a NULL exit_code and one with a value
    null_id = database.write(_run(), 'runs')
    set_id = database.write(_run(exit_code=0), 'runs')

    # read/count/exists with a None filter match only the NULL row
    rows = database.read('runs', where={'exit_code': None})
    assert [row['run_id'] for row in rows] == [null_id]
    assert database.count('runs', where={'exit_code': None}) == 1
    assert database.exists('runs', where={'exit_code': None})

    # a None filter AND-joins with equality filters
    rows = database.read('runs', where={'status': 'active', 'exit_code': None})
    assert [row['run_id'] for row in rows] == [null_id]

    # update and delete also target NULL rows through the None filter
    database.update({'status': 'completed'}, 'runs', where={'exit_code': None})
    assert database.read('runs', where={'run_id': null_id})[0]['status'] == 'completed'
    database.delete('runs', where={'exit_code': None})
    assert [row['run_id'] for row in database.read('runs')] == [set_id]


def test_connect_sets_generous_busy_timeout(database: Database) -> None:
    """Every handle carries the 30s busy timeout (raised from the 5s default).

    Under wide node fan-out the fleet contends on one DB; a generous busy timeout
    makes a writer wait for the lock rather than failing fast and aborting the
    loop. ``PRAGMA busy_timeout`` reports it in milliseconds.
    """
    for read_only in (False, True):
        connection = database._connect(read_only=read_only)
        try:
            timeout_ms = connection.execute('PRAGMA busy_timeout').fetchone()[0]
        finally:
            connection.close()
        assert timeout_ms == 30000


def test_concurrent_writers_serialize(database: Database) -> None:
    """Concurrent writers to one DB all succeed rather than failing on a lock.

    SQLite (WAL) admits one writer at a time; the connection's busy timeout makes
    a contending writer wait for the lock instead of failing with "database is
    locked". Every running node writes to the one central DB, so this pins the
    contract: N threads each writing M rows leave exactly N*M rows, with no
    errors.
    """
    # each of N threads writes M rows, recording any error it hits
    writers = 6
    per_writer = 20
    errors: list[Exception] = []

    def writer(tag: int) -> None:
        try:
            for index in range(per_writer):
                database.write(_run(metadata=f'{tag}:{index}'), 'runs')
        except Exception as e:
            errors.append(e)

    # run the writers concurrently against the shared DB
    threads = [threading.Thread(target=writer, args=(tag,)) for tag in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # every write lands: no lock errors, exactly N*M rows
    assert errors == [], errors
    assert database.count('runs') == writers * per_writer


def test_transaction_commits_and_rolls_back(database: Database) -> None:
    """A transaction's writes land together on exit and vanish on error.

    ``transaction()`` yields the connection every table operation joins via
    ``connection=``: reads inside the block see the block's uncommitted
    writes, a clean exit commits them all at once, and an exception rolls
    the whole block back -- the atomicity a multi-statement lifecycle
    transition (read, decide, write) is built on.
    """
    # a clean block commits, and in-transaction reads see uncommitted
    # writes -- every table operation joins through connection=
    with database.transaction() as connection:
        run_id = database.write(_run(), 'runs', connection=connection)
        database.update(
            {'status': 'completed'},
            'runs',
            where={'run_id': run_id},
            connection=connection,
        )
        inside = database.read(
            'runs',
            where={'run_id': run_id},
            connection=connection,
        )
        assert inside[0]['status'] == 'completed'
        assert database.exists('runs', where={'run_id': run_id}, connection=connection)
        assert database.count('runs', connection=connection) == 1
        node_id = database.merge(
            {'node': 'task.a', 'status': 'idle'},
            'nodes',
            conflict=['node'],
            connection=connection,
        )
        assert node_id
        database.delete('nodes', where={'node': 'task.a'}, connection=connection)
        assert not database.exists(
            'nodes',
            where={'node': 'task.a'},
            connection=connection,
        )
    assert database.read('runs', where={'run_id': run_id})[0]['status'] == 'completed'

    # an exception rolls the whole block back and re-raises
    def fail_mid_transaction() -> None:
        with database.transaction() as connection:
            database.write(_run(node='rollback'), 'runs', connection=connection)
            raise RuntimeError('boom')

    with pytest.raises(RuntimeError, match='boom'):
        fail_mid_transaction()
    assert not database.exists('runs', where={'node': 'rollback'})


def test_update_reports_rowcount_for_compare_and_swap(database: Database) -> None:
    """``update`` returns the matched-row count -- the observable CAS verdict.

    A fenced transition writes through a guarded ``where`` (the run closers
    guard on ``ended_at IS NULL``): rowcount 1 means this writer won the
    swap, 0 means another writer got there first and the loser observes
    instead of overwriting.
    """
    run_id = database.write(_run(), 'runs')
    # the first closer wins the swap
    won = database.update(
        {'status': 'completed', 'ended_at': _STARTED},
        'runs',
        where={'run_id': run_id, 'ended_at': None},
    )
    assert won == 1
    # a competing closer on the same guard observes the loss
    lost = database.update(
        {'status': 'killed', 'ended_at': _STARTED},
        'runs',
        where={'run_id': run_id, 'ended_at': None},
    )
    assert lost == 0
    assert database.read('runs', where={'run_id': run_id})[0]['status'] == 'completed'
