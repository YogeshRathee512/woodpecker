from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.extensions import Base


class FailedPuzzle(Base):
    """Append-only archive entry for a Lichess puzzle the user has failed."""

    __tablename__ = "failed_puzzles"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    training_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("training_items.id"), nullable=True
    )
    puzzle_id: Mapped[str] = mapped_column(Text, nullable=False)
    lichess_url: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False, default="lichess_puzzle_activity", server_default="lichess_puzzle_activity")
    first_failed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_failed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    latest_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    latest_result: Mapped[bool] = mapped_column(Boolean, nullable=False)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    themes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    fen: Mapped[str] = mapped_column(Text, nullable=False)
    moves: Mapped[str] = mapped_column(Text, nullable=False)
    imported_training_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    imported_training_solved: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    imported_training_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    imported_first_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    imported_last_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    imported_first_solved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    imported_last_solved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    removed_from_collection: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("puzzle_id", name="uq_failed_puzzles_puzzle_id"),
        Index("ix_failed_puzzles_user_active", "user_id", "removed_from_collection"),
        Index("ix_failed_puzzles_added", "user_id", "created_at"),
        Index("ix_failed_puzzles_rating", "user_id", "rating"),
        Index("ix_failed_puzzles_latest_activity", "user_id", "latest_activity_at"),
    )


class FailedPuzzleActivity(Base):
    """An activity row actually returned by Lichess, deduplicated across rescans."""

    __tablename__ = "failed_puzzle_activities"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    puzzle_id: Mapped[str] = mapped_column(Text, nullable=False)
    activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    won: Mapped[bool] = mapped_column(Boolean, nullable=False)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    themes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    fen: Mapped[str] = mapped_column(Text, nullable=False)
    moves: Mapped[str] = mapped_column(Text, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id", "puzzle_id", "activity_at", "won",
            name="uq_failed_puzzle_activity_event",
        ),
        Index("ix_failed_puzzle_activities_user_date", "user_id", "activity_at"),
    )


class FailedPuzzleSyncState(Base):
    __tablename__ = "failed_puzzle_sync_states"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    oldest_known_activity: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    newest_known_activity: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_new_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    scan_in_progress: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    scan_full_rescan: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    scan_since_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scan_before_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scan_new_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    scan_activity_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    scan_events_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
