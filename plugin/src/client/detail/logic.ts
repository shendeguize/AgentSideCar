/**
 * Pure view-model logic for the session-detail timeline view (design §5.1
 * view 2, §5.3 honest presentation). No React, no I/O, no imports from the
 * data layer — everything arrives and leaves as plain values, so this whole
 * module is unit-testable in a bare node environment (same posture as
 * board/logic.ts and inject/logic.ts).
 *
 * Decoupling contract (T5.3 ↔ S7 integration): the `*Wire` types below are
 * this module's OWN hand-written mirrors of the host M3 response shapes
 * (src/routes.ts `timelineBody` over src/fusion.ts `TimelinePage`) — the
 * client TS program cannot import host modules. The integration layer owns
 * transport and feeds pages in; this module owns accumulation, dedup,
 * ordering, gap detection and presentation derivation.
 *
 * Honesty invariants implemented here (design §4.b.3 / §5.3):
 * - a seq discontinuity between consecutive seq-carrying entries inserts a
 *   visible gap marker row (「缺口:可能有 N 条事件未捕获」); seq-less
 *   entries are skipped by the detector and can never fake or mask a gap;
 * - page provenance (`sources`) is surfaced as badges, never dropped;
 * - listen-mode merges only ever append facts (dedup by entry identity),
 *   and newly appended entries are reported for highlight, not invented.
 *
 * @module
 */

import { DETAIL_STRINGS } from './strings.ts'

// ---------------------------------------------------------------------------
// Wire mirrors (host source of truth: routes.ts timelineBody / fusion.ts).
// ---------------------------------------------------------------------------

/** One merged timeline entry as serialized by the host (fusion.ts `TimelineEntry`). */
export interface TimelineEntryWire {
  origin: 'dsh' | 'sidecar'
  /** dsh log seq (native or mirrored via sidecar `extra.seq`); null when unknown. */
  seq: number | null
  /** Unix epoch milliseconds. */
  ts: number
  /** dsh native event type for dsh entries, normalized kind for sidecar entries. */
  kind: string
  /** Normalized sidecar text; `''` for dsh entries without a sidecar twin. */
  text: string
  /** Raw dsh event data; JSON drops it entirely for sidecar-only entries. */
  data?: unknown
  extra: Record<string, unknown> | null
}

/** Decoded cursor object echoed next to the opaque token (routes.ts). */
export interface TimelineCursorWire {
  seq: number | null
  ts: number
}

/** Which sources contributed to a page (fusion.ts `TimelineSources`). */
export interface TimelineSourcesWire {
  dshLive: boolean
  dshCold: boolean
  sidecarReplay: boolean
  sidecarBuffer: boolean
}

/**
 * One timeline page as answered by `GET session/<id>` (nested) and
 * `GET session/<id>/timeline` (routes.ts `timelineBody`). `nextCursor` is
 * the opaque pagination token and must be round-tripped verbatim.
 */
export interface TimelinePageWire {
  sessionId: string
  entries: TimelineEntryWire[]
  cursor: TimelineCursorWire | null
  nextCursor: string | null
  sources: TimelineSourcesWire
}

// ---------------------------------------------------------------------------
// Small shared helpers.
// ---------------------------------------------------------------------------

const MINUTE_MS = 60_000
const HOUR_MS = 3_600_000
const DAY_MS = 86_400_000
const KEY_SEP = '\u0000'

/** Resolve `{name}` placeholders in a message template. */
export function formatTemplate(
  template: string,
  params: Record<string, string | number>,
): string {
  return template.replace(/\{(\w+)\}/g, (match, key: string) => {
    const value = params[key]
    return value === undefined ? match : String(value)
  })
}

/**
 * Coarse relative time: <60s (including clock skew into the future) is
 * 刚刚, then whole minutes/hours/days. Non-finite input renders empty.
 */
export function formatRelativeTime(thenMs: number, nowMs: number): string {
  if (!Number.isFinite(thenMs)) return ''
  const delta = nowMs - thenMs
  if (delta < MINUTE_MS) return DETAIL_STRINGS.time.justNow
  if (delta < HOUR_MS) {
    return formatTemplate(DETAIL_STRINGS.time.minutesAgo, { n: Math.floor(delta / MINUTE_MS) })
  }
  if (delta < DAY_MS) {
    return formatTemplate(DETAIL_STRINGS.time.hoursAgo, { n: Math.floor(delta / HOUR_MS) })
  }
  return formatTemplate(DETAIL_STRINGS.time.daysAgo, { n: Math.floor(delta / DAY_MS) })
}

function pad2(n: number): string {
  return n < 10 ? `0${n}` : String(n)
}

/**
 * Absolute short timestamp for the time column (UX-13): the same local
 * calendar day renders「HH:mm」, anything older (or a different day)
 * renders「MM-DD HH:mm」— so a column of same-age rows stays tellable
 * apart, unlike the coarse relative buckets. The format rule is
 * copy-implemented to match the board column (no cross-surface import);
 * the full ISO timestamp stays in the hover title.
 */
export function formatEventTime(thenMs: number, nowMs: number): string {
  if (!Number.isFinite(thenMs) || !Number.isFinite(nowMs)) return ''
  const then = new Date(thenMs)
  const now = new Date(nowMs)
  const hhmm = `${pad2(then.getHours())}:${pad2(then.getMinutes())}`
  const sameDay =
    then.getFullYear() === now.getFullYear() &&
    then.getMonth() === now.getMonth() &&
    then.getDate() === now.getDate()
  return sameDay ? hhmm : `${pad2(then.getMonth() + 1)}-${pad2(then.getDate())} ${hhmm}`
}

// ---------------------------------------------------------------------------
// Event-kind classification (icon + label).
// ---------------------------------------------------------------------------

/** Normalized event-kind vocabulary; anything unrecognized maps to 'other'. */
export type TimelineKindToken =
  | 'user'
  | 'assistant'
  | 'thinking'
  | 'toolCall'
  | 'toolResult'
  | 'turn'
  | 'step'
  | 'error'
  | 'other'

const KIND_GLYPHS: Record<TimelineKindToken, string> = {
  user: '▷',
  assistant: '◁',
  thinking: '…',
  toolCall: '⚙',
  toolResult: '↩',
  turn: '§',
  step: '·',
  error: '✕',
  other: '•',
}

/** One path segment → family token, or null when it names no family. */
function segmentToken(segment: string): TimelineKindToken | null {
  if (segment === 'user') return 'user'
  if (segment === 'assistant') return 'assistant'
  if (segment === 'thinking' || segment === 'reasoning') return 'thinking'
  if (segment === 'tool_call' || segment === 'tool-call' || segment === 'toolcall') {
    return 'toolCall'
  }
  if (segment === 'tool_result' || segment === 'tool-result' || segment === 'toolresult') {
    return 'toolResult'
  }
  if (segment.startsWith('turn_') || segment === 'turn') return 'turn'
  if (segment.startsWith('step_') || segment === 'step') return 'step'
  if (segment === 'error') return 'error'
  return null
}

/**
 * Map a raw event kind onto the glyph/label vocabulary. Covers the sidecar
 * normalized kinds — user/assistant/thinking/tool_call/tool_result plus
 * the turn_ and step_ prefixes (sidecar/model.py) — and dsh native
 * slash-path types in BOTH orders: `message/user` style (family last) and
 * the observed `user/message` / `assistant/chunk` / `turn/start` style
 * (family first; live-data fact, UX-03 — without it the conversation
 * filter would misfile real dsh user messages as protocol noise). The
 * last segment stays authoritative; the first is only a fallback.
 * Everything else is honestly 'other'.
 */
export function classifyKind(kind: string): TimelineKindToken {
  const k = kind.trim().toLowerCase()
  const segments = k.split('/')
  const last = segmentToken(segments[segments.length - 1] ?? k)
  if (last !== null) return last
  if (segments.length > 1) {
    const first = segmentToken(segments[0] ?? '')
    if (first !== null) return first
  }
  return 'other'
}

/** Glyph for a kind token. */
export function kindGlyph(token: TimelineKindToken): string {
  return KIND_GLYPHS[token]
}

/**
 * Display label: the Chinese vocabulary for recognized kinds; unknown kinds
 * keep their raw text (the view never invents a category, design §5.3).
 */
export function kindLabel(token: TimelineKindToken, rawKind: string): string {
  if (token === 'other') {
    const trimmed = rawKind.trim()
    return trimmed === '' ? DETAIL_STRINGS.kind.other : trimmed
  }
  return DETAIL_STRINGS.kind[token]
}

// ---------------------------------------------------------------------------
// Entry normalization.
// ---------------------------------------------------------------------------

/** One-line summary cap (characters, not bytes — display concern only). */
export const SUMMARY_MAX_CHARS = 120
/** Expanded-body cap so a single pathological entry cannot freeze the tab. */
export const BODY_MAX_CHARS = 4_000

/** One normalized, render-ready timeline entry. */
export interface TimelineEntryVM {
  /** Stable identity for dedup and React keys (see {@link entryKey}). */
  key: string
  origin: 'dsh' | 'sidecar'
  seq: number | null
  /** Unix epoch ms. */
  ts: number
  /** Raw wire kind (hover/debug). */
  kindRaw: string
  kind: TimelineKindToken
  glyph: string
  label: string
  /** First line of the body, truncated to {@link SUMMARY_MAX_CHARS}. */
  summary: string
  /** Expanded body (full text or pretty-printed dsh data); null when the summary says it all. */
  body: string | null
  expandable: boolean
}

/**
 * Dedup identity of one wire entry within a session timeline. Seq-carrying
 * entries collapse on seq+kind+text — NOT seq alone: one dsh record can
 * normalize into several sibling events sharing one seq (reasoning+text
 * blocks, multi-block messages), and a seq-only key would silently fold
 * them away (F1). Same rule as fusion.ts `sidecarEventKey`, so a client
 * multi-page merge matches the host's single-page merge. Seq-less entries
 * fall back to ts+kind+text.
 */
export function entryKey(entry: {
  seq: number | null
  ts: number
  kind: string
  text: string
}): string {
  return entry.seq !== null
    ? `s:${entry.seq}${KEY_SEP}${entry.kind}${KEY_SEP}${entry.text}`
    : `t:${entry.ts}${KEY_SEP}${entry.kind}${KEY_SEP}${entry.text}`
}

const ELLIPSIS = '…'

function truncateChars(text: string, max: number): string {
  return text.length <= max ? text : `${text.slice(0, max)}${ELLIPSIS}`
}

/** Best-effort primary text of an entry: normalized text, else common data fields. */
function extractBaseText(entry: TimelineEntryWire): string {
  if (entry.text !== '') return entry.text
  const data = entry.data
  if (typeof data === 'string') return data
  if (typeof data === 'object' && data !== null && !Array.isArray(data)) {
    const record = data as Record<string, unknown>
    for (const field of ['text', 'title', 'content', 'message']) {
      const value = record[field]
      if (typeof value === 'string' && value !== '') return value
    }
  }
  return ''
}

function safePrettyJson(value: unknown): string | null {
  try {
    const json = JSON.stringify(value, null, 2)
    return typeof json === 'string' ? json : null
  } catch {
    return null
  }
}

/**
 * Wire entry → render-ready view model. Summary is the first line of the
 * best-effort text; the expandable body is the full text when the summary
 * truncated it, else the pretty-printed dsh `data` payload when one exists
 * (both bounded by {@link BODY_MAX_CHARS}).
 */
export function normalizeTimelineEntry(entry: TimelineEntryWire): TimelineEntryVM {
  const token = classifyKind(entry.kind)
  const baseText = extractBaseText(entry)
  const firstLine = baseText.split('\n', 1)[0] ?? ''
  const summary = truncateChars(firstLine.trim(), SUMMARY_MAX_CHARS)

  let body: string | null = null
  if (baseText.trim() !== '' && baseText.trim() !== summary) {
    body = truncateChars(baseText, BODY_MAX_CHARS)
  } else if (entry.data !== undefined) {
    const json = safePrettyJson(entry.data)
    if (json !== null && json !== summary) body = truncateChars(json, BODY_MAX_CHARS)
  }

  return {
    key: entryKey(entry),
    origin: entry.origin,
    seq: entry.seq,
    ts: entry.ts,
    kindRaw: entry.kind,
    kind: token,
    glyph: kindGlyph(token),
    label: kindLabel(token, entry.kind),
    summary,
    body,
    expandable: body !== null,
  }
}

// ---------------------------------------------------------------------------
// Ordering (mirrors fusion.ts mergeTimeline so client order == host order).
// ---------------------------------------------------------------------------

/**
 * Sort entries the way the host merges them: seq-carrying entries keep
 * exact seq order among themselves, seq-less entries sort by ts and are
 * interleaved by ts (tie: seq domain first). This keeps a client-side
 * multi-page merge byte-identical to what one giant host page would be.
 * Within one seq (sibling block events of one dsh record) the dsh entry
 * sorts first — mirroring the host merge — and sidecar siblings keep
 * their arrival order (the sort is stable).
 */
export function sortTimelineEntries(entries: readonly TimelineEntryVM[]): TimelineEntryVM[] {
  const seqDomain = entries.filter((e) => e.seq !== null)
  const unseqed = entries.filter((e) => e.seq === null)
  seqDomain.sort(
    (a, b) =>
      (a.seq ?? 0) - (b.seq ?? 0) ||
      (a.origin === b.origin ? 0 : a.origin === 'dsh' ? -1 : 1),
  )
  unseqed.sort((a, b) => a.ts - b.ts || a.key.localeCompare(b.key))

  const out: TimelineEntryVM[] = []
  let i = 0
  let j = 0
  for (;;) {
    const a = seqDomain[i]
    const b = unseqed[j]
    if (a === undefined && b === undefined) break
    if (b === undefined || (a !== undefined && a.ts <= b.ts)) {
      if (a !== undefined) {
        out.push(a)
        i += 1
      }
    } else {
      out.push(b)
      j += 1
    }
  }
  return out
}

// ---------------------------------------------------------------------------
// Timeline accumulation state (owned by the integration layer, fed to the
// component as the `timeline` prop).
// ---------------------------------------------------------------------------

/**
 * Accumulated timeline state across pages and listen updates. Plain data:
 * every mutation helper returns a fresh object (React-state friendly).
 */
export interface TimelineVM {
  sessionId: string
  /** Ascending (host merge order), deduplicated, normalized. */
  entries: TimelineEntryVM[]
  /** Union of the provenance flags of every merged page. */
  sources: TimelineSourcesWire
  /**
   * Opaque pagination token of the oldest merged history page; null when
   * the log start was reached (or nothing was loaded yet). Round-trip it
   * verbatim to `GET session/<id>/timeline?cursor=`.
   */
  nextCursor: string | null
  /** True once the log start was reached (nextCursor exhausted). */
  reachedStart: boolean
  /** Keys appended by the most recent listen-mode merge (highlight set). */
  newKeys: readonly string[]
}

/** Fresh empty state for a session. */
export function createTimelineVM(sessionId: string): TimelineVM {
  return {
    sessionId,
    entries: [],
    sources: { dshLive: false, dshCold: false, sidecarReplay: false, sidecarBuffer: false },
    nextCursor: null,
    reachedStart: false,
    newKeys: [],
  }
}

function unionSources(a: TimelineSourcesWire, b: TimelineSourcesWire): TimelineSourcesWire {
  return {
    dshLive: a.dshLive || b.dshLive,
    dshCold: a.dshCold || b.dshCold,
    sidecarReplay: a.sidecarReplay || b.sidecarReplay,
    sidecarBuffer: a.sidecarBuffer || b.sidecarBuffer,
  }
}

interface MergeOutcome {
  entries: TimelineEntryVM[]
  appendedKeys: string[]
}

/** (seq, kind) twin-slot sub-key of the convergence maps in mergeEntries. */
function seqKindKey(seq: number, kind: string): string {
  return `${seq}${KEY_SEP}${kind}`
}

/** The key an un-supplemented dsh entry (empty wire text) carries. */
function emptyTextKey(seq: number, kind: string): string {
  return `s:${seq}${KEY_SEP}${kind}${KEY_SEP}`
}

/**
 * Dedup-by-key merge, then host-order sort.
 *
 * Cross-page convergence: the host folds the first sidecar twin's text
 * into the matching dsh entry, so the SAME event can arrive with
 * `text: ''` in one page (twin not yet observed) and with the folded
 * text in a later one — two different keys for one event. A text-carrying
 * arrival therefore upgrades the empty-text (seq, kind) slot in place,
 * and an empty-text arrival for an already-supplemented (seq, kind) is
 * dropped as stale. This keeps the accumulated client merge equal to the
 * host's latest single-page merge instead of duplicating the entry.
 * Genuine same-seq siblings always carry distinct kind/text and are
 * untouched by the rule.
 */
function mergeEntries(
  existing: readonly TimelineEntryVM[],
  incoming: readonly TimelineEntryWire[],
): MergeOutcome {
  const entries = [...existing]
  const seen = new Set(entries.map((e) => e.key))
  const emptySlot = new Map<string, number>()
  const filled = new Set<string>()
  for (let i = 0; i < entries.length; i += 1) {
    const e = entries[i]
    if (e === undefined || e.seq === null) continue
    const sk = seqKindKey(e.seq, e.kindRaw)
    if (e.key === emptyTextKey(e.seq, e.kindRaw)) emptySlot.set(sk, i)
    else filled.add(sk)
  }

  const appendedKeys: string[] = []
  let changed = false
  for (const wire of incoming) {
    const vm = normalizeTimelineEntry(wire)
    if (seen.has(vm.key)) continue
    if (vm.seq !== null) {
      const sk = seqKindKey(vm.seq, vm.kindRaw)
      if (wire.text === '') {
        if (filled.has(sk)) continue
        emptySlot.set(sk, entries.length)
      } else {
        filled.add(sk)
        const slot = emptySlot.get(sk)
        if (slot !== undefined) {
          entries[slot] = vm
          emptySlot.delete(sk)
          seen.add(vm.key)
          appendedKeys.push(vm.key)
          changed = true
          continue
        }
      }
    }
    seen.add(vm.key)
    entries.push(vm)
    appendedKeys.push(vm.key)
    changed = true
  }
  if (!changed) return { entries, appendedKeys: [] }
  return { entries: sortTimelineEntries(entries), appendedKeys }
}

/**
 * Merge one HISTORY page (the initial newest page, or an older page fetched
 * via `nextCursor`) into the state. Advances the pagination token to the
 * page's own `nextCursor` and never marks entries as new (paging back is
 * not fresh activity). Duplicate entries across overlapping pages dedup on
 * {@link entryKey}.
 */
export function applyTimelinePage(vm: TimelineVM, page: TimelinePageWire): TimelineVM {
  const merged = mergeEntries(vm.entries, page.entries)
  return {
    ...vm,
    entries: merged.entries,
    sources: unionSources(vm.sources, page.sources),
    nextCursor: page.nextCursor,
    reachedStart: page.nextCursor === null,
    newKeys: vm.newKeys,
  }
}

/**
 * Merge one LISTEN update (the newest page refetched after an SSE `state`
 * signal — stream snapshots carry no per-session events, so listen mode is
 * snapshot-trigger + timeline-refetch, ADR-2/ADR-3). Appended entries are
 * reported in `newKeys` for highlight; replays of already-known entries
 * dedup silently and do NOT re-highlight. The pagination token is left
 * alone: a newest-window page must never reset the older-history cursor.
 */
export function applyListenPage(vm: TimelineVM, page: TimelinePageWire): TimelineVM {
  const merged = mergeEntries(vm.entries, page.entries)
  return {
    ...vm,
    entries: merged.entries,
    sources: unionSources(vm.sources, page.sources),
    newKeys: merged.appendedKeys,
  }
}

// ---------------------------------------------------------------------------
// Gap detection + row derivation.
// ---------------------------------------------------------------------------

/** One rendered event row (extracted so aggregation can carry members). */
export interface TimelineEventRowVM {
  type: 'event'
  key: string
  entry: TimelineEntryVM
  /** Absolute short time label (see {@link formatEventTime}). */
  timeLabel: string
  /** Hover title: ISO timestamp + raw kind + origin (+ seq) + relative age. */
  hoverTitle: string
  /** True when this entry arrived in the latest listen merge. */
  isNew: boolean
}

/**
 * One rendered timeline row: a real event, an honesty gap marker, or an
 * aggregated run of adjacent empty streaming chunks (UX-03 — the members
 * are carried verbatim so the view can expand the run without data loss).
 */
export type TimelineRowVM =
  | TimelineEventRowVM
  | {
      type: 'gap'
      key: string
      /** Lower bound of dropped events implied by the seq break. */
      missingCount: number
      label: string
    }
  | {
      type: 'chunks'
      key: string
      /** Raw wire kind shared by every member of the run. */
      kindRaw: string
      count: number
      /** 「N 个流式分块」 */
      label: string
      /** Time label of the newest member. */
      timeLabel: string
      hoverTitle: string
      /** True when any member arrived in the latest listen merge. */
      isNew: boolean
      /** The collapsed rows, verbatim, for lossless expansion. */
      members: TimelineEventRowVM[]
    }

function isoOrEmpty(ts: number): string {
  if (!Number.isFinite(ts)) return ''
  try {
    return new Date(ts).toISOString()
  } catch {
    return ''
  }
}

function eventHoverTitle(entry: TimelineEntryVM, nowMs: number): string {
  const parts = [isoOrEmpty(entry.ts), entry.kindRaw, entry.origin]
  if (entry.seq !== null) {
    parts.push(formatTemplate(DETAIL_STRINGS.timeline.seq, { n: entry.seq }))
  }
  parts.push(formatRelativeTime(entry.ts, nowMs))
  return parts.filter((p) => p !== '').join(' · ')
}

/** Gap marker text: 「缺口:可能有 N 条事件未捕获(256 队列上限或未持久化)」. */
export function gapLabel(missingCount: number): string {
  return formatTemplate(DETAIL_STRINGS.gap.label, { n: missingCount })
}

/**
 * Derive render rows from the accumulated state, inserting a gap marker
 * wherever consecutive seq-carrying entries jump by more than 1 (honest
 * presentation of the 256-slot subscribe queue drop / unpersisted events,
 * design §4.b.3). Seq-less entries are transparent to the detector: they
 * neither trigger a gap nor reset the last-seen seq, so mixed timelines
 * cannot produce false positives. Nothing is inserted before the first
 * seq entry — unloaded older history is pagination, not a gap.
 */
export function buildTimelineRows(vm: TimelineVM, nowMs: number): TimelineRowVM[] {
  const newKeys = new Set(vm.newKeys)
  const rows: TimelineRowVM[] = []
  let lastSeq: number | null = null
  for (const entry of vm.entries) {
    if (entry.seq !== null) {
      if (lastSeq !== null && entry.seq > lastSeq + 1) {
        const missing = entry.seq - lastSeq - 1
        rows.push({
          type: 'gap',
          key: `gap:${lastSeq}-${entry.seq}`,
          missingCount: missing,
          label: gapLabel(missing),
        })
      }
      lastSeq = entry.seq
    }
    rows.push({
      type: 'event',
      key: entry.key,
      entry,
      timeLabel: formatEventTime(entry.ts, nowMs),
      hoverTitle: eventHoverTitle(entry, nowMs),
      isNew: newKeys.has(entry.key),
    })
  }
  return rows
}

// ---------------------------------------------------------------------------
// Kind filter + chunk-run aggregation (UX-03: signal over protocol noise).
// ---------------------------------------------------------------------------

/** Kind-filter modes; pure UI state owned by the component. */
export type TimelineFilterMode = 'conversation' | 'all'

/** The kinds that count as conversation (review UX-03 vocabulary). */
export const CONVERSATION_KINDS: ReadonlySet<TimelineKindToken> = new Set([
  'user',
  'assistant',
  'error',
])

export interface FilteredRows {
  rows: TimelineRowVM[]
  /** Exactly how many event rows the filter removed (honest count). */
  hiddenCount: number
}

/**
 * Kind filter over derived rows: 'conversation' keeps user/assistant/error
 * events only; 'all' passes everything through. Gap markers ALWAYS stay —
 * honesty rows are not noise and hiding them could fake a clean timeline.
 * Runs BEFORE {@link aggregateChunkRows} (gaps are detected on the full
 * entry list upstream, so filtering can never fabricate a gap).
 */
export function filterTimelineRows(
  rows: readonly TimelineRowVM[],
  mode: TimelineFilterMode,
): FilteredRows {
  if (mode === 'all') return { rows: [...rows], hiddenCount: 0 }
  const out: TimelineRowVM[] = []
  let hiddenCount = 0
  for (const row of rows) {
    if (row.type === 'event' && !CONVERSATION_KINDS.has(row.entry.kind)) {
      hiddenCount += 1
      continue
    }
    out.push(row)
  }
  return { rows: out, hiddenCount }
}

/**
 * True for a protocol streaming-chunk entry: an empty one-line summary and
 * a chunk-flavored kind (`assistant/chunk` style, matched on the last
 * slash segment). These are the rows that drowned real conversation in
 * the walkthrough (18 of 38 rows, review UX-03).
 */
export function isStreamChunkEntry(entry: TimelineEntryVM): boolean {
  if (entry.summary !== '') return false
  const k = entry.kindRaw.trim().toLowerCase()
  const last = k.includes('/') ? (k.split('/').pop() ?? k) : k
  return last === 'chunk' || last.endsWith('_chunk') || last.endsWith('-chunk')
}

/**
 * Collapse each maximal run of ≥2 adjacent same-kind streaming-chunk rows
 * into one 'chunks' row carrying the members verbatim (lossless — the view
 * offers 展开). Gap markers and any non-chunk row break a run, so the
 * aggregation can never paper over a seq discontinuity. Single chunks stay
 * as plain rows (a 1-run header would add noise, not remove it).
 */
export function aggregateChunkRows(rows: readonly TimelineRowVM[]): TimelineRowVM[] {
  const out: TimelineRowVM[] = []
  let run: TimelineEventRowVM[] = []

  const flush = (): void => {
    if (run.length >= 2) {
      const first = run[0]!
      const last = run[run.length - 1]!
      out.push({
        type: 'chunks',
        key: `chunks:${first.key}`,
        kindRaw: first.entry.kindRaw,
        count: run.length,
        label: formatTemplate(DETAIL_STRINGS.timeline.chunkRun, { n: run.length }),
        timeLabel: last.timeLabel,
        hoverTitle: `${first.entry.kindRaw} ×${run.length}`,
        isNew: run.some((r) => r.isNew),
        members: run,
      })
    } else {
      out.push(...run)
    }
    run = []
  }

  for (const row of rows) {
    const chunk = row.type === 'event' && isStreamChunkEntry(row.entry)
    if (chunk) {
      const sameKind = run.length === 0 || run[run.length - 1]!.entry.kindRaw === row.entry.kindRaw
      if (!sameKind) flush()
      run.push(row as TimelineEventRowVM)
      continue
    }
    flush()
    out.push(row)
  }
  flush()
  return out
}

// ---------------------------------------------------------------------------
// Viewport landing rule (UX-04: open on the newest events).
// ---------------------------------------------------------------------------

/**
 * Whether the view should pin its scroll position to the newest rows:
 * on the first non-empty render (initial landing — understanding the
 * current context needs the latest events, review UX-04), and on every
 * append while listen mode is on. Loading older history must never yank
 * the viewport (`positioned` stays true after the first landing).
 */
export function shouldStickToLatest(input: {
  entryCount: number
  /** True once the initial landing already happened. */
  positioned: boolean
  listening: boolean
}): boolean {
  if (input.entryCount === 0) return false
  return !input.positioned || input.listening
}

// ---------------------------------------------------------------------------
// Segmented rendering bound (perf stopgap; no full virtualization, see task
// report — page sizes already bound growth, this bounds pathological cases).
// ---------------------------------------------------------------------------

/** Default cap of rendered rows; older rows collapse behind a notice. */
export const DEFAULT_MAX_RENDER_ROWS = 400

export interface LimitedRows {
  rows: TimelineRowVM[]
  /** Rows hidden from the top (oldest side); 0 when nothing was cut. */
  hiddenCount: number
  /** Collapse notice text; null when nothing was cut. */
  notice: string | null
}

/**
 * Keep only the newest `max` rows (the tail — listen mode appends there).
 * The data stays in the VM; this is purely a render bound the component
 * can lift via its「全部显示」toggle.
 */
export function limitTimelineRows(
  rows: readonly TimelineRowVM[],
  max: number = DEFAULT_MAX_RENDER_ROWS,
): LimitedRows {
  if (!Number.isFinite(max) || max <= 0 || rows.length <= max) {
    return { rows: [...rows], hiddenCount: 0, notice: null }
  }
  const hiddenCount = rows.length - max
  return {
    rows: rows.slice(hiddenCount),
    hiddenCount,
    notice: formatTemplate(DETAIL_STRINGS.timeline.hiddenNotice, { n: hiddenCount }),
  }
}

// ---------------------------------------------------------------------------
// Source badges.
// ---------------------------------------------------------------------------

/** Color-token buckets the CSS maps to `--dsw-alias-*` variables. */
export type BadgeTone = 'success' | 'warn' | 'neutral' | 'muted' | 'danger'

export interface SourceBadgeVM {
  id: keyof TimelineSourcesWire
  label: string
  tone: BadgeTone
}

const SOURCE_ORDER: ReadonlyArray<{ id: keyof TimelineSourcesWire; tone: BadgeTone }> = [
  { id: 'dshLive', tone: 'success' },
  { id: 'dshCold', tone: 'neutral' },
  { id: 'sidecarReplay', tone: 'neutral' },
  { id: 'sidecarBuffer', tone: 'muted' },
]

/**
 * Provenance badges for the header, stable order: dsh 实时 → dsh 冷读 →
 * sidecar 重放 → sidecar 缓冲. Only contributing sources appear; an
 * all-false set yields an empty list (the empty state explains itself).
 */
export function deriveSourceBadges(sources: TimelineSourcesWire): SourceBadgeVM[] {
  const out: SourceBadgeVM[] = []
  for (const { id, tone } of SOURCE_ORDER) {
    if (sources[id]) out.push({ id, label: DETAIL_STRINGS.sources[id], tone })
  }
  return out
}

// ---------------------------------------------------------------------------
// Header status badge.
// ---------------------------------------------------------------------------

/** Normalized session status vocabulary; anything else maps to 'unknown'. */
export type SessionStatusToken = 'working' | 'waiting' | 'idle' | 'dead' | 'unknown'

const KNOWN_STATUSES: readonly SessionStatusToken[] = ['working', 'waiting', 'idle', 'dead']

const STATUS_TONE: Record<SessionStatusToken, BadgeTone> = {
  working: 'success',
  waiting: 'warn',
  idle: 'neutral',
  unknown: 'neutral',
  dead: 'muted',
}

export interface DetailStatusVM {
  status: SessionStatusToken
  tone: BadgeTone
  label: string
}

/**
 * Raw observed status → badge. Unknown raw statuses keep their raw text as
 * the label (the view never invents a state); empty raw text reads 未知.
 */
export function deriveDetailStatus(rawStatus: string): DetailStatusVM {
  const cleaned = rawStatus.trim().toLowerCase()
  const status = (KNOWN_STATUSES as readonly string[]).includes(cleaned)
    ? (cleaned as SessionStatusToken)
    : 'unknown'
  const trimmed = rawStatus.trim()
  const label =
    status === 'unknown'
      ? trimmed === ''
        ? DETAIL_STRINGS.status.unknown
        : trimmed
      : DETAIL_STRINGS.status[status]
  return { status, tone: STATUS_TONE[status], label }
}

/** Single-character agent marker (board vocabulary; unknown → neutral dot). */
const AGENT_GLYPHS: Record<string, string> = {
  dsh: '◆',
  claude: '✳',
  codex: '▣',
  cursor: '▮',
  'cursor-cli': '▮',
  'cursor-ide': '▮',
  copilot: '◉',
  kimi: '◐',
}

export function agentGlyph(agent: string): string {
  return AGENT_GLYPHS[agent.trim().toLowerCase()] ?? '●'
}

// ---------------------------------------------------------------------------
// Loading / empty / error body states.
// ---------------------------------------------------------------------------

/**
 * Friendly text for a machine error reason (ApiError.reason / server
 * `{reason}` codes). Unknown codes fall back to an honest 错误码 template.
 */
export function detailErrorText(reason: string): string {
  const table: Record<string, string> = DETAIL_STRINGS.states.errors
  const mapped = table[reason]
  return mapped ?? formatTemplate(DETAIL_STRINGS.states.errorFallback, { reason })
}

export type DetailBodyKind = 'loading' | 'error' | 'empty' | 'list'

export interface DetailBodyStateVM {
  kind: DetailBodyKind
  /** Full-body title/hint for the non-list states; null for 'list'. */
  title: string | null
  hint: string | null
  /** Non-null when stale entries stay visible while a refresh/page failed. */
  errorBanner: string | null
}

/**
 * Body-state resolution, honest-by-priority:
 * - entries present → always 'list' (never hide data already shown); a
 *   concurrent error surfaces as an inline banner instead;
 * - no entries + error → 'error' (mapped text);
 * - no entries + loading → 'loading';
 * - otherwise → 'empty'.
 */
export function deriveDetailBodyState(input: {
  loading: boolean
  /** Machine reason code, or null when the last load succeeded. */
  error: string | null
  entryCount: number
}): DetailBodyStateVM {
  if (input.entryCount > 0) {
    return {
      kind: 'list',
      title: null,
      hint: null,
      errorBanner: input.error === null ? null : detailErrorText(input.error),
    }
  }
  if (input.error !== null) {
    return {
      kind: 'error',
      title: DETAIL_STRINGS.states.errorTitle,
      hint: detailErrorText(input.error),
      errorBanner: null,
    }
  }
  if (input.loading) {
    return {
      kind: 'loading',
      title: DETAIL_STRINGS.states.loadingTitle,
      hint: null,
      errorBanner: null,
    }
  }
  return {
    kind: 'empty',
    title: DETAIL_STRINGS.states.emptyTitle,
    hint: DETAIL_STRINGS.states.emptyHint,
    errorBanner: null,
  }
}
