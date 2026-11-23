import type { DrawingKind } from 'lightweight-charts-drawing'
import type { Timeframe } from './api'
import type { ActiveStrategy } from './Chart'
import { STRATEGIES, defaultParams } from './strategies'

export const TOOLS: { kind: DrawingKind | null; icon: string; label: string }[] = [
  { kind: null, icon: '↖', label: 'Curseur' },
  { kind: 'trend-line', icon: '╱', label: 'Ligne de tendance' },
  { kind: 'ray', icon: '↗', label: 'Rayon' },
  { kind: 'horizontal-line', icon: '―', label: 'Ligne horizontale' },
  { kind: 'vertical-line', icon: '│', label: 'Ligne verticale' },
  { kind: 'parallel-channel', icon: '⫽', label: 'Canal parallèle' },
  { kind: 'rectangle', icon: '▭', label: 'Rectangle' },
  { kind: 'fib-retracement', icon: 'ƒ', label: 'Retracement de Fibonacci' },
  { kind: 'long-position', icon: '⬆', label: 'Position longue' },
  { kind: 'short-position', icon: '⬇', label: 'Position courte' },
  { kind: 'price-range', icon: '↕', label: 'Mesure de prix' },
  { kind: 'arrow-mark-up', icon: '▲', label: 'Flèche haut' },
  { kind: 'arrow-mark-down', icon: '▼', label: 'Flèche bas' },
]

interface Props {
  timeframes: Timeframe[]
  tf: number
  onTf: (tf: number) => void
  tool: DrawingKind | null
  onTool: (k: DrawingKind | null) => void
  magnet: boolean
  onMagnet: (on: boolean) => void
  onClearDrawings: () => void
  strategies: ActiveStrategy[]
  onStrategies: (s: ActiveStrategy[]) => void
  live: boolean
}

export function TopBar(p: Props) {
  const toggle = (id: string) => {
    const on = p.strategies.find((s) => s.id === id)
    if (on) p.onStrategies(p.strategies.filter((s) => s.id !== id))
    else p.onStrategies([...p.strategies, { id, params: defaultParams(STRATEGIES.find((s) => s.id === id)!) }])
  }
  const setParam = (id: string, key: string, value: number) =>
    p.onStrategies(p.strategies.map((s) => (s.id === id ? { ...s, params: { ...s.params, [key]: value } } : s)))

  return (
    <header className="topbar">
      <div className="group">
        {p.timeframes.map((t) => (
          <button key={t.tf} className={t.tf === p.tf ? 'on' : ''} onClick={() => p.onTf(t.tf)}>{t.label}</button>
        ))}
      </div>
      <div className="group strategies">
        {STRATEGIES.map((s) => {
          const active = p.strategies.find((a) => a.id === s.id)
          return (
            <span key={s.id} className="strategy" title={s.description}>
              <button className={active ? 'on' : ''} onClick={() => toggle(s.id)}>{s.name}</button>
              {active && s.params.map((param) => (
                <input
                  key={param.key}
                  type="number"
                  title={param.label}
                  min={param.min}
                  max={param.max}
                  value={active.params[param.key]}
                  onChange={(e) => {
                    const v = Number(e.target.value)
                    if (v >= param.min && v <= param.max) setParam(s.id, param.key, v)
                  }}
                />
              ))}
            </span>
          )
        })}
      </div>
      <div className="group right">
        <span className={`dot ${p.live ? 'ok' : 'ko'}`} title="Flux temps réel" />
        <span className="muted small">{p.live ? 'temps réel' : 'hors ligne'}</span>
      </div>
    </header>
  )
}

export function DrawBar(p: Props) {
  return (
    <nav className="drawbar">
      {TOOLS.map((t) => (
        <button
          key={t.label}
          title={t.label}
          className={p.tool === t.kind ? 'on' : ''}
          onClick={() => p.onTool(t.kind)}
        >
          {t.icon}
        </button>
      ))}
      <hr />
      <button title="Aimant (accroche aux prix OHLC)" className={p.magnet ? 'on' : ''} onClick={() => p.onMagnet(!p.magnet)}>🧲</button>
      <button title="Effacer tous les dessins de cet actif" onClick={p.onClearDrawings}>🗑</button>
    </nav>
  )
}
