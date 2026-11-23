import { useEffect, useState } from 'react'
import { api, type GroupStats, type Timeframe, type TrendNow, type TrendReport } from './api'
import { Modal } from './Strategies'

const FOREX = [
  { symbol: 'frxEURUSD', name: 'EUR/USD' },
  { symbol: 'frxGBPUSD', name: 'GBP/USD' },
  { symbol: 'frxUSDJPY', name: 'USD/JPY' },
]
const pct = (n: number | null | undefined, sign = false) =>
  n == null ? '—' : `${sign && n > 0 ? '+' : ''}${(n * 100).toFixed(1)}${sign ? ' pts' : ' %'}`
const num = (n: number | null | undefined, d = 2) => (n == null ? '—' : n.toFixed(d))

interface Props {
  selected: string
  trendTfs: Timeframe[]
  onSelect: (symbol: string) => void
  onError: (e: unknown) => void
}

export default function TrendsPanel(p: Props) {
  const [rows, setRows] = useState<TrendNow[]>([])
  const [reportOpen, setReportOpen] = useState(false)
  const tfs = [...p.trendTfs].sort((a, b) => b.tf - a.tf)

  const refresh = () => api.trendsNow().then(setRows).catch(p.onError)
  useEffect(() => {
    refresh()
    const id = window.setInterval(refresh, 30_000)
    return () => window.clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const follow = async (symbol: string) => {
    await api.addWatch(symbol, 365).catch(p.onError)
    refresh()
  }
  const missing = FOREX.filter((f) => !rows.find((r) => r.symbol === f.symbol && r.backfill_days >= 365))

  return (
    <div className="panel">
      <div className="side-head row">
        <span className="muted small">Tendance par unité de temps (bougies clôturées)</span>
        <button className="primary" onClick={() => setReportOpen(true)}>Évaluer</button>
      </div>
      {missing.length > 0 && (
        <div className="quick-add">
          <div className="muted small">Paires forex avec 1 an d'historique :</div>
          {missing.map((f) => (
            <button key={f.symbol} onClick={() => follow(f.symbol)}>+ {f.name}</button>
          ))}
        </div>
      )}
      <table className="trend-table">
        <thead>
          <tr>
            <th>Actif</th>
            {tfs.map((t) => <th key={t.tf}>{t.label}</th>)}
            <th title="Alignement des trois unités de temps">Align.</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.symbol} className={r.symbol === p.selected ? 'active' : ''} onClick={() => p.onSelect(r.symbol)}>
              <td className="t-name" title={r.symbol}>{r.display_name}</td>
              {tfs.map((t) => {
                const s = r.states[String(t.tf)]
                return (
                  <td key={t.tf} title={s ? `force ${s.strength} · ADX ${s.adx?.toFixed(1)}` : 'pas encore calculé'}>
                    {s ? <Cell state={s.state} strength={s.strength} /> : <span className="muted">…</span>}
                  </td>
                )
              })}
              <td>
                {r.align == null ? <span className="muted">…</span>
                  : r.align === 0 ? <span className="muted small">mixte</span>
                  : <span className={`align ${r.align > 0 ? 'up' : 'down'}`} title={r.align > 0 ? 'Aligné haussier' : 'Aligné baissier'}>
                      {r.align > 0 ? '▲' : '▼'} {r.strength}</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="muted small legend-note">
        La bande sous le graphique montre l'historique : une ligne par unité de temps (vert haussier, rouge baissier,
        gris neutre ; plus c'est opaque, plus c'est fort) et la ligne « Align. » quand les trois sont d'accord.
      </p>
      {reportOpen && (
        <ReportModal symbols={rows.map((r) => r.symbol)} tfs={tfs} onClose={() => setReportOpen(false)} onError={p.onError} />
      )}
    </div>
  )
}

function Cell({ state, strength }: { state: number; strength: number }) {
  const cls = state > 0 ? 'up' : state < 0 ? 'down' : 'muted'
  return <span className={`cell ${cls}`}>{state > 0 ? '▲' : state < 0 ? '▼' : '■'} <small>{strength}</small></span>
}

// --------------------------------------------------------------------------- //
function ReportModal({ symbols, tfs, onClose, onError }: {
  symbols: string[]; tfs: Timeframe[]; onClose: () => void; onError: (e: unknown) => void
}) {
  const [chosen, setChosen] = useState<string[]>(symbols.filter((s) => s.startsWith('frx')).length
    ? symbols.filter((s) => s.startsWith('frx')) : symbols.slice(0, 3))
  const [days, setDays] = useState(90)
  const [rep, setRep] = useState<TrendReport | null>(null)
  const [history, setHistory] = useState<TrendReport[]>([])
  const [view, setView] = useState('pooled')
  const [h, setH] = useState('24')

  useEffect(() => { api.trendReports().then(setHistory).catch(onError) }, []) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!rep || rep.status === 'done' || rep.status === 'error') return
    const id = window.setTimeout(() => api.trendReport(rep.id).then((r) => {
      setRep(r)
      if (r.status === 'done') api.trendReports().then(setHistory).catch(() => {})
    }).catch(onError), 1000)
    return () => window.clearTimeout(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rep])

  const run = async () => {
    try {
      const { id } = await api.runTrendReport(chosen, days)
      setRep({ id, status: 'pending' } as TrendReport)
      setView('pooled')
    } catch (e) { onError(e) }
  }
  const busy = rep && (rep.status === 'pending' || rep.status === 'running')
  const res = rep?.status === 'done' ? rep.result : null
  const table: Record<string, GroupStats> | undefined = res
    ? (view === 'pooled' ? res.pooled : res.symbols.find((s) => s.symbol === view)?.horizons)?.[h]
    : undefined
  const label = (tf: number) => tfs.find((t) => t.tf === tf)?.label ?? `${tf}s`

  return (
    <Modal title="Évaluer le détecteur de tendance" onClose={onClose}>
      <div className="form">
        <p className="muted small hint">
          Question testée : quand les unités de temps sont alignées, le prix part-il <b>ensuite</b> dans ce sens plus
          souvent qu'à un instant quelconque (la <b>référence</b>) ? Mesures en ATR H1, sur les heures de marché
          suivantes. Les 30 % les plus récents servent de validation.
        </p>
        <div className="sym-pick">
          {symbols.map((s) => (
            <label key={s} className="check">
              <input type="checkbox" checked={chosen.includes(s)}
                     onChange={(e) => setChosen(e.target.checked ? [...chosen, s] : chosen.filter((x) => x !== s))} />
              {s}
            </label>
          ))}
        </div>
        <div className="row-inline">
          <label>Période
            <select value={days} onChange={(e) => setDays(Number(e.target.value))}>
              {[30, 90, 180, 365].map((d) => <option key={d} value={d}>{d} jours</option>)}
            </select>
          </label>
          <button className="primary" disabled={!!busy || !chosen.length} onClick={run}>
            {busy ? 'Calcul…' : "Lancer l'évaluation"}
          </button>
        </div>

        {rep?.status === 'error' && <p className="error">Erreur : {rep.error}</p>}
        {res && (
          <>
            {res.missing.length > 0 && (
              <p className="muted small warn">Historique insuffisant pour : {res.missing.join(', ')} (le collecteur le
                charge peut-être encore).</p>
            )}
            {res.symbols.length > 0 && table && (
              <>
                <div className="row-inline">
                  <label>Vue
                    <select value={view} onChange={(e) => setView(e.target.value)}>
                      <option value="pooled">Toutes les paires</option>
                      {res.symbols.map((s) => <option key={s.symbol} value={s.symbol}>{s.symbol}</option>)}
                    </select>
                  </label>
                  <label>Horizon
                    <select value={h} onChange={(e) => setH(e.target.value)}>
                      {res.horizons.map((x) => <option key={x} value={String(x)}>{x} h de marché</option>)}
                    </select>
                  </label>
                </div>
                <Verdict t={table} />
                <table className="report">
                  <thead>
                    <tr><th>Situation</th><th>Part du temps</th><th>Épisodes</th><th>Bon sens</th>
                      <th>Avantage</th><th>Mvt moyen (ATR)</th><th>MFE / MAE</th></tr>
                  </thead>
                  <tbody>
                    <tr className="muted">
                      <td>Référence (tout instant)</td><td>100 %</td><td>—</td>
                      <td title="% des cas où le prix monte">{pct(table.baseline?.pct_up)} ↑</td><td>—</td>
                      <td>{num(table.baseline?.mean_fwd)}</td><td>—</td>
                    </tr>
                    <Row name="Aligné haussier (3/3)" g={table.aligned_up} strong />
                    <Row name="Aligné baissier (3/3)" g={table.aligned_down} strong />
                    {tfs.map((t) => [
                      <Row key={`${t.tf}u`} name={`${t.label} seul haussier`} g={table[`${t.tf}_up`]} />,
                      <Row key={`${t.tf}d`} name={`${t.label} seul baissier`} g={table[`${t.tf}_down`]} />,
                    ])}
                  </tbody>
                </table>
                <fieldset>
                  <legend>Tient-il dans le temps ? (réglage 70 % / validation 30 %)</legend>
                  <table className="report">
                    <thead><tr><th></th><th>Avantage période 1</th><th>Avantage validation</th><th>Mvt validation (ATR)</th></tr></thead>
                    <tbody>
                      {(['aligned_up', 'aligned_down'] as const).map((k) => (
                        <tr key={k}>
                          <td>{k === 'aligned_up' ? 'Aligné haussier' : 'Aligné baissier'}</td>
                          <td className={cls(table[k]?.in_sample?.edge_hit)}>{pct(table[k]?.in_sample?.edge_hit, true)}</td>
                          <td className={cls(table[k]?.out_of_sample?.edge_hit)}>{pct(table[k]?.out_of_sample?.edge_hit, true)}</td>
                          <td>{num(table[k]?.out_of_sample?.mean_dir)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </fieldset>
                {view !== 'pooled' && (() => {
                  const sr = res.symbols.find((s) => s.symbol === view)
                  if (!sr) return null
                  return (
                    <fieldset>
                      <legend>Stabilité du détecteur</legend>
                      <table className="report">
                        <thead><tr><th></th><th>Haussier</th><th>Baissier</th><th>Neutre</th><th>Changements / jour</th><th>Durée médiane</th><th>Force moy.</th></tr></thead>
                        <tbody>
                          {tfs.map((t) => {
                            const st = sr.stability[String(t.tf)]
                            if (!st) return null
                            return (
                              <tr key={t.tf}>
                                <td>{label(t.tf)}</td><td>{pct(st.share_up)}</td><td>{pct(st.share_down)}</td>
                                <td>{pct(st.share_neutral)}</td><td>{num(st.flips_per_day, 1)}</td>
                                <td>{num(st.median_run_hours, 1)} h</td><td>{num(st.mean_strength, 0)}</td>
                              </tr>
                            )
                          })}
                        </tbody>
                      </table>
                    </fieldset>
                  )
                })()}
                <details className="howto">
                  <summary>Comment lire ce rapport</summary>
                  <ul className="small">
                    <li><b>Bon sens</b> : % des cas où le prix est allé dans la direction annoncée à la fin de l'horizon.</li>
                    <li><b>Avantage</b> : écart avec la référence. C'est LE chiffre : +5 pts = le détecteur voit juste 5 fois
                      de plus sur 100 que le hasard. Proche de 0 = aucune valeur.</li>
                    <li><b>Mvt moyen</b> : chemin parcouru dans le sens annoncé, en ATR H1 (1 = une bougie H1 « typique »).</li>
                    <li><b>MFE / MAE</b> : excursion favorable maximale ÷ défavorable maximale. Au-dessus de 1, le prix va plus
                      loin dans notre sens que contre nous — utile pour placer stop et objectif.</li>
                    <li><b>Épisodes</b> : nombre de périodes d'alignement distinctes. C'est la vraie taille d'échantillon :
                      en dessous d'une trentaine, les pourcentages sont fragiles.</li>
                    <li><b>Lignes « seul »</b> : une seule unité de temps. Si l'alignement 3/3 ne fait pas mieux qu'elles, la
                      confirmation fractale n'apporte rien.</li>
                    <li><b>Validation</b> : l'avantage doit garder le même signe sur les 30 % de données les plus récentes.</li>
                  </ul>
                </details>
              </>
            )}
          </>
        )}

        {history.length > 0 && (
          <fieldset>
            <legend>Rapports précédents</legend>
            <ul className="bt-history">
              {history.map((r) => (
                <li key={r.id} className={r.id === rep?.id ? 'on' : ''}
                    onClick={() => api.trendReport(r.id).then(setRep).catch(onError)}>
                  <span>#{r.id} · {r.days} j · {r.symbols.join(', ')}</span><span>{r.status}</span>
                </li>
              ))}
            </ul>
          </fieldset>
        )}
      </div>
    </Modal>
  )
}

const cls = (x: number | null | undefined) => (x == null ? '' : x > 0.02 ? 'up' : x < -0.02 ? 'down' : 'muted')

function Row({ name, g, strong }: { name: string; g?: GroupStats; strong?: boolean }) {
  if (!g || !g.n) return <tr className="muted"><td>{name}</td><td colSpan={6}>aucun cas</td></tr>
  return (
    <tr className={strong ? 'strong' : ''}>
      <td>{name}</td>
      <td>{pct(g.share)}</td>
      <td>{g.episodes ?? '—'}</td>
      <td>{pct(g.hit)}</td>
      <td className={cls(g.edge_hit)}><b>{pct(g.edge_hit, true)}</b></td>
      <td>{num(g.mean_dir)}</td>
      <td>{num(g.mfe_mae)}</td>
    </tr>
  )
}

function Verdict({ t }: { t: Record<string, GroupStats> }) {
  const up = t.aligned_up, dn = t.aligned_down
  if (!up?.n || !dn?.n) return <p className="muted small">Pas assez de périodes alignées pour conclure.</p>
  const eps = (up.episodes ?? 0) + (dn.episodes ?? 0)
  const both = (up.edge_hit ?? 0) > 0.03 && (dn.edge_hit ?? 0) > 0.03
  const holds = (up.out_of_sample?.edge_hit ?? 0) > 0 && (dn.out_of_sample?.edge_hit ?? 0) > 0
  const beatsSingle = Object.entries(t)
    .filter(([k]) => k.endsWith('_up') && k !== 'aligned_up')
    .every(([, g]) => (up.edge_hit ?? 0) >= (g.edge_hit ?? 0))
  let msg: string, c: string
  if (eps < 30) { msg = `Seulement ${eps} épisodes : trop peu pour conclure. Allongez la période ou ajoutez des paires.`; c = 'warn' }
  else if (both && holds) {
    msg = `Le détecteur apporte un avantage dans les deux sens, confirmé sur la période de validation${beatsSingle ? ', et l\'alignement 3/3 fait mieux qu\'une unité de temps seule' : ' (mais l\'alignement ne fait pas mieux qu\'une unité de temps seule)'}.`
    c = 'good'
  } else if (both) { msg = 'Avantage sur l\'ensemble, mais il ne se confirme pas sur la période de validation : prudence.'; c = 'warn' }
  else { msg = 'Pas d\'avantage net et régulier dans les deux sens : le détecteur ne fait pas mieux que le hasard ici.'; c = 'bad' }
  return <p className={`verdict ${c}`}>{msg}</p>
}
