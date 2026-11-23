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
const sig = (g?: GroupStats) => {
  // couleur seulement si l'avantage dépasse la marge d'erreur
  if (!g || g.edge_hit == null || g.ci == null) return ''
  return g.edge_hit > g.ci ? 'up' : g.edge_hit < -g.ci ? 'down' : 'muted'
}
const edge = (g?: GroupStats) => (g?.edge_hit == null ? '—'
  : `${g.edge_hit > 0 ? '+' : ''}${(g.edge_hit * 100).toFixed(1)}${g.ci != null ? ` ±${(g.ci * 100).toFixed(1)}` : ''}`)

function ReportModal({ symbols, tfs, onClose, onError }: {
  symbols: string[]; tfs: Timeframe[]; onClose: () => void; onError: (e: unknown) => void
}) {
  const fx = symbols.filter((s) => s.startsWith('frx'))
  const [chosen, setChosen] = useState<string[]>(fx.length ? fx : symbols.slice(0, 3))
  const [days, setDays] = useState(365)
  const [rep, setRep] = useState<TrendReport | null>(null)
  const [history, setHistory] = useState<TrendReport[]>([])
  const [variant, setVariant] = useState('A')
  const [view, setView] = useState('pooled')
  const [h, setH] = useState('24')

  useEffect(() => { api.trendReports().then(setHistory).catch(onError) }, []) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!rep || rep.status === 'done' || rep.status === 'error') return
    const id = window.setTimeout(() => api.trendReport(rep.id).then((r) => {
      setRep(r)
      if (r.status === 'done') api.trendReports().then(setHistory).catch(() => {})
    }).catch(onError), 1500)
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
  const vars = res?.variants ?? []
  const cur = vars.find((v) => v.id === variant) ?? vars[0]
  const table = cur ? (view === 'pooled' ? cur.pooled : cur.symbols.find((s) => s.symbol === view)?.horizons)?.[h] : undefined
  const label = (tf: number) => tfs.find((t) => t.tf === tf)?.label ?? ({ 300: '5m', 900: '15m', 3600: '1h', 14400: '4h' } as Record<number, string>)[tf] ?? `${tf}s`
  const lag = cur ? (view === 'pooled' ? cur.lag : cur.symbols.find((s) => s.symbol === view)?.lag) : undefined

  return (
    <Modal title="Évaluer le détecteur de tendance" onClose={onClose}>
      <div className="form">
        <p className="muted small hint">
          Compare 5 variantes du détecteur sur les mêmes données. Pour chacune : la tendance confirmée annonce-t-elle
          la suite mieux que le hasard, avec quel retard, selon sa phase, et un <b>repli dans le sens de la
          tendance</b> repart-il dans ce sens ? Mesures en ATR H1 ; les 30 % les plus récents servent de validation.
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
            {busy ? 'Calcul… (≈ 1 min)' : "Lancer l'évaluation"}
          </button>
        </div>

        {rep?.status === 'error' && <p className="error">Erreur : {rep.error}</p>}
        {res && !res.variants && <p className="muted small warn">Rapport de l'ancienne version : relancez une évaluation.</p>}
        {res && vars.length > 0 && (
          <>
            {res.missing.length > 0 && (
              <p className="muted small warn">Historique insuffisant pour : {res.missing.join(', ')} (certaines
                variantes ; le collecteur le charge peut-être encore).</p>
            )}
            <div className="row-inline">
              <label>Horizon
                <select value={h} onChange={(e) => setH(e.target.value)}>
                  {res.horizons.map((x) => <option key={x} value={String(x)}>{x} h de marché</option>)}
                </select>
              </label>
            </div>
            <Verdict vars={vars} h={h} />
            <fieldset>
              <legend>Comparaison des variantes (toutes paires)</legend>
              <table className="report compare">
                <thead>
                  <tr>
                    <th>Variante</th><th title="Périodes d'alignement distinctes">Épisodes</th>
                    <th title="Avantage ± marge d'erreur à 95 %">Avantage</th><th>Validation</th>
                    <th title="Heures entre le départ d'un vrai mouvement et la confirmation">Retard</th>
                    <th title="Part du mouvement déjà faite au moment de la confirmation">Déjà fait</th>
                    <th title="Vrais mouvements jamais confirmés">Manqués</th>
                    <th title="Repli du RSI de la petite UT dans le sens de la tendance des 2 grandes UT">Repli dans le sens</th>
                  </tr>
                </thead>
                <tbody>
                  {vars.map((v) => {
                    const t = v.pooled[h] ?? {}
                    const al = t.aligned
                    return (
                      <tr key={v.id} className={`${v.id === cur?.id ? 'strong' : ''} clickable`} onClick={() => setVariant(v.id)}
                          title={v.description}>
                        <td><b>{v.id}</b> {v.name}</td>
                        <td>{al?.episodes ?? '—'}</td>
                        <td className={sig(al)}>{edge(al)}</td>
                        <td className={sig(al?.out_of_sample)}>{edge(al?.out_of_sample)}</td>
                        <td>{v.lag.lag_hours_median != null ? `${v.lag.lag_hours_median} h` : '—'}</td>
                        <td>{v.lag.done_pct_median != null ? `${Math.round(v.lag.done_pct_median * 100)} %` : '—'}</td>
                        <td>{v.lag.missed_pct != null ? `${Math.round(v.lag.missed_pct * 100)} %` : '—'}</td>
                        <td className={sig(t.pb_with)}>{edge(t.pb_with)}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
              <p className="muted small">Avantages en points de % ± marge d'erreur à 95 %. Vert / rouge seulement quand
                l'écart dépasse la marge ; gris = dans le bruit. Cliquez une variante pour le détail.</p>
            </fieldset>

            {cur && table && (
              <fieldset>
                <legend>Détail — {cur.id} {cur.name} ({cur.tfs.map(label).join(' / ')}{cur.align_mode === 'bias' ? ', grande UT = biais' : ''})</legend>
                <p className="muted small hint">{cur.description}</p>
                <div className="row-inline">
                  <label>Vue
                    <select value={view} onChange={(e) => setView(e.target.value)}>
                      <option value="pooled">Toutes les paires</option>
                      {cur.symbols.map((s) => <option key={s.symbol} value={s.symbol}>{s.symbol}</option>)}
                    </select>
                  </label>
                </div>
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
                    <Row name="Aligné haussier" g={table.aligned_up} strong />
                    <Row name="Aligné baissier" g={table.aligned_down} strong />
                    <tr className="sep"><td colSpan={7}>Selon la phase de la tendance au moment de la mesure</td></tr>
                    <Row name="Naissante (jeune, proche de sa moyenne)" g={table.phase_young} />
                    <Row name="Mature" g={table.phase_mature} />
                    <Row name="Étendue (≥ 2,5 ATR de sa moyenne)" g={table.phase_extended} />
                    <tr className="sep"><td colSpan={7}>Repli du RSI de la petite UT (sous 40 / au-dessus de 60)</td></tr>
                    <Row name="Dans le sens de la tendance" g={table.pb_with} strong />
                    <Row name="Hors tendance (2 grandes UT pas d'accord)" g={table.pb_neutral} />
                    <Row name="À contre-tendance" g={table.pb_counter} />
                    <tr className="sep"><td colSpan={7}>Une seule unité de temps</td></tr>
                    {cur.tfs.map((tf) => [
                      <Row key={`${tf}u`} name={`${label(tf)} seul haussier`} g={table[`${tf}_up`]} />,
                      <Row key={`${tf}d`} name={`${label(tf)} seul baissier`} g={table[`${tf}_down`]} />,
                    ])}
                  </tbody>
                </table>
                {lag && lag.legs > 0 && (
                  <p className="muted small">
                    Retard : sur {lag.legs} vrais mouvements (≥ 4 ATR H1, repérés après coup), {lag.detected} confirmés,
                    en médiane <b>{lag.lag_hours_median} h</b> après leur départ, quand <b>{Math.round((lag.done_pct_median ?? 0) * 100)} %</b>
                    {' '}du mouvement était déjà fait ; {Math.round((lag.missed_pct ?? 0) * 100)} % jamais confirmés.
                  </p>
                )}
                {view !== 'pooled' && (() => {
                  const sr = cur.symbols.find((s) => s.symbol === view)
                  if (!sr) return null
                  return (
                    <table className="report">
                      <thead><tr><th>Stabilité</th><th>Haussier</th><th>Baissier</th><th>Neutre</th><th>Changements / jour</th><th>Durée médiane</th></tr></thead>
                      <tbody>
                        {cur.tfs.map((tf) => {
                          const st = sr.stability[String(tf)]
                          if (!st) return null
                          return (
                            <tr key={tf}><td>{label(tf)}</td><td>{pct(st.share_up)}</td><td>{pct(st.share_down)}</td>
                              <td>{pct(st.share_neutral)}</td><td>{num(st.flips_per_day, 1)}</td><td>{num(st.median_run_hours, 1)} h</td></tr>
                          )
                        })}
                      </tbody>
                    </table>
                  )
                })()}
              </fieldset>
            )}
            <details className="howto">
              <summary>Comment lire ce rapport</summary>
              <ul className="small">
                <li><b>Avantage</b> : écart (en points de %) entre le taux de réussite et celui du hasard dans le même
                  sens. « +6,0 ±4,2 » = l'avantage est probablement entre +1,8 et +10,2 : réel. « +2,0 ±4,2 » = dans le bruit.</li>
                <li><b>Validation</b> : le même calcul sur les 30 % de données les plus récentes, jamais utilisées pour
                  choisir. Un avantage qui disparaît en validation ne doit pas être cru.</li>
                <li><b>Retard / Déjà fait</b> : sur les grands mouvements réels, combien d'heures après leur départ la
                  tendance est confirmée et quelle part du mouvement est déjà consommée. Plus c'est bas, mieux c'est.</li>
                <li><b>Phases</b> : si « Naissante » est positif et « Étendue » négatif, le problème vient d'entrées
                  trop tardives ; il faudra n'entrer que tôt dans la tendance.</li>
                <li><b>Repli dans le sens</b> : la question de votre méthode — un repli pendant une tendance confirmée
                  par les 2 grandes UT repart-il dans le sens de la tendance ? À comparer avec « Hors tendance ».</li>
                <li><b>Épisodes</b> : périodes distinctes ; c'est la vraie taille d'échantillon, d'où la marge d'erreur.</li>
              </ul>
            </details>
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

function Row({ name, g, strong }: { name: string; g?: GroupStats; strong?: boolean }) {
  if (!g || !g.n) return <tr className="muted"><td>{name}</td><td colSpan={6}>aucun cas</td></tr>
  return (
    <tr className={strong ? 'strong' : ''}>
      <td>{name}</td>
      <td>{pct(g.share)}</td>
      <td>{g.episodes ?? '—'}</td>
      <td>{pct(g.hit)}</td>
      <td className={sig(g)}><b>{edge(g)}</b></td>
      <td>{num(g.mean_dir)}</td>
      <td>{num(g.mfe_mae)}</td>
    </tr>
  )
}

function Verdict({ vars, h }: { vars: { id: string; name: string; pooled: Record<string, Record<string, GroupStats>> }[]; h: string }) {
  const real = (g?: GroupStats) => !!g && g.edge_hit != null && g.ci != null && g.edge_hit > g.ci
  const neg = (g?: GroupStats) => !!g && g.edge_hit != null && g.ci != null && g.edge_hit < -g.ci
  const good = vars.filter((v) => real(v.pooled[h]?.aligned) && (v.pooled[h]?.aligned?.out_of_sample?.edge_hit ?? 0) > 0)
  const pb = vars.filter((v) => real(v.pooled[h]?.pb_with) && (v.pooled[h]?.pb_with?.out_of_sample?.edge_hit ?? 0) > 0)
  const bad = vars.filter((v) => neg(v.pooled[h]?.aligned))
  const parts: string[] = []
  let c = 'bad'
  if (good.length) { parts.push(`Tendance confirmée utile (au-delà de la marge, et tenue en validation) : ${good.map((v) => v.id).join(', ')}.`); c = 'good' }
  if (pb.length) { parts.push(`Le repli dans le sens de la tendance a un avantage réel avec : ${pb.map((v) => v.id).join(', ')}.`); c = 'good' }
  if (!good.length && !pb.length) {
    if (bad.length) parts.push(`Aucune variante n'apporte d'avantage ; ${bad.map((v) => v.id).join(', ')} font même significativement moins bien que le hasard : sur cet horizon, le marché a tendance à revenir en arrière après un mouvement (comportement de range).`)
    else parts.push('Aucune variante ne sort de la marge d\'erreur : pas d\'avantage démontré sur cet horizon. Plus de données (365 j, plus de paires) ou un autre horizon sont nécessaires.')
    c = bad.length ? 'bad' : 'warn'
  }
  return <p className={`verdict ${c}`}>{parts.join(' ')}</p>
}
