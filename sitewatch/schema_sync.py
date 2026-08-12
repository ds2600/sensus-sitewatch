"""Adds missing columns to already-existing tables at startup.

db.create_all() (what `flask init-db` runs) only creates missing TABLES —
it silently no-ops on a table that already exists, even if the model has
grown new columns since. Found the hard way: lag_interface_a_id/b_id
landed in models.py, `flask init-db` reported success, and the columns
still weren't there. That's exactly the failure mode CLAUDE.md's
backup-compatibility rule exists to prevent — a routine additive schema
change shouldn't force wiping and reimporting the database just to pick
up a nullable column.

sync_schema() runs create_all() for any wholly-new tables, then walks
every existing table and ALTER TABLE ADD COLUMNs anything the models
define that the real table doesn't have yet. Purely additive — never
touches an existing column, never drops/renames anything. Called once at
app startup (see __init__.py) so a `git pull` alone is enough; no command
to remember to run.
"""
import logging

from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.schema import CreateColumn

log = logging.getLogger(__name__)


def sync_schema(db):
    db.create_all()

    inspector = inspect(db.engine)
    existing_tables = set(inspector.get_table_names())

    for table in db.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # brand new table, create_all() above already made it (with every column)
        existing_columns = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing_columns:
                continue
            ddl = str(CreateColumn(column).compile(dialect=db.engine.dialect))
            log.info("Schema sync: adding column %s.%s", table.name, column.name)
            try:
                with db.engine.begin() as conn:
                    conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN {ddl}'))
            except OperationalError as e:
                # "duplicate column" means another process (e.g. a second
                # gunicorn worker starting at the same moment) already added
                # it between our inspect() and this ALTER — already done,
                # not a real failure. Anything else is real; don't swallow it.
                if "duplicate column" not in str(e).lower():
                    raise
