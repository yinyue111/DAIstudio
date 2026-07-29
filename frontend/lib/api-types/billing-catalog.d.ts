export type CreditTransactionType = "grant" | "freeze" | "settle" | "refund" | "unlock" | "consume";

export type BillingKind = "generation" | "reverse" | "workflow" | "prompt" | "unlock" | "recharge" | "grant" | "other";

export interface CreditTransaction {
  id: number;
  type: CreditTransactionType;
  change: number;
  balance_delta: number;
  frozen_delta: number;
  balance_after: number;
  frozen_after?: number | null;
  reserved_amount?: number | null;
  real_cost?: number | null;
  biz_type?: string | null;
  biz_ref?: number | null;
  note?: string | null;
  created_at: string;
}

export interface CreditTransactionPage {
  items: CreditTransaction[];
  next_cursor?: string | null;
  has_more: boolean;
  total: number;
}

export interface BillingEntry {
  key: string;
  kind: BillingKind;
  biz_type: string;
  biz_ref?: number | null;
  title: string;
  subtitle?: string | null;
  status: string;
  status_group: string;
  model_config_id?: number | null;
  model_name?: string | null;
  model_id?: string | null;
  model_provider?: string | null;
  quote_id?: number | null;
  quote_kind?: string | null;
  quote_status?: string | null;
  quoted_credits?: number | null;
  price_version_id?: number | null;
  related_kind?: string | null;
  related_id?: number | null;
  review_status?: string | null;
  review_reason?: string | null;
  review_note?: string | null;
  reviewed_at?: string | null;
  frozen_credits: number;
  settled_credits: number;
  refunded_credits: number;
  settlement_returned_credits: number;
  reservation_refunded_credits: number;
  consumed_refunded_credits: number;
  outstanding_frozen_credits: number;
  net_consumed_credits: number;
  credited_credits: number;
  net_balance_change: number;
  balance_conserved: boolean;
  reservation_conserved: boolean;
  balance_after: number;
  frozen_after?: number | null;
  transaction_count: number;
  created_at: string;
  updated_at: string;
}

export interface BillingEntryPage {
  items: BillingEntry[];
  next_cursor?: string | null;
  has_more: boolean;
  total: number;
}

export type CatalogMetadataOrigin = "recorded" | "legacy_backfill";

export interface ModelCatalogMetadataSnapshot {
  schema_version: "model-catalog-metadata.v1";
  origin: CatalogMetadataOrigin;
  model_id: string;
  display_name: string;
  is_default: boolean;
  sort_order: number;
  enabled: boolean;
}

export interface ModelCapabilityVersion {
  id: number;
  version: number;
  schema_version: string;
  capabilities: Record<string, unknown>;
  metadata_snapshot: ModelCatalogMetadataSnapshot;
  status: "draft" | "published" | "disabled" | "retired";
  is_active: boolean;
  source_version_id?: number | null;
  activated_at?: string | null;
  disabled_at?: string | null;
  retired_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface ModelPriceVersion {
  id: number;
  version: number;
  schema_version: string;
  base_cost_credits: number;
  unlock_cost_credits: number;
  pricing: Record<string, unknown>;
  status: "draft" | "published" | "disabled" | "retired";
  is_active: boolean;
  source_version_id?: number | null;
  activated_at?: string | null;
  disabled_at?: string | null;
  retired_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface ModelCatalogItem {
  id: number;
  use: "vision" | "image" | "video" | "prompt";
  name: string;
  model_id: string;
  provider: string;
  provider_label: string;
  is_default: boolean;
  sort_order: number;
  cost_credits: number;
  unlock_cost: number;
  preview_cost?: number | null;
  final_cost: number;
  enabled: boolean;
  capabilities: Record<string, unknown>;
  route_availability?: {
    configured: number;
    available: number;
    status: "available" | "unavailable";
    legacy?: boolean;
  } | null;
  capability_version?: ModelCapabilityVersion | null;
  price_version?: ModelPriceVersion | null;
  capability_versions?: ModelCapabilityVersion[];
  price_versions?: ModelPriceVersion[];
}

export interface AdminModelRoute {
  id: number;
  model_config_id: number;
  route_key: string;
  name: string;
  model_id?: string | null;
  provider?: string | null;
  base_url?: string | null;
  gateway_format?: string | null;
  api_key_configured: boolean;
  extra: Record<string, unknown>;
  priority: number;
  enabled: boolean;
  managed_by_model_config: boolean;
  config_revision: number;
  failure_threshold: number;
  window_seconds: number;
  cooldown_seconds: number;
  health_status: "closed" | "open" | "half_open";
  window_requests: number;
  window_failures: number;
  consecutive_failures: number;
  opened_at?: string | null;
  cooldown_until?: string | null;
  last_probe_at?: string | null;
  last_success_at?: string | null;
  last_failure_at?: string | null;
  latency_ema_ms?: number | null;
  last_error_code?: string | null;
  created_at: string;
  updated_at: string;
  active_version?: AdminModelRouteVersion | null;
}

export interface AdminModelRouteVersion {
  id: number;
  route_id: number;
  version: number;
  schema_version: string;
  config: Record<string, unknown>;
  api_key_configured: boolean;
  status: "draft" | "published" | "disabled" | "retired";
  is_active: boolean;
  source_version_id?: number | null;
  activated_at?: string | null;
  disabled_at?: string | null;
  retired_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface AdminModelRouteHealthEvent {
  id: number;
  operation: string;
  outcome: "success" | "failure" | "ignored";
  counts_toward_circuit: boolean;
  latency_ms?: number | null;
  error_code?: string | null;
  created_at: string;
}

export type ToolCatalogCategory = "image" | "video" | "workflow" | "utility";

export interface ToolCatalogMetadataSnapshot {
  schema_version: "tool-catalog-metadata.v1";
  origin: CatalogMetadataOrigin;
  slug: string;
  name: string;
  description: string | null;
  category: ToolCatalogCategory;
  renderer: string;
  entry_path: string;
  icon: string | null;
  sort_order: number;
  enabled: boolean;
  featured: boolean;
}

export interface ToolCatalogVersion {
  id: number;
  version: number;
  schema_version: string;
  input_schema: Record<string, unknown>;
  workflow: Record<string, unknown>;
  pricing_policy: Record<string, unknown>;
  capabilities: Record<string, unknown>;
  metadata_snapshot: ToolCatalogMetadataSnapshot;
  status: "draft" | "published" | "disabled" | "retired";
  is_active: boolean;
  source_version_id?: number | null;
  activated_at?: string | null;
  disabled_at?: string | null;
  retired_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface ToolCatalogItem {
  id: number;
  slug: string;
  name: string;
  description?: string | null;
  category: ToolCatalogCategory;
  renderer: string;
  entry_path: string;
  icon?: string | null;
  sort_order: number;
  enabled: boolean;
  featured: boolean;
  active_version?: ToolCatalogVersion | null;
  versions?: ToolCatalogVersion[];
  created_at: string;
  updated_at: string;
}

export type ModelUse = "vision" | "image" | "video" | "prompt";

export interface ModelOption {
  id: number;
  use: ModelUse;
  name: string;
  model_id: string;
  provider: string;
  provider_label: string;
  cost_credits: number;
  unlock_cost: number;
  is_default: boolean;
  capabilities: Record<string, boolean | number | string | string[]>;
}
