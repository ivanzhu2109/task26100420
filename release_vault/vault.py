"""阶段放行证据封存库主入口。"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from .errors import (
    ApprovalIncompleteError,
    DigestMismatchError,
    MaterialMissingError,
    StageAlreadySealedError,
    StageOrderError,
    UnknownStageError,
    VaultError,
)
from .models import (
    ApprovalRecord,
    EvidenceView,
    SealRequest,
    SealResult,
    SealedMaterial,
    StageSpec,
)
from .store import ReleaseRow, ReleaseStore

_DB_NAME = "releases.db"
_EVIDENCE_DIR = "evidence"
_STAGING_DIR = "staging"
_MATERIALS_SUBDIR = "materials"
_MANIFEST_NAME = "manifest.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_safe_name(kind: str, value: str) -> None:
    if (
        not value
        or value in (".", "..")
        or "/" in value
        or "\\" in value
        or os.sep in value
    ):
        raise VaultError(f"非法{kind}: {value!r}")


def _hash_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _business_hash(
    project_id: str,
    stage: str,
    materials: Sequence[SealedMaterial],
    approvals: Sequence[ApprovalRecord],
) -> str:
    """业务内容摘要：项目、阶段、材料标识及摘要、批准记录（不含原路径与请求号）。"""
    payload = {
        "project_id": project_id,
        "stage": stage,
        "materials": sorted(
            ({"material_id": m.material_id, "sha256": m.sha256} for m in materials),
            key=lambda item: item["material_id"],
        ),
        "approvals": sorted(
            (
                {
                    "reviewer": a.reviewer,
                    "decision": a.decision,
                    "approved_sha256": a.approved_sha256,
                    "approved_at": a.approved_at,
                    "comment": a.comment,
                }
                for a in approvals
            ),
            key=lambda item: (
                item["reviewer"],
                item["approved_sha256"],
                item["approved_at"],
            ),
        ),
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


class ReleaseVault:
    """阶段放行证据封存库。

    放行状态保存在 SQLite，封存材料副本与 JSON 签批清单保存在本地目录。
    """

    def __init__(
        self,
        root_dir: str,
        project_id: str,
        stages: Sequence[StageSpec],
    ) -> None:
        if not stages:
            raise VaultError("阶段顺序不能为空")
        _check_safe_name("项目标识", project_id)
        seen = set()
        for spec in stages:
            _check_safe_name("阶段名", spec.name)
            if spec.name in seen:
                raise VaultError(f"阶段重复: {spec.name!r}")
            seen.add(spec.name)
            for material_id in spec.required_materials:
                _check_safe_name("材料标识", material_id)

        self._root_dir = os.path.abspath(root_dir)
        self._project_id = project_id
        self._stage_order = [spec.name for spec in stages]
        self._specs: Dict[str, StageSpec] = {spec.name: spec for spec in stages}

        os.makedirs(self._root_dir, exist_ok=True)
        os.makedirs(self._evidence_root(), exist_ok=True)
        self._store = ReleaseStore(os.path.join(self._root_dir, _DB_NAME))
        self._recover_interrupted()

    # ------------------------------------------------------------------ 路径

    def _evidence_root(self) -> str:
        return os.path.join(self._root_dir, _EVIDENCE_DIR, self._project_id)

    def _staging_root(self) -> str:
        return os.path.join(self._root_dir, _STAGING_DIR)

    def _final_evidence_dir(self, stage: str) -> str:
        return os.path.join(self._evidence_root(), stage)

    # -------------------------------------------------------------- 崩溃恢复

    def _recover_interrupted(self) -> None:
        """清理中断封存留下的痕迹：暂存区与未提交放行记录的孤儿证据目录。"""
        shutil.rmtree(self._staging_root(), ignore_errors=True)
        evidence_root = self._evidence_root()
        if not os.path.isdir(evidence_root):
            return
        for entry in os.listdir(evidence_root):
            candidate = os.path.join(evidence_root, entry)
            if not os.path.isdir(candidate):
                continue
            if self._store.get_release(self._project_id, entry) is None:
                shutil.rmtree(candidate, ignore_errors=True)

    # ---------------------------------------------------------------- 封存

    def seal(self, request: SealRequest) -> SealResult:
        """申请封存并放行一个阶段。"""
        if request.project_id != self._project_id:
            raise VaultError(
                f"请求项目 {request.project_id!r} 与库项目 {self._project_id!r} 不一致"
            )
        stage = request.stage
        if stage not in self._specs:
            raise UnknownStageError(f"未知阶段: {stage!r}")

        materials = tuple(
            SealedMaterial(
                material_id=m.material_id,
                sha256=m.approved_sha256.lower(),
                stored_path=os.path.join(_MATERIALS_SUBDIR, m.material_id),
            )
            for m in request.materials
        )
        digest = _business_hash(
            self._project_id, stage, materials, request.approvals
        )

        existing = self._store.get_release(self._project_id, stage)
        if existing is not None:
            if existing.business_hash == digest:
                return self._result_from_row(existing, already_sealed=True)
            raise StageAlreadySealedError(
                f"阶段 {stage!r} 已放行，业务内容不一致，不能重复封存"
            )

        self._check_predecessors(stage)
        self._validate_request(request)

        sealed_at = _utc_now()
        staging_dir = self._copy_materials(request, materials)
        try:
            manifest_path = self._write_manifest(
                staging_dir, request, materials, sealed_at, digest
            )
            final_dir = self._final_evidence_dir(stage)
            if os.path.exists(final_dir):
                shutil.rmtree(final_dir)
            os.rename(staging_dir, final_dir)
            _fsync_dir(self._evidence_root())
        except BaseException:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise

        row = ReleaseRow(
            project_id=self._project_id,
            stage=stage,
            request_id=request.request_id,
            business_hash=digest,
            sealed_at=sealed_at,
            manifest_path=os.path.join(final_dir, _MANIFEST_NAME),
            evidence_dir=final_dir,
        )
        try:
            self._store.insert_release(row)
        except Exception:
            shutil.rmtree(final_dir, ignore_errors=True)
            raise
        return self._result_from_row(row, already_sealed=False)

    def _check_predecessors(self, stage: str) -> None:
        index = self._stage_order.index(stage)
        for predecessor in self._stage_order[:index]:
            if self._store.get_release(self._project_id, predecessor) is None:
                raise StageOrderError(
                    f"前序阶段 {predecessor!r} 未封存，不能放行 {stage!r}"
                )

    def _validate_request(self, request: SealRequest) -> None:
        spec = self._specs[request.stage]
        required = set(spec.required_materials)
        submitted: Dict[str, str] = {}
        for material in request.materials:
            _check_safe_name("材料标识", material.material_id)
            if material.material_id in submitted:
                raise MaterialMissingError(
                    f"材料重复提交: {material.material_id!r}"
                )
            submitted[material.material_id] = material.approved_sha256.lower()
        missing = required - set(submitted)
        if missing:
            raise MaterialMissingError(f"缺少必交材料: {sorted(missing)}")
        extra = set(submitted) - required
        if extra:
            raise MaterialMissingError(f"提交了非必交材料: {sorted(extra)}")

        approved_digests = set(submitted.values())
        approved_materials = set()
        for approval in request.approvals:
            digest = approval.approved_sha256.lower()
            if digest not in approved_digests:
                raise ApprovalIncompleteError(
                    f"批准记录摘要 {digest!r} 不对应任何提交材料"
                )
            approved_materials.add(digest)
        if approved_materials != approved_digests:
            missing_approval = approved_digests - approved_materials
            raise ApprovalIncompleteError(
                f"以下材料缺少批准记录: {sorted(missing_approval)}"
            )

    def _copy_materials(
        self, request: SealRequest, materials: Sequence[SealedMaterial]
    ) -> str:
        staging_dir = os.path.join(self._staging_root(), uuid.uuid4().hex)
        materials_dir = os.path.join(staging_dir, _MATERIALS_SUBDIR)
        os.makedirs(materials_dir)
        try:
            by_id = {m.material_id: m for m in request.materials}
            for sealed in materials:
                source_path = by_id[sealed.material_id].source_path
                if not os.path.isfile(source_path):
                    raise MaterialMissingError(
                        f"材料源文件不可读: {source_path!r}"
                    )
                actual = _hash_file(source_path)
                if actual != sealed.sha256:
                    raise DigestMismatchError(
                        f"材料 {sealed.material_id!r} 实际摘要 {actual} "
                        f"与获批摘要 {sealed.sha256} 不符"
                    )
                target = os.path.join(staging_dir, sealed.stored_path)
                shutil.copyfile(source_path, target)
                with open(target, "rb") as handle:
                    os.fsync(handle.fileno())
            _fsync_dir(materials_dir)
        except BaseException:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise
        return staging_dir

    def _write_manifest(
        self,
        staging_dir: str,
        request: SealRequest,
        materials: Sequence[SealedMaterial],
        sealed_at: str,
        digest: str,
    ) -> str:
        manifest = {
            "request_id": request.request_id,
            "project_id": self._project_id,
            "stage": request.stage,
            "sealed_at": sealed_at,
            "business_hash": digest,
            "materials": [
                {
                    "material_id": m.material_id,
                    "sha256": m.sha256,
                    "stored_path": m.stored_path,
                }
                for m in materials
            ],
            "approvals": [
                {
                    "reviewer": a.reviewer,
                    "decision": a.decision,
                    "approved_sha256": a.approved_sha256.lower(),
                    "approved_at": a.approved_at,
                    "comment": a.comment,
                }
                for a in request.approvals
            ],
        }
        manifest_path = os.path.join(staging_dir, _MANIFEST_NAME)
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_dir(staging_dir)
        return manifest_path

    # ---------------------------------------------------------------- 读取

    def _result_from_row(self, row: ReleaseRow, already_sealed: bool) -> SealResult:
        view = self._evidence_from_row(row)
        return SealResult(
            request_id=row.request_id,
            project_id=row.project_id,
            stage=row.stage,
            sealed_at=row.sealed_at,
            manifest_path=row.manifest_path,
            materials=view.materials,
            approvals=view.approvals,
            already_sealed=already_sealed,
        )

    def _evidence_from_row(self, row: ReleaseRow) -> EvidenceView:
        manifest_path = row.manifest_path
        if not os.path.isfile(manifest_path):
            raise VaultError(f"签批清单缺失: {manifest_path!r}")
        with open(manifest_path, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        materials = []
        for entry in manifest["materials"]:
            stored = os.path.join(row.evidence_dir, entry["stored_path"])
            if not os.path.isfile(stored):
                raise VaultError(f"材料副本缺失: {stored!r}")
            actual = _hash_file(stored)
            if actual != entry["sha256"]:
                raise VaultError(
                    f"材料副本 {entry['material_id']!r} 摘要与签批清单不符"
                )
            materials.append(
                SealedMaterial(
                    material_id=entry["material_id"],
                    sha256=entry["sha256"],
                    stored_path=stored,
                )
            )
        approvals = tuple(
            ApprovalRecord(
                reviewer=a["reviewer"],
                decision=a["decision"],
                approved_sha256=a["approved_sha256"],
                approved_at=a["approved_at"],
                comment=a.get("comment", ""),
            )
            for a in manifest["approvals"]
        )
        return EvidenceView(
            project_id=row.project_id,
            stage=row.stage,
            sealed_at=row.sealed_at,
            manifest_path=manifest_path,
            materials=tuple(materials),
            approvals=approvals,
        )

    def get_evidence(self, stage: str) -> Optional[EvidenceView]:
        """读取某阶段的完整证据；未放行返回 None。"""
        row = self._store.get_release(self._project_id, stage)
        if row is None:
            return None
        return self._evidence_from_row(row)

    def released_stages(self) -> List[str]:
        """按阶段顺序返回已放行阶段（连续前缀）。"""
        released = []
        for stage in self._stage_order:
            if self._store.get_release(self._project_id, stage) is None:
                break
            released.append(stage)
        return released

    def release_count(self) -> int:
        return self._store.count_releases(self._project_id)

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> "ReleaseVault":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
