"""模型可见文字的单一来源。

C3 第 5 节要求 L1/L2/L3 各有单一来源且不得互相内联。本包目前只收敛 L1
（调查纪律）；L3 工具描述待 ``opspilot/tools/registry.py`` 进 main 后另立模块。
"""

from .discipline import (
    VARIANTS,
    Segment,
    UnknownVariantError,
    discipline_revision,
    prompt_face_sha256,
    prompt_revision,
    render,
    template_projection,
)

__all__ = [
    "VARIANTS",
    "Segment",
    "UnknownVariantError",
    "discipline_revision",
    "prompt_face_sha256",
    "prompt_revision",
    "render",
    "template_projection",
]
