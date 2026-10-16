import { useEffect, useRef } from "react";
import { createChart, IChartApi } from "lightweight-charts";

type Point = { time: string; equity: number };

export function EquityChart({ points }: { points: Point[] }) {
  const ref = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);

  useEffect(() => {
    if (!ref.current) return;
    const chart = createChart(ref.current, {
      height: 180,
      layout: { background: { color: "transparent" }, textColor: "#94a3b8" },
      grid: { vertLines: { color: "#1a2438" }, horzLines: { color: "#1a2438" } },
      rightPriceScale: { borderColor: "#243049" },
      timeScale: { borderColor: "#243049", visible: false },
    });
    const series = chart.addAreaSeries({
      lineColor: "#e8c872",
      topColor: "rgba(232, 200, 114, 0.28)",
      bottomColor: "rgba(232, 200, 114, 0.02)",
    });
    const data = points.map((p, i) => ({
      time: (Math.floor(new Date(p.time).getTime() / 1000) + i) as never,
      value: p.equity,
    }));
    series.setData(data);
    chart.timeScale().fitContent();
    chartRef.current = chart;
    const resize = () => {
      if (ref.current) chart.applyOptions({ width: ref.current.clientWidth });
    };
    resize();
    window.addEventListener("resize", resize);
    return () => {
      window.removeEventListener("resize", resize);
      chart.remove();
    };
  }, [points]);

  return <div ref={ref} className="w-full" />;
}
