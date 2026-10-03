"""阶段放行证据封存库的数据类型定义。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class StageSpec:
    """单个阶段的定义：阶段名与必交材料标识清单。"""

    name: str
    required_materials: Tuple[str, ...]


@dataclass(frozen=True)
class MaterialSubmission:
    """一次封存提交中的单个材料。"""

    material_id: str
    source_path: str
    approved_sha256: str


@dataclass(frozen=True)
class ApprovalRecord:
    """指定审阅人的批准记录（已由上层核验）。"""

    reviewer: str
    decision: str
    approved_sha256: str
    approved_at: str
    comment: str = ""


@dataclass(frozen=True)
class SealRequest:
    """封存请求：请求号 + 项目 + 阶段 + 材料 + 全部批准记录。"""

    request_id: str
    project_id: str
    stage: str
    materials: Tuple[MaterialSubmission, ...]
    approvals: Tuple[ApprovalRecord, ...]


@dataclass(frozen=True)
class SealedMaterial:
    """已封存材料：标识、摘要与库内副本路径。"""

    material_id: str
    sha256: str
    stored_path: str


@dataclass(frozen=True)
class SealResult:
    """封存结果：放行记录 + 证据清单位置 + 材料副本清单。"""

    request_id: str
    project_id: str
    stage: str
    sealed_at: str
    manifest_path: str
    materials: Tuple[SealedMaterial, ...]
    approvals: Tuple[ApprovalRecord, ...]
    already_sealed: bool = False


@dataclass(frozen=True)
class EvidenceView:
    """读取已放行阶段证据的视图。"""

    project_id: str
    stage: str
    sealed_at: str
    manifest_path: str
    materials: Tuple[SealedMaterial, ...]
    approvals: Tuple[ApprovalRecord, ...]
