/** Formatação localizada pt-PT. Os valores brutos nos cálculos permanecem intactos. */

const LOCALE = "pt-PT";

export function formatNumber(value: number, options?: Intl.NumberFormatOptions): string {
  return new Intl.NumberFormat(LOCALE, {
    maximumFractionDigits: 2,
    ...options,
  }).format(value);
}

export function formatPrice(value: number, digits = 5): string {
  return new Intl.NumberFormat(LOCALE, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(value);
}

export function formatPercent(value: number, digits = 1): string {
  return `${formatNumber(value, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })} %`;
}

export function formatMoney(value: number, currency = "USD"): string {
  try {
    return new Intl.NumberFormat(LOCALE, {
      style: "currency",
      currency,
      currencyDisplay: "code",
    }).format(value);
  } catch {
    return `${currency} ${formatNumber(value, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  }
}

export function formatDateTime(value: string | Date, timeZone: "Europe/Lisbon" | "UTC" = "Europe/Lisbon"): string {
  const date = typeof value === "string" ? new Date(value) : value;
  return new Intl.DateTimeFormat(LOCALE, {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
    timeZone,
  }).format(date);
}

export function formatDate(value: string | Date, timeZone: "Europe/Lisbon" | "UTC" = "UTC"): string {
  const date = typeof value === "string" ? new Date(value) : value;
  return new Intl.DateTimeFormat(LOCALE, {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    timeZone,
  }).format(date);
}

export function recommendationLabel(code: string, t: (key: string) => string): string {
  if (code === "BUY" || code === "SELL" || code === "HOLD") {
    return t(`rec.${code}`);
  }
  return code;
}
