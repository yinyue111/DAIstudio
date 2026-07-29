"use client";

import useStudioFoundation from "../../hooks/studio/useStudioFoundation";
import useStudioGenerationDomain from "../../hooks/studio/useStudioGenerationDomain";
import useStudioModelDomain from "../../hooks/studio/useStudioModelDomain";
import useStudioOwnerLifecycle from "../../hooks/studio/useStudioOwnerLifecycle";
import useStudioPromptDomain from "../../hooks/studio/useStudioPromptDomain";
import useStudioReferenceDomain from "../../hooks/studio/useStudioReferenceDomain";
import useStudioReverseDomain, {
  useStudioReverseOperationBridge,
} from "../../hooks/studio/useStudioReverseDomain";
import useStudioTaskDomain from "../../hooks/studio/useStudioTaskDomain";
import { buildStudioPresentation } from "./buildStudioPresentation";
import StudioWorkspaceView from "./StudioWorkspaceView";

export default function StudioWorkspaceRoot() {
  const foundation = useStudioFoundation();
  const model = useStudioModelDomain(foundation);
  const task = useStudioTaskDomain(foundation, model);
  const operationBridge = useStudioReverseOperationBridge(foundation);
  const prompt = useStudioPromptDomain(
    foundation,
    model,
    operationBridge.reverseOperationForPendingResult,
  );
  const reverse = useStudioReverseDomain(foundation, model, prompt, task, operationBridge);
  const reference = useStudioReferenceDomain(foundation, model, prompt, reverse);
  const generation = useStudioGenerationDomain(foundation, model, prompt, task, reverse);

  useStudioOwnerLifecycle(
    foundation,
    model,
    prompt,
    task,
    reverse,
    reference,
    generation,
  );

  const presentation = buildStudioPresentation({
    foundation,
    model,
    prompt,
    task,
    reverse,
    reference,
    generation,
  });

  return (
    <StudioWorkspaceView
      me={foundation.me}
      gatewayStatus={presentation.gatewayStatus}
      creationController={presentation.creationController}
      resultsController={presentation.resultsController}
      assetPickerController={presentation.assetPickerController}
      submitBarProps={presentation.submitBarProps}
    />
  );
}
