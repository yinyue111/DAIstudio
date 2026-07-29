export type AssetType = "image" | "video";

export type TaskStatus = "queued" | "running" | "succeeded" | "failed" | "needs_review" | "canceled";

export interface AppNavigationItem {
  key: string;
  label: string;
  href: string;
  width: "w-16" | "w-20" | "w-24";
  permission?: "admin";
  reserve_desktop?: boolean;
  enabled: boolean;
  visible: boolean;
  disabled_reason: string | null;
}

export interface AppNavigationCatalog {
  schema_version: number;
  default_key: string;
  items: AppNavigationItem[];
}
