import Nav from "../../components/Nav";
import StudioAssetPickerDialog from "./StudioAssetPickerDialog";
import StudioCreationConsole from "./StudioCreationConsole";
import StudioResultsSection from "./StudioResultsSection";
import StudioSubmitBar from "./StudioSubmitBar";

export default function StudioWorkspaceView({
  me,
  gatewayStatus,
  creationController,
  resultsController,
  assetPickerController,
  submitBarProps,
}) {
  return (
    <div className="min-h-screen">
      <Nav me={me} active="studio" />

      <main className="mx-auto max-w-7xl overflow-x-hidden px-3 pb-28 pt-7 sm:px-6 sm:pb-24 sm:pt-10">
        <section className="mx-auto mb-6 max-w-3xl text-center animate-fadeup sm:mb-8">
          <div className="mb-4 inline-flex items-center gap-2 rounded-full border border-line bg-white/5 px-3 py-1 text-xs text-mist">
            <span className="h-1.5 w-1.5 rounded-full bg-aqua animate-glowpulse" />
            {gatewayStatus}
          </div>
          <h1 className="text-3xl font-extrabold leading-tight sm:text-5xl">
            一句话，<span className="text-grad">生成你的画面</span>
          </h1>
          <p className="mt-3 text-[15px] text-mist">
            输入提示词即刻生成，或用风格参考 + 产品主体做同款广告素材。
          </p>
        </section>

        <StudioCreationConsole controller={creationController} />
        <StudioResultsSection {...resultsController} />
      </main>
      <StudioAssetPickerDialog {...assetPickerController} />
      <StudioSubmitBar variant="mobile" {...submitBarProps} />
    </div>
  );
}
