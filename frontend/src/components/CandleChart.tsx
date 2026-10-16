import { useEffect, useRef } from "react";
import { createChart, IChartApi, ISeriesApi } from "lightweight-charts";
import type { Candle, ChartMarker } from "../api/client";

type Props = { candles: Candle[]; markers?: ChartMarker[]; height?: number };

export function CandleChart({ candles, markers = [], height = 360 }: Props) {
  const ref = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);

  useEffect(() => {
    if (!ref.current) return;
    const chart = createChart(ref.current, {
      height,
      layout: { background: { color: "transparent" }, textColor: "#94a3b8" },
      grid: { vertLines: { color: "#1a2438" }, horzLines: { color: "#1a2438" } },
      rightPriceScale: { borderColor: "#243049" },
      timeScale: { borderColor: "#243049", timeVisible: true },
      crosshair: { mode: 0 },
    });
    const series = chart.addCandlestickSeries({
      upColor: "#3dd68c",
      downColor: "#ff5d6c",
      borderUpColor: "#3dd68c",
      borderDownColor: "#ff5d6c",
      wickUpColor: "#3dd68c",
      wickDownColor: "#ff5d6c",
    });
    chartRef.current = chart;
    seriesRef.current = series;
    const resize = () => {
      if (ref.current) chart.applyOptions({ width: ref.current.clientWidth });
    };
    resize();
    window.addEventListener("resize", resize);
    return () => {
      window.removeEventListener("resize", resize);
      chart.remove();
    };
  }, [height]);

  useEffect(() => {
    if (!seriesRef.current) return;
    seriesRef.current.setData(candles.map((c) => ({ ...c, time: c.time as never })));
    seriesRef.current.setMarkers(
      markers.map((m) => ({
        time: m.time as never,
        position: m.position,
        color: m.color,
        shape: m.shape as never,
        text: m.text,
      })),
    );
    chartRef.current?.timeScale().fitContent();
  }, [candles, markers]);

  return <div ref={ref} className="w-full" />;
}
