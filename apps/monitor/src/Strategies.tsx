import { useEffect, useState } from 'react'
import {
  api,
  type Backtest,
  type BacktestTrade,
  type DurationUnit,
  type StrategyConfig,
  type StrategyKind,
  type StrategyRow,
  type Timeframe,
} from './api'

const UNITS: Record<DurationUnit, string> = { t: 'ticks', s: 'secondes', m: 'minutes' }
const money = (n: number | null | undefined) => (n == null ? '—' : `${n > 0 ? '+' : ''}${n.toFixed(2)}`)
const pct = (n: number | null | undefined) => (n == null ? '—' : `${(n * 100).toFixed(1)} %`)

interface PanelProps {
  symbol: string
  tf: number
  timeframes: Timeframe[]
  onFocus: (symbol: string, tf: number) => void
  onOverlay: (o: { label: string; symbol: string; tf: number; trades: BacktestTrade[] } | null) => void
  onError: (e: unknown) => void
  engineAlive: boolean | null
}

export default function StrategiesPanel(p: PanelProps) {
  const [rows, setRows] = useState<StrategyRow[]>([])
  const [kinds, setKinds] = useState<StrategyKind[]>([])
  const [editing, setEditing] = useState<StrategyRow | 'new' | null>(null)
  const [testing, setTesting] = useState<StrategyRow | null>(null)

  const refresh = () => api.strategies().then(setRows).catch(p.onError)
  useEffect(() => {
    refresh()
    api.kinds().then(setKinds).catch(p.onError)
    const id = window.setInterval(refresh, 4000)
    return () => window.clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const toggle = async (s: StrategyRow) => {
    if (!s.enabled && s.mode === 'demo' &&
        !window.confirm(`« ${s.name} » va passer de vrais contrats sur le compte DÉMO Deriv (mise ${s.stake}). Continuer ?`)) return
    await api.patchStrategy(s.id, { enabled: !s.enabled }).catch(p.onError)
    refresh()
  }
  const remove = async (s: StrategyRow) => {
    if (!window.confirm(`Supprimer « ${s.name} » et tout son historique de trades ?`)) return
    await api.deleteStrategy(s.id).catch(p.onError)
    refresh()
  }
  const tfLabel = (tf: number) => p.timeframes.find((t) => t.tf === tf)?.label ?? `${tf}s`

  return (
    <div className="strat-panel">
      <div className="side-head row">
        <span>
          <span className={`dot ${p.engineAlive ? 'ok' : 'ko'}`} />
          Moteur {p.engineAlive == null ? 'inconnu' : p.engineAlive ? 'actif' : 'arrêté'}
        </span>
        <button className="primary" onClick={() => setEditing('new')}>+ Nouvelle</button>
      </div>
      <ul className="cards">
        {rows.map((s) => {
          const here = s.symbol === p.symbol
          return (
            <li key={s.id} className={`card ${here ? 'here' : ''} ${s.enabled ? '' : 'off'}`}>
              <div className="card-top">
                <label className="switch" title={s.enabled ? 'Désactiver' : 'Activer'}>
                  <input type="checkbox" checked={s.enabled} onChange={() => toggle(s)} />
                  <span />
                </label>
                <strong className="grow">{s.name}</strong>
                <span className={`badge ${s.mode}`}>{s.mode === 'demo' ? 'DÉMO' : 'PAPER'}</span>
              </div>
              <div className="muted small">
                {s.kind_name ?? s.kind} · {s.symbol} · {tfLabel(s.tf)} · {s.duration} {UNITS[s.duration_unit]} · mise {s.stake}
              </div>
              <div className="card-stats small">
                <span>Aujourd'hui : {s.today_trades} trades · <b className={s.today_pnl >= 0 ? 'up' : 'down'}>{money(s.today_pnl)}</b>
                  {s.today_trades > 0 && ` · ${pct(s.today_wins / s.today_trades)}`}</span>
                <span className="muted">Total : {s.total_trades} · {money(s.total_pnl)}
                  {s.open_trades > 0 && ' · 1 ouvert'}{s.errors > 0 && ` · ⚠ ${s.errors} erreur(s)`}</span>
              </div>
              <div className="card-actions">
                <button onClick={() => p.onFocus(s.symbol, s.tf)}>Voir</button>
                <button onClick={() => setTesting(s)}>Backtest</button>
                <button onClick={() => setEditing(s)}>Éditer</button>
                <button onClick={() => remove(s)} title="Supprimer">🗑</button>
              </div>
            </li>
          )
        })}
        {!rows.length && (
          <li className="muted empty">
            Aucune stratégie. Créez-en une : elle tourne d'abord en <b>paper</b> (simulée), puis en <b>démo</b> quand
            elle vous convainc.
          </li>
        )}
      </ul>

      {editing && (
        <StrategyForm
          initial={editing === 'new' ? null : editing}
          kinds={kinds}
          symbol={p.symbol}
          tf={p.tf}
          timeframes={p.timeframes}
          onClose={() => setEditing(null)}
          onSaved={() => { setEditing(null); refresh() }}
          onError={p.onError}
        />
      )}
      {testing && (
        <BacktestModal
          strategy={testing}
          tfLabel={tfLabel(testing.tf)}
          onClose={() => setTesting(null)}
          onShow={(bt) => {
            p.onFocus(bt.symbol, bt.tf)
            p.onOverlay({ label: `Backtest #${bt.id}`, symbol: bt.symbol, tf: bt.tf, trades: bt.trades ?? [] })
            setTesting(null)
          }}
          onError={p.onError}
        />
      )}
    </div>
  )
}

// --------------------------------------------------------------------------- //
export function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  useEffect(() => {
    const k = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', k)
    return () => window.removeEventListener('keydown', k)
  }, [onClose])
  return (
    <div className="modal-bg" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal">
        <div className="modal-head">
          <strong>{title}</strong>
          <button onClick={onClose}>✕</button>
        </div>
        {children}
      </div>
    </div>
  )
}

interface FormProps {
  initial: StrategyRow | null
  kinds: StrategyKind[]
  symbol: string
  tf: number
  timeframes: Timeframe[]
  onClose: () => void
  onSaved: () => void
  onError: (e: unknown) => void
}

function StrategyForm(p: FormProps) {
  const first = p.kinds[0]
  const [c, setC] = useState<StrategyConfig>(() => p.initial ? { ...p.initial } : {
    name: first ? `${first.name} ${p.symbol}` : '',
    kind: first?.kind ?? '',
    symbol: p.symbol,
    tf: p.tf,
    params: Object.fromEntries((first?.params ?? []).map((x) => [x.key, x.default])),
    mode: 'paper',
    enabled: false,
    stake: 1,
    duration: 5,
    duration_unit: 't',
    payout_ratio: 0.95,
    max_trades_per_day: 20,
    max_daily_loss: 10,
  })
  const [symbols, setSymbols] = useState<string[]>([p.symbol])
  const [saving, setSaving] = useState(false)
  useEffect(() => {
    api.watchlist().then((w) => setSymbols([...new Set([c.symbol, ...w.map((x) => x.symbol)])])).catch(() => {})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const kind = p.kinds.find((k) => k.kind === c.kind)
  const set = <K extends keyof StrategyConfig>(k: K, v: StrategyConfig[K]) => setC((x) => ({ ...x, [k]: v }))
  const changeKind = (k: string) => {
    const kk = p.kinds.find((x) => x.kind === k)
    setC((x) => ({
      ...x, kind: k,
      params: Object.fromEntries((kk?.params ?? []).map((q) => [q.key, q.default])),
      name: !p.initial && kk ? `${kk.name} ${x.symbol}` : x.name,
    }))
  }

  const save = async (e: React.FormEvent) => {
    e.preventDefault()
    if (c.enabled && c.mode === 'demo' && !(p.initial?.enabled && p.initial.mode === 'demo') &&
        !window.confirm('Cette stratégie va passer de vrais contrats sur le compte DÉMO Deriv. Continuer ?')) return
    setSaving(true)
    try {
      if (p.initial) {
        const { kind: _k, ...rest } = c
        void _k
        await api.patchStrategy(p.initial.id, rest)
      } else {
        await api.createStrategy(c)
      }
      p.onSaved()
    } catch (err) {
      p.onError(err)
    } finally {
      setSaving(false)
    }
  }

  const num = (v: string) => (v === '' ? 0 : Number(v))
  return (
    <Modal title={p.initial ? `Modifier « ${p.initial.name} »` : 'Nouvelle stratégie'} onClose={p.onClose}>
      <form className="form" onSubmit={save}>
        <label>Nom<input required value={c.name} onChange={(e) => set('name', e.target.value)} /></label>
        <label>Type
          <select value={c.kind} disabled={!!p.initial} onChange={(e) => changeKind(e.target.value)}>
            {p.kinds.map((k) => <option key={k.kind} value={k.kind}>{k.name}</option>)}
          </select>
        </label>
        {kind && <p className="muted small hint">{kind.description}</p>}
        <div className="row2">
          <label>Actif
            <select value={c.symbol} onChange={(e) => set('symbol', e.target.value)}>
              {symbols.map((s) => <option key={s}>{s}</option>)}
            </select>
          </label>
          <label>Unité de temps
            <select value={c.tf} onChange={(e) => set('tf', Number(e.target.value))}>
              {p.timeframes.map((t) => <option key={t.tf} value={t.tf}>{t.label}</option>)}
            </select>
          </label>
        </div>

        {kind && kind.params.length > 0 && (
          <fieldset>
            <legend>Paramètres</legend>
            <div className="row2">
              {kind.params.map((q) => (
                <label key={q.key}>{q.label}
                  <input type="number" min={q.min} max={q.max} step={q.step} required
                         value={c.params[q.key] ?? q.default}
                         onChange={(e) => set('params', { ...c.params, [q.key]: num(e.target.value) })} />
                </label>
              ))}
            </div>
          </fieldset>
        )}

        <fieldset>
          <legend>Contrat Rise/Fall</legend>
          <div className="row3">
            <label>Durée<input type="number" min={1} required value={c.duration}
                               onChange={(e) => set('duration', num(e.target.value))} /></label>
            <label>Unité
              <select value={c.duration_unit} onChange={(e) => set('duration_unit', e.target.value as DurationUnit)}>
                {Object.entries(UNITS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
              </select>
            </label>
            <label>Mise<input type="number" min={0.35} step={0.01} required value={c.stake}
                              onChange={(e) => set('stake', num(e.target.value))} /></label>
          </div>
          <label title="Gain net par unité misée quand le contrat gagne (paper et backtest). En démo, Deriv fixe le vrai paiement.">
            Taux de paiement (paper / backtest)
            <input type="number" min={0.01} max={10} step={0.01} required value={c.payout_ratio}
                   onChange={(e) => set('payout_ratio', num(e.target.value))} />
          </label>
        </fieldset>

        <fieldset>
          <legend>Exécution et risque</legend>
          <div className="modes">
            <label className={c.mode === 'paper' ? 'on' : ''}>
              <input type="radio" checked={c.mode === 'paper'} onChange={() => set('mode', 'paper')} />
              <b>Paper</b><span className="muted small">contrats simulés avec les vrais ticks, aucun ordre</span>
            </label>
            <label className={c.mode === 'demo' ? 'on' : ''}>
              <input type="radio" checked={c.mode === 'demo'} onChange={() => set('mode', 'demo')} />
              <b>Démo</b><span className="muted small">vrais contrats sur le compte démo Deriv (argent virtuel)</span>
            </label>
          </div>
          <div className="row2">
            <label>Trades max / jour<input type="number" min={1} required value={c.max_trades_per_day}
                                           onChange={(e) => set('max_trades_per_day', num(e.target.value))} /></label>
            <label>Perte max / jour<input type="number" min={0.01} step={0.01} required value={c.max_daily_loss}
                                          onChange={(e) => set('max_daily_loss', num(e.target.value))} /></label>
          </div>
          <label className="check">
            <input type="checkbox" checked={c.enabled} onChange={(e) => set('enabled', e.target.checked)} />
            Activer la stratégie
          </label>
        </fieldset>

        <div className="form-actions">
          <button type="button" onClick={p.onClose}>Annuler</button>
          <button type="submit" className="primary" disabled={saving}>{saving ? 'Enregistrement…' : 'Enregistrer'}</button>
        </div>
      </form>
    </Modal>
  )
}

// --------------------------------------------------------------------------- //
interface BtProps {
  strategy: StrategyRow
  tfLabel: string
  onClose: () => void
  onShow: (bt: Backtest) => void
  onError: (e: unknown) => void
}

function BacktestModal(p: BtProps) {
  const [days, setDays] = useState(7)
  const [current, setCurrent] = useState<Backtest | null>(null)
  const [history, setHistory] = useState<Backtest[]>([])

  const loadHistory = () => api.backtests(p.strategy.id).then(setHistory).catch(p.onError)
  useEffect(() => { loadHistory() }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // suivi du backtest en cours
  useEffect(() => {
    if (!current || current.status === 'done' || current.status === 'error') return
    const id = window.setTimeout(() => {
      api.backtest(current.id).then((b) => {
        setCurrent(b)
        if (b.status === 'done' || b.status === 'error') loadHistory()
      }).catch(p.onError)
    }, 800)
    return () => window.clearTimeout(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [current])

  const run = async () => {
    try {
      const { id } = await api.runBacktest(p.strategy.id, days)
      setCurrent({ id, status: 'pending' } as Backtest)
    } catch (e) {
      p.onError(e)
    }
  }
  const open = (id: number) => api.backtest(id).then(setCurrent).catch(p.onError)
  const s = current?.stats
  const busy = current && (current.status === 'pending' || current.status === 'running')

  return (
    <Modal title={`Backtest — ${p.strategy.name}`} onClose={p.onClose}>
      <div className="form">
        <p className="muted small hint">
          Rejoue la stratégie sur l'historique stocké ({p.strategy.symbol}, {p.tfLabel}, contrat {p.strategy.duration}{' '}
          {UNITS[p.strategy.duration_unit]}, mise {p.strategy.stake}, paiement {p.strategy.payout_ratio}) avec exactement le
          même code que le moteur live.
        </p>
        <div className="row-inline">
          <label>Période
            <select value={days} onChange={(e) => setDays(Number(e.target.value))}>
              {[1, 3, 7, 14, 30, 90].map((d) => <option key={d} value={d}>{d} jour{d > 1 ? 's' : ''}</option>)}
            </select>
          </label>
          <button className="primary" onClick={run} disabled={!!busy}>{busy ? 'Calcul…' : 'Lancer le backtest'}</button>
        </div>

        {current?.status === 'error' && <p className="error">Erreur : {current.error}</p>}
        {s && current?.status === 'done' && (
          <div className="bt-result">
            {s.trades === 0 ? (
              <p className="muted">Aucun trade sur la période ({s.bars} bougies). Élargissez la période ou vérifiez
                que l'historique est collecté.</p>
            ) : (
              <>
                <div className="stat-grid">
                  <Stat label="Trades" value={String(s.trades)} />
                  <Stat label="Gagnants" value={pct(s.win_rate)}
                        cls={(s.win_rate ?? 0) > s.breakeven_win_rate ? 'up' : 'down'}
                        sub={`seuil de rentabilité ${pct(s.breakeven_win_rate)}`} />
                  <Stat label="P&L" value={money(s.pnl)} cls={s.pnl >= 0 ? 'up' : 'down'} />
                  <Stat label="Drawdown max" value={`-${s.max_drawdown.toFixed(2)}`} />
                  <Stat label="Profit factor" value={s.profit_factor?.toFixed(2) ?? '—'} />
                  <Stat label="Séries" value={`${s.best_win_streak} ✓ / ${s.worst_loss_streak} ✗`} />
                </div>
                <Equity points={s.equity} />
                <p className="muted small">
                  {s.bars} bougies · {s.skipped_signals} signal(aux) ignoré(s) car un contrat était déjà ouvert ·
                  prix : {s.resolution}
                </p>
                {s.resolution !== 'ticks' && (
                  <p className="muted small warn">
                    Les ticks bruts ne couvrent pas cette période : prix approchés par les bougies d'une minute.
                    {(s.unresolved_signals ?? 0) > 0 && ` ${s.unresolved_signals} signal(aux) non résolu(s) (contrat plus court que la précision disponible) ont été exclus.`}
                    {' '}Pour un résultat exact sur des contrats courts, backtestez une période récente (≤ 3 jours) déjà collectée en ticks.
                  </p>
                )}
                <div className="form-actions">
                  <button className="primary" onClick={() => p.onShow(current)}>Afficher les trades sur le graphique</button>
                </div>
              </>
            )}
          </div>
        )}

        {history.length > 0 && (
          <fieldset>
            <legend>Backtests précédents</legend>
            <ul className="bt-history">
              {history.map((b) => (
                <li key={b.id} onClick={() => open(b.id)} className={b.id === current?.id ? 'on' : ''}>
                  <span>#{b.id} · {Math.round((b.date_to - b.date_from) / 86400)} j · {b.status}</span>
                  {b.stats && <span>{b.stats.trades} trades · {pct(b.stats.win_rate)} · <b className={b.stats.pnl >= 0 ? 'up' : 'down'}>{money(b.stats.pnl)}</b></span>}
                </li>
              ))}
            </ul>
          </fieldset>
        )}
      </div>
    </Modal>
  )
}

function Stat({ label, value, sub, cls }: { label: string; value: string; sub?: string; cls?: string }) {
  return (
    <div className="stat">
      <div className="muted small">{label}</div>
      <div className={`stat-v ${cls ?? ''}`}>{value}</div>
      {sub && <div className="muted small">{sub}</div>}
    </div>
  )
}

function Equity({ points }: { points: [number, number][] }) {
  if (points.length < 2) return null
  const W = 560, H = 120, pad = 4
  const xs = points.map((p) => p[0]), ys = [0, ...points.map((p) => p[1])]
  const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys)
  const X = (x: number) => pad + ((x - x0) / (x1 - x0 || 1)) * (W - 2 * pad)
  const Y = (y: number) => H - pad - ((y - y0) / (y1 - y0 || 1)) * (H - 2 * pad)
  const d = points.map((p, i) => `${i ? 'L' : 'M'}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join(' ')
  const last = points[points.length - 1][1]
  return (
    <svg className="equity" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none">
      <line x1={pad} x2={W - pad} y1={Y(0)} y2={Y(0)} className="zero" />
      <path d={d} className={last >= 0 ? 'pos' : 'neg'} />
    </svg>
  )
}
