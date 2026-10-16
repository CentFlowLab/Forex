import { Outlet } from "react-router-dom";
import { useTranslation } from "react-i18next";

export function Layout() {
  const { t } = useTranslation();

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-20 border-b border-white/10 bg-ink-950">
        <div className="mx-auto flex max-w-[1200px] items-center justify-between px-5 py-3">
          <div>
            <div className="text-sm font-semibold tracking-[0.16em] text-accent-gold">{t("app.name")}</div>
            <div className="text-[11px] text-slate-500">{t("robot.tagline")}</div>
          </div>
          <div className="text-[11px] text-slate-500">{t("env.liveOff")}</div>
        </div>
      </header>
      <main className="mx-auto max-w-[1200px] px-5 py-4">
        <Outlet />
      </main>
    </div>
  );
}
