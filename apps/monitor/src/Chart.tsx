import { useEffect, useRef, useState } from 'react'
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  LineSeries,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from 'lightweight-charts'
import { DrawingManager, type DrawingKind } from 'lightweight-charts-drawing'
import { api, LiveFeed, type BacktestTrade, type Candle, type LiveMessage, type Trade } from './api'
import { STRATEGIES } from './strategies'

export interface ActiveStrategy {
  id: string
  params: Record<string, number>
}

interface Props {
  symbol: string
  tf: number
  tfLabel: string
  tool: DrawingKind | null
  magnet: boolean
  strategies: ActiveStrategy[]
  clearToken: number
  overlay: { label: string; symbol: string; tf: number; trades: BacktestTrade[] } | null
  showTrades: boolean
  onToolDone: () => void
  onLive: (connected: boolean) => void
  onError: (e: unknown) => void
}

const t = (s: number) => s as UTCTimestamp

export default function Chart(p: Props) {
  const box = useRef<HTMLDivElement>(null)
  const chart = useRef<IChartApi | null>(null)
  const series = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const markers = useRef<ISeriesMarkersPluginApi<Time> | null>(null)
  const lines = useRef<ISeriesApi<'Line'>[]>([])
  const drawings = useRef<DrawingManager | null>(null)
  const feed = useRef<LiveFeed | null>(null)
  const bars = useRef<Candle[]>([])
  const trades = useRef<Map<number, Trade>>(new Map())
  const view = useRef({ symbol: p.symbol, tf: p.tf, loading: false, exhausted: false, importing: false })
  const latest = useRef(p)
  latest.current = p
  const [legend, setLegend] = useState<Candle | null>(null)
  const [count, setCount] = useState(0)

  // ---- création du graphique (une seule fois) ------------------------------
  useEffect(() => {
    const c = createChart(box.current!, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: '#131722' }, textColor: '#d1d4dc', fontSize: 12 },
      grid: { vertLines: { color: '#1e222d' }, horzLines: { color: '#1e222d' } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: '#2a2e39' },
      timeScale: { borderColor: '#2a2e39', timeVisible: true, secondsVisible: false, rightOffset: 8 },
    })
    const s = c.addSeries(CandlestickSeries, {
      upColor: '#26a69a', downColor: '#ef5350', wickUpColor: '#26a69a', wickDownColor: '#ef5350',
      borderVisible: false,
    })
    chart.current = c
    series.current = s
    markers.current = createSeriesMarkers(s, [])

    const dm = new DrawingManager(c, s, { magnet: 'weak' })
    drawings.current = dm
    let saveTimer: number | undefined
    dm.on('change', () => {
      if (view.current.importing) return
      window.clearTimeout(saveTimer)
      const sym = view.current.symbol
      saveTimer = window.setTimeout(() => {
        api.saveDrawings(sym, dm.exportJSON()).catch(latest.current.onError)
      }, 600)
    })
    dm.on('tool', (kind) => { if (kind === null) latest.current.onToolDone() })

    c.subscribeCrosshairMove((param) => {
      const d = param.seriesData.get(s) as Candle | undefined
      setLegend(d ?? bars.current[bars.current.length - 1] ?? null)
    })

    // Charger plus d'historique quand on fait défiler vers la gauche
    c.timeScale().subscribeVisibleLogicalRangeChange((range) => {
      const v = view.current
      if (!range || range.from > 30 || v.loading || v.exhausted || !bars.current.length) return
      v.loading = true
      const { symbol, tf } = v
      api.candles(symbol, tf, bars.current[0].time)
        .then((older) => {
          if (symbol !== view.current.symbol || tf !== view.current.tf) return
          if (!older.length) { v.exhausted = true; return }
          bars.current = [...older, ...bars.current]
          s.setData(bars.current.map((b) => ({ ...b, time: t(b.time) })))
          redrawStrategies()
          setCount(bars.current.length)
        })
        .catch(latest.current.onError)
        .finally(() => { v.loading = false })
    })

    feed.current = new LiveFeed(onLive, (ok) => latest.current.onLive(ok))

    return () => {
      feed.current?.close()
      dm.destroy()
      c.remove()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // ---- temps réel ----------------------------------------------------------
  function onLive(m: LiveMessage) {
    const v = view.current
    if (m.type === 'trade' && m.symbol === v.symbol) {
      trades.current.set(m.trade.id, m.trade)
      redrawStrategies()
      return
    }
    if (m.type !== 'candle' || m.symbol !== v.symbol || m.tf !== v.tf || !series.current) return
    const b = bars.current
    const last = b[b.length - 1]
    if (last && m.candle.time < last.time) return
    if (last && m.candle.time === last.time) b[b.length - 1] = m.candle
    else b.push(m.candle)
    series.current.update({ ...m.candle, time: t(m.candle.time) })
    redrawStrategies()
    setLegend((cur) => (cur && cur.time !== m.candle.time ? cur : m.candle))
  }

  // ---- changement d'actif ou d'unité de temps --------------------------------
  useEffect(() => {
    const v = view.current
    v.symbol = p.symbol
    v.tf = p.tf
    v.exhausted = false
    bars.current = []
    trades.current = new Map()
    series.current?.setData([])
    feed.current?.subscribe(p.symbol, p.tf)
    drawings.current?.setInterval(p.tfLabel)
    let cancelled = false

    Promise.all([api.candles(p.symbol, p.tf), api.drawings(p.symbol)])
      .then(([candles, saved]) => {
        if (cancelled || !series.current) return
        bars.current = candles
        series.current.setData(candles.map((b) => ({ ...b, time: t(b.time) })))
        chart.current?.timeScale().scrollToRealTime()
        setLegend(candles[candles.length - 1] ?? null)
        setCount(candles.length)
        redrawStrategies()
        if (candles.length) {
          api.trades({ symbol: p.symbol, since: candles[0].time, limit: 2000 }).then((ts) => {
            if (cancelled) return
            trades.current = new Map(ts.map((t) => [t.id, t]))
            redrawStrategies()
          }).catch(p.onError)
        }
        const dm = drawings.current!
        v.importing = true
        try {
          dm.clear()
          dm.importJSON(JSON.stringify(saved ?? []))
        } finally {
          v.importing = false
        }
      })
      .catch((e) => { if (!cancelled) p.onError(e) })
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [p.symbol, p.tf])

  // ---- outils de dessin --------------------------------------------------------
  useEffect(() => { drawings.current?.setTool(p.tool) }, [p.tool])
  useEffect(() => { drawings.current?.setMagnet(p.magnet ? 'weak' : 'off') }, [p.magnet])
  useEffect(() => { if (p.clearToken) drawings.current?.clear() }, [p.clearToken])

  // ---- stratégies --------------------------------------------------------------
  useEffect(() => { redrawStrategies() }, [p.strategies, p.overlay, p.showTrades]) // eslint-disable-line react-hooks/exhaustive-deps

  function redrawStrategies() {
    const c = chart.current
    if (!c || !markers.current) return
    const out = latest.current.strategies.flatMap((a) => {
      const s = STRATEGIES.find((x) => x.id === a.id)
      return s ? [s.compute(bars.current, a.params)] : []
    })
    const wanted = out.flatMap((o) => o.lines)
    while (lines.current.length > wanted.length) c.removeSeries(lines.current.pop()!)
    while (lines.current.length < wanted.length) {
      lines.current.push(c.addSeries(LineSeries, {
        lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
      }))
    }
    wanted.forEach((l, i) => {
      lines.current[i].applyOptions({ color: l.color, title: l.name })
      lines.current[i].setData(l.data.map((d) => ({ time: t(d.time), value: d.value })))
    })
    const tf = view.current.tf
    const first = bars.current[0]?.time ?? 0
    const snap = (x: number) => x - (x % tf)      // un marqueur doit tomber sur une bougie
    const m: SeriesMarker<Time>[] = out
      .flatMap((o) => o.signals)
      .map((s) => s.side === 'CALL'
        ? { time: t(s.time), position: 'belowBar' as const, color: '#26a69a88', shape: 'arrowUp' as const, text: '' }
        : { time: t(s.time), position: 'aboveBar' as const, color: '#ef535088', shape: 'arrowDown' as const, text: '' })

    const L = latest.current
    if (L.showTrades) {
      const colors = { open: '#2962ff', won: '#26a69a', lost: '#ef5350', error: '#787b86' }
      for (const tr of trades.current.values()) {
        if (tr.entry_time < first) continue
        const label = `${tr.mode === 'demo' ? 'D' : 'P'}${tr.profit != null ? ` ${tr.profit > 0 ? '+' : ''}${tr.profit.toFixed(2)}` : tr.status === 'error' ? ' ⚠' : ''}`
        m.push({
          time: t(snap(tr.entry_time)),
          position: tr.side === 'CALL' ? 'belowBar' : 'aboveBar',
          shape: tr.side === 'CALL' ? 'arrowUp' : 'arrowDown',
          color: colors[tr.status],
          text: label,
        })
      }
    }
    if (L.overlay && L.overlay.symbol === view.current.symbol && L.overlay.tf === tf) {
      for (const bt of L.overlay.trades) {
        if (bt.time < first) continue
        m.push({
          time: t(snap(bt.time)),
          position: bt.side === 'CALL' ? 'belowBar' : 'aboveBar',
          shape: 'circle',
          color: bt.won ? '#26a69a' : '#ef5350',
          text: bt.won ? '✓' : '✗',
        })
      }
    }
    m.sort((a, b) => (a.time as number) - (b.time as number))
    markers.current.setMarkers(m)
  }

  const up = legend ? legend.close >= legend.open : true
  const fmt = (n: number) => n.toFixed(n >= 100 ? 2 : 5)
  return (
    <div className="chart-wrap">
      <div className="legend">
        <strong>{p.symbol}</strong> · {p.tfLabel}
        {legend && (
          <span className={up ? 'up' : 'down'}>
            {' '}O {fmt(legend.open)} H {fmt(legend.high)} L {fmt(legend.low)} C {fmt(legend.close)}
          </span>
        )}
        <span className="muted"> · {count} bougies</span>
      </div>
      <div ref={box} className="chart" />
    </div>
  )
}
