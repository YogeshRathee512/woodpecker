# Failed puzzle archive

The app keeps discovered Lichess failures in the persistent Woodpecker PostgreSQL database. The `Yogesh Failed Puzzles` subset is private to its owner and can be selected like other locked subsets when creating a practice schedule. Removing a puzzle only removes it from that active subset; restoring it reuses the same archive entry.

## Lichess setup

Create a Lichess API token with only the `puzzle:read` permission. Add it to the server's environment as `LICHESS_PUZZLE_ACTIVITY_TOKEN`; do not paste it into source code or commit it in `.env`. The first sync reads the available historical activity. Later syncs use the last successful sync point. Use **Full historical rescan** to check the full history again. Each activity page and its next-page cursor are committed together, so a timed-out or interrupted scan resumes at the next page. Lichess asks API clients to wait one minute after a rate-limit response; the sync follows that guidance.

Sync is only allowed when the configured token belongs to the Lichess account currently signed in. The rest of the Woodpecker app retains its existing Lichess OAuth login and whitelist access controls.

## Local development

1. Add `LICHESS_PUZZLE_ACTIVITY_TOKEN=...` to the untracked `.env` file.
2. Start the app with `make up-build` (or `make up` after the first build).
3. Apply migrations with `make migrate-upgrade`.
4. Sign in with the token owner's Lichess account and open **Failed Puzzles**.

The local Compose database uses the named `pgdata` volume, so normal container stops and rebuilds preserve the archive. Avoid `docker compose down -v` unless you intend to delete the local database.

## Backups and recovery

The Failed Puzzles page can export JSON and CSV, and restore a JSON export. JSON retains puzzle positions, Lichess history summary, active/removed state, and aggregate Woodpecker training statistics. Imports are append-only and skip puzzle IDs already in the database. For a complete operational backup, use the deployment's PostgreSQL backup procedure as described in [deployment docs](../deploy/DEPLOYMENT.md); this also preserves individual run and attempt records.

## Limits

The live table paginates archive rows. The sync runs in the existing request worker; a very long first scan can outlast the configured web request timeout. Committed pages are retained and the next click resumes the scan. A background-job worker with live progress is a later reliability improvement for very large histories. The current Woodpecker authentication is Lichess OAuth; GitHub login is not part of this feature. Other Woodpecker users cannot read a private archive, but an installation intended for one person should keep its existing login whitelist limited to that person's account.
