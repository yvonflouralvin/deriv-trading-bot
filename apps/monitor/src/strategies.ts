// Stratégies affichées sur le graphique.
// Chaque stratégie reçoit les bougies et renvoie des courbes + des marqueurs
// (signaux). Pour en ajouter une : écrire un objet Strategy et l'ajouter à
// STRATEGIES.

import type { Candle } from './api'

export interface StrategyLine {
  name: string
  color: string
  data: { time: number; value: number }[]
}

export interface StrategySignal {
  time: number
  side: 'CALL' | 'PUT'
  price: number
}

export interface StrategyOutput {
  lines: StrategyLine[]
  signals: StrategySignal[]
}

export interface StrategyParam {
  key: string
  label: string
  default: number
  min: number
  max: number
}

export interface Strategy {
  id: string
  name: string
  description: string
  params: StrategyParam[]
  compute: (bars: Candle[], params: Record<string, number>) => StrategyOutput
}

function sma(values: number[], n: number): (number | null)[] {
  const out: (number | null)[] = []
  let sum = 0
  values.forEach((v, i) => {
    sum += v
    if (i >= n) sum -= values[i - n]
    out.push(i >= n - 1 ? sum / n : null)
  })
  return out
}

function ema(values: number[], n: number): (number | null)[] {
  const k = 2 / (n + 1)
  const out: (number | null)[] = []
  let prev: number | null = null
  values.forEach((v, i) => {
    if (i < n - 1) return out.push(null)
    if (prev === null) prev = values.slice(0, n).reduce((a, b) => a + b, 0) / n
    else prev = v * k + prev * (1 - k)
    out.push(prev)
  })
  return out
}

function crossSignals(bars: Candle[], fast: (number | null)[], slow: (number | null)[]): StrategySignal[] {
  const signals: StrategySignal[] = []
  for (let i = 1; i < bars.length; i++) {
    const f0 = fast[i - 1], s0 = slow[i - 1], f1 = fast[i], s1 = slow[i]
    if (f0 == null || s0 == null || f1 == null || s1 == null) continue
    const d0 = f0 - s0, d1 = f1 - s1
    if (d0 <= 0 && d1 > 0) signals.push({ time: bars[i].time, side: 'CALL', price: bars[i].close })
    else if (d0 >= 0 && d1 < 0) signals.push({ time: bars[i].time, side: 'PUT', price: bars[i].close })
  }
  return signals
}

function toLine(bars: Candle[], vals: (number | null)[], name: string, color: string): StrategyLine {
  return {
    name,
    color,
    data: bars.flatMap((b, i) => (vals[i] == null ? [] : [{ time: b.time, value: vals[i] as number }])),
  }
}

export const STRATEGIES: Strategy[] = [
  {
    id: 'sma-cross',
    name: 'Croisement SMA',
    description: 'CALL quand la moyenne courte passe au-dessus de la longue, PUT dans l\'autre sens (logique du bot).',
    params: [
      { key: 'fast', label: 'Rapide', default: 5, min: 2, max: 200 },
      { key: 'slow', label: 'Lente', default: 20, min: 3, max: 400 },
    ],
    compute: (bars, p) => {
      const closes = bars.map((b) => b.close)
      const fast = sma(closes, p.fast), slow = sma(closes, p.slow)
      return {
        lines: [toLine(bars, fast, `SMA ${p.fast}`, '#f7c948'), toLine(bars, slow, `SMA ${p.slow}`, '#2962ff')],
        signals: crossSignals(bars, fast, slow),
      }
    },
  },
  {
    id: 'ema-cross',
    name: 'Croisement EMA',
    description: 'Même logique avec des moyennes exponentielles (plus réactives).',
    params: [
      { key: 'fast', label: 'Rapide', default: 9, min: 2, max: 200 },
      { key: 'slow', label: 'Lente', default: 21, min: 3, max: 400 },
    ],
    compute: (bars, p) => {
      const closes = bars.map((b) => b.close)
      const fast = ema(closes, p.fast), slow = ema(closes, p.slow)
      return {
        lines: [toLine(bars, fast, `EMA ${p.fast}`, '#ff9800'), toLine(bars, slow, `EMA ${p.slow}`, '#ab47bc')],
        signals: crossSignals(bars, fast, slow),
      }
    },
  },
]

export function defaultParams(s: Strategy): Record<string, number> {
  return Object.fromEntries(s.params.map((p) => [p.key, p.default]))
}
