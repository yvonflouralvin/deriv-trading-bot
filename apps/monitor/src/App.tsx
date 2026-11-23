import { useEffect, useState } from 'react'
import type { DrawingKind } from 'lightweight-charts-drawing'
import { api, auth, AuthError, type BacktestTrade, type CollectorStatus, type Timeframe } from './api'
import Chart, { type ActiveStrategy } from './Chart'
import Sidebar from './Sidebar'
import StrategiesPanel from './Strategies'
import TrendsPanel from './Trends'
import { DrawBar, TopBar } from './Toolbar'

const PREFS = 'monitor.prefs'
interface Prefs {
  symbol: string
  tf: number
  strategies: ActiveStrategy[]
  magnet: boolean
  tab: 'watchlist' | 'trends' | 'strategies'
  showTrades: boolean
  trendBand: boolean
}
type Overlay = { label: string; symbol: string; tf: number; trades: BacktestTrade[] }

function loadPrefs(): Prefs {
  const def: Prefs = { symbol: 'R_100', tf: 60, strategies: [], magnet: true, tab: 'watchlist', showTrades: true, trendBand: true }
  try {
    return { ...def, ...JSON.parse(localStorage.getItem(PREFS) ?? '{}') }
  } catch {
    return def
  }
}

function Login({ onDone }: { onDone: () => void }) {
  const [token, setToken] = useState('')
  const [error, setError] = useState('')
  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    auth.set(token.trim())
    try {
      await api.watchlist()
      onDone()
    } catch {
      auth.clear()
      setError('Token refusé.')
    }
  }
  return (
    <form className="login" onSubmit={submit}>
      <h1>Monitor</h1>
      <p className="muted">Entrez le token d'accès (variable API_TOKEN du serveur).</p>
      <input type="password" autoFocus value={token} onChange={(e) => setToken(e.target.value)} />
      <button type="submit">Entrer</button>
      {error && <p className="error">{error}</p>}
    </form>
  )
}

export default function App() {
  const [ready, setReady] = useState<'loading' | 'login' | 'ok'>('loading')
  const [prefs, setPrefs] = useState<Prefs>(loadPrefs)
  const [timeframes, setTimeframes] = useState<Timeframe[]>([])
  const [tool, setTool] = useState<DrawingKind | null>(null)
  const [live, setLive] = useState(false)
  const [clearToken, setClearToken] = useState(0)
  const [toast, setToast] = useState('')
  const [status, setStatus] = useState<Record<string, CollectorStatus>>({})
  const [overlay, setOverlay] = useState<Overlay | null>(null)
  const [trendTfs, setTrendTfs] = useState<Timeframe[]>([])

  const update = (patch: Partial<Prefs>) => setPrefs((p) => ({ ...p, ...patch }))
  useEffect(() => localStorage.setItem(PREFS, JSON.stringify(prefs)), [prefs])

  const onError = (e: unknown) => {
    if (e instanceof AuthError) { auth.clear(); setReady('login'); return }
    console.error(e)
    setToast(e instanceof Error ? e.message : String(e))
    window.setTimeout(() => setToast(''), 5000)
  }

  const start = () => {
    api.timeframes()
      .then((tfs) => {
        setTimeframes(tfs)
        setReady('ok')
        api.trendConfig().then((c) => setTrendTfs(c.tfs)).catch(() => {})
      })
      .catch((e) => (e instanceof AuthError ? setReady('login') : onError(e)))
  }
  useEffect(start, []) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (ready !== 'ok') return
    const poll = () => api.status().then(setStatus).catch(() => setStatus({}))
    poll()
    const id = window.setInterval(poll, 5000)
    return () => window.clearInterval(id)
  }, [ready])

  if (ready === 'loading') return <div className="center muted">Connexion…</div>
  if (ready === 'login') return <Login onDone={start} />

  const tfLabel = timeframes.find((t) => t.tf === prefs.tf)?.label ?? `${prefs.tf}s`
  const barProps = {
    timeframes, tf: prefs.tf, onTf: (tf: number) => update({ tf }),
    tool, onTool: setTool, magnet: prefs.magnet, onMagnet: (magnet: boolean) => update({ magnet }),
    onClearDrawings: () => {
      if (window.confirm(`Effacer tous les dessins de ${prefs.symbol} ?`)) setClearToken(Date.now())
    },
    strategies: prefs.strategies, onStrategies: (strategies: ActiveStrategy[]) => update({ strategies }),
    live,
  }

  return (
    <div className="layout">
      <TopBar {...barProps} />
      <DrawBar {...barProps} />
      <main className="main">
        <div className="chart-flags">
          <button className={prefs.trendBand ? 'on' : ''} onClick={() => update({ trendBand: !prefs.trendBand })}
                  title="Bande de tendance multi-timeframe sous le graphique">Tendances</button>
          <button className={prefs.showTrades ? 'on' : ''} onClick={() => update({ showTrades: !prefs.showTrades })}
                  title="Afficher les trades du moteur (P = paper, D = démo)">Trades du moteur</button>
          {overlay && (
            <span className="flag">
              {overlay.label} · {overlay.trades.length} trades
              <button onClick={() => setOverlay(null)} title="Masquer">✕</button>
            </span>
          )}
        </div>
        <Chart
          symbol={prefs.symbol}
          tf={prefs.tf}
          tfLabel={tfLabel}
          tool={tool}
          magnet={prefs.magnet}
          strategies={prefs.strategies}
          clearToken={clearToken}
          overlay={overlay}
          showTrades={prefs.showTrades}
          trendBand={prefs.trendBand}
          trendTfs={trendTfs}
          onToolDone={() => setTool(null)}
          onLive={setLive}
          onError={onError}
        />
      </main>
      <aside className="sidebar">
        <div className="tabs">
          <button className={prefs.tab === 'watchlist' ? 'on' : ''} onClick={() => update({ tab: 'watchlist' })}>Watchlist</button>
          <button className={prefs.tab === 'trends' ? 'on' : ''} onClick={() => update({ tab: 'trends' })}>Tendances</button>
          <button className={prefs.tab === 'strategies' ? 'on' : ''} onClick={() => update({ tab: 'strategies' })}>Stratégies</button>
        </div>
        {prefs.tab === 'trends' ? (
          <TrendsPanel selected={prefs.symbol} trendTfs={trendTfs} onSelect={(symbol) => update({ symbol })} onError={onError} />
        ) : prefs.tab === 'watchlist' ? (
          <Sidebar selected={prefs.symbol} onSelect={(symbol) => update({ symbol })} onError={onError}
                   status={status.collector ?? null} />
        ) : (
          <StrategiesPanel
            symbol={prefs.symbol}
            tf={prefs.tf}
            timeframes={timeframes}
            engineAlive={status.engine ? Boolean(status.engine.alive) : null}
            onFocus={(symbol, tf) => update({ symbol, tf })}
            onOverlay={setOverlay}
            onError={onError}
          />
        )}
      </aside>
      {toast && <div className="toast">{toast}</div>}
    </div>
  )
}
