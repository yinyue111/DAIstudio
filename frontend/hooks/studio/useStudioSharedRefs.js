"use client";

import { useRef } from "react";

export default function useStudioSharedRefs() {
  return {
    revokeUploadedObjectUrlsRef: useRef(null),
    workspacesRef: useRef(null),
    cloudDraftLoadedRef: useRef(false),
    studioInitSeqRef: useRef(0),
    studioDraftClockRef: useRef(null),
    studioOwnerSessionCoordinatorRef: useRef(null),
    studioOwnerSessionRef: useRef(null),
    latestMeRequestRef: useRef(null),
    studioOwnerUserIdRef: useRef(""),
    initialStudioUiStateRef: useRef(null),
    cloudDraftWriterRef: useRef(null),
    studioUiStateRef: useRef(null),
    variationRestoreContextRef: useRef(null),
    subjectProfilePendingRequestRef: useRef(null),
    subjectProfileResultCacheRef: useRef(null),
    subjectProfileOperationsRef: useRef({}),
    reverseResumeOperationsRef: useRef([]),
    localReverseDraftRef: useRef({ owner: "", active: false }),
    studioActionPendingRequestRef: useRef([]),
    workflowBootstrapRef: useRef(""),
    catalogModelBootstrapRef: useRef(""),
    reverseTaskBootstrapRef: useRef(""),
  };
}
