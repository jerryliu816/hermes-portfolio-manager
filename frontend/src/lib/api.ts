/**
 * The typed API client.
 *
 * Money is `string` in every one of these types, deliberately and throughout.
 * If a field here were ever typed `number`, TypeScript would happily let a
 * component do float arithmetic on a dollar amount, which is the exact failure
 * the server-side string convention exists to prevent.
 */

const BASE = "/api/v1";

/**
 * `fetch`, except that a lapsed sign-in goes back to the sign-in page.
 *
 * The server marks its refusal with `X-Auth-Required` so an ordinary 401 from
 * some other cause is not mistaken for one. The current page is passed along so
 * signing in returns there.
 */
export async function apiFetch(input: string, init?: RequestInit): Promise<Response> {
  const response = await fetch(input, init);
  if (response.status === 401 && response.headers.get("X-Auth-Required")) {
    const here = window.location.pathname + window.location.search;
    window.location.assign(`/login?next=${encodeURIComponent(here)}`);
    // Never settles: the page is leaving, and an error would flash first.
    return new Promise<Response>(() => {});
  }
  return response;
}

async function get<T>(path: string): Promise<T> {
  const response = await apiFetch(`${BASE}${path}`);
  if (!response.ok) {
    const detail = await response.json().catch(() => ({ detail: response.statusText }));
    throw new ApiError(response.status, detail?.detail ?? response.statusText);
  }
  return response.json() as Promise<T>;
}

async function send<T>(path: string, method: string, body?: unknown): Promise<T> {
  const response = await apiFetch(`${BASE}${path}`, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({ detail: response.statusText }));
    throw new ApiError(response.status, detail?.detail ?? response.statusText);
  }
  return response.json() as Promise<T>;
}

export class ApiError extends Error {
  readonly status: number;
  /** The server's `detail` as sent, so structured refusals can be read. */
  readonly detail: unknown;

  constructor(status: number, message: unknown) {
    super(
      typeof message === "string"
        ? message
        : typeof (message as { message?: unknown })?.message === "string"
          ? (message as { message: string }).message
          : JSON.stringify(message),
    );
    this.status = status;
    this.detail = message;
  }
  get isMissing() {
    return this.status === 404;
  }
}

// --- types ----------------------------------------------------------------

export interface DataQuality {
  stale_value: string;
  stale_fraction: string;
  unclassified_value: string;
  unclassified_fraction: string;
  missing_basis_value: string;
  missing_basis_fraction: string;
  aggregated_value: string;
  aggregated_fraction: string;
  stale_accounts: string[];
  unclassified_accounts: string[];
  is_fully_trusted: boolean;
  caveats: string[];
}

export interface PortfolioSummary {
  snapshot_id: string;
  as_of_date: string;
  completeness: "complete" | "partial" | "unknown";
  total_value: string;
  holdings_value: string;
  cash_value: string;
  account_count: number;
  position_count: number;
  security_count: number;
  closed_position_count: number;
  skipped_account_count: number;
  quality: DataQuality;
  warnings: string[];
}

export interface Slice {
  key: string;
  label: string;
  value: string;
  fraction: string;
  position_count: number;
}

export interface CashBucket {
  key: "taxable" | "tax_sheltered" | "unknown";
  label: string;
  settlement_value: string;
  holdings_value: string;
  total_value: string;
  position_count: number;
  account_count: number;
  fraction: string;
  treatments: string[];
}

export interface CashEquivalents {
  buckets: CashBucket[];
  total_value: string;
  settlement_value: string;
  holdings_value: string;
  fraction_of_portfolio: string;
  taxable_value: string;
  unconfirmed_value: string;
}

export interface Breakdown {
  slices: Slice[];
  total: string;
  excluded_value: string;
  excluded_fraction: string;
  excluded_label: string;
}

export interface Account {
  id: string;
  account_key_masked: string;
  display_name: string;
  institution: string;
  account_type: string;
  tax_treatment: string;
  is_aggregated: boolean;
  holdings_value: string;
  cash_balance: string;
  total_value: string;
  position_count: number;
  is_stale: boolean;
  staleness_reason: string | null;
  supplied_by: string | null;
  as_of_date: string | null;
  as_of_confidence: string;
  needs_classification: boolean;
  preferred_source: string | null;
}

export interface Holding {
  account_id: string;
  account_name: string;
  institution: string;
  tax_treatment: string;
  security_id: string;
  symbol: string | null;
  security_name: string;
  security_type: string;
  asset_class: string;
  quantity: string | null;
  price: string | null;
  market_value: string | null;
  cost_basis: string | null;
  cost_basis_source: string;
  unrealized_gain_loss: string | null;
  is_closed: boolean;
  value_source: string;
  account_is_stale: boolean;
}

export interface AccountHolding {
  account_id: string;
  account_name: string;
  institution: string;
  tax_treatment: string;
  quantity: string | null;
  price: string | null;
  value: string;
  is_stale: boolean;
}

export interface SecurityExposure {
  security_id: string;
  symbol: string | null;
  name: string;
  asset_class: string;
  total_value: string;
  fraction_of_portfolio: string;
  account_count: number;
  has_divergent_prices: boolean;
  holdings: AccountHolding[];
}

export interface Concentration {
  top_1_fraction: string;
  top_5_fraction: string;
  top_10_fraction: string;
  hhi: string;
  threshold: string;
  largest: SecurityExposure[];
  over_threshold: SecurityExposure[];
}

export interface EmbeddedGains {
  market_value: string;
  cost_basis: string;
  unrealized_gain: string;
  coverage: {
    covered_value: string;
    uncovered_value: string;
    covered_fraction: string;
    caveat: string | null;
  };
}

export interface Snapshot {
  id: string;
  as_of_date: string;
  completeness: string;
  status: string;
  total_value: string;
  account_count: number;
  position_count: number;
  superseded_by: string | null;
  /** Left off the value-over-time chart. Display only: the snapshot is kept. */
  hidden_from_history: boolean;
  skipped_accounts: Record<string, unknown>[];
  unmatched_enrichment: Record<string, unknown>[];
}

export interface SnapshotRemoval {
  snapshot: Snapshot;
  account_count: number;
  position_count: number;
  /** Documents only this snapshot used: their records go, so they can be imported again. */
  documents: string[];
  /** Documents another snapshot also uses: kept, and still not re-importable. */
  shared_documents: string[];
  recommendation_count: number;
  resurrected: string[];
  /** Non-empty means the delete is refused unless forced. */
  objections: string[];
  needs_force: boolean;
}

export interface ClassDrift {
  asset_class: string;
  label: string;
  target: string;
  actual: string;
  band: string;
  value: string;
  drift: string;
  is_in_band: boolean;
  direction: "in_band" | "overweight" | "underweight";
  rebalance_value: string;
}

export interface ConstraintResult {
  id: string;
  kind: string;
  description: string;
  limit: string;
  actual: string;
  satisfied: boolean;
  breach: string;
  detail: string;
}

export interface PolicyAssessment {
  policy_name: string;
  policy_version: number;
  total_value: string;
  classified_value: string;
  unclassified_value: string;
  unclassified_fraction: string;
  is_compliant: boolean;
  is_provisional: boolean;
  drift: ClassDrift[];
  out_of_band: ClassDrift[];
  constraints: ConstraintResult[];
  breaches: ConstraintResult[];
  placement: {
    asset_class: string;
    preferred: string;
    value_in_preferred: string;
    value_elsewhere: string;
    fraction_in_preferred: string;
  }[];
  caveats: string[];
  data_caveats: string[];
  policy_notes: string[];
}

export interface Decision {
  id: string;
  decision: "accepted" | "rejected" | "deferred";
  note: string | null;
  decided_by: string;
  decided_at: string;
}

export interface Recommendation {
  id: string;
  title: string;
  rationale: string;
  source: string;
  status: "pending" | "accepted" | "rejected" | "deferred";
  is_open: boolean;
  proposal: Record<string, unknown>;
  estimated_tax_impact: string | null;
  snapshot_id: string | null;
  created_at: string;
  kind: "note" | "trades" | "target_change";
  /** Proposed target weights, as decimal strings. */
  targets: Record<string, string> | null;
  /** The engine's report, computed by the server when it was saved. */
  simulation: Simulation | GapsReport | null;
  context_ids: string[];
  advisor_session_id: string | null;
  decisions: Decision[];
}

export interface JournalStats {
  total_recommendations: number;
  pending: number;
  accepted: number;
  rejected: number;
  deferred: number;
  decided: number;
  acceptance_rate: string | null;
}

export interface Policy {
  name: string;
  version: number;
  is_active: boolean;
  document: Record<string, unknown>;
  raw_yaml: string | null;
}

// --- advisor ----------------------------------------------------------------

/** What every engine report carries: the basis it was computed on, and how far to trust it. */
export interface ProposalBasis {
  snapshot_id: string;
  as_of_date: string;
  policy_version: number | null;
  targets: "policy" | "proposed";
  proposed_targets: Record<string, string> | null;
  basis_notes: string[];
  completeness: string;
  stale_fraction: string | null;
  unclassified_tax_fraction: string | null;
  missing_cost_basis_fraction: string | null;
  data_caveats: string[];
  caveats: string[];
}

export interface TargetGap {
  asset_class: string;
  target: string | null;
  actual: string | null;
  band: string | null;
  current_value: string | null;
  target_value: string | null;
  gap: string | null;
  direction: string;
  untargeted: boolean;
}

export interface Gaps {
  targets_proposed: boolean;
  classified_value: string | null;
  unclassified_value: string | null;
  uninvested_cash: string | null;
  gaps: TargetGap[];
  caveats: string[];
}

export type GapsReport = ProposalBasis & Gaps;

export interface TaxEstimate {
  status: "estimated" | "basis_missing" | "no_realization_event" | "unknown";
  sale_value: string | null;
  estimated_gain: string | null;
  basis_coverage: string | null;
  reason: string | null;
}

export interface SimulatedTrade {
  index: number;
  account_id: string;
  account: string | null;
  action: "sell" | "buy";
  security: string;
  requested_amount: string | null;
  applied_amount: string | null;
  asset_class: string | null;
  tax: TaxEstimate | null;
}

export interface WireBreakdown {
  total: string | null;
  excluded_value: string | null;
  excluded_label: string | null;
  slices: { key: string; label: string; value: string | null; fraction: string | null }[];
}

export interface Simulation extends ProposalBasis {
  is_feasible: boolean;
  trades: SimulatedTrade[];
  before: { by_asset_class: WireBreakdown };
  after: { by_asset_class: WireBreakdown; gaps: Gaps };
  cash: { account_id: string; account: string | null; starting_cash: string | null; net_change: string | null; ending_cash: string | null }[];
  tax: {
    estimated_gain: string | null;
    taxable_sale_value: string | null;
    taxable_sale_value_with_basis: string | null;
    sheltered_sale_value: string | null;
    unknown_treatment_sale_value: string | null;
    caveat: string | null;
  };
  violations: { kind: string; detail: string; trade_index: number | null; account_id: string | null }[];
}

export interface AdvisorProposal {
  has_proposal: boolean;
  trades: Record<string, unknown>[] | null;
  targets: Record<string, string> | null;
  simulation: Simulation | null;
  error: string | null;
}

export interface Philosophy {
  version: number;
  is_active: boolean;
  body_markdown: string;
  created_at: string;
}

export type ContextKind = "view" | "preference" | "url" | "document";

export interface AdvisorContextItem {
  id: string;
  kind: ContextKind;
  title: string;
  body: string;
  /** Whether `body` holds the item's text; `no_text` is an unreadable PDF. */
  extraction: "not_applicable" | "extracted" | "no_text" | "not_captured";
  source_url: string | null;
  original_filename: string | null;
  expires_on: string | null;
  is_expired: boolean;
  is_active: boolean;
  created_by: "user" | "hermes";
  /** False for an advisor's paraphrase the user has not confirmed. */
  confirmed: boolean;
  created_at: string;
}

export interface AdvisorContextList {
  current: AdvisorContextItem[];
  expired: AdvisorContextItem[];
}

export interface AdvisorStatus {
  enabled: boolean;
  reachable: boolean;
  url: string;
  detail: string | null;
}

export interface AdvisorSession {
  id: string;
  title: string;
  snapshot_id: string | null;
  forked_from_id: string | null;
  has_proposal: boolean;
  created_at: string;
  updated_at: string;
}

/** A message as Hermes stores it. Content may be a string or a list of parts. */
export interface HermesMessage {
  role: string;
  content?: unknown;
  [key: string]: unknown;
}

export interface AdvisorEvent {
  event: string;
  data: Record<string, unknown>;
}

/**
 * Send a message and hand each server-sent event to `onEvent` as it arrives.
 * Resolves when the stream ends. A refusal before the stream starts (Hermes
 * down, disabled) rejects with the server's explanation.
 */
async function streamAdvisorMessage(
  sessionId: string,
  message: string,
  onEvent: (event: AdvisorEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await apiFetch(`${BASE}/advisor/sessions/${sessionId}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({ message }),
    signal,
  });
  if (!response.ok || !response.body) {
    const detail = await response.json().catch(() => ({ detail: response.statusText }));
    throw new ApiError(response.status, detail?.detail ?? response.statusText);
  }
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  let name = "message";
  let data: string[] = [];
  const flush = () => {
    if (!data.length) return;
    const text = data.join("\n");
    let parsed: Record<string, unknown>;
    try {
      const value: unknown = JSON.parse(text);
      parsed = value && typeof value === "object" ? (value as Record<string, unknown>) : { value };
    } catch {
      parsed = { raw: text };
    }
    onEvent({ event: name, data: parsed });
    name = "message";
    data = [];
  };
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value;
    let newline = buffer.indexOf("\n");
    while (newline >= 0) {
      const line = buffer.slice(0, newline).replace(/\r$/, "");
      buffer = buffer.slice(newline + 1);
      if (line === "") flush();
      else if (!line.startsWith(":")) {
        const colon = line.indexOf(":");
        const field = colon < 0 ? line : line.slice(0, colon);
        const rest = colon < 0 ? "" : line.slice(colon + 1).replace(/^ /, "");
        if (field === "event") name = rest;
        else if (field === "data") data.push(rest);
      }
      newline = buffer.indexOf("\n");
    }
  }
  flush();
}

async function upload<T>(path: string, form: FormData): Promise<T> {
  const response = await apiFetch(`${BASE}${path}`, { method: "POST", body: form });
  const body = await response.json().catch(() => ({ detail: response.statusText }));
  if (!response.ok) throw new ApiError(response.status, body?.detail ?? response.statusText);
  return body as T;
}

async function remove(path: string): Promise<void> {
  const response = await apiFetch(`${BASE}${path}`, { method: "DELETE" });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({ detail: response.statusText }));
    throw new ApiError(response.status, detail?.detail ?? response.statusText);
  }
}

type ContextPatch = Partial<{
  title: string;
  body: string;
  expires_on: string;
  clear_expiry: boolean;
  is_active: boolean;
  confirmed: boolean;
}>;

export const advisorApi = {
  status: () => get<AdvisorStatus>("/advisor/status"),

  philosophy: () => get<Philosophy>("/advisor/philosophy"),
  starterPhilosophy: async () => (await apiFetch(`${BASE}/advisor/philosophy/starter`)).text(),
  savePhilosophy: (body_markdown: string) =>
    send<Philosophy>("/advisor/philosophy", "PUT", { body_markdown }),

  context: (includeArchived = false) =>
    get<AdvisorContextList>(`/advisor/context?include_archived=${includeArchived}`),
  addNote: (body: {
    kind: "view" | "preference";
    title: string;
    body: string;
    expires_on?: string | null;
  }) => send<AdvisorContextItem>("/advisor/context/notes", "POST", body),
  addUrl: (body: { url: string; title?: string; note?: string; expires_on?: string | null }) =>
    send<AdvisorContextItem>("/advisor/context/urls", "POST", body),
  addDocument: (file: File, options: { title?: string; note?: string; expires_on?: string } = {}) => {
    const form = new FormData();
    form.append("file", file);
    if (options.title) form.append("title", options.title);
    if (options.note) form.append("note", options.note);
    if (options.expires_on) form.append("expires_on", options.expires_on);
    return upload<AdvisorContextItem>("/advisor/context/documents", form);
  },
  updateContext: (id: string, body: ContextPatch) =>
    send<AdvisorContextItem>(`/advisor/context/${id}`, "PATCH", body),
  deleteContext: (id: string) => remove(`/advisor/context/${id}`),

  sessions: () => get<AdvisorSession[]>("/advisor/sessions"),
  createSession: (title?: string) => send<AdvisorSession>("/advisor/sessions", "POST", { title }),
  forkSession: (id: string, title?: string) =>
    send<AdvisorSession>(`/advisor/sessions/${id}/fork`, "POST", { title }),
  messages: (id: string) => get<HermesMessage[]>(`/advisor/sessions/${id}/messages`),
  stream: streamAdvisorMessage,
  proposal: (id: string) => get<AdvisorProposal>(`/advisor/sessions/${id}/proposal`),
  saveProposal: (id: string, body: { title: string; rationale: string }) =>
    send<Recommendation>(`/advisor/sessions/${id}/proposal/save`, "POST", body),
};

// --- endpoints ------------------------------------------------------------

export const api = {
  health: () =>
    apiFetch("/health").then((r) => r.json() as Promise<{ status: string; auth_enabled: boolean }>),
  summary: () => get<PortfolioSummary>("/portfolio/summary"),
  assetAllocation: () => get<Breakdown>("/portfolio/allocation/asset-class"),
  taxAllocation: () => get<Breakdown>("/portfolio/allocation/tax-treatment"),
  cash: () => get<CashEquivalents>("/portfolio/cash"),
  institutionAllocation: () => get<Breakdown>("/portfolio/allocation/institution"),
  accountAllocation: () => get<Breakdown>("/portfolio/allocation/account"),
  concentration: () => get<Concentration>("/portfolio/concentration"),
  gains: () => get<EmbeddedGains>("/portfolio/gains"),
  securities: (divergentOnly = false) =>
    get<SecurityExposure[]>(`/portfolio/securities?divergent_only=${divergentOnly}`),

  accounts: () => get<Account[]>("/accounts"),
  needsClassification: () => get<Account[]>("/accounts/needs-classification"),
  classifyAccount: (id: string, body: { account_type: string; tax_treatment: string; reason?: string }) =>
    send<Account>(`/accounts/${id}/classification`, "POST", body),

  holdings: (params: Record<string, string> = {}) =>
    get<Holding[]>(`/holdings?${new URLSearchParams(params).toString()}`),

  snapshots: () => get<Snapshot[]>("/snapshots"),
  snapshotRemoval: (id: string) => get<SnapshotRemoval>(`/snapshots/${id}/removal`),
  deleteSnapshot: async (id: string, force = false) => {
    const response = await apiFetch(`${BASE}/snapshots/${id}?force=${force}`, { method: "DELETE" });
    if (!response.ok) {
      const detail = await response.json().catch(() => ({ detail: response.statusText }));
      throw new ApiError(response.status, detail?.detail ?? response.statusText);
    }
  },
  setSnapshotHidden: (id: string, hidden: boolean) =>
    send<Snapshot>(`/snapshots/${id}`, "PATCH", { hidden_from_history: hidden }),

  policy: () => get<Policy>("/policy"),
  policyYaml: async () => (await apiFetch(`${BASE}/policy/yaml`)).text(),
  starterPolicy: async () => (await apiFetch(`${BASE}/policy/starter`)).text(),
  savePolicy: (yaml: string) => send<Policy>("/policy", "PUT", { yaml }),
  assessment: () => get<PolicyAssessment>("/policy/assessment"),

  recommendations: () => get<Recommendation[]>("/recommendations"),
  journalStats: () => get<JournalStats>("/recommendations/stats"),
  decide: (id: string, decision: string, note?: string) =>
    send<Recommendation>(`/recommendations/${id}/decision`, "POST", { decision, note }),
  addRecommendation: (title: string, rationale: string) =>
    send<Recommendation>("/recommendations", "POST", { title, rationale }),

  uploadDocument: async (file: File, provider?: string) => {
    const form = new FormData();
    form.append("file", file);
    const query = provider ? `?provider=${provider}` : "";
    const response = await apiFetch(`${BASE}/imports${query}`, { method: "POST", body: form });
    const body = await response.json();
    if (!response.ok) throw new ApiError(response.status, body?.detail ?? response.statusText);
    return body;
  },

  /** Several documents filed as one portfolio: widest first, the rest join it. */
  uploadDocuments: async (
    files: File[],
    options: { provider?: string; asOf?: string; forceAsOf?: boolean } = {},
  ) => {
    const form = new FormData();
    for (const file of files) form.append("files", file);
    const params = new URLSearchParams();
    if (options.provider) params.set("provider", options.provider);
    // Filing onto an existing snapshot's date is how a document joins it.
    if (options.asOf) params.set("as_of", options.asOf);
    if (options.forceAsOf) params.set("force_as_of", "true");
    const query = params.size ? `?${params.toString()}` : "";
    const response = await apiFetch(`${BASE}/imports/batch${query}`, { method: "POST", body: form });
    const body = await response.json();
    if (!response.ok) throw new ApiError(response.status, body?.detail ?? response.statusText);
    return body;
  },
};

export const SERIES = [
  "var(--series-1)",
  "var(--series-2)",
  "var(--series-3)",
  "var(--series-4)",
  "var(--series-5)",
  "var(--series-6)",
] as const;
