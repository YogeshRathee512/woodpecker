import * as React from 'react'
import { Link } from '@tanstack/react-router'
import { ExternalLink, Loader2, RotateCcw, Trash2, Undo2 } from 'lucide-react'
import { PageWrapper } from '../components/PageWrapper'
import { Button } from '../components/ui/button'
import { Badge } from '../components/ui/badge'
import { api, type FailedPuzzle, type FailedPuzzleSettings } from '../lib/api'
import { toast } from '../lib/toast'

function formatDate(value: string | null): string {
  if (!value) return 'Never'
  return new Date(value).toLocaleString()
}

export function FailedPuzzlesPage(): React.ReactElement {
  const [settings, setSettings] = React.useState<FailedPuzzleSettings | null>(null)
  const [puzzles, setPuzzles] = React.useState<FailedPuzzle[]>([])
  const [includeRemoved, setIncludeRemoved] = React.useState(false)
  const [search, setSearch] = React.useState('')
  const [page, setPage] = React.useState(1)
  const [totalPages, setTotalPages] = React.useState(1)
  const [loading, setLoading] = React.useState(true)
  const [syncing, setSyncing] = React.useState(false)
  const [exporting, setExporting] = React.useState(false)
  const [importing, setImporting] = React.useState(false)
  const [busyId, setBusyId] = React.useState<number | null>(null)
  const [lastSyncResult, setLastSyncResult] = React.useState<Awaited<ReturnType<typeof api.failedPuzzles.sync>> | null>(null)

  const refresh = React.useCallback(async () => {
    setLoading(true)
    try {
      const [s, p] = await Promise.all([
        api.failedPuzzles.getSettings(),
        api.failedPuzzles.list(page, 50, includeRemoved, search),
      ])
      setSettings(s)
      setPuzzles(p.items)
      setTotalPages(p.totalPages)
    } catch {
      toast.error('Could not load your failed puzzles')
    } finally {
      setLoading(false)
    }
  }, [includeRemoved, page, search])

  React.useEffect(() => { void refresh() }, [refresh])

  async function sync(fullRescan = false): Promise<void> {
    setSyncing(true)
    try {
      const result = await api.failedPuzzles.sync(fullRescan)
      setLastSyncResult(result)
      toast.success('Lichess sync finished', { description: `${result.newFailures} new puzzles added to the practice collection.` })
      await refresh()
    } catch {
      toast.error('Sync failed', { description: 'Check the Lichess token configuration and try again.' })
    } finally {
      setSyncing(false)
    }
  }

  async function exportArchive(format: 'json' | 'csv'): Promise<void> {
    setExporting(true)
    try {
      const first = await api.failedPuzzles.list(1, 100, true)
      const all = [...first.items]
      for (let p = 2; p <= first.totalPages; p++) {
        const result = await api.failedPuzzles.list(p, 100, true)
        all.push(...result.items)
      }
      const content = format === 'json'
        ? JSON.stringify({ exportVersion: 1, exportedAt: new Date().toISOString(), lichessUsername: settings?.lichessUsername, puzzles: all }, null, 2)
        : [
            ['source', 'puzzleId', 'lichessUrl', 'rating', 'themes', 'fen', 'moves', 'firstFailedAt', 'lastFailedAt', 'failureCount', 'latestResult', 'removedFromCollection', 'createdAt', 'updatedAt', 'trainingAttempts', 'trainingSolved', 'trainingFailed', 'trainingSuccessRate', 'firstAttemptedAt', 'lastAttemptedAt', 'firstSolvedAt', 'lastSolvedAt'],
            ...all.map((p) => [p.source, p.puzzleId, p.lichessUrl, p.rating, p.themes.join('|'), p.fen, p.moves, p.firstFailedAt, p.lastFailedAt, p.failureCount, p.latestResult, p.removedFromCollection, p.createdAt, p.updatedAt, p.trainingAttempts, p.trainingSolved, p.trainingFailed, p.trainingSuccessRate ?? '', p.firstAttemptedAt ?? '', p.lastAttemptedAt ?? '', p.firstSolvedAt ?? '', p.lastSolvedAt ?? '']),
          ].map((row) => row.map((cell) => `"${String(cell).replaceAll('"', '""')}"`).join(',')).join('\r\n')
      const blob = new Blob([content], { type: format === 'json' ? 'application/json' : 'text/csv' })
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = `yogesh-failed-puzzles.${format}`
      anchor.click()
      URL.revokeObjectURL(url)
    } catch {
      toast.error('Could not export your puzzle archive')
    } finally {
      setExporting(false)
    }
  }

  async function importArchive(file: File | undefined): Promise<void> {
    if (!file) return
    setImporting(true)
    try {
      const payload: unknown = JSON.parse(await file.text())
      if (typeof payload !== 'object' || payload === null || !('exportVersion' in payload) || !('puzzles' in payload)
        || payload.exportVersion !== 1 || !Array.isArray(payload.puzzles)) {
        throw new Error('Unsupported backup file')
      }
      let imported = 0
      let skipped = 0
      for (let start = 0; start < payload.puzzles.length; start += 500) {
        const result = await api.failedPuzzles.import({
          exportVersion: 1,
          puzzles: payload.puzzles.slice(start, start + 500) as FailedPuzzle[],
        })
        imported += result.imported
        skipped += result.skipped
      }
      toast.success('Backup imported', { description: `${imported} added; ${skipped} already existed.` })
      setPage(1)
      await refresh()
    } catch {
      toast.error('Could not import backup', { description: 'Choose a valid Yogesh Failed Puzzles JSON export.' })
    } finally {
      setImporting(false)
    }
  }

  async function setRemoved(puzzle: FailedPuzzle, removed: boolean): Promise<void> {
    setBusyId(puzzle.id)
    try {
      if (removed) await api.failedPuzzles.remove(puzzle.id)
      else await api.failedPuzzles.restore(puzzle.id)
      await refresh()
    } catch {
      toast.error(removed ? 'Could not remove puzzle' : 'Could not restore puzzle')
    } finally {
      setBusyId(null)
    }
  }

  return (
    <PageWrapper className="flex flex-col gap-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-base font-semibold">Failed Puzzles</h1>
          <p className="mt-1 text-sm text-muted-foreground">Your private Lichess puzzle history, ready to practise in Woodpecker.</p>
        </div>
        <div className="flex flex-wrap gap-2">
        <Link to="/app/schedules/new" className="inline-flex h-9 items-center justify-center rounded-md border border-input bg-background px-3 text-sm font-medium hover:bg-accent hover:text-accent-foreground">Practice collection</Link>
        <Button variant="outline" onClick={() => void sync(true)} disabled={syncing || loading || settings?.tokenConfigured === false}>
          Full historical rescan
        </Button>
        <Button onClick={() => void sync()} disabled={syncing || loading || settings?.tokenConfigured === false}>
          {syncing ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <RotateCcw className="mr-2 h-4 w-4" />}
          {syncing ? 'Syncing…' : 'Sync Failed Puzzles'}
        </Button>
        </div>
      </div>

      {settings && !settings.tokenConfigured && (
        <div className="rounded-md border border-amber-500/40 bg-amber-50 p-4 text-sm text-amber-950 dark:bg-amber-950/30 dark:text-amber-100">
          Lichess sync is not configured on this server. Set <code>LICHESS_PUZZLE_ACTIVITY_TOKEN</code> with the <code>puzzle:read</code> permission, then restart the backend.
        </div>
      )}

      {settings && (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Stat label="Saved puzzles" value={settings.archivedCount} />
          <Stat label="In practice collection" value={settings.activeCount} />
          <Stat label="Removed" value={settings.removedCount} />
          <Stat label="Last synced" value={formatDate(settings.lastSyncAt)} />
          <Stat label="Oldest activity found" value={formatDate(settings.oldestKnownActivity)} />
          <Stat label="Training attempts" value={settings.trainingAttempts} />
          <Stat label="Successful solves" value={settings.trainingSolved} />
          <Stat label="Overall success rate" value={settings.trainingSuccessRate == null ? '—' : `${settings.trainingSuccessRate}%`} />
          <Stat label="New at last sync" value={settings.newPuzzlesSinceLastSync} />
        </div>
      )}

      {lastSyncResult && <div className="rounded-md border bg-muted/20 p-4 text-sm">
        <p className="font-semibold">Sync completed</p>
        <div className="mt-2 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
          <span>Activities checked: <strong>{lastSyncResult.activitiesChecked.toLocaleString()}</strong></span>
          <span>Failed puzzle IDs: <strong>{lastSyncResult.failedPuzzleIdsDiscovered.toLocaleString()}</strong></span>
          <span>Already known: <strong>{lastSyncResult.alreadyKnown.toLocaleString()}</strong></span>
          <span>Added to collection: <strong>{lastSyncResult.addedToCollection.toLocaleString()}</strong></span>
          <span>Previously removed: <strong>{lastSyncResult.previouslyRemoved.toLocaleString()}</strong></span>
        </div>
        <p className="mt-2 text-muted-foreground">Oldest activity returned: {formatDate(lastSyncResult.oldestActivityReturned)}</p>
      </div>}

      <div className="flex items-center justify-between gap-3">
        <p className="text-sm text-muted-foreground">Lichess account: <strong className="font-medium text-foreground">{settings?.lichessUsername ?? '—'}</strong></p>
        <div className="flex flex-wrap gap-2">
        <Button variant="outline" size="sm" disabled={exporting} onClick={() => void exportArchive('json')}>Export JSON</Button>
        <Button variant="outline" size="sm" disabled={exporting} onClick={() => void exportArchive('csv')}>Export CSV</Button>
        <label className={`inline-flex h-9 cursor-pointer items-center justify-center rounded-md border border-input bg-background px-3 text-sm font-medium hover:bg-accent ${importing ? 'pointer-events-none opacity-50' : ''}`}>
          {importing ? 'Importing…' : 'Restore JSON backup'}
          <input type="file" accept="application/json,.json" className="sr-only" disabled={importing} onChange={(event) => { void importArchive(event.currentTarget.files?.[0]); event.currentTarget.value = '' }} />
        </label>
        <Button variant="outline" size="sm" onClick={() => { setIncludeRemoved((value) => !value); setPage(1) }}>
          {includeRemoved ? 'Hide removed' : 'Show removed'}
        </Button>
        </div>
      </div>

      <input
        type="search"
        value={search}
        onChange={(event) => { setSearch(event.target.value); setPage(1) }}
        placeholder="Search puzzle IDs and themes"
        aria-label="Search failed puzzles"
        className="h-9 w-full max-w-sm rounded-md border border-input bg-background px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
      />

      <div className="overflow-x-auto rounded-md border">
        <table className="w-full text-sm">
          <thead className="bg-muted/50 text-left text-muted-foreground">
            <tr><th className="p-3">Puzzle</th><th className="p-3">Rating</th><th className="p-3">Added</th><th className="p-3">Lichess failures</th><th className="p-3">Practice attempts</th><th className="p-3">Success rate</th><th className="p-3">Last practiced</th><th className="p-3">Last Lichess activity</th><th className="p-3">Result</th><th className="p-3" /></tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={10} className="p-8 text-center text-muted-foreground">Loading your archive…</td></tr>
            ) : puzzles.length === 0 ? (
              <tr><td colSpan={10} className="p-8 text-center text-muted-foreground">No failed puzzles saved yet. Sync your Lichess activity to get started.</td></tr>
            ) : puzzles.map((puzzle) => (
              <tr key={puzzle.id} className="border-t">
                <td className="p-3">
                  <a className="inline-flex items-center gap-1.5 font-medium hover:underline" href={puzzle.lichessUrl} target="_blank" rel="noopener noreferrer">
                    {puzzle.puzzleId}<ExternalLink className="h-3.5 w-3.5" />
                  </a>
                  <div className="mt-1 flex flex-wrap gap-1">{puzzle.themes.slice(0, 4).map((theme) => <Badge key={theme} variant="outline" className="text-xs">{theme}</Badge>)}</div>
                </td>
                <td className="p-3 tabular-nums">{puzzle.rating}</td>
                <td className="p-3">{formatDate(puzzle.createdAt)}</td>
                <td className="p-3 tabular-nums">{puzzle.failureCount}</td>
                <td className="p-3 tabular-nums">{puzzle.trainingAttempts} <span className="text-muted-foreground">({puzzle.trainingSolved} solved)</span></td>
                <td className="p-3 tabular-nums">{puzzle.trainingSuccessRate == null ? '—' : `${puzzle.trainingSuccessRate}%`}</td>
                <td className="p-3">{formatDate(puzzle.lastAttemptedAt)}</td>
                <td className="p-3">{formatDate(puzzle.latestActivityAt)}</td>
                <td className="p-3"><Badge variant={puzzle.latestResult === 'solved' ? 'secondary' : 'destructive'}>{puzzle.latestResult}</Badge>{puzzle.removedFromCollection && <Badge variant="outline" className="ml-1">Removed</Badge>}</td>
                <td className="p-3 text-right">
                  <Button variant="ghost" size="sm" disabled={busyId === puzzle.id} onClick={() => void setRemoved(puzzle, !puzzle.removedFromCollection)}>
                    {busyId === puzzle.id ? <Loader2 className="h-4 w-4 animate-spin" /> : puzzle.removedFromCollection ? <Undo2 className="mr-1 h-4 w-4" /> : <Trash2 className="mr-1 h-4 w-4" />}
                    {puzzle.removedFromCollection ? 'Restore' : 'Remove'}
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!loading && totalPages > 1 && <div className="flex items-center justify-between text-sm">
        <span className="text-muted-foreground">Page {page} of {totalPages}</span>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" disabled={page <= 1} onClick={() => setPage((p) => Math.max(1, p - 1))}>Previous</Button>
          <Button variant="outline" size="sm" disabled={page >= totalPages} onClick={() => setPage((p) => Math.min(totalPages, p + 1))}>Next</Button>
        </div>
      </div>}
    </PageWrapper>
  )
}

function Stat({ label, value }: { label: string; value: string | number }): React.ReactElement {
  return <div className="rounded-md border p-3"><div className="text-xs text-muted-foreground">{label}</div><div className="mt-1 text-lg font-semibold">{value}</div></div>
}
