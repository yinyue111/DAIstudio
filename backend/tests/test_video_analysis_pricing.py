"""视频反推档位定价与文案一致性。

四档默认同价（按次计费，与帧预算解耦），因此档位描述不得暗示价格差异；
真实价格以 preset_options 的 max_cost 下发，管理员可在模型目录按档位覆盖。
"""
from app.services.generation_pricing import (
    REVERSE_AUDIO_SURCHARGE_COST,
    REVERSE_VIDEO_PRESET_COSTS,
    public_pricing_config,
    reverse_cost,
)
from app.services.video_analysis import (
    VIDEO_ANALYSIS_PRESETS,
    frame_count_for_duration,
    max_frame_count,
    preset_options,
)

PRICE_CLAIM_WORDS = ("便宜", "低价", "更贵", "省钱")


def test_preset_descriptions_do_not_claim_price_tiers_while_costs_are_uniform():
    costs = set(REVERSE_VIDEO_PRESET_COSTS.values())
    # 默认价目表四档同价；若未来真的分级，本断言应随价目表一起调整。
    assert costs == {5}
    for preset in VIDEO_ANALYSIS_PRESETS.values():
        for word in PRICE_CLAIM_WORDS:
            assert word not in preset.description, (
                f"档位 {preset.key} 的描述宣称价格差异（{word}），但四档默认同价"
            )
            assert word not in preset.label


def test_preset_options_expose_real_cost_per_tier():
    options = preset_options()
    assert [item["key"] for item in options] == ["fast", "standard", "fine", "ultra"]
    for item in options:
        # 用户看到的 max_cost 必须来自真实价目表（含管理员覆盖入口）。
        assert item["max_cost"] == REVERSE_VIDEO_PRESET_COSTS[item["key"]]
    # 管理员按档位覆盖后，下发的价格必须跟随覆盖值。
    overridden = preset_options(preset_costs={"fast": 3, "ultra": 12})
    by_key = {item["key"]: item for item in overridden}
    assert by_key["fast"]["max_cost"] == 3
    assert by_key["ultra"]["max_cost"] == 12
    assert by_key["standard"]["max_cost"] == REVERSE_VIDEO_PRESET_COSTS["standard"]


def test_reverse_cost_matches_preset_table_and_audio_surcharge():
    for key, cost in REVERSE_VIDEO_PRESET_COSTS.items():
        assert reverse_cost("video", preset=key) == cost
        assert (
            reverse_cost("video", preset=key, include_audio=True)
            == cost + REVERSE_AUDIO_SURCHARGE_COST
        )
    # 未知档位回退 standard，避免报价与结算不一致。
    assert reverse_cost("video", preset="unknown") == REVERSE_VIDEO_PRESET_COSTS["standard"]
    # 公开定价配置与价目表一致（/api/config 的 pricing.reverse）。
    assert (
        public_pricing_config()["reverse"]["video_preset_costs"]
        == REVERSE_VIDEO_PRESET_COSTS
    )


def test_frame_budget_scales_with_preset_even_though_price_does_not():
    budgets = [max_frame_count(key) for key in ("fast", "standard", "fine", "ultra")]
    assert budgets == sorted(budgets)
    assert budgets[0] < budgets[-1]
    for key, preset in VIDEO_ANALYSIS_PRESETS.items():
        assert frame_count_for_duration(10, key) == preset.min_frames
        assert frame_count_for_duration(None, key) == preset.max_frames
