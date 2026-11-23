import { useEffect, useState } from 'react'
import { api, type CollectorStatus, type SymbolInfo, type WatchItem } from './api'

interface Props {
  selected: string
  onSelect: (symbol: string) => void
  onError: (e: unknown) => void
  status: CollectorStatus | null
}

export default function Sidebar({ selected, onSelect, onError, status }: Props) {
  const [items, setItems] = useState<WatchItem[]>([])
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<SymbolInfo[]>([])

  const refresh = () => {
    api.watchlist().then(setItems).catch(onError)
  }

  useEffect(() => {
    refresh()
    const id = window.setInterval(refresh, 5000)
    return () => window.clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (!query.trim()) { setResults([]); return }
    const id = window.setTimeout(() => api.symbols(query).then(setResults).catch(onError), 250)
    return () => window.clearTimeout(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query])

  const add = async (s: string) => {
    await api.addWatch(s).catch(onError)
    setQuery('')
    refresh()
    onSelect(s)
  }
  const remove = async (s: string) => {
    if (!window.confirm(`Arrêter de collecter ${s} ? (l'historique déjà stocké est conservé)`)) return
    await api.removeWatch(s).catch(onError)
    refresh()
  }
  const toggle = async (w: WatchItem) => {
    await api.patchWatch(w.symbol, { enabled: !w.enabled }).catch(onError)
    refresh()
  }

  const live = status?.alive && status.connected
  return (
    <div className="panel">
      <div className="side-head">
        <span className={`dot ${live ? 'ok' : 'ko'}`} />
        Collecteur {status ? (live ? 'actif' : status.alive ? 'déconnecté de Deriv' : 'arrêté') : 'inconnu'}
        {status && <span className="muted"> · {status.ticks_written.toLocaleString('fr-FR')} ticks</span>}
      </div>

      <div className="search">
        <input
          placeholder="Ajouter un actif (ex. Volatility, EUR/USD)…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        {results.length > 0 && (
          <ul className="results">
            {results.map((r) => (
              <li key={r.symbol}>
                <div>
                  <div>{r.display_name}</div>
                  <div className="muted small">{r.symbol} · {r.market_display}{r.exchange_open === false ? ' · fermé' : ''}</div>
                </div>
                {r.watched
                  ? <span className="muted small">suivi</span>
                  : <button onClick={() => add(r.symbol)}>+ Suivre</button>}
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="side-title">Watchlist</div>
      <ul className="watchlist">
        {items.map((w) => {
          const failed = status?.failed?.[w.symbol]
          return (
            <li
              key={w.symbol}
              className={`${w.symbol === selected ? 'active' : ''} ${w.enabled ? '' : 'paused'}`}
              onClick={() => onSelect(w.symbol)}
              title={failed ?? ''}
            >
              <div className="w-name">
                <div>{w.display_name}</div>
                <div className="muted small">
                  {w.symbol}{failed ? ' · ⚠ indisponible' : ''}{w.enabled ? '' : ' · en pause'}
                </div>
              </div>
              <div className="w-price">{w.last_price != null ? w.last_price.toFixed(w.last_price >= 100 ? 2 : 5) : '—'}</div>
              <div className="w-actions" onClick={(e) => e.stopPropagation()}>
                <button title={w.enabled ? 'Mettre en pause' : 'Reprendre'} onClick={() => toggle(w)}>
                  {w.enabled ? '❚❚' : '▶'}
                </button>
                <button title="Retirer" onClick={() => remove(w.symbol)}>✕</button>
              </div>
            </li>
          )
        })}
        {!items.length && <li className="muted">Aucun actif suivi. Ajoutez-en un ci-dessus.</li>}
      </ul>
    </div>
  )
}
