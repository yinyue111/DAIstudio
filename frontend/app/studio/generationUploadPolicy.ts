export type StudioUploadRole =
  | "reference"
  | "product"
  | "product_detail"
  | "last_frame"
  | "video_reference";

interface GenerationUploadPolicyInput {
  uploading?: boolean;
  uploadingRole?: StudioUploadRole | null;
  selected?: unknown;
}

export function generationRequiresPendingUpload({
  uploading = false,
  uploadingRole = null,
  selected = null,
}: GenerationUploadPolicyInput): boolean {
  if (!uploading) return false;

  switch (uploadingRole) {
    case "reference":
    case "video_reference":
      // A new optional reference can be omitted, but replacing an existing one
      // must wait so generation cannot silently use the old asset.
      return Boolean(selected);
    case "product_detail":
      return false;
    case "product":
    case "last_frame":
      return true;
    default:
      // Preserve the previous fail-closed behavior for unknown/stale state.
      return true;
  }
}
