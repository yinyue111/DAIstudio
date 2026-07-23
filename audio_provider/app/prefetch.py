from .config import ProviderSettings
from .inference import FasterWhisperEngine


def main() -> None:
    engine = FasterWhisperEngine(ProviderSettings.from_env())
    engine.warmup()
    if engine.health().get("status") != "available":
        raise SystemExit("failed to load local ASR model")


if __name__ == "__main__":
    main()
