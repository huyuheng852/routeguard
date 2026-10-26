"""路径工具：包内数据与默认输出目录。

打包成 wheel 后，示例数据在 `routeguard/data/` 里随包分发；
用户运行时的产物默认落在**当前工作目录**的 `out/`，而不是安装目录。
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path


def package_data(*parts: str) -> Path:
    """返回包内 data 目录下的文件路径（对 wheel 安装同样有效）。"""
    return Path(str(files("routeguard").joinpath("data", *parts)))


def sample_guide() -> Path:
    return package_data("sample", "route_guide.md")


def sample_gpx() -> Path:
    return package_data("sample", "demo_route.gpx")


def osm_fixture() -> Path:
    return package_data("fixtures", "osm_sample.json")


def default_out(name: str) -> Path:
    """默认输出到当前目录的 out/<name>，避免往 site-packages 里写文件。"""
    return Path.cwd() / "out" / name
