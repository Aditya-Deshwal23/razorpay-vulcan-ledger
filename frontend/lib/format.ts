import type { ReconState } from "@/types/api";

export const stateMeta: Record<
  ReconState,
  { label: string; tone: "matched" | "ai" | "review" | "approved" | "rejected" }
> = {
  DETERMINISTIC_MATCH: { label: "Matched by rules", tone: "matched" },
  AI_RESOLVED: { label: "AI-resolved", tone: "ai" },
  PENDING_HITL_REVIEW: { label: "Needs review", tone: "review" },
  HITL_APPROVED: { label: "Approved", tone: "approved" },
  HITL_REJECTED: { label: "Rejected", tone: "rejected" },
};

export function formatMoney(value: string | null | undefined) {
  if (value === null || value === undefined) return "—";
  const [whole, fraction = "00"] = value.replace("-", "").split(".");
  const grouped = Number(whole).toLocaleString("en-IN");
  return `${value.startsWith("-") ? "−" : ""}${grouped}.${fraction}`;
}

export function formatDate(value: string | null | undefined, includeTime = false) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("en-IN", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    ...(includeTime ? { hour: "2-digit", minute: "2-digit", hour12: false } : {}),
  }).format(new Date(value));
}

export function conciseId(value: string, start = 11, end = 7) {
  return value.length > start + end + 1 ? `${value.slice(0, start)}…${value.slice(-end)}` : value;
}

export function absoluteMoney(value: string) {
  return value.startsWith("-") ? value.slice(1) : value;
}

export function sumAbsoluteMoney(values: Array<string | null | undefined>) {
  const total = values.reduce((sum, value) => {
    if (!value) return sum;
    const normalized = absoluteMoney(value);
    const [whole, fraction = "00"] = normalized.split(".");
    return sum + BigInt(whole) * 100n + BigInt(fraction.padEnd(2, "0").slice(0, 2));
  }, 0n);
  return `${total / 100n}.${(total % 100n).toString().padStart(2, "0")}`;
}

function moneyToPaise(value: string) {
  const negative = value.startsWith("-");
  const [whole, fraction = "00"] = value.replace("-", "").split(".");
  const paise = BigInt(whole) * 100n + BigInt(fraction.padEnd(2, "0").slice(0, 2));
  return negative ? -paise : paise;
}

/** Adds API money strings without ever converting a financial value to a float. */
export function sumMoney(values: Array<string | null | undefined>) {
  const total = values.reduce((sum, value) => sum + (value ? moneyToPaise(value) : 0n), 0n);
  const sign = total < 0n ? "-" : "";
  const absolute = total < 0n ? -total : total;
  return `${sign}${absolute / 100n}.${(absolute % 100n).toString().padStart(2, "0")}`;
}

export function compareAbsoluteMoney(left: string, right: string) {
  const leftPaise = moneyToPaise(absoluteMoney(left));
  const rightPaise = moneyToPaise(absoluteMoney(right));
  return leftPaise === rightPaise ? 0 : leftPaise > rightPaise ? 1 : -1;
}

type ExceptionFacts = {
  settlement_id: string;
  raw_narration: string;
  deterministic_variance: string;
  settlement_utr: string | null;
  bank_utr: string | null;
};

export function exceptionTitle(item: ExceptionFacts) {
  const evidence = `${item.settlement_id} ${item.raw_narration}`.toLowerCase();
  if (evidence.includes("chargeback")) return "Chargeback needs attribution";
  if (evidence.includes("delayed")) return "Delayed settlement confirmation";
  if (evidence.includes("missing_utr") || evidence.includes("missing utr")) return "Transfer reference is missing";
  if (absoluteMoney(item.deterministic_variance) !== "0.00") return "Bank credit does not reconcile";
  if (!item.settlement_utr || !item.bank_utr) return "Settlement identity is unverified";
  return "Evidence requires controller judgment";
}

export function exceptionExplanation(item: ExceptionFacts) {
  if (absoluteMoney(item.deterministic_variance) !== "0.00") {
    return `Ledger variance is ₹${formatMoney(absoluteMoney(item.deterministic_variance))}.`;
  }
  if (!item.settlement_utr || !item.bank_utr) {
    return "Amounts agree, but no verified transfer reference proves the pairing.";
  }
  return "The system did not determine a safe automatic reconciliation.";
}
