import type { AssetType } from "./core";
import type { ReverseAnalysisFocus } from "./reverse-reproduction";

export interface AdminUsersQuery {
  q?: string;
  status?: string;
  is_admin?: boolean | "";
  limit?: number;
  offset?: number;
}

export interface AdminUsageDashboardResponse {
  summary: {
    dau: number;
    task_count: number;
    success_count: number;
    failed_count: number;
    needs_review_count: number;
    success_rate: number;
  };
  by_category: Record<string, number>;
  failure_reasons: Record<string, number>;
  avg_duration_seconds: number;
  daily: Array<{ date: string; tasks: number; succeeded: number; failed: number }>;
}

export interface AdminModelCostRow {
  model_id: string;
  kind: string;
  call_count: number;
  ok_count: number;
  failed_count: number;
  failure_rate: number;
  total_tokens: number;
  estimated_cost_credits: number;
  avg_latency_ms: number;
}

export interface AdminModelCostsResponse {
  models: AdminModelCostRow[];
}

export type AdminReverseCostStatus = "complete" | "partial" | "unavailable";

export interface AdminReverseUsageSummary {
  operation_count: number;
  succeeded: number;
  failed: number;
  canceled: number;
  needs_confirmation: number;
  success_rate: number;
  failure_rate: number;
  cancel_rate: number;
  settled_credits: number;
}

export interface AdminReverseQualityMetrics {
  video_operation_count: number;
  cover_fallback_count: number;
  cover_fallback_rate: number;
  cover_confirmation_required_count: number;
  cover_confirmed_count: number;
  cover_confirmation_rate: number;
  repair_count: number;
  repair_succeeded_count: number;
  repair_failed_count: number;
  repair_rate: number;
  avg_evidence_coverage: number;
  feedback_count: number;
  useful_count: number;
  not_useful_count: number;
  useful_rate: number;
  adopted_operation_count: number;
  adoption_rate: number;
  edited_operation_count: number;
  edit_rate: number;
  avg_edit_ratio: number;
  generated_operation_count: number;
  generation_conversion_rate: number;
  recipe_operation_count: number;
  recipe_conversion_rate: number;
  issue_types: Record<string, number>;
}

export interface AdminReverseEconomics {
  basis: string;
  cost_status: AdminReverseCostStatus;
  revenue_credits: number;
  provider_cost_credits: number;
  attributed_provider_cost_credits: number;
  unattributed_provider_cost_credits: number;
  gross_profit_credits: number | null;
  gross_margin_rate: number | null;
  gateway_call_count: number;
  gateway_cost_record_count: number;
  gateway_cost_coverage_rate: number;
  operation_cost_coverage_rate: number;
}

export interface AdminReverseQualityDimensionRow {
  media_type?: AssetType | "unknown";
  model?: string;
  focus?: ReverseAnalysisFocus | "unknown";
  operation_count: number;
  succeeded: number;
  failed: number;
  canceled: number;
  settled_credits: number;
  adopted: number;
  edited: number;
  generated: number;
  recipe: number;
  feedback_count: number;
  useful: number;
  success_rate: number;
  avg_evidence_coverage: number;
  adoption_rate: number;
  edit_rate: number;
  avg_edit_ratio: number;
  generation_conversion_rate: number;
  recipe_conversion_rate: number;
  useful_rate: number;
  provider_cost_credits: number;
  cost_status: AdminReverseCostStatus;
  gross_profit_credits: number | null;
  gross_margin_rate: number | null;
  cost_coverage_rate: number;
}

export interface AdminReverseUsageResponse {
  summary: AdminReverseUsageSummary;
  by_status: Record<string, number>;
  latency: {
    queue_p50_seconds: number | null;
    queue_p95_seconds: number | null;
    total_p50_seconds: number | null;
    total_p95_seconds: number | null;
  };
  quality: AdminReverseQualityMetrics;
  economics: AdminReverseEconomics;
  filters: {
    selected: {
      media_type: AssetType | null;
      model: string | null;
      focus: ReverseAnalysisFocus | string | null;
    };
    options: {
      media_types: Array<AssetType | "unknown">;
      models: string[];
      focuses: string[];
    };
  };
  by_preset: Array<{ preset: string; operation_count: number; succeeded: number; settled_credits: number }>;
  by_target: Array<{ target: string; operation_count: number; succeeded: number; settled_credits: number }>;
  by_focus: AdminReverseQualityDimensionRow[];
  by_media_type: AdminReverseQualityDimensionRow[];
  by_model_quality: AdminReverseQualityDimensionRow[];
  model_costs: Array<{
    model_id: string;
    call_count: number;
    failed_count: number;
    total_tokens: number;
    cost_credits: number;
  }>;
}
