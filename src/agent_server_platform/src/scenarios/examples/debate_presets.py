# Debate preset configs — 观点论证推理 模式预设
# Keyed by config_id. A scenario created with config_id=341c3477-... resolves
# to the debate default config here; explicit config fields override the preset.
from typing import Any, Dict


# 341c3477-1adc-4f67-b896-525ee05f2191 — 默认辩论模式
DEBATE_PRESETS: Dict[str, Dict[str, Any]] = {
    "341c3477-1adc-4f67-b896-525ee05f2191": {
        "topic": "人工智能是否会取代人类的大部分工作",
        "rounds": 1,
        "timeout": 180,
    },
}


def resolve_preset(config_id: str) -> Dict[str, Any]:
    """Return a copy of the preset for config_id, or {} if unknown."""
    return dict(DEBATE_PRESETS.get(config_id, {}))
