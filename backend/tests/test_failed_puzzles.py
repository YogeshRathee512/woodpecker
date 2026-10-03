from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from app.exceptions import ForbiddenError
from app.models.failed_puzzle import FailedPuzzle, FailedPuzzleActivity
from app.models.subset import Subset
from app.models.user import User
from app.services import failed_puzzles as failed_puzzles_svc
from app.services import subset as subset_svc


def _activity(puzzle_id: str, date: int, won: bool, rating: int = 1500) -> dict[str, object]:
    return {
        "date": date,
        "win": won,
        "puzzle": {
            "id": puzzle_id,
            "rating": rating,
            "plays": 100,
            "solution": ["e2e4", "e7e5"],
            "themes": ["fork"],
            "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            "lastMove": "d2d4",
        },
    }


def _make_user(session, username: str = "failed_puzzle_test") -> User:  # type: ignore[misc]
    user = User(lichess_username=username, display_name=username, created_at=datetime.now(timezone.utc))
    session.add(user)
    session.flush()
    return user


def test_parse_activity_preserves_result_metadata_and_solution() -> None:
    parsed = failed_puzzles_svc._parse_activity(_activity("ABC123", 1_700_000_000_000, False))
    assert parsed["puzzleId"] == "ABC123"
    assert parsed["won"] is False
    assert parsed["moves"] == "d2d4 e2e4 e7e5"
    assert parsed["themes"] == ["fork"]


def test_parse_activity_rejects_missing_puzzle_moves() -> None:
    payload = _activity("ABC123", 1_700_000_000_000, False)
    payload["puzzle"] = {"id": "ABC123"}
    with pytest.raises(Exception, match="omitted puzzle moves"):
        failed_puzzles_svc._parse_activity(payload)


def test_sync_archives_unique_failed_puzzle_and_repeat_activity(db_session, monkeypatch) -> None:  # type: ignore[misc]
    user = _make_user(db_session)
    monkeypatch.setenv("LICHESS_PUZZLE_ACTIVITY_TOKEN", "test-token")
    monkeypatch.setattr(failed_puzzles_svc, "_request_account", lambda _token: user.lichess_username)
    records = [
        _activity("ABC123", 1_700_000_000_200, True, 1510),
        _activity("ABC123", 1_700_000_000_100, False, 1505),
        _activity("ABC123", 1_700_000_000_000, False, 1500),
        _activity("NEW234", 1_700_000_000_300, False, 1600),
    ]
    monkeypatch.setattr(failed_puzzles_svc, "_iter_activity", lambda _token, _since, _before=None: iter([records]))

    first = failed_puzzles_svc.sync_failed_puzzles(user.id)
    assert first["activitiesChecked"] == 4
    assert first["newFailures"] == 2
    assert first["activityEventsRecorded"] == 4
    assert first["failedPuzzleIdsDiscovered"] == 2

    archived = db_session.execute(
        sa.select(FailedPuzzle).where(FailedPuzzle.puzzle_id == "ABC123")
    ).scalar_one()
    assert archived.failure_count == 2
    assert archived.first_failed_at == datetime.fromtimestamp(1_700_000_000_000 / 1000, tz=timezone.utc)
    assert archived.last_failed_at == datetime.fromtimestamp(1_700_000_000_100 / 1000, tz=timezone.utc)
    assert archived.latest_result is True
    assert archived.rating == 1510

    second = failed_puzzles_svc.sync_failed_puzzles(user.id)
    assert second["newFailures"] == 0
    assert second["activityEventsRecorded"] == 0
    assert db_session.scalar(sa.select(sa.func.count()).select_from(FailedPuzzle)) == 2
    assert db_session.scalar(sa.select(sa.func.count()).select_from(FailedPuzzleActivity)) == 4


def test_sync_rejects_token_for_other_lichess_account(db_session, monkeypatch) -> None:  # type: ignore[misc]
    user = _make_user(db_session)
    monkeypatch.setenv("LICHESS_PUZZLE_ACTIVITY_TOKEN", "test-token")
    monkeypatch.setattr(failed_puzzles_svc, "_request_account", lambda _token: "someone-else")
    with pytest.raises(ForbiddenError):
        failed_puzzles_svc.sync_failed_puzzles(user.id)


def test_sync_resumes_after_committed_page(db_session, monkeypatch) -> None:  # type: ignore[misc]
    user = _make_user(db_session)
    monkeypatch.setenv("LICHESS_PUZZLE_ACTIVITY_TOKEN", "test-token")
    monkeypatch.setattr(failed_puzzles_svc, "_request_account", lambda _token: user.lichess_username)

    def interrupted(_token, _since, before=None):
        assert before is None
        yield [_activity("PAGE1", 1_700_000_000_200, False)]
        raise RuntimeError("simulated worker interruption")

    monkeypatch.setattr(failed_puzzles_svc, "_iter_activity", interrupted)
    with pytest.raises(RuntimeError, match="simulated worker interruption"):
        failed_puzzles_svc.sync_failed_puzzles(user.id, full_rescan=True)

    state = db_session.get(failed_puzzles_svc.FailedPuzzleSyncState, user.id)
    assert state is not None and state.scan_in_progress
    assert state.scan_before_ms == 1_700_000_000_199
    assert state.scan_new_count == 1
    assert db_session.scalar(sa.select(sa.func.count()).select_from(FailedPuzzle)) == 1

    resumed_before: list[int | None] = []

    def resumed(_token, _since, before=None):
        resumed_before.append(before)
        yield [_activity("PAGE2", 1_699_999_999_000, False)]

    monkeypatch.setattr(failed_puzzles_svc, "_iter_activity", resumed)
    result = failed_puzzles_svc.sync_failed_puzzles(user.id)
    assert resumed_before == [1_700_000_000_199]
    assert result["newFailures"] == 2
    assert db_session.scalar(sa.select(sa.func.count()).select_from(FailedPuzzle)) == 2


def test_removed_archive_entry_stays_archived_and_can_be_restored(db_session) -> None:  # type: ignore[misc]
    user = _make_user(db_session)
    row = FailedPuzzle(
        user_id=user.id,
        puzzle_id="ABC123",
        lichess_url="https://lichess.org/training/ABC123",
        first_failed_at=datetime.now(timezone.utc),
        last_failed_at=datetime.now(timezone.utc),
        latest_activity_at=datetime.now(timezone.utc),
        latest_result=False,
        rating=1500,
        themes=["fork"],
        fen="startpos",
        moves="e2e4 e7e5",
    )
    db_session.add(row)
    db_session.commit()

    removed = failed_puzzles_svc.set_removed(row.id, user.id, True)
    assert removed["removedFromCollection"] is True
    assert failed_puzzles_svc.list_failed_puzzles(user.id)["total"] == 0
    assert failed_puzzles_svc.list_failed_puzzles(user.id, include_removed=True)["total"] == 1

    activity = failed_puzzles_svc._parse_activity(_activity("ABC123", 1_700_000_010_000, False))
    failed_puzzles_svc._store_activity(user.id, activity)
    db_session.commit()
    assert failed_puzzles_svc.list_failed_puzzles(user.id)["total"] == 0
    assert failed_puzzles_svc.list_failed_puzzles(user.id, include_removed=True)["total"] == 1

    restored = failed_puzzles_svc.set_removed(row.id, user.id, False)
    assert restored["removedFromCollection"] is False
    assert db_session.scalar(sa.select(sa.func.count()).select_from(FailedPuzzle)) == 1


def test_archive_entry_cannot_be_changed_by_another_user(db_session) -> None:  # type: ignore[misc]
    owner = _make_user(db_session, "failed_owner")
    other = _make_user(db_session, "failed_other")
    row = FailedPuzzle(
        user_id=owner.id,
        puzzle_id="ABC123",
        lichess_url="https://lichess.org/training/ABC123",
        first_failed_at=datetime.now(timezone.utc),
        last_failed_at=datetime.now(timezone.utc),
        latest_activity_at=datetime.now(timezone.utc),
        latest_result=False,
        rating=1500,
        themes=["fork"],
        fen="startpos",
        moves="e2e4 e7e5",
    )
    db_session.add(row)
    db_session.commit()
    with pytest.raises(ForbiddenError):
        failed_puzzles_svc.set_removed(row.id, other.id, True)


def test_json_backup_import_restores_stats_and_is_idempotent(db_session) -> None:  # type: ignore[misc]
    user = _make_user(db_session)
    stamp = "2026-09-01T10:00:00+00:00"
    backup = {
        "exportVersion": 1,
        "puzzles": [{
            "puzzleId": "RESTORE1",
            "lichessUrl": "https://lichess.org/training/RESTORE1",
            "rating": 1490,
            "themes": ["fork"],
            "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            "moves": "d2d4 e7e5",
            "firstFailedAt": stamp,
            "lastFailedAt": stamp,
            "latestActivityAt": stamp,
            "latestResult": "failed",
            "failureCount": 3,
            "removedFromCollection": False,
            "trainingAttempts": 4,
            "trainingSolved": 3,
            "trainingFailed": 1,
            "createdAt": stamp,
        }],
    }
    assert failed_puzzles_svc.import_failed_puzzles(user.id, backup) == {
        "imported": 1, "skipped": 0, "received": 1,
    }
    row = db_session.scalar(sa.select(FailedPuzzle).where(FailedPuzzle.puzzle_id == "RESTORE1"))
    assert row is not None
    assert row.imported_training_attempts == 4
    assert failed_puzzles_svc.import_failed_puzzles(user.id, backup)["skipped"] == 1


def test_locked_private_collection_is_visible_only_to_owner(db_session) -> None:  # type: ignore[misc]
    owner = _make_user(db_session, "private_owner")
    other = _make_user(db_session, "private_other")
    subset = Subset(
        user_id=owner.id,
        name="Yogesh Failed Puzzles",
        locked_at=datetime.now(timezone.utc),
        locked_puzzle_count=0,
        is_private=True,
        is_failed_puzzle_collection=True,
    )
    db_session.add(subset)
    db_session.commit()

    assert subset_svc._get_viewable_subset(subset.id, owner.id).id == subset.id
    with pytest.raises(ForbiddenError):
        subset_svc._get_viewable_subset(subset.id, other.id)
