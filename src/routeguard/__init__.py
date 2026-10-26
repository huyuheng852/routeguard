"""RouteGuard：可信骑行路书的多智能体环境风险裁决。

骨架版本。数据层用合成数据，算法层已可运行、可单测、可复现。
"""

__version__ = "0.9.4"

from .schema import (  # noqa: F401
    Evidence,
    Finding,
    RouteReport,
    Segment,
    SegmentVerdict,
)
