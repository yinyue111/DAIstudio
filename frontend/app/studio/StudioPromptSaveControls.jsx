export default function StudioPromptSaveControls({
  visible,
  title,
  category,
  favorite,
  onTitleChange,
  onCategoryChange,
  onFavoriteChange,
  onSave,
}) {
  if (!visible) return null;

  return (
    <div className="mt-3 flex flex-wrap items-center justify-end gap-2 rounded-xl2 border border-line bg-white/[0.03] p-2">
      <input
        className="input min-w-0 flex-1 px-3 py-2 text-xs sm:max-w-[220px]"
        placeholder="保存标题（可选）"
        value={title}
        onChange={(event) => onTitleChange(event.target.value)}
      />
      <select
        className="input px-3 py-2 text-xs"
        value={category}
        onChange={(event) => onCategoryChange(event.target.value)}
        aria-label="提示词分类"
      >
        <option value="image">图片</option>
        <option value="video">视频</option>
        <option value="general">通用</option>
      </select>
      <label className="chip cursor-pointer gap-1.5 px-3 py-2 text-xs">
        <input
          type="checkbox"
          checked={favorite}
          onChange={(event) => onFavoriteChange(event.target.checked)}
          className="h-3.5 w-3.5 accent-brand"
        />
        收藏
      </label>
      <button type="button" onClick={onSave} className="btn-secondary btn-sm">
        保存到我的提示词
      </button>
    </div>
  );
}
