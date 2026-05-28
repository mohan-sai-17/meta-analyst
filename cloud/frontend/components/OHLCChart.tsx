"use client";
import { useEffect, useRef } from "react";
import {
  createChart,
  ColorType,
  LineStyle,
  CandlestickSeries,
} from "lightweight-charts";

interface OHLCRow { Date: string; Open: number; High: number; Low: number; Close: number }

export default function OHLCChart({
  data,
  target3m,
  target6m,
}: {
  data: OHLCRow[];
  target3m?: number;
  target6m?: number;
}) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!ref.current || data.length === 0) return;

    const chart = createChart(ref.current, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: "#18181b" },
        textColor: "#a1a1aa",
      },
      grid: {
        vertLines: { color: "#27272a" },
        horzLines: { color: "#27272a" },
      },
      crosshair: { mode: 1 },
      rightPriceScale: { borderColor: "#3f3f46" },
      timeScale: { borderColor: "#3f3f46", timeVisible: false },
      height: 320,
    });

    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor:        "#4ade80",
      downColor:      "#f87171",
      borderUpColor:  "#4ade80",
      borderDownColor:"#f87171",
      wickUpColor:    "#4ade80",
      wickDownColor:  "#f87171",
    });

    // Deduplicate by date (API may return duplicate rows) and sort ascending
    const seen = new Map<string, typeof data[0]>();
    for (const d of data) seen.set(d.Date.slice(0, 10), d);
    const rows = [...seen.values()].sort((a, b) => a.Date.localeCompare(b.Date));

    candleSeries.setData(
      rows.map((d) => ({
        time:  d.Date.slice(0, 10) as import("lightweight-charts").Time,
        open:  d.Open,
        high:  d.High,
        low:   d.Low,
        close: d.Close,
      }))
    );

    // Target price lines as horizontal price lines (visible across full chart)
    if (target3m) {
      candleSeries.createPriceLine({
        price: target3m, color: "#86efac", lineWidth: 1,
        lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: "3M",
      });
    }
    if (target6m) {
      candleSeries.createPriceLine({
        price: target6m, color: "#4ade80", lineWidth: 1,
        lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: "6M",
      });
    }

    chart.timeScale().fitContent();

    return () => { chart.remove(); };
  }, [data, target3m, target6m]);

  return <div ref={ref} style={{ height: "320px" }} />;
}
