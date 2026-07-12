export type PromptOptimizationContext = {
  creationMode: string;
  category: string;
  subjectMode: string;
  productGenerationMode: boolean;
  promptText?: string;
};

export type PromptOptimizationRequest = {
  id: number;
  contextKey: string;
};

export function promptOptimizationContextKey(context: PromptOptimizationContext): string {
  return JSON.stringify([
    String(context.creationMode || ""),
    String(context.category || ""),
    String(context.subjectMode || ""),
    Boolean(context.productGenerationMode),
    String(context.promptText || "").trim(),
  ]);
}

export function isPromptOptimizationResultCurrent(
  request: PromptOptimizationRequest,
  currentRequestId: number | undefined,
  currentContextKey: string | undefined,
): boolean {
  return request.id === currentRequestId && request.contextKey === currentContextKey;
}
