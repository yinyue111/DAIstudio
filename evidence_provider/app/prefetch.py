from .config import ProviderSettings
from .inference import CAPABILITIES, TorchvisionEvidenceEngine


def main() -> None:
    engine = TorchvisionEvidenceEngine(ProviderSettings.from_env())
    engine.warmup()
    unavailable = [
        capability
        for capability in CAPABILITIES
        if engine.health(capability).get("status") != "available"
    ]
    if unavailable:
        raise SystemExit(
            f"failed to prefetch evidence models: {', '.join(unavailable)}"
        )


if __name__ == "__main__":
    main()
