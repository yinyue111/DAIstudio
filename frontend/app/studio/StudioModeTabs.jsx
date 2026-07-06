"use client";

export default function StudioModeTabs({
  modes,
  activeMode,
  isModelEnabled,
  onModeChange,
}) {
  return (
    <div className="mb-2.5 grid grid-cols-2 gap-1 rounded-2xl border border-line bg-base2/50 p-1 text-sm sm:grid-cols-4">
      {modes.map(({ key, label, icon }) => {
        const disabled = !isModelEnabled(key);
        return (
          <button
            key={key}
            type="button"
            onClick={() => onModeChange(key)}
            disabled={disabled}
            title={disabled ? "模型未启用，请联系管理员配置" : ""}
            className={`rounded-xl px-3 py-2 font-display font-medium transition-all ${
              disabled
                ? "cursor-not-allowed text-fog opacity-45"
                : activeMode === key
                  ? "bg-brand text-white shadow-glow-sm"
                  : "text-mist hover:text-snow"
            }`}
          >
            <span className="mr-1.5">{icon}</span>
            {label}
          </button>
        );
      })}
    </div>
  );
}
