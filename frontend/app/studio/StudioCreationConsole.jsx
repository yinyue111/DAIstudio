import StudioGenerationControls from "../../components/StudioGenerationControls";
import { errorMessage, reportBackgroundError } from "../../lib/errorHandling";
import { CREATION_MODES, creationModeLabel } from "./constants";
import StudioMessageBar from "./StudioMessageBar";
import StudioModeTabs from "./StudioModeTabs";
import StudioPromptSaveControls from "./StudioPromptSaveControls";
import StudioPromptWorkspace from "./StudioPromptWorkspace";
import StudioRecentReversePanel from "./StudioRecentReversePanel";
import StudioReferencePanel from "./StudioReferencePanel";
import StudioReverseApplyConflict from "./StudioReverseApplyConflict";
import StudioReverseResultPanel from "./StudioReverseResultPanel";
import StudioStructuredEditor from "./StudioStructuredEditor";
import StudioSubmitBar from "./StudioSubmitBar";
import {
  assetSignature,
  composePromptFromStructured,
  composeStyleTransferPrompt,
} from "./helpers";

export default function StudioCreationConsole({ controller }) {
  const {
    mode,
    prompt: promptController,
    workspace,
    generation,
    reference,
    reverse,
    ui,
  } = controller;
  const {
    cfg, creationMode, modelEnabled, switchCreationMode, currentModelEnabled,
    category, isEditMode, isImageEditMode, subjectMode, portraitGenerationMode,
    productGenerationMode, changeEditSubjectMode, imageEditProductMode,
  } = mode;
  const {
    prompt, promptPlaceholder, editReadySteps, editStyleKeys,
    updatePromptFromUser, setPromptDirty, optimizeDirectPrompt, promptReadyForOptimization,
    selectedPromptModelConfigId, optimizingPrompt,
    promptOptimizationProposal, promptOptimizationSetting, selectedPromptModel,
    changePromptOptimizationSetting, acceptPromptOptimization, rejectPromptOptimization,
    promptOptimizationUndoAvailable, undoAcceptedPromptOptimization, promptModelOptions,
    changeModelSelection, negative, showNegative, setShowNegative, setNegative,
    setNegativeTouched, structured, recompose, undoStructuredChanges, structuredDirty,
    promptDirty, structuredSource, promptSourceSignature, lastReversePromptRef,
    promptSaveTitle, promptSaveCategory, promptSaveFavorite, setPromptSaveTitle,
    setPromptSaveCategory, setPromptSaveFavorite, saveReversePromptToLibrary,
  } = promptController;
  const {
    clearCurrentWorkspace, clearAllWorkspaces, selected, assets, variationSource,
    setWorkspacePatch, saveCreationRecipe, recipesEnabled,
  } = workspace;
  const {
    selectedGenerationModelConfigId, generationModelOptions, changeGenerationModelSelection,
    productVideoStrategySelection, effectiveProductVideoTemplate, changeProductVideoTemplate,
    submit, ratioOptions, ratio, setRatio, imageQuality, setImageQuality,
    currentImageSize, maxImageN, imageCount, n, setN, maxVideoDuration, videoDuration,
    vDuration, setVDuration, vResolution, setVResolution,
    seed, setSeed, editMaskMode, setEditMaskMode, productPixelLockMode,
    setProductPixelLockMode, submitting, selectedGenerationModel, targetModelId,
    compileStoryboardShot, applyStoryboardShot, handleGenerationSubmitted,
    requestQuoteConfirmation, videoCompositionEnabled,
  } = generation;
  const {
    url, updateReferenceUrl, lastFrameAsset, productAsset, productProfile,
    portraitProfile, parsing, uploading, productProfiling, subjectProtection,
    subjectProtectionLoading, effectiveLastFrameAsset, firstLastFrameEnabled,
    productDetailAssets, productDetailValidation, productDetailLimit, appliedUrl,
    profileOperation, reverseSources, imageUploadInputRef, productUploadInputRef,
    productDetailUploadInputRef, lastFrameUploadInputRef, videoUploadInputRef, clearRef,
    clearProductAsset, doParse, doUploadImage, doUploadProductImage,
    doUploadProductDetailImages, doUploadLastFrameImage, setAssetPicker, doUploadVideo,
    pickAsset,
  } = reference;
  const {
    reversing, batchReverseAssets, pendingReverseResult, reverseEnabled, reverseImageCost,
    selectedReverseCost, selectedReverseCostLabel, reverseConfig, setReverseConfig,
    videoAnalysisPreset, reverseVideoPresets,
    reverseVideoAnalysis, reverseOperation, workspaceReverseOperation, reverseBatch,
    recentReverseBatches, reverseBatchBusyAction, reverseActionBusy, reverseBatchEnabled,
    visionModelOptions, selectedVisionModelConfigId, setVideoAnalysisPreset,
    doReverse, confirmReverseCover, cancelReverseOperationForMode,
    toggleBatchReverseAsset, removeBatchReverseAsset, startReverseBatch, cancelReverseBatch,
    openReverseBatch, openRecentReverseOperation, retryReverse,
    saveReverseBatchRecipes, reverseOperationForPendingResult, reverseResultTab,
    reverseResultRevisions, reverseAppliedVersion, reverseAppliedRevisionId, reverseFeedback,
    reverseUndoSnapshot, applyPendingReverseResult, undoAppliedReverseResult,
    savePendingReverseVersion, submitReverseFeedback, restoreReverseRevision, reverseApplyConflict,
    recentReverseOperations, recentReverseLoading, recentReverseError, recentReverseBusyId,
    refreshRecentReverseOperations,
  } = reverse;
  const {
    submitBarProps, refOpen, setRefOpen, structOpen, setStructOpen, notify, msg, setMsg,
  } = ui;
  const pendingReverseOperation = reverseOperationForPendingResult();

  return (
    <>
    {/* creation console */}
    <section className="mx-auto max-w-5xl min-w-0 lg:animate-fadeup">
      <div className="panel min-w-0 p-2.5">
        <StudioModeTabs
          modes={CREATION_MODES}
          activeMode={creationMode}
          isModelEnabled={modelEnabled}
          onModeChange={switchCreationMode}
        />
        {!currentModelEnabled && (
          <p className="mb-2 rounded-xl border border-warn/30 bg-warn/10 px-3 py-2 text-sm text-warn">
            当前{creationModeLabel(creationMode)}模型未启用，请管理员在后台模型配置中启用后再生成。
          </p>
        )}

        {/* prompt + reference */}
        <div className="grid min-w-0 items-start gap-3 lg:grid-cols-[minmax(0,1fr)_320px]">
          <div className="min-w-0 rounded-xl3 border border-line bg-base2/40 p-3 lg:self-start">
            <StudioPromptWorkspace
              category={category}
              creationModeLabel={creationModeLabel(creationMode)}
              isEditMode={isEditMode}
              isImageEditMode={isImageEditMode}
              subjectMode={subjectMode}
              portraitGenerationMode={portraitGenerationMode}
              productGenerationMode={productGenerationMode}
              prompt={prompt}
              placeholder={promptPlaceholder}
              readySteps={editReadySteps}
              editStyleKeys={editStyleKeys}
              onPromptChange={updatePromptFromUser}
              onPromptDirty={setPromptDirty}
              onOptimizePrompt={optimizeDirectPrompt}
              canOptimizePrompt={Boolean(
                promptReadyForOptimization
                && (!cfg?.model_options || selectedPromptModelConfigId)
                && selectedGenerationModelConfigId
              )}
              optimizingPrompt={optimizingPrompt}
              optimizationProposal={promptOptimizationProposal}
              optimizationDirection={promptOptimizationSetting.direction}
              optimizationTargetLanguage={promptOptimizationSetting.targetLanguage}
              optimizationEstimatedCredits={Number(selectedPromptModel?.cost_credits || 0)}
              onOptimizationSettingsChange={changePromptOptimizationSetting}
              onAcceptOptimization={acceptPromptOptimization}
              onRejectOptimization={rejectPromptOptimization}
              optimizationUndoAvailable={promptOptimizationUndoAvailable}
              onUndoOptimization={undoAcceptedPromptOptimization}
              generationModelOptions={generationModelOptions}
              selectedGenerationModelConfigId={selectedGenerationModelConfigId}
              onGenerationModelChange={changeGenerationModelSelection}
              productVideoStrategyOptions={productVideoStrategySelection.options}
              productVideoTemplate={effectiveProductVideoTemplate}
              productVideoStrategySupported={productVideoStrategySelection.supported}
              onProductVideoTemplateChange={changeProductVideoTemplate}
              promptModelOptions={promptModelOptions}
              selectedPromptModelConfigId={selectedPromptModelConfigId}
              onPromptModelChange={(id) => changeModelSelection("prompt", id)}
              onClearWorkspace={clearCurrentWorkspace}
              onClearAllWorkspaces={clearAllWorkspaces}
              canClearWorkspace={Boolean(
                prompt.trim()
                || negative.trim()
                || url.trim()
                || selected
                || lastFrameAsset
                || productAsset
                || assets.length
                || Object.keys(structured || {}).length
                || productProfile
                || portraitProfile
                || variationSource
                || parsing
                || uploading
                || reversing
                || batchReverseAssets.length
                || productProfiling
                || subjectProtection
                || optimizingPrompt
                || pendingReverseResult
              )}
              onSubjectModeChange={changeEditSubjectMode}
              onRecompose={recompose}
              onSubmitPreview={() => submit(category === "video" ? "final" : "preview")}
            />
            <div className="mt-3 border-t border-line pt-3">
              <StudioGenerationControls
                category={category}
                ratioOptions={ratioOptions}
                ratio={ratio}
                onRatioChange={setRatio}
                imageQuality={imageQuality}
                onImageQualityChange={setImageQuality}
                currentImageSize={currentImageSize}
                maxImageN={maxImageN}
                imageCount={imageCount}
                n={n}
                onImageCountChange={setN}
                maxVideoDuration={maxVideoDuration}
                videoDuration={videoDuration}
                vDuration={vDuration}
                onVideoDurationChange={setVDuration}
                vResolution={vResolution}
                onVideoResolutionChange={setVResolution}
                isEditMode={isEditMode}
                productGenerationMode={productGenerationMode}
                portraitGenerationMode={portraitGenerationMode}
                showNegative={showNegative}
                onToggleNegative={() => setShowNegative((s) => !s)}
                seed={seed}
                onSeedChange={setSeed}
                editMaskMode={editMaskMode}
                onEditMaskModeChange={setEditMaskMode}
                productPixelLockMode={productPixelLockMode}
                onProductPixelLockModeChange={setProductPixelLockMode}
                subjectProtection={subjectProtection}
                subjectProtectionLoading={subjectProtectionLoading}
                negative={negative}
                onNegativeChange={setNegative}
                onNegativeTouched={setNegativeTouched}
                submitBar={<StudioSubmitBar {...submitBarProps} />}
              />
            </div>
          </div>

          <StudioReferencePanel
            category={category}
            creationMode={creationMode}
            imageEditProductMode={imageEditProductMode}
            editSubjectMode={subjectMode}
            isEditMode={isEditMode}
            selected={selected}
            lastFrameAsset={effectiveLastFrameAsset}
            firstLastFrameEnabled={firstLastFrameEnabled}
            productAsset={productAsset}
            productDetailAssets={productDetailAssets}
            productDetailValidation={productDetailValidation}
            productDetailLimit={productDetailLimit}
            url={url}
            appliedUrl={appliedUrl}
            setUrl={updateReferenceUrl}
            parsing={parsing}
            uploading={uploading || submitting}
            productBusy={submitting}
            productProfiling={productProfiling}
            profileOperation={profileOperation}
            assets={assets}
            refOpen={refOpen}
            setRefOpen={setRefOpen}
            reversing={reversing}
            reverseEnabled={Boolean(
              reverseEnabled
              && (!cfg?.model_options || selectedVisionModelConfigId)
            )}
            reverseImageCost={reverseImageCost}
            selectedReverseCost={selectedReverseCost}
            selectedReverseCostLabel={selectedReverseCostLabel}
            reverseConfig={reverseConfig}
            setReverseConfig={setReverseConfig}
            reverseSources={reverseSources}
            setReverseSources={(value) => setWorkspacePatch({ reverseSources: value })}
            videoAnalysisPreset={videoAnalysisPreset}
            videoAnalysisPresets={reverseVideoPresets}
            reverseVideoAnalysis={reverseVideoAnalysis}
            reverseOperation={reverseOperation || workspaceReverseOperation}
            batchReverseAssets={batchReverseAssets}
            reverseBatch={reverseBatch}
            recentReverseBatches={recentReverseBatches}
            reverseBatchBusyAction={reverseBatchBusyAction || reverseActionBusy}
            reverseBatchCapabilities={cfg?.reverse?.batch_capabilities || cfg?.reverse?.capabilities || null}
            reverseBatchEnabled={reverseBatchEnabled}
            visionModelOptions={visionModelOptions}
            selectedVisionModelConfigId={selectedVisionModelConfigId}
            onVisionModelChange={(id) => changeModelSelection("vision", id)}
            setVideoAnalysisPreset={setVideoAnalysisPreset}
            imageUploadInputRef={imageUploadInputRef}
            productUploadInputRef={productUploadInputRef}
            productDetailUploadInputRef={productDetailUploadInputRef}
            lastFrameUploadInputRef={lastFrameUploadInputRef}
            videoUploadInputRef={videoUploadInputRef}
            onClear={clearRef}
            onClearProductAsset={clearProductAsset}
            onParse={doParse}
            onUploadImage={doUploadImage}
            onUploadProductImage={doUploadProductImage}
            onUploadProductDetailImages={doUploadProductDetailImages}
            onUploadLastFrameImage={doUploadLastFrameImage}
            onClearLastFrameAsset={() => setWorkspacePatch({ lastFrameAsset: null })}
            onOpenAssetPicker={(role) => {
              if (role === "product_detail" && !productAsset) {
                setMsg("请先选择产品主题图");
                return;
              }
              setAssetPicker(role === "reverse_source"
                ? { role, mediaType: category === "video" ? "video" : "image" }
                : { role });
            }}
            onRemoveProductDetail={(index) => setWorkspacePatch((current) => ({
              productDetailAssets: (current.productDetailAssets || []).filter((_, itemIndex) => itemIndex !== index),
            }))}
            onMoveProductDetail={(index, direction) => setWorkspacePatch((current) => {
              const next = [...(current.productDetailAssets || [])];
              const target = index + direction;
              if (target < 0 || target >= next.length) return {};
              [next[index], next[target]] = [next[target], next[index]];
              return { productDetailAssets: next };
            })}
            onUploadVideo={doUploadVideo}
            onPickAsset={pickAsset}
            onReverse={doReverse}
            onConfirmCover={(fallbackFile) => confirmReverseCover(fallbackFile).catch((e) => {
              setMsg(e.message || "封面分析确认失败，请重试。");
              reportBackgroundError(e, "confirm reverse cover");
            })}
            onCancelReverse={() => cancelReverseOperationForMode(creationMode).catch((e) => {
              reportBackgroundError(e, "cancel reverse from reference panel");
            })}
            onToggleBatchAsset={toggleBatchReverseAsset}
            onRemoveBatchAsset={removeBatchReverseAsset}
            onOpenBatchAssetPicker={() => {
              if (reverseBatchEnabled) setAssetPicker({ role: "reverse_batch" });
            }}
            onStartReverseBatch={startReverseBatch}
            onCancelReverseBatch={() => cancelReverseBatch().then(() => {
              setMsg("已请求取消批量反推，运行中的单项会按任务状态完成取消。");
            }).catch((error) => {
              const text = errorMessage(error, "取消批量反推失败");
              setMsg(text);
              notify.error(text);
            })}
            onOpenReverseBatch={openReverseBatch}
            onOpenReverseBatchItem={openRecentReverseOperation}
            onRetryReverseBatchItem={retryReverse}
            onSaveReverseBatchRecipes={saveReverseBatchRecipes}
          />
        </div>

        <StudioReverseResultPanel
          pending={pendingReverseResult}
          operation={pendingReverseOperation}
          activeTab={reverseResultTab}
          revisions={reverseResultRevisions}
          appliedVersion={reverseAppliedVersion}
          appliedRevisionId={reverseAppliedRevisionId}
          feedback={reverseFeedback}
          canUndo={Boolean(reverseUndoSnapshot)}
          busyAction={reverseActionBusy}
          generationModelName={selectedGenerationModel?.display_name || targetModelId || "当前视频模型"}
          onTabChange={(value) => setWorkspacePatch({ reverseResultTab: value })}
          onResultChange={(value) => setWorkspacePatch({ pendingReverseResult: value })}
          onApply={applyPendingReverseResult}
          onUndo={undoAppliedReverseResult}
          onClose={() => setWorkspacePatch({ pendingReverseResult: null })}
          onRetry={() => retryReverse()}
          onSavePrompt={saveReversePromptToLibrary}
          onSaveVersion={savePendingReverseVersion}
          onSaveRecipe={() => saveCreationRecipe()}
          onFeedback={submitReverseFeedback}
          onRestoreRevision={restoreReverseRevision}
          onCompileStoryboardShot={compileStoryboardShot}
          onApplyStoryboardShot={applyStoryboardShot}
          onGenerationSubmitted={handleGenerationSubmitted}
          onPickCompositionAsset={(request) => setAssetPicker(request)}
          requestQuoteConfirmation={requestQuoteConfirmation}
          videoCompositionEnabled={videoCompositionEnabled}
          recipesEnabled={recipesEnabled}
        />

        <StudioReverseApplyConflict
          conflict={reverseApplyConflict}
          onDismiss={() => setWorkspacePatch({ reverseApplyConflict: null })}
        />

        <StudioRecentReversePanel
          operations={recentReverseOperations.filter((item) => ["image", "video"].includes(item.target))}
          loading={recentReverseLoading}
          error={recentReverseError}
          busyOperationId={recentReverseBusyId}
          onRefresh={refreshRecentReverseOperations}
          onOpen={openRecentReverseOperation}
          onRetry={retryReverse}
          onSaveRecipe={(item) => saveCreationRecipe(item, { reuseCurrent: false })}
          recipesEnabled={recipesEnabled}
        />

        <StudioStructuredEditor
          structured={structured}
          open={structOpen}
          onToggleOpen={() => setStructOpen((open) => !open)}
          onRecompose={recompose}
          onUndo={undoStructuredChanges}
          onClear={clearRef}
          structuredDirty={structuredDirty}
          promptDirty={promptDirty}
          onChange={(key, value) => {
            const nextStructured = { ...structured, [key]: value };
            if (promptDirty) {
              setWorkspacePatch({ structured: nextStructured, structuredDirty: true });
              return;
            }
            const nextPrompt = isEditMode
              ? composeStyleTransferPrompt(nextStructured, prompt, { video: category === "video", subject: subjectMode })
              : composePromptFromStructured(nextStructured, prompt, { target: category });
            setWorkspacePatch({
              structured: nextStructured,
              structuredBaseline: nextStructured,
              structuredDirty: false,
              prompt: nextPrompt,
              promptDirty: false,
              promptSourceSignature: structuredSource || assetSignature(selected) || "",
            });
          }}
        />
        <StudioPromptSaveControls
          visible={Boolean(
            prompt?.trim()
            && (structuredSource
              || promptSourceSignature
              || lastReversePromptRef.current?.[creationMode]?.prompt)
          )}
          title={promptSaveTitle}
          category={promptSaveCategory}
          favorite={promptSaveFavorite}
          onTitleChange={setPromptSaveTitle}
          onCategoryChange={setPromptSaveCategory}
          onFavoriteChange={setPromptSaveFavorite}
          onSave={saveReversePromptToLibrary}
        />

      </div>

      <StudioMessageBar message={msg} onDismiss={setMsg} />
    </section>

    </>
  );
}
