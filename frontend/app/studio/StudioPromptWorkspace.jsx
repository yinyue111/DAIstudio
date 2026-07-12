"use client";

import { EXAMPLES } from "./constants";

const EDIT_PROMPT_CHIPS = [
  "保留品牌标识",
  "包装文字逐字保留",
  "保留人像身份",
  "强化产品卖点",
  "产品边缘自然融入场景",
  "干净商业广告质感",
  "小红书种草氛围",
];

function promptChipsForEditMode({
  isImageEditMode,
  portraitGenerationMode,
  productGenerationMode,
}) {
  if (!isImageEditMode) {
    return portraitGenerationMode
      ? ["保留人物身份", "参考视频动作节奏", "同款镜头语言", "保留五官和发型", "自然面部表情", "不迁移参考人物长相"]
      : EDIT_PROMPT_CHIPS;
  }
  if (portraitGenerationMode) {
    return ["保留人像身份", "同款光线和构图", "小红书封面写真", "职业形象照", "自然皮肤质感", "不改变五官"];
  }
  if (productGenerationMode) {
    return ["包装文字逐字保留", "保留包装和Logo", "生成电商主图", "小红书产品种草图", "替换广告场景", "产品边缘自然融入"];
  }
  return ["替换为干净棚拍背景", "保留主体和Logo", "增加商业广告光感", "调整为小红书封面", "去除杂乱背景"];
}

function transferRulesForEditMode({
  isImageEditMode,
  portraitGenerationMode,
  productGenerationMode,
}) {
  if (!isImageEditMode) {
    return portraitGenerationMode
      ? [
          ["人物重构目标", "上传人像作为唯一身份参考"],
          ["迁移视频风格", "参考视频只提供动作、镜头、场景和节奏"],
          ["能力边界", "当前为人物参考驱动重构，非逐帧换脸"],
        ]
      : [
          ["保留产品身份", "Logo、包装、颜色和形状不漂移"],
          ["迁移参考气质", "只迁移场景、构图、光线和广告质感"],
          ["输出商业素材", "产品清晰，边缘自然融入新场景"],
        ];
  }
  if (portraitGenerationMode) {
    return [
      ["锁定人物身份", "五官、脸型、发型、肤色和年龄感不变"],
      ["迁移参考风格", "只迁移场景、构图、光线、妆造和画面质感"],
      ["高保真人像", "脸部清晰自然，避免变成参考图里的人"],
    ];
  }
  if (productGenerationMode) {
    return [
      ["锁定产品身份", "SKU、Logo、包装结构、品牌色和表面文字不改"],
      ["迁移商业场景", "只生成背景、道具、构图、光线和广告质感"],
      ["高保真输出", "产品清晰锐利，包装文字逐字保留"],
    ];
  }
  return [
    ["按提示词编辑", "只改用户明确要求修改的部分"],
    ["保留源图细节", "主体、Logo、文字和比例默认保持"],
    ["可选参考增强", "可用右侧参考图迁移光线、构图和质感"],
  ];
}

function modeTitle({
  isImageEditMode,
  portraitGenerationMode,
  productGenerationMode,
  creationModeLabel,
}) {
  if (isImageEditMode) {
    if (portraitGenerationMode) return "上传人像生成同款风格写真";
    if (productGenerationMode) return "上传产品图生成商业素材";
    return "上传图片后按提示词编辑";
  }
  return portraitGenerationMode ? "上传人物生成目标视频风格" : `${creationModeLabel}素材重构`;
}

function modeBadge({
  category,
  portraitGenerationMode,
  productGenerationMode,
}) {
  if (category === "video") return portraitGenerationMode ? "人物重构" : "图生视频源";
  if (portraitGenerationMode) return "人像高保真";
  if (productGenerationMode) return "产品高保真";
  return "图片编辑源";
}

function EditSubjectSelector({
  isImageEditMode,
  subjectMode,
  onSubjectModeChange,
}) {
  const options = isImageEditMode
    ? [
        { key: "general", label: "普通编辑", desc: "按提示修图" },
        { key: "product", label: "产品生产", desc: "强保护Logo和包装" },
        { key: "portrait", label: "人像写真", desc: "强保护五官和身份" },
      ]
    : [
        { key: "product", label: "产品视频", desc: "上传产品图做主体" },
        { key: "portrait", label: "人物重构", desc: "人像参考生成同款视频" },
      ];

  return (
    <div className={`mt-4 grid gap-2 rounded-xl border border-line bg-base/35 p-1 ${isImageEditMode ? "grid-cols-1 sm:grid-cols-3" : "grid-cols-2"}`}>
      {options.map((item) => {
        const active = item.key === subjectMode;
        return (
          <button
            key={item.key}
            type="button"
            onClick={() => onSubjectModeChange(item.key)}
            className={`rounded-lg px-3 py-2 text-left transition ${
              active
                ? "border border-aqua/50 bg-aqua/15 text-snow shadow-glow-sm"
                : "border border-transparent text-fog hover:bg-white/[0.04] hover:text-mist"
            }`}
          >
            <span className="block text-sm font-display font-semibold">{item.label}</span>
            <span className="mt-0.5 block text-[11px] leading-snug">{item.desc}</span>
          </button>
        );
      })}
    </div>
  );
}

function EditBrief({
  category,
  isImageEditMode,
  subjectMode,
  portraitGenerationMode,
  productGenerationMode,
  readySteps,
  creationModeLabel,
  onSubjectModeChange,
}) {
  return (
    <div className="rounded-2xl border border-line2 bg-gradient-to-br from-iris/15 via-white/[0.055] to-aqua/10 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs font-display text-fog">编辑 Brief</p>
          <h2 className="mt-1 text-xl font-display font-semibold text-snow">
            {modeTitle({ isImageEditMode, portraitGenerationMode, productGenerationMode, creationModeLabel })}
          </h2>
        </div>
        <span className="badge bg-brand-soft text-snow">
          {modeBadge({ category, portraitGenerationMode, productGenerationMode })}
        </span>
      </div>

      <EditSubjectSelector
        isImageEditMode={isImageEditMode}
        subjectMode={subjectMode}
        onSubjectModeChange={onSubjectModeChange}
      />

      <div className="mt-4 grid gap-2 sm:grid-cols-3">
        {readySteps.map((step) => (
          <div
            key={step.label}
            className={`rounded-xl border px-3 py-2 ${
              step.ready ? "border-aqua/35 bg-aqua/10" : "border-line bg-base/40"
            }`}
          >
            <p className="text-[11px] text-fog">{step.label}</p>
            <p className={`mt-1 text-sm font-display font-medium ${step.ready ? "text-aqua" : "text-mist"}`}>
              {step.ready ? step.readyText : step.pendingText}
            </p>
          </div>
        ))}
      </div>
    </div>
  );
}

function EditPromptPanel({
  prompt,
  placeholder,
  promptLibraryOpen,
  isImageEditMode,
  portraitGenerationMode,
  productGenerationMode,
  onPromptChange,
  onPromptDirty,
  onAppendPrompt,
  onTogglePromptLibrary,
  onOptimizePrompt,
  canOptimizePrompt,
  optimizingPrompt,
  onClearWorkspace,
  canClearWorkspace,
  onSubmitPreview,
}) {
  const chips = promptChipsForEditMode({ isImageEditMode, portraitGenerationMode, productGenerationMode });

  return (
    <div className="rounded-2xl border border-line bg-base/35 p-3">
      <div className="mb-2 flex items-center justify-between gap-3">
        <label className="label m-0">补充要求 / 卖点 / 必须保留</label>
        <span className="text-[11px] text-fog">⌘/Ctrl + Enter</span>
      </div>
      <textarea
        className="textarea h-28 resize-none border-line bg-base2/60 text-[14px]"
        placeholder={placeholder}
        value={prompt}
        onChange={(e) => {
          onPromptChange(e.target.value);
          onPromptDirty(true);
        }}
        onKeyDown={(e) => {
          if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
            e.preventDefault();
            onSubmitPreview();
          }
        }}
      />
      <div className="mt-2 flex flex-wrap gap-1.5">
        {chips.map((ex) => (
          <button
            key={ex}
            type="button"
            onClick={() => onAppendPrompt(ex)}
            className="chip"
          >
            {ex}
          </button>
        ))}
        <button
          type="button"
          onClick={onTogglePromptLibrary}
          className={`chip ${promptLibraryOpen ? "chip-active" : ""}`}
        >
          提示词库
        </button>
        <button
          type="button"
          onClick={onOptimizePrompt}
          disabled={!canOptimizePrompt || optimizingPrompt}
          className="chip disabled:cursor-not-allowed disabled:opacity-45"
          title="使用独立模型优化当前直接输入的提示词"
        >
          {optimizingPrompt ? "优化中…" : "✦ 优化提示词"}
        </button>
        <button
          type="button"
          onClick={onClearWorkspace}
          disabled={!canClearWorkspace}
          className="chip disabled:cursor-not-allowed disabled:opacity-45"
          title="清空提示词、上传素材和反推结果"
        >
          ✕ 清空
        </button>
      </div>
    </div>
  );
}

function EditAssistPanels({
  isImageEditMode,
  portraitGenerationMode,
  productGenerationMode,
  editStyleKeys,
  onRecompose,
}) {
  const rules = transferRulesForEditMode({ isImageEditMode, portraitGenerationMode, productGenerationMode });

  return (
    <div className="grid min-w-0 gap-3 xl:grid-cols-[1fr_1.15fr]">
      <div className="rounded-2xl border border-line bg-white/[0.035] p-3">
        <p className="text-xs font-display font-medium text-mist">迁移规则</p>
        <div className="mt-3 space-y-2">
          {rules.map(([title, desc]) => (
            <div key={title} className="rounded-xl border border-line bg-base/35 px-3 py-2">
              <p className="text-sm font-display font-medium text-snow">{title}</p>
              <p className="mt-0.5 text-xs text-fog">{desc}</p>
            </div>
          ))}
        </div>
      </div>

      <div className="rounded-2xl border border-line bg-white/[0.035] p-3">
        <div className="flex items-center justify-between gap-3">
          <p className="text-xs font-display font-medium text-mist">已提取风格维度</p>
          {editStyleKeys.length > 0 && (
            <button type="button" onClick={onRecompose} className="chip px-2 py-0.5">
              重组提示词
            </button>
          )}
        </div>
        {editStyleKeys.length > 0 ? (
          <div className="mt-3 flex flex-wrap gap-1.5">
            {editStyleKeys.slice(0, 12).map((key) => (
              <span key={key} className="rounded-full border border-iris/30 bg-iris/10 px-2.5 py-1 text-xs text-mist">
                {key}
              </span>
            ))}
            {editStyleKeys.length > 12 && (
              <span className="rounded-full border border-line bg-white/5 px-2.5 py-1 text-xs text-fog">
                +{editStyleKeys.length - 12}
              </span>
            )}
          </div>
        ) : (
          <div className="mt-3 rounded-xl border border-dashed border-line2 bg-base/25 px-3 py-4">
            <p className="text-sm text-mist">右侧反推后会显示可迁移的风格维度。</p>
            <p className="mt-1 text-xs text-fog">也可以直接在上方补充产品卖点后生成。</p>
          </div>
        )}
      </div>
    </div>
  );
}

function DefaultPromptPanel({
  prompt,
  placeholder,
  promptLibraryOpen,
  onPromptChange,
  onPromptDirty,
  onTogglePromptLibrary,
  onOptimizePrompt,
  canOptimizePrompt,
  optimizingPrompt,
  onClearWorkspace,
  canClearWorkspace,
  onSubmitPreview,
}) {
  return (
    <>
      <textarea
        className="textarea h-36 resize-none border-0 bg-transparent px-1 text-[15px] focus:ring-0 lg:h-40"
        placeholder={placeholder}
        value={prompt}
        onChange={(e) => {
          onPromptChange(e.target.value);
          onPromptDirty(true);
        }}
        onKeyDown={(e) => {
          if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
            e.preventDefault();
            onSubmitPreview();
          }
        }}
      />
      <div className="mt-1.5 flex flex-wrap gap-1.5 border-t border-line pt-2.5">
        {EXAMPLES.map((ex, i) => (
          <button
            key={i}
            type="button"
            onClick={() => {
              onPromptChange(ex);
              onPromptDirty(true);
            }}
            className="chip"
            title={ex}
          >
            ✦ {ex.slice(0, 12)}…
          </button>
        ))}
        <button
          type="button"
          onClick={onTogglePromptLibrary}
          className={`chip ${promptLibraryOpen ? "chip-active" : ""}`}
        >
          提示词库
        </button>
        <button
          type="button"
          onClick={onOptimizePrompt}
          disabled={!canOptimizePrompt || optimizingPrompt}
          className="chip disabled:cursor-not-allowed disabled:opacity-45"
          title="使用独立模型优化当前直接输入的提示词"
        >
          {optimizingPrompt ? "优化中…" : "✦ 优化提示词"}
        </button>
        <button
          type="button"
          onClick={onClearWorkspace}
          disabled={!canClearWorkspace}
          className="chip disabled:cursor-not-allowed disabled:opacity-45"
          title="清空提示词、上传素材和反推结果"
        >
          ✕ 清空
        </button>
      </div>
    </>
  );
}

export default function StudioPromptWorkspace({
  category,
  creationModeLabel,
  isEditMode,
  isImageEditMode,
  subjectMode,
  portraitGenerationMode,
  productGenerationMode,
  prompt,
  placeholder,
  promptLibraryOpen,
  readySteps,
  editStyleKeys,
  onPromptChange,
  onPromptDirty,
  onAppendPrompt,
  onTogglePromptLibrary,
  onOptimizePrompt,
  canOptimizePrompt,
  optimizingPrompt,
  onClearWorkspace,
  canClearWorkspace,
  onSubjectModeChange,
  onRecompose,
  onSubmitPreview,
}) {
  if (!isEditMode) {
    return (
      <DefaultPromptPanel
        prompt={prompt}
        placeholder={placeholder}
        promptLibraryOpen={promptLibraryOpen}
        onPromptChange={onPromptChange}
        onPromptDirty={onPromptDirty}
        onTogglePromptLibrary={onTogglePromptLibrary}
        onOptimizePrompt={onOptimizePrompt}
        canOptimizePrompt={canOptimizePrompt}
        optimizingPrompt={optimizingPrompt}
        onClearWorkspace={onClearWorkspace}
        canClearWorkspace={canClearWorkspace}
        onSubmitPreview={onSubmitPreview}
      />
    );
  }

  return (
    <div className="grid gap-3">
      <EditBrief
        category={category}
        isImageEditMode={isImageEditMode}
        subjectMode={subjectMode}
        portraitGenerationMode={portraitGenerationMode}
        productGenerationMode={productGenerationMode}
        readySteps={readySteps}
        creationModeLabel={creationModeLabel}
        onSubjectModeChange={onSubjectModeChange}
      />

      <EditPromptPanel
        prompt={prompt}
        placeholder={placeholder}
        promptLibraryOpen={promptLibraryOpen}
        isImageEditMode={isImageEditMode}
        portraitGenerationMode={portraitGenerationMode}
        productGenerationMode={productGenerationMode}
        onPromptChange={onPromptChange}
        onPromptDirty={onPromptDirty}
        onAppendPrompt={onAppendPrompt}
        onTogglePromptLibrary={onTogglePromptLibrary}
        onOptimizePrompt={onOptimizePrompt}
        canOptimizePrompt={canOptimizePrompt}
        optimizingPrompt={optimizingPrompt}
        onClearWorkspace={onClearWorkspace}
        canClearWorkspace={canClearWorkspace}
        onSubmitPreview={onSubmitPreview}
      />

      <EditAssistPanels
        isImageEditMode={isImageEditMode}
        portraitGenerationMode={portraitGenerationMode}
        productGenerationMode={productGenerationMode}
        editStyleKeys={editStyleKeys}
        onRecompose={onRecompose}
      />
    </div>
  );
}
