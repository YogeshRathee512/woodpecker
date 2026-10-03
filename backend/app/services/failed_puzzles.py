import json
import os
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import requests
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from app.exceptions import ConflictError, ExternalServiceError, ForbiddenError, NotFoundError, ValidationError
from app.extensions import db
from app.models.failed_puzzle import FailedPuzzle, FailedPuzzleActivity, FailedPuzzleSyncState
from app.models.subset import Subset, SubsetTrainingItem
from app.models.training_item import TrainingItem, TrainingItemSource
from app.models.user import User

ACTIVITY_URL = "https://lichess.org/api/puzzle/activity"
ACCOUNT_URL = "https://lichess.org/api/account"
PAGE_SIZE = 500
MAX_RETRIES = 5
MIN_ACTIVITY_MS = 1_356_998_400_070


def _to_datetime(epoch_ms: int) -> datetime:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)


def _to_epoch_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _get_or_create_collection(user_id: int) -> Subset:
    collection = db.session.scalar(
        sa.select(Subset).where(
            Subset.user_id == user_id,
            Subset.is_failed_puzzle_collection.is_(True),
        ).with_for_update()
    )
    if collection is None:
        collection = Subset(
            user_id=user_id,
            name="Yogesh Failed Puzzles",
            puzzle_count=None,
            config=None,
            locked_at=datetime.now(timezone.utc),
            locked_puzzle_count=0,
            is_private=True,
            is_failed_puzzle_collection=True,
        )
        db.session.add(collection)
        db.session.flush()
    return collection


def _ensure_collection_membership(puzzle: FailedPuzzle) -> None:
    if puzzle.removed_from_collection or puzzle.training_item_id is None:
        return
    collection = _get_or_create_collection(puzzle.user_id)
    membership = db.session.get(
        SubsetTrainingItem, (collection.id, puzzle.training_item_id)
    )
    if membership is None:
        max_position = db.session.scalar(
            sa.select(sa.func.max(SubsetTrainingItem.position)).where(
                SubsetTrainingItem.subset_id == collection.id
            )
        )
        db.session.add(SubsetTrainingItem(
            subset_id=collection.id,
            training_item_id=puzzle.training_item_id,
            position=(max_position if max_position is not None else -1) + 1,
        ))
    active_count = db.session.scalar(
        sa.select(sa.func.count()).select_from(SubsetTrainingItem).where(
            SubsetTrainingItem.subset_id == collection.id
        )
    ) or 0
    # The count query autoflushes the pending membership, so it already includes it.
    collection.locked_puzzle_count = active_count


def _get_token() -> str:
    token = os.environ.get("LICHESS_PUZZLE_ACTIVITY_TOKEN", "").strip()
    if not token:
        raise ValidationError(
            "Lichess token is not configured",
            "Set LICHESS_PUZZLE_ACTIVITY_TOKEN on the server with the puzzle:read permission.",
        )
    return token


def _request_account(token: str) -> str:
    try:
        response = requests.get(
            ACCOUNT_URL,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=(10, 30),
        )
    except requests.RequestException as exc:
        raise ExternalServiceError(
            "Could not reach Lichess", "Lichess could not be reached. Please try again later."
        ) from exc
    if response.status_code in (401, 403):
        raise ValidationError(
            "Lichess token was rejected",
            "Check that the server token is valid and has the puzzle:read permission.",
        )
    if not response.ok:
        raise ExternalServiceError(
            "Lichess account check failed", f"Lichess returned HTTP {response.status_code}. Please try again later."
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise ExternalServiceError("Unexpected Lichess response", "Lichess returned invalid account data.") from exc
    username = payload.get("username") if isinstance(payload, dict) else None
    if not isinstance(username, str) or not username:
        raise ExternalServiceError("Unexpected Lichess response", "Lichess did not return an account name.")
    return username


def _request_page(token: str, before: int | None, since: int | None) -> list[dict[str, Any]]:
    params: dict[str, str] = {"max": str(PAGE_SIZE)}
    if before is not None:
        params["before"] = str(before)
    if since is not None:
        params["since"] = str(since)
    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with requests.get(
                f"{ACTIVITY_URL}?{urlencode(params)}",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/x-ndjson"},
                timeout=(10, 90),
                stream=True,
            ) as response:
                if response.status_code in (401, 403):
                    raise ValidationError(
                        "Lichess token was rejected",
                        "Check that the server token is valid and has the puzzle:read permission.",
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt == MAX_RETRIES:
                        raise ExternalServiceError(
                            "Lichess is temporarily unavailable",
                            "Lichess could not complete the sync after several retries. Try again later.",
                        )
                    retry_after = response.headers.get("Retry-After")
                    try:
                        delay = max(60.0, float(retry_after)) if retry_after else 60.0
                    except ValueError:
                        delay = 60.0
                    time.sleep(delay)
                    continue
                if not response.ok:
                    raise ExternalServiceError(
                        "Lichess sync failed", f"Lichess returned HTTP {response.status_code}."
                    )
                records: list[dict[str, Any]] = []
                for raw_line in response.iter_lines(decode_unicode=True):
                    if not raw_line:
                        continue
                    line = raw_line if isinstance(raw_line, str) else raw_line.decode("utf-8")
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ExternalServiceError(
                            "Invalid Lichess activity response",
                            "Lichess returned an unreadable activity record; this sync was not marked complete.",
                        ) from exc
                    if not isinstance(record, dict):
                        raise ExternalServiceError(
                            "Invalid Lichess activity response",
                            "Lichess returned an unexpected activity record.",
                        )
                    records.append(record)
                return records
        except requests.RequestException as exc:
            last_error = exc
            if attempt == MAX_RETRIES:
                break
            time.sleep(min(30.0, 2.0 ** attempt))
    raise ExternalServiceError(
        "Could not reach Lichess", "Lichess could not complete the sync. Please try again later."
    ) from last_error


def _iter_activity(
    token: str,
    since: int | None,
    before: int | None = None,
) -> Iterator[list[dict[str, Any]]]:
    previous_cursor: int | None = None
    while True:
        records = _request_page(token, before, since)
        if not records:
            return
        yield records
        dates = [
            value for row in records
            if isinstance((value := row.get("date")), int) and value >= MIN_ACTIVITY_MS
        ]
        if len(dates) != len(records):
            raise ExternalServiceError(
                "Invalid Lichess activity response",
                "An activity row was missing a valid timestamp; the sync was not marked complete.",
            )
        cursor = min(dates)
        if previous_cursor is not None and cursor >= previous_cursor:
            raise ExternalServiceError(
                "Lichess pagination stopped progressing",
                "Lichess returned the same page boundary twice. Try a full historical rescan later.",
            )
        if cursor <= MIN_ACTIVITY_MS:
            return
        previous_cursor = cursor
        before = cursor - 1


def _parse_activity(record: dict[str, Any]) -> dict[str, Any]:
    date = record.get("date")
    won = record.get("win")
    puzzle = record.get("puzzle")
    if not isinstance(date, int) or date < MIN_ACTIVITY_MS or not isinstance(won, bool):
        raise ExternalServiceError("Invalid Lichess activity response", "An activity row has invalid result or date data.")
    if not isinstance(puzzle, dict):
        raise ExternalServiceError("Invalid Lichess activity response", "An activity row is missing puzzle details.")
    puzzle_id = puzzle.get("id")
    fen = puzzle.get("fen")
    rating = puzzle.get("rating")
    themes = puzzle.get("themes")
    solution = puzzle.get("solution")
    last_move = puzzle.get("lastMove")
    if (
        not isinstance(puzzle_id, str) or not puzzle_id.strip()
        or not isinstance(fen, str) or not fen
        or not isinstance(rating, int)
        or not isinstance(themes, list) or not all(isinstance(theme, str) for theme in themes)
        or not isinstance(solution, list) or not solution or not all(isinstance(move, str) for move in solution)
        or not isinstance(last_move, str) or not last_move
    ):
        raise ExternalServiceError(
            "Invalid Lichess puzzle data",
            "Lichess omitted puzzle moves or metadata required to save and practice this puzzle.",
        )
    return {
        "puzzleId": puzzle_id.strip(),
        "activityAt": _to_datetime(date),
        "won": won,
        "rating": rating,
        "themes": themes,
        "fen": fen,
        "moves": " ".join([last_move, *solution]),
    }


def _store_activity(user_id: int, activity: dict[str, Any]) -> tuple[bool, bool]:
    puzzle_id = activity["puzzleId"]
    event = FailedPuzzleActivity(
        user_id=user_id,
        puzzle_id=puzzle_id,
        activity_at=activity["activityAt"],
        won=activity["won"],
        rating=activity["rating"],
        themes=activity["themes"],
        fen=activity["fen"],
        moves=activity["moves"],
    )
    statement = insert(FailedPuzzleActivity).values(
        user_id=event.user_id,
        puzzle_id=event.puzzle_id,
        activity_at=event.activity_at,
        won=event.won,
        rating=event.rating,
        themes=event.themes,
        fen=event.fen,
        moves=event.moves,
    ).on_conflict_do_nothing(constraint="uq_failed_puzzle_activity_event").returning(FailedPuzzleActivity.id)
    event_id = db.session.execute(statement).scalar_one_or_none()
    if event_id is None:
        return False, False

    row = db.session.execute(
        sa.select(FailedPuzzle).where(FailedPuzzle.puzzle_id == puzzle_id).with_for_update()
    ).scalar_one_or_none()
    if row is None and activity["won"]:
        return True, False
    if row is None:
        training_item = TrainingItem(source_type=TrainingItemSource.LICHESS_FAILED_PUZZLE)
        db.session.add(training_item)
        db.session.flush()
        training_item_id = training_item.id
        row = FailedPuzzle(
            user_id=user_id,
            training_item_id=training_item_id,
            puzzle_id=puzzle_id,
            lichess_url=f"https://lichess.org/training/{puzzle_id}",
            first_failed_at=activity["activityAt"],
            last_failed_at=activity["activityAt"],
            failure_count=1,
            latest_activity_at=activity["activityAt"],
            latest_result=False,
            rating=activity["rating"],
            themes=activity["themes"],
            fen=activity["fen"],
            moves=activity["moves"],
        )
        db.session.add(row)
        latest_event = db.session.scalar(
            sa.select(FailedPuzzleActivity)
            .where(
                FailedPuzzleActivity.user_id == user_id,
                FailedPuzzleActivity.puzzle_id == puzzle_id,
            )
            .order_by(FailedPuzzleActivity.activity_at.desc(), FailedPuzzleActivity.id.desc())
            .limit(1)
        )
        if latest_event is not None and latest_event.activity_at >= row.latest_activity_at:
            row.latest_activity_at = latest_event.activity_at
            row.latest_result = latest_event.won
            row.rating = latest_event.rating
            row.themes = latest_event.themes
            row.fen = latest_event.fen
            row.moves = latest_event.moves
        _ensure_collection_membership(row)
        return True, True

    if row.user_id != user_id:
        raise ForbiddenError("Puzzle archive conflict", "This puzzle ID is already archived by another account.")
    _ensure_collection_membership(row)
    if row.training_item_id is None:
        training_item = TrainingItem(source_type=TrainingItemSource.LICHESS_FAILED_PUZZLE)
        db.session.add(training_item)
        db.session.flush()
        row.training_item_id = training_item.id
    if not activity["won"]:
        row.first_failed_at = min(row.first_failed_at, activity["activityAt"])
        row.last_failed_at = max(row.last_failed_at, activity["activityAt"])
        row.failure_count += 1
    if activity["activityAt"] >= row.latest_activity_at:
        row.latest_activity_at = activity["activityAt"]
        row.latest_result = activity["won"]
        row.rating = activity["rating"]
        row.themes = activity["themes"]
        row.fen = activity["fen"]
        row.moves = activity["moves"]
    return True, False


def sync_failed_puzzles(user_id: int, full_rescan: bool = False) -> dict[str, object]:
    user = db.session.get(User, user_id)
    if user is None:
        raise NotFoundError("Account not found", "Your Woodpecker account could not be found.")
    token = _get_token()
    lichess_username = _request_account(token)
    if lichess_username.casefold() != user.lichess_username.casefold():
        raise ForbiddenError(
            "Lichess account does not match",
            "The configured Lichess token belongs to a different account than the account signed in to Woodpecker.",
        )

    lock_connection = db.engine.connect()
    try:
        lock_acquired = bool(lock_connection.execute(
            sa.text("SELECT pg_try_advisory_lock(1735289201, :user_id)"), {"user_id": user_id}
        ).scalar_one())
        lock_connection.commit()
        if not lock_acquired:
            raise ConflictError("Sync already running", "A failed-puzzle sync is already running for your account.")
    except Exception:
        lock_connection.rollback()
        lock_connection.close()
        raise

    try:
        state = db.session.get(FailedPuzzleSyncState, user_id)
        if state is None:
            state = FailedPuzzleSyncState(user_id=user_id)
            db.session.add(state)
            db.session.commit()
        if state.scan_in_progress:
            since = state.scan_since_ms
            before = state.scan_before_ms
            full_rescan = state.scan_full_rescan
        else:
            since = None if full_rescan or state.last_sync_at is None else max(
                MIN_ACTIVITY_MS, _to_epoch_ms(state.last_sync_at) - 1
            )
            before = None
            state.scan_in_progress = True
            state.scan_full_rescan = full_rescan
            state.scan_since_ms = since
            state.scan_before_ms = None
            state.scan_new_count = 0
            state.scan_activity_count = 0
            state.scan_events_count = 0
            db.session.commit()

        oldest_seen: datetime | None = None
        newest_seen: datetime | None = None

        for records in _iter_activity(token, since, before):
            page_oldest: datetime | None = None
            page_new_failures = 0
            page_events_recorded = 0
            for record in records:
                activity = _parse_activity(record)
                page_oldest = activity["activityAt"] if page_oldest is None else min(page_oldest, activity["activityAt"])
                newest_seen = activity["activityAt"] if newest_seen is None else max(newest_seen, activity["activityAt"])
                inserted, created = _store_activity(user_id, activity)
                if inserted:
                    page_events_recorded += 1
                if created:
                    page_new_failures += 1
            if page_oldest is not None:
                assert newest_seen is not None
                oldest_seen = page_oldest if oldest_seen is None else min(oldest_seen, page_oldest)
                # Persist the next page boundary with the page's archive rows in one
                # transaction. A retried request can continue after a timeout/restart.
                state = db.session.get(FailedPuzzleSyncState, user_id)
                assert state is not None
                state.scan_before_ms = max(MIN_ACTIVITY_MS, _to_epoch_ms(page_oldest) - 1)
                state.oldest_known_activity = (
                    page_oldest if state.oldest_known_activity is None
                    else min(state.oldest_known_activity, page_oldest)
                )
                state.newest_known_activity = (
                    newest_seen if state.newest_known_activity is None
                    else max(state.newest_known_activity, newest_seen)
                )
            else:
                state = db.session.get(FailedPuzzleSyncState, user_id)
                assert state is not None
            state.scan_new_count += page_new_failures
            state.scan_activity_count += len(records)
            state.scan_events_count += page_events_recorded
            # Each page is its own transaction. A crash leaves committed archive rows
            # intact; event uniqueness makes the next sync safe to retry.
            db.session.commit()
            from app.services.training_item_content import clear_payload_cache

            clear_payload_cache()

        state = db.session.get(FailedPuzzleSyncState, user_id)
        assert state is not None
        completed_at = datetime.now(timezone.utc)
        completed_new_failures = state.scan_new_count
        completed_activity_count = state.scan_activity_count
        completed_events_count = state.scan_events_count
        scan_since = state.scan_since_ms
        observed_failed_ids = sa.select(FailedPuzzleActivity.puzzle_id).where(
            FailedPuzzleActivity.user_id == user_id,
            FailedPuzzleActivity.won.is_(False),
        )
        if scan_since is not None:
            observed_failed_ids = observed_failed_ids.where(
                FailedPuzzleActivity.activity_at >= _to_datetime(scan_since)
            )
        completed_failed_ids = int(db.session.scalar(
            sa.select(sa.func.count(sa.distinct(observed_failed_ids.subquery().c.puzzle_id)))
        ) or 0)
        previously_removed = int(db.session.scalar(
            sa.select(sa.func.count()).select_from(FailedPuzzle).where(
                FailedPuzzle.user_id == user_id,
                FailedPuzzle.removed_from_collection.is_(True),
                FailedPuzzle.puzzle_id.in_(observed_failed_ids),
            )
        ) or 0)
        state.last_sync_at = completed_at
        state.last_sync_new_count = completed_new_failures
        state.scan_in_progress = False
        state.scan_full_rescan = False
        state.scan_since_ms = None
        state.scan_before_ms = None
        state.scan_new_count = 0
        state.scan_activity_count = 0
        state.scan_events_count = 0
        if oldest_seen is not None:
            state.oldest_known_activity = (
                oldest_seen if state.oldest_known_activity is None
                else min(state.oldest_known_activity, oldest_seen)
            )
        if newest_seen is not None:
            state.newest_known_activity = (
                newest_seen if state.newest_known_activity is None
                else max(state.newest_known_activity, newest_seen)
            )
        db.session.commit()

        return {
            "completedAt": completed_at.isoformat(),
            "activitiesChecked": completed_activity_count,
            "failedPuzzleIdsDiscovered": completed_failed_ids,
            "alreadyKnown": max(0, completed_failed_ids - completed_new_failures),
            "newFailures": completed_new_failures,
            "addedToCollection": completed_new_failures,
            "previouslyRemoved": previously_removed,
            "activityEventsRecorded": completed_events_count,
            "oldestActivityReturned": oldest_seen.isoformat() if oldest_seen else None,
            "fullRescan": full_rescan,
        }
    except Exception:
        db.session.rollback()
        raise
    finally:
        try:
            lock_connection.execute(
                sa.text("SELECT pg_advisory_unlock(1735289201, :user_id)"), {"user_id": user_id}
            )
            lock_connection.commit()
        finally:
            lock_connection.close()


def list_failed_puzzles(
    user_id: int,
    page: int = 1,
    page_size: int = 25,
    include_removed: bool = False,
    search: str = "",
) -> dict[str, object]:
    page = max(1, page)
    page_size = min(100, max(1, page_size))
    filters = [FailedPuzzle.user_id == user_id]
    if not include_removed:
        filters.append(FailedPuzzle.removed_from_collection.is_(False))
    if search.strip():
        pattern = f"%{search.strip()}%"
        filters.append(sa.or_(FailedPuzzle.puzzle_id.ilike(pattern), sa.cast(FailedPuzzle.themes, sa.Text).ilike(pattern)))
    total = db.session.scalar(sa.select(sa.func.count()).select_from(FailedPuzzle).where(*filters)) or 0
    rows = db.session.scalars(
        sa.select(FailedPuzzle).where(*filters)
        .order_by(FailedPuzzle.created_at.desc(), FailedPuzzle.id.desc())
        .limit(page_size).offset((page - 1) * page_size)
    ).all()
    training_item_ids = [row.training_item_id for row in rows if row.training_item_id is not None]
    training_stats: dict[int, Any] = {}
    if training_item_ids:
        stats_rows = db.session.execute(sa.text("""
            SELECT fp.training_item_id,
                   COUNT(a.id) AS attempts,
                   COUNT(a.id) FILTER (WHERE a.status = 'solved') AS solved,
                   COUNT(a.id) FILTER (WHERE a.status = 'failed') AS failed,
                   MIN(a.started_at) AS first_attempted,
                   MAX(COALESCE(a.completed_at, a.started_at)) AS last_attempted,
                   MIN(a.completed_at) FILTER (WHERE a.status = 'solved') AS first_solved,
                   MAX(a.completed_at) FILTER (WHERE a.status = 'solved') AS last_solved
            FROM failed_puzzles fp
            LEFT JOIN run_training_items rti ON rti.training_item_id = fp.training_item_id
            LEFT JOIN runs r ON r.id = rti.run_id
            LEFT JOIN trainings t ON t.id = r.training_id AND t.user_id = fp.user_id
            LEFT JOIN training_attempts a ON a.run_training_item_id = rti.id AND t.id IS NOT NULL
            WHERE fp.user_id = :uid AND fp.training_item_id = ANY(:ids)
            GROUP BY fp.training_item_id
        """), {"uid": user_id, "ids": training_item_ids}).all()
        training_stats = {row.training_item_id: row for row in stats_rows}
    return {
        "items": [_puzzle_dict(row, training_stats.get(row.training_item_id)) for row in rows],
        "page": page,
        "pageSize": page_size,
        "total": total,
        "totalPages": max(1, (total + page_size - 1) // page_size),
    }


def _puzzle_dict(row: FailedPuzzle, training_stats: Any | None = None) -> dict[str, object]:
    attempts = row.imported_training_attempts + (int(training_stats.attempts or 0) if training_stats is not None else 0)
    solved = row.imported_training_solved + (int(training_stats.solved or 0) if training_stats is not None else 0)
    failed = row.imported_training_failed + (int(training_stats.failed or 0) if training_stats is not None else 0)
    first_attempted = training_stats.first_attempted if training_stats is not None else None
    last_attempted = training_stats.last_attempted if training_stats is not None else None
    first_solved = training_stats.first_solved if training_stats is not None else None
    last_solved = training_stats.last_solved if training_stats is not None else None
    if row.imported_first_attempted_at and (first_attempted is None or row.imported_first_attempted_at < first_attempted):
        first_attempted = row.imported_first_attempted_at
    if row.imported_last_attempted_at and (last_attempted is None or row.imported_last_attempted_at > last_attempted):
        last_attempted = row.imported_last_attempted_at
    if row.imported_first_solved_at and (first_solved is None or row.imported_first_solved_at < first_solved):
        first_solved = row.imported_first_solved_at
    if row.imported_last_solved_at and (last_solved is None or row.imported_last_solved_at > last_solved):
        last_solved = row.imported_last_solved_at
    return {
        "id": row.id,
        "puzzleId": row.puzzle_id,
        "lichessUrl": row.lichess_url,
        "source": row.source,
        "firstFailedAt": row.first_failed_at.isoformat(),
        "lastFailedAt": row.last_failed_at.isoformat(),
        "failureCount": row.failure_count,
        "latestActivityAt": row.latest_activity_at.isoformat(),
        "latestResult": "solved" if row.latest_result else "failed",
        "rating": row.rating,
        "themes": row.themes,
        "fen": row.fen,
        "moves": row.moves,
        "removedFromCollection": row.removed_from_collection,
        "trainingAttempts": attempts,
        "trainingSolved": solved,
        "trainingFailed": failed,
        "trainingSuccessRate": round(solved / (solved + failed) * 100, 1) if solved + failed else None,
        "firstAttemptedAt": first_attempted.isoformat() if first_attempted else None,
        "lastAttemptedAt": last_attempted.isoformat() if last_attempted else None,
        "firstSolvedAt": first_solved.isoformat() if first_solved else None,
        "lastSolvedAt": last_solved.isoformat() if last_solved else None,
        "createdAt": row.created_at.isoformat(),
        "updatedAt": row.updated_at.isoformat(),
    }


def set_removed(puzzle_row_id: int, user_id: int, removed: bool) -> dict[str, object]:
    row = db.session.scalar(sa.select(FailedPuzzle).where(FailedPuzzle.id == puzzle_row_id).with_for_update())
    if row is None:
        raise NotFoundError("Puzzle not found", "The requested puzzle is not in your failed-puzzle archive.")
    if row.user_id != user_id:
        raise ForbiddenError("Access denied", "You do not have permission to change this puzzle.")
    row.removed_from_collection = removed
    collection = db.session.scalar(
        sa.select(Subset).where(
            Subset.user_id == user_id,
            Subset.is_failed_puzzle_collection.is_(True),
        ).with_for_update()
    )
    if not removed and collection is None and row.training_item_id is not None:
        collection = _get_or_create_collection(user_id)
    if collection is not None and row.training_item_id is not None:
        membership = db.session.get(SubsetTrainingItem, (collection.id, row.training_item_id))
        if removed and membership is not None:
            db.session.delete(membership)
            db.session.flush()
        elif not removed and membership is None:
            _ensure_collection_membership(row)
        collection.locked_puzzle_count = db.session.scalar(
            sa.select(sa.func.count()).select_from(SubsetTrainingItem).where(
                SubsetTrainingItem.subset_id == collection.id
            )
        ) or 0
    db.session.commit()
    return _puzzle_dict(row)


def _parse_export_datetime(value: object, field_name: str, required: bool = False) -> datetime | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ValidationError("Invalid backup", f"{field_name} must be an ISO timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("Invalid backup", f"{field_name} must be an ISO timestamp.") from exc
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def import_failed_puzzles(user_id: int, payload: object) -> dict[str, int]:
    if not isinstance(payload, dict) or payload.get("exportVersion") != 1:
        raise ValidationError("Invalid backup", "This is not a supported failed-puzzle JSON backup.")
    puzzles = payload.get("puzzles")
    if not isinstance(puzzles, list) or len(puzzles) > 500:
        raise ValidationError("Invalid backup", "Upload at most 500 puzzle records per import request.")

    imported = 0
    skipped = 0
    for item in puzzles:
        if not isinstance(item, dict):
            raise ValidationError("Invalid backup", "Each puzzle record must be an object.")
        puzzle_id = item.get("puzzleId")
        lichess_url = item.get("lichessUrl")
        fen = item.get("fen")
        moves = item.get("moves")
        rating = item.get("rating")
        failure_count = item.get("failureCount", 1)
        themes = item.get("themes", [])
        removed = item.get("removedFromCollection", False)
        latest_result = item.get("latestResult", "failed")
        source = item.get("source", "lichess_puzzle_activity")
        if (
            not isinstance(puzzle_id, str) or not puzzle_id.strip()
            or not isinstance(lichess_url, str) or not lichess_url.startswith("https://lichess.org/")
            or not isinstance(fen, str) or not fen
            or not isinstance(moves, str) or not moves
            or not isinstance(rating, int) or isinstance(rating, bool) or rating < 0
            or not isinstance(failure_count, int) or isinstance(failure_count, bool) or failure_count < 1
            or not isinstance(themes, list) or not all(isinstance(theme, str) for theme in themes)
            or not isinstance(removed, bool)
            or not isinstance(source, str) or not source.strip()
            or latest_result not in ("failed", "solved")
        ):
            raise ValidationError("Invalid backup", "A puzzle record is missing valid ID, URL, position, moves, rating, or result data.")

        first_failed_at = _parse_export_datetime(item.get("firstFailedAt"), "firstFailedAt", required=True)
        last_failed_at = _parse_export_datetime(item.get("lastFailedAt"), "lastFailedAt", required=True)
        latest_activity_at = _parse_export_datetime(item.get("latestActivityAt", item.get("lastFailedAt")), "latestActivityAt", required=True)
        existing = db.session.scalar(
            sa.select(FailedPuzzle).where(FailedPuzzle.puzzle_id == puzzle_id).with_for_update()
        )
        if existing is not None:
            skipped += 1
            continue

        count_values: dict[str, int] = {}
        for field in ("trainingAttempts", "trainingSolved", "trainingFailed"):
            value = item.get(field, 0)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValidationError("Invalid backup", f"{field} must be a nonnegative integer.")
            count_values[field] = value
        if count_values["trainingSolved"] + count_values["trainingFailed"] > count_values["trainingAttempts"]:
            raise ValidationError("Invalid backup", "Solved and failed totals cannot exceed attempts.")

        training_item = TrainingItem(source_type=TrainingItemSource.LICHESS_FAILED_PUZZLE)
        db.session.add(training_item)
        db.session.flush()
        row = FailedPuzzle(
            user_id=user_id,
            training_item_id=training_item.id,
            puzzle_id=puzzle_id.strip(),
            lichess_url=lichess_url,
            source=source,
            first_failed_at=first_failed_at,
            last_failed_at=last_failed_at,
            failure_count=failure_count,
            latest_activity_at=latest_activity_at,
            latest_result=latest_result == "solved",
            rating=rating,
            themes=themes,
            fen=fen,
            moves=moves,
            removed_from_collection=removed,
            imported_training_attempts=count_values["trainingAttempts"],
            imported_training_solved=count_values["trainingSolved"],
            imported_training_failed=count_values["trainingFailed"],
            imported_first_attempted_at=_parse_export_datetime(item.get("firstAttemptedAt"), "firstAttemptedAt"),
            imported_last_attempted_at=_parse_export_datetime(item.get("lastAttemptedAt"), "lastAttemptedAt"),
            imported_first_solved_at=_parse_export_datetime(item.get("firstSolvedAt"), "firstSolvedAt"),
            imported_last_solved_at=_parse_export_datetime(item.get("lastSolvedAt"), "lastSolvedAt"),
            created_at=_parse_export_datetime(item.get("createdAt"), "createdAt") or datetime.now(timezone.utc),
            updated_at=_parse_export_datetime(item.get("updatedAt"), "updatedAt") or datetime.now(timezone.utc),
        )
        db.session.add(row)
        _ensure_collection_membership(row)
        imported += 1

    db.session.commit()
    return {"imported": imported, "skipped": skipped, "received": len(puzzles)}


def get_sync_settings(user_id: int) -> dict[str, object]:
    state = db.session.get(FailedPuzzleSyncState, user_id)
    counts = db.session.execute(
        sa.select(
            sa.func.count(FailedPuzzle.id).label("archived"),
            sa.func.count(FailedPuzzle.id).filter(FailedPuzzle.removed_from_collection.is_(False)).label("active"),
            sa.func.count(FailedPuzzle.id).filter(FailedPuzzle.removed_from_collection.is_(True)).label("removed"),
        ).where(FailedPuzzle.user_id == user_id)
    ).one()
    practice = db.session.execute(sa.text("""
        SELECT COUNT(a.id) AS attempts,
               COUNT(a.id) FILTER (WHERE a.status = 'solved') AS solved,
               COUNT(a.id) FILTER (WHERE a.status = 'failed') AS failed
        FROM failed_puzzles fp
        JOIN run_training_items rti ON rti.training_item_id = fp.training_item_id
        JOIN runs r ON r.id = rti.run_id
        JOIN trainings t ON t.id = r.training_id AND t.user_id = fp.user_id
        JOIN training_attempts a ON a.run_training_item_id = rti.id
        WHERE fp.user_id = :uid
    """), {"uid": user_id}).one()
    attempts = int(practice.attempts or 0)
    solved = int(practice.solved or 0)
    failed = int(practice.failed or 0)
    return {
        "lichessUsername": db.session.get(User, user_id).lichess_username,
        "tokenConfigured": bool(os.environ.get("LICHESS_PUZZLE_ACTIVITY_TOKEN", "").strip()),
        "lastSyncAt": state.last_sync_at.isoformat() if state and state.last_sync_at else None,
        "oldestKnownActivity": state.oldest_known_activity.isoformat() if state and state.oldest_known_activity else None,
        "newestKnownActivity": state.newest_known_activity.isoformat() if state and state.newest_known_activity else None,
        "lastSyncNewCount": state.last_sync_new_count if state else 0,
        "newPuzzlesSinceLastSync": state.last_sync_new_count if state else 0,
        "archivedCount": int(counts.archived or 0),
        "activeCount": int(counts.active or 0),
        "removedCount": int(counts.removed or 0),
        "trainingAttempts": attempts,
        "trainingSolved": solved,
        "trainingFailed": failed,
        "trainingSuccessRate": round(solved / (solved + failed) * 100, 1) if solved + failed else None,
    }
