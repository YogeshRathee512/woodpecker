"""create append-only failed puzzle archive and activity history

Revision ID: w6x7y8z9a0b1
Revises: v5w6x7y8z9a0
Create Date: 2026-10-03
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "w6x7y8z9a0b1"
down_revision = "v5w6x7y8z9a0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE training_item_source ADD VALUE IF NOT EXISTS 'LICHESS_FAILED_PUZZLE'")
    op.add_column("subsets", sa.Column("is_private", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column(
        "subsets",
        sa.Column("is_failed_puzzle_collection", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.create_index(
        "uq_subsets_failed_puzzle_collection_owner",
        "subsets",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("is_failed_puzzle_collection"),
    )
    op.create_table(
        "failed_puzzles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("training_item_id", sa.Integer(), nullable=True),
        sa.Column("puzzle_id", sa.Text(), nullable=False),
        sa.Column("lichess_url", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), server_default="lichess_puzzle_activity", nullable=False),
        sa.Column("first_failed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_failed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("failure_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("latest_activity_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("latest_result", sa.Boolean(), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("themes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("fen", sa.Text(), nullable=False),
        sa.Column("moves", sa.Text(), nullable=False),
        sa.Column("imported_training_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("imported_training_solved", sa.Integer(), server_default="0", nullable=False),
        sa.Column("imported_training_failed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("imported_first_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("imported_last_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("imported_first_solved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("imported_last_solved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("removed_from_collection", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["training_item_id"], ["training_items.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("puzzle_id", name="uq_failed_puzzles_puzzle_id"),
    )
    op.create_index("ix_failed_puzzles_user_active", "failed_puzzles", ["user_id", "removed_from_collection"])
    op.create_index("ix_failed_puzzles_added", "failed_puzzles", ["user_id", "created_at"])
    op.create_index("ix_failed_puzzles_rating", "failed_puzzles", ["user_id", "rating"])
    op.create_index("ix_failed_puzzles_latest_activity", "failed_puzzles", ["user_id", "latest_activity_at"])

    op.create_table(
        "failed_puzzle_activities",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("puzzle_id", sa.Text(), nullable=False),
        sa.Column("activity_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("won", sa.Boolean(), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("themes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("fen", sa.Text(), nullable=False),
        sa.Column("moves", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "puzzle_id", "activity_at", "won",
            name="uq_failed_puzzle_activity_event",
        ),
    )
    op.create_index("ix_failed_puzzle_activities_user_date", "failed_puzzle_activities", ["user_id", "activity_at"])
    op.create_index("ix_run_training_items_training_item_id", "run_training_items", ["training_item_id"])

    op.create_table(
        "failed_puzzle_sync_states",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("oldest_known_activity", sa.DateTime(timezone=True), nullable=True),
        sa.Column("newest_known_activity", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_new_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("scan_in_progress", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("scan_full_rescan", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("scan_since_ms", sa.Integer(), nullable=True),
        sa.Column("scan_before_ms", sa.Integer(), nullable=True),
        sa.Column("scan_new_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("scan_activity_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("scan_events_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("user_id"),
    )


def downgrade() -> None:
    op.drop_table("failed_puzzle_sync_states")
    op.drop_index("ix_run_training_items_training_item_id", table_name="run_training_items")
    op.drop_index("ix_failed_puzzle_activities_user_date", table_name="failed_puzzle_activities")
    op.drop_table("failed_puzzle_activities")
    op.drop_index("ix_failed_puzzles_latest_activity", table_name="failed_puzzles")
    op.drop_index("ix_failed_puzzles_rating", table_name="failed_puzzles")
    op.drop_index("ix_failed_puzzles_added", table_name="failed_puzzles")
    op.drop_index("ix_failed_puzzles_user_active", table_name="failed_puzzles")
    op.drop_table("failed_puzzles")
    op.drop_index("uq_subsets_failed_puzzle_collection_owner", table_name="subsets")
    op.drop_column("subsets", "is_failed_puzzle_collection")
    op.drop_column("subsets", "is_private")
