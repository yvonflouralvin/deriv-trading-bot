// Accès à l'API de la plateforme (même origine : /api, relayé par nginx).

export interface Candle {
  time: number
  open: number
  high: number
  low: number
  close: number
}

export interface SymbolInfo {
  symbol: string
  display_name: string
  market_display: string | null
  submarket_display: string | null
  exchange_open: boolean | null
  watched: boolean
}

export interface WatchItem {
  symbol: string
  display_name: string
  market_display: string | null
  enabled: boolean
  backfill_days: number
  last_price: number | null
  last_time: number | null
}

export interface Timeframe {
  tf: number
  label: string
}

export interface CollectorStatus {
  alive: boolean
  age_s: number
  connected: boolean
  symbols: string[]
  failed: Record<string, string>
  ticks_written: number
}

export interface ParamSpec {
  key: string
  label: string
  default: number
  min: number
  max: number
  step: number
}

export interface StrategyKind {
  kind: string
  name: string
  description: string
  params: ParamSpec[]
}

export type Mode = 'paper' | 'demo'
export type DurationUnit = 't' | 's' | 'm'

export interface StrategyConfig {
  name: string
  kind: string
  symbol: string
  tf: number
  params: Record<string, number>
  mode: Mode
  enabled: boolean
  stake: number
  duration: number
  duration_unit: DurationUnit
  payout_ratio: number
  max_trades_per_day: number
  max_daily_loss: number
}

export interface StrategyRow extends StrategyConfig {
  id: number
  kind_name: string | null
  today_trades: number
  today_pnl: number
  today_wins: number
  total_trades: number
  total_pnl: number
  total_wins: number
  open_trades: number
  errors: number
}

export interface Trade {
  id: number
  strategy_id: number | null
  strategy_name?: string | null
  mode: Mode
  symbol: string
  side: 'CALL' | 'PUT'
  stake: number
  payout: number | null
  status: 'open' | 'won' | 'lost' | 'error'
  entry_time: number
  entry_spot: number | null
  exit_time: number | null
  exit_spot: number | null
  profit: number | null
  error: string | null
}

export interface BacktestTrade {
  time: number
  side: 'CALL' | 'PUT'
  entry: number
  exit: number
  exit_time: number
  won: boolean
  profit: number
}

export interface BacktestStats {
  trades: number
  wins: number
  losses: number
  win_rate: number | null
  breakeven_win_rate: number
  pnl: number
  max_drawdown: number
  profit_factor: number | null
  best_win_streak: number
  worst_loss_streak: number
  skipped_signals: number
  unresolved_signals?: number
  resolution: string
  equity: [number, number][]
  bars: number
}

export interface Backtest {
  id: number
  strategy_id: number | null
  kind: string
  symbol: string
  tf: number
  params: Record<string, number>
  duration: number
  duration_unit: DurationUnit
  date_from: number
  date_to: number
  status: 'pending' | 'running' | 'done' | 'error'
  stats: BacktestStats | null
  trades: BacktestTrade[] | null
  error: string | null
  created_at: number
}

export interface TrendNow {
  symbol: string
  display_name: string
  backfill_days: number
  states: Record<string, { state: number; strength: number; adx: number | null; time: number }>
  align: number | null
  strength: number | null
}

export interface TrendSeries {
  tfs: number[]
  states: Record<string, [number, number, number][]>   // [début de bougie, état, force]
}

export interface GroupStats {
  n: number
  share?: number | null
  pct_up?: number | null
  mean_fwd?: number | null
  hit?: number | null
  edge_hit?: number | null
  mean_dir?: number | null
  edge_move?: number | null
  mfe?: number | null
  mae?: number | null
  mfe_mae?: number | null
  episodes?: number
  episode_hit?: number | null
  ci?: number | null
  in_sample?: GroupStats
  out_of_sample?: GroupStats
}

export interface Stability {
  bars: number
  share_up: number
  share_down: number
  share_neutral: number
  flips_per_day: number
  median_run_hours: number
  mean_strength: number
}

export interface LagStats {
  legs: number
  detected?: number
  missed_pct?: number | null
  lag_hours_median?: number | null
  done_pct_median?: number | null
}

export interface SymbolReport {
  symbol: string
  samples: number
  period_start: number
  horizons: Record<string, Record<string, GroupStats>>
  stability: Record<string, Stability>
  lag: LagStats
}

export interface VariantReport {
  id: string
  name: string
  description: string
  tfs: number[]
  align_mode: 'all' | 'bias'
  symbols: SymbolReport[]
  skipped: string[]
  pooled: Record<string, Record<string, GroupStats>>
  lag: LagStats
  period_start: number | null
}

export interface TrendReport {
  id: number
  symbols: string[]
  tfs: number[]
  days: number
  status: 'pending' | 'running' | 'done' | 'error'
  error: string | null
  created_at: number
  result: {
    variants?: VariantReport[]
    missing: string[]
    horizons: number[]
  } | null
}

const BASE = import.meta.env.VITE_API_URL ?? ''
const TOKEN_KEY = 'monitor.token'

export const auth = {
  get: () => localStorage.getItem(TOKEN_KEY) ?? '',
  set: (t: string) => localStorage.setItem(TOKEN_KEY, t),
  clear: () => localStorage.removeItem(TOKEN_KEY),
}

export class AuthError extends Error {}

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  const token = auth.get()
  if (token) headers.set('Authorization', `Bearer ${token}`)
  if (init.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  const res = await fetch(`${BASE}/api${path}`, { ...init, headers })
  if (res.status === 401) throw new AuthError('Token invalide')
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`)
  return res.json() as Promise<T>
}

export const api = {
  health: () => call<{ ok: boolean; auth_required: boolean }>('/health'),
  status: () => call<Record<string, CollectorStatus>>('/status'),
  timeframes: () => call<Timeframe[]>('/timeframes'),
  symbols: (q: string) => call<SymbolInfo[]>(`/symbols?q=${encodeURIComponent(q)}&limit=30`),
  watchlist: () => call<WatchItem[]>('/watchlist'),
  addWatch: (symbol: string, backfill_days = 7) =>
    call('/watchlist', { method: 'POST', body: JSON.stringify({ symbol, backfill_days }) }),
  patchWatch: (symbol: string, body: { enabled?: boolean }) =>
    call(`/watchlist/${encodeURIComponent(symbol)}`, { method: 'PATCH', body: JSON.stringify(body) }),
  removeWatch: (symbol: string) =>
    call(`/watchlist/${encodeURIComponent(symbol)}`, { method: 'DELETE' }),
  candles: (symbol: string, tf: number, before?: number, limit = 1500) =>
    call<Candle[]>(
      `/candles?symbol=${encodeURIComponent(symbol)}&tf=${tf}&limit=${limit}` +
        (before ? `&before=${before}` : ''),
    ),
  drawings: (symbol: string) => call<unknown[]>(`/drawings/${encodeURIComponent(symbol)}`),
  saveDrawings: (symbol: string, json: string) =>
    call(`/drawings/${encodeURIComponent(symbol)}`, { method: 'PUT', body: json }),
  kinds: () => call<StrategyKind[]>('/strategy-kinds'),
  strategies: () => call<StrategyRow[]>('/strategies'),
  createStrategy: (c: StrategyConfig) => call<StrategyRow>('/strategies', { method: 'POST', body: JSON.stringify(c) }),
  patchStrategy: (id: number, c: Partial<StrategyConfig>) =>
    call<StrategyRow>(`/strategies/${id}`, { method: 'PATCH', body: JSON.stringify(c) }),
  deleteStrategy: (id: number) => call(`/strategies/${id}`, { method: 'DELETE' }),
  trades: (q: { symbol?: string; strategy_id?: number; since?: number; limit?: number }) =>
    call<Trade[]>(`/trades?${new URLSearchParams(
      Object.entries(q).filter(([, v]) => v !== undefined).map(([k, v]) => [k, String(v)]),
    )}`),
  runBacktest: (strategy_id: number, days: number) =>
    call<{ id: number }>('/backtests', { method: 'POST', body: JSON.stringify({ strategy_id, days }) }),
  backtests: (strategy_id: number) => call<Backtest[]>(`/backtests?strategy_id=${strategy_id}&limit=10`),
  backtest: (id: number) => call<Backtest>(`/backtests/${id}`),
  trendConfig: () => call<{ tfs: Timeframe[] }>('/trend-config'),
  trends: (symbol: string, since?: number) =>
    call<TrendSeries>(`/trends?symbol=${encodeURIComponent(symbol)}${since ? `&since=${since}` : ''}`),
  trendsNow: () => call<TrendNow[]>('/trends/current'),
  runTrendReport: (symbols: string[], days: number) =>
    call<{ id: number }>('/trend-reports', { method: 'POST', body: JSON.stringify({ symbols, days }) }),
  trendReport: (id: number) => call<TrendReport>(`/trend-reports/${id}`),
  trendReports: () => call<TrendReport[]>('/trend-reports'),
}

export type LiveMessage =
  | { type: 'tick'; symbol: string; time: number; price: number }
  | { type: 'candle'; symbol: string; tf: number; candle: Candle }
  | { type: 'trade'; symbol: string; trade: Trade }
  | { type: 'signal'; symbol: string; strategy_id: number; side: string; time: number; price: number; action: string }
  | { type: 'subscribed' | 'pong' }

/** WebSocket temps réel avec reconnexion automatique. */
export class LiveFeed {
  private ws: WebSocket | null = null
  private sub: { symbol: string; tf: number } | null = null
  private closed = false
  private retry = 1000
  private onMessage: (m: LiveMessage) => void
  private onState: (connected: boolean) => void

  constructor(onMessage: (m: LiveMessage) => void, onState: (connected: boolean) => void) {
    this.onMessage = onMessage
    this.onState = onState
    this.open()
  }

  private open() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    const host = BASE ? new URL(BASE).host : location.host
    const token = auth.get()
    const ws = new WebSocket(`${proto}://${host}/api/ws${token ? `?token=${encodeURIComponent(token)}` : ''}`)
    this.ws = ws
    ws.onopen = () => {
      this.retry = 1000
      this.onState(true)
      if (this.sub) ws.send(JSON.stringify({ action: 'subscribe', ...this.sub }))
    }
    ws.onmessage = (e) => this.onMessage(JSON.parse(e.data) as LiveMessage)
    ws.onclose = () => {
      this.onState(false)
      if (this.closed) return
      setTimeout(() => this.open(), this.retry)
      this.retry = Math.min(this.retry * 2, 15000)
    }
  }

  subscribe(symbol: string, tf: number) {
    this.sub = { symbol, tf }
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ action: 'subscribe', symbol, tf }))
    }
  }

  close() {
    this.closed = true
    this.ws?.close()
  }
}
