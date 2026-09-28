"""search trigram indexes, hot-path indexes, one escrow per connection

Revision ID: 33f0b331948b
Revises: c787ebf5ebc4
Create Date: 2026-09-28 12:00:00.000000

* ``pg_trgm`` GIN indexes on every column the catalog search runs ``~*`` /
  ``ILIKE`` against (title, search_text, location_city, location_country, plus
  category and description -- the search ORs all of them per token, and a
  single un-indexed branch forces a sequential scan for the whole OR).
* Partial index for the catalog's default "newest active" page.
* ``connection_messages.sender_id`` and ``match_results.rfq_id`` (FK columns
  that were scanned on every cascade delete and every per-user lookup).
* ``escrow_accounts.connection_id`` becomes UNIQUE. ``get_or_create_escrow``
  is a check-then-insert, so two concurrent calls could create two vaults for
  one deal. Existing duplicates are collapsed first (see ``_dedupe_escrows``).

The indexes are built inside the migration transaction (not CONCURRENTLY):
fine for the current data volume. For a large production table, build them
CONCURRENTLY out of band first; the ``IF NOT EXISTS`` guards make this
migration a no-op for indexes that already exist.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "33f0b331948b"
down_revision: Union[str, None] = "c787ebf5ebc4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TRGM_COLUMNS = (
    "title",
    "search_text",
    "location_city",
    "location_country",
    "category",
    "description",
)


# Kept as plain statements so the test suite can replay them against seeded
# duplicates (tests/test_audit_fixes_platform.py).
DEDUPE_ESCROW_SQL = (
    """
    CREATE TEMP TABLE _escrow_dedupe ON COMMIT DROP AS
    WITH ranked AS (
        SELECT
            e.id,
            e.connection_id,
            row_number() OVER (
                PARTITION BY e.connection_id
                ORDER BY
                    (COALESCE(e.funded_amount, 0)
                     + COALESCE(e.released_amount, 0)
                     + COALESCE(e.refunded_amount, 0)) DESC,
                    (SELECT count(*) FROM deal_disputes d
                      WHERE d.escrow_account_id = e.id) DESC,
                    e.created_at ASC,
                    e.id ASC
            ) AS rn
        FROM escrow_accounts e
    )
    SELECT loser.id AS loser_id, keeper.id AS keeper_id
    FROM ranked loser
    JOIN ranked keeper
      ON keeper.connection_id = loser.connection_id AND keeper.rn = 1
    WHERE loser.rn > 1
    """,
    """
    UPDATE deal_disputes d
       SET escrow_account_id = x.keeper_id
      FROM _escrow_dedupe x
     WHERE d.escrow_account_id = x.loser_id
    """,
    "DELETE FROM escrow_accounts e USING _escrow_dedupe x WHERE e.id = x.loser_id",
    "DROP TABLE IF EXISTS _escrow_dedupe",
)


def _dedupe_escrows() -> None:
    """Keep exactly one escrow per connection before adding the unique index.

    The survivor is the row with the most money movement (funded + released +
    refunded), then the most disputes, then the oldest. Disputes that pointed
    at a removed row are re-pointed to the survivor, so no dispute history is
    lost. Milestones are *not* re-pointed: each vault owns its own 30/40/30
    schedule, and merging two schedules would double the payable amount. The
    losing rows' milestones are removed by the existing ON DELETE CASCADE.
    """
    for statement in DEDUPE_ESCROW_SQL:
        op.execute(statement)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    for column in TRGM_COLUMNS:
        op.execute(
            f"CREATE INDEX IF NOT EXISTS ix_rfqs_{column}_trgm "
            f"ON rfqs USING gin ({column} gin_trgm_ops)"
        )

    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_rfqs_active_created "
        "ON rfqs (created_at DESC) WHERE status = 'active'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_connection_messages_sender_id "
        "ON connection_messages (sender_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_match_results_rfq_id ON match_results (rfq_id)"
    )

    _dedupe_escrows()
    # Replace the plain lookup index with a unique one of the same name: it
    # serves the same lookups, so keeping both would only cost write time.
    op.execute("DROP INDEX IF EXISTS ix_escrow_accounts_connection_id")
    op.execute(
        "CREATE UNIQUE INDEX ix_escrow_accounts_connection_id "
        "ON escrow_accounts (connection_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_escrow_accounts_connection_id")
    op.execute(
        "CREATE INDEX ix_escrow_accounts_connection_id ON escrow_accounts (connection_id)"
    )
    # Removed duplicate escrow rows are not restored: they were redundant
    # copies of the surviving vault and their disputes were re-pointed.

    op.execute("DROP INDEX IF EXISTS ix_match_results_rfq_id")
    op.execute("DROP INDEX IF EXISTS ix_connection_messages_sender_id")
    op.execute("DROP INDEX IF EXISTS ix_rfqs_active_created")
    for column in TRGM_COLUMNS:
        op.execute(f"DROP INDEX IF EXISTS ix_rfqs_{column}_trgm")
    # The extension is left installed: other objects may depend on it, and it
    # is harmless on its own.
