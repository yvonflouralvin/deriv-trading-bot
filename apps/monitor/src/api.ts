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
}

export type LiveMessage =
  | { type: 'tick'; symbol: string; time: number; price: number }
  | { type: 'candle'; symbol: string; tf: number; candle: Candle }
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
