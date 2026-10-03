from flask import Blueprint, Response, jsonify, request, session

from app.decorators import login_required
from app.services import failed_puzzles as failed_puzzles_svc

failed_puzzles_bp = Blueprint("failed_puzzles", __name__, url_prefix="/failed-puzzles")


@failed_puzzles_bp.get("")
@login_required
def list_puzzles() -> Response:
    page = request.args.get("page", default=1, type=int) or 1
    page_size = request.args.get("pageSize", default=25, type=int) or 25
    include_removed = request.args.get("includeRemoved", "false").lower() == "true"
    search = request.args.get("q", "")
    return jsonify(failed_puzzles_svc.list_failed_puzzles(
        session["user_id"], page, page_size, include_removed, search
    ))


@failed_puzzles_bp.get("/settings")
@login_required
def settings() -> Response:
    return jsonify(failed_puzzles_svc.get_sync_settings(session["user_id"]))


@failed_puzzles_bp.post("/import")
@login_required
def import_backup() -> tuple[Response, int] | Response:
    result = failed_puzzles_svc.import_failed_puzzles(
        session["user_id"], request.get_json(silent=True)
    )
    return jsonify(result)


@failed_puzzles_bp.post("/sync")
@login_required
def sync() -> Response:
    data: dict[str, object] = request.get_json(silent=True) or {}
    full_rescan_raw = data.get("fullRescan", False)
    if not isinstance(full_rescan_raw, bool):
        return jsonify({"title": "Invalid request", "detail": "fullRescan must be true or false."}), 400
    return jsonify(failed_puzzles_svc.sync_failed_puzzles(session["user_id"], full_rescan_raw))


@failed_puzzles_bp.post("/<int:puzzle_row_id>/remove")
@login_required
def remove(puzzle_row_id: int) -> Response:
    return jsonify(failed_puzzles_svc.set_removed(puzzle_row_id, session["user_id"], True))


@failed_puzzles_bp.post("/<int:puzzle_row_id>/restore")
@login_required
def restore(puzzle_row_id: int) -> Response:
    return jsonify(failed_puzzles_svc.set_removed(puzzle_row_id, session["user_id"], False))
