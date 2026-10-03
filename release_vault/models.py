"""封存库数据类型定义。"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Dict, List

from .errors import EvidenceIntegrityError


@dataclass(frozen=True)
class Material:
    """一次封存携带的单个材料：标识、获批摘要与当前路径。"""

    material_id: str
    sha256: str
    source_path: str


@dataclass(frozen=True)
class Approval:
    """单个指定审阅人的批准记录（已由上层核验）。"""

    reviewer: str
    approved_at: str
    remark: str = ""


@dataclass(frozen=True)
class SealRequest:
    """封存申请：请求号 + 业务内容（项目、阶段、材料、批准记录）。"""

    request_id: str
    project_id: str
    stage: str
    materials: List[Material] = field(default_factory=list)
    approvals: List[Approval] = field(default_factory=list)


@dataclass(frozen=True)
class SealResult:
    """封存成功结果。"""

    project_id: str
    stage: str
    request_id: str
    sealed_at: str
    manifest: Dict


@dataclass(frozen=True)
class EvidencePackage:
    """读取到的完整证据：签批清单 + 逐字节读回材料的能力。"""

    project_id: str
    stage: str
    request_id: str
    sealed_at: str
    manifest: Dict
    _material_dir: str = field(compare=False, default="")

    def read_material(self, material_id: str) -> bytes:
        """逐字节读回已封存材料副本，并校验其 SHA-256 与签批清单一致。"""
        entries = {
            entry["material_id"]: entry
            for entry in self.manifest.get("materials", [])
        }
        entry = entries.get(material_id)
        if entry is None:
            raise EvidenceIntegrityError(f"签批清单中不存在材料: {material_id}")
        path = os.path.join(self._material_dir, entry["stored_name"])
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            raise EvidenceIntegrityError(
                f"材料副本缺失或不可读: {material_id}"
            ) from exc
        actual = hashlib.sha256(data).hexdigest()
        if actual != entry["sha256"]:
            raise EvidenceIntegrityError(
                f"材料副本摘要与签批清单不符: {material_id}"
            )
        return data
