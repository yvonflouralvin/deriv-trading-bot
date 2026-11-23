import { useEffect, useState } from 'react'
import type { DrawingKind } from 'lightweight-charts-drawing'
import { api, auth, AuthError, type Timeframe } from './api'
import Chart, { type ActiveStrategy } from './Chart'
import Sidebar from './Sidebar'
import { DrawBar, TopBar } from './Toolbar'

const PREFS = 'monitor.prefs'
interface Prefs { symbol: string; tf: number; strategies: ActiveStrategy[]; magnet: boolean }

function loadPrefs(): Prefs {
  const def: Prefs = { symbol: 'R_100', tf: 60, strategies: [], magnet: true }
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
      .then((tfs) => { setTimeframes(tfs); setReady('ok') })
      .catch((e) => (e instanceof AuthError ? setReady('login') : onError(e)))
  }
  useEffect(start, []) // eslint-disable-line react-hooks/exhaustive-deps

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
        <Chart
          symbol={prefs.symbol}
          tf={prefs.tf}
          tfLabel={tfLabel}
          tool={tool}
          magnet={prefs.magnet}
          strategies={prefs.strategies}
          clearToken={clearToken}
          onToolDone={() => setTool(null)}
          onLive={setLive}
          onError={onError}
        />
      </main>
      <Sidebar selected={prefs.symbol} onSelect={(symbol) => update({ symbol })} onError={onError} />
      {toast && <div className="toast">{toast}</div>}
    </div>
  )
}
