"""阶段放行证据封存库主入口。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .errors import (
    ApprovalError,
    ConflictError,
    EvidenceIntegrityError,
    MaterialError,
    StageLockedError,
    UnknownStageError,
    VaultError,
)
from .models import EvidencePackage, SealRequest, SealResult
from .store import ReleaseStore

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fsync_dir(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_file(path: str) -> None:
    with open(path, "rb") as fh:
        os.fsync(fh.fileno())


class EvidenceVault:
    """阶段放行证据封存库。

    放行状态保存在 SQLite，封存材料副本与签批清单保存在本地目录。
    目录布局::

        <root>/releases.db
        <root>/projects/<project>/evidence/<stage>/manifest.json
        <root>/projects/<project>/evidence/<stage>/materials/<副本>
        <root>/projects/<project>/staging/<stage>-<uuid>/   (封存中的暂存区)
    """

    def __init__(
        self,
        root_dir: str,
        stage_order: List[str],
        required_materials: Dict[str, List[str]],
        required_reviewers: Dict[str, List[str]],
    ) -> None:
        if not stage_order:
            raise VaultError("阶段顺序不能为空")
        self._root = os.path.abspath(root_dir)
        self._stage_order = list(stage_order)
        self._required_materials = {
            stage: list(ids) for stage, ids in required_materials.items()
        }
        self._required_reviewers = {
            stage: list(names) for stage, names in required_reviewers.items()
        }
        os.makedirs(self._root, exist_ok=True)
        self._store = ReleaseStore(os.path.join(self._root, "releases.db"))
        self._recover_interrupted()

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> "EvidenceVault":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 崩溃恢复：打开时清理未完成的暂存区与未登记的证据目录。
    # 只有写入了放行记录的阶段才算已放行，其余半成品整体撤回。
    # ------------------------------------------------------------------
    def _recover_interrupted(self) -> None:
        projects_dir = os.path.join(self._root, "projects")
        if not os.path.isdir(projects_dir):
            return
        for project_id in os.listdir(projects_dir):
            project_dir = os.path.join(projects_dir, project_id)
            if not os.path.isdir(project_dir):
                continue
            staging_dir = os.path.join(project_dir, "staging")
            if os.path.isdir(staging_dir):
                shutil.rmtree(staging_dir, ignore_errors=True)
            evidence_dir = os.path.join(project_dir, "evidence")
            if not os.path.isdir(evidence_dir):
                continue
            for stage in os.listdir(evidence_dir):
                if self._store.get_release(project_id, stage) is None:
                    shutil.rmtree(
                        os.path.join(evidence_dir, stage), ignore_errors=True
                    )

    # ------------------------------------------------------------------
    # 封存
    # ------------------------------------------------------------------
    def seal(self, request: SealRequest) -> SealResult:
        """申请封存一个阶段。幂等：相同请求号+相同业务内容返回原结果。"""
        if request.stage not in self._stage_order:
            raise UnknownStageError(f"未知阶段: {request.stage}")
        content_hash = self._content_hash(request)

        existing = self._store.get_release(request.project_id, request.stage)
        if existing is not None:
            return self._replay_existing(existing, request, content_hash)
        other = self._store.find_by_request_id(
            request.project_id, request.request_id
        )
        if other is not None:
            raise ConflictError(
                f"请求号 {request.request_id} 已用于阶段 {other['stage']}"
            )

        self._check_prerequisites(request)
        self._validate_request(request)

        sealed_at = _utc_now()
        manifest = self._build_manifest(request, content_hash, sealed_at)
        final_dir = self._evidence_dir(request.project_id, request.stage)
        staging_dir = self._staging_dir(request.project_id, request.stage)
        try:
            self._materialize(request, manifest, staging_dir, final_dir)
            self._store.insert_release(
                request.project_id,
                request.stage,
                request.request_id,
                content_hash,
                os.path.join(final_dir, "manifest.json"),
                sealed_at,
            )
        except BaseException:
            shutil.rmtree(staging_dir, ignore_errors=True)
            if self._store.get_release(request.project_id, request.stage) is None:
                shutil.rmtree(final_dir, ignore_errors=True)
            raise
        return SealResult(
            project_id=request.project_id,
            stage=request.stage,
            request_id=request.request_id,
            sealed_at=sealed_at,
            manifest=manifest,
        )

    def _replay_existing(
        self, existing: dict, request: SealRequest, content_hash: str
    ) -> SealResult:
        if existing["request_id"] != request.request_id:
            raise ConflictError(
                f"阶段 {request.stage} 已放行，不允许修改或重复放行"
            )
        if existing["content_hash"] != content_hash:
            raise ConflictError(
                f"请求号 {request.request_id} 携带的业务内容与已放行记录不一致"
            )
        package = self.get_release(request.project_id, request.stage)
        if package is None:
            raise EvidenceIntegrityError(
                f"阶段 {request.stage} 的放行记录存在但证据不可读"
            )
        return SealResult(
            project_id=package.project_id,
            stage=package.stage,
            request_id=package.request_id,
            sealed_at=package.sealed_at,
            manifest=package.manifest,
        )

    def _check_prerequisites(self, request: SealRequest) -> None:
        index = self._stage_order.index(request.stage)
        for prior in self._stage_order[:index]:
            if self._store.get_release(request.project_id, prior) is None:
                raise StageLockedError(
                    f"前序阶段 {prior} 未封存，不能申请阶段 {request.stage}"
                )

    def _validate_request(self, request: SealRequest) -> None:
        required = set(self._required_materials.get(request.stage, []))
        provided = [m.material_id for m in request.materials]
        if len(provided) != len(set(provided)):
            raise MaterialError("材料标识重复")
        if set(provided) != required:
            missing = sorted(required - set(provided))
            extra = sorted(set(provided) - required)
            raise MaterialError(
                f"材料集合与必交清单不符, 缺失: {missing}, 多余: {extra}"
            )
        required_reviewers = set(self._required_reviewers.get(request.stage, []))
        approved = {a.reviewer for a in request.approvals}
        if not required_reviewers <= approved:
            missing = sorted(required_reviewers - approved)
            raise ApprovalError(f"缺少指定审阅人的批准记录: {missing}")

    @staticmethod
    def _content_hash(request: SealRequest) -> str:
        business = {
            "project_id": request.project_id,
            "stage": request.stage,
            "materials": sorted(
                ({"material_id": m.material_id, "sha256": m.sha256}
                 for m in request.materials),
                key=lambda m: m["material_id"],
            ),
            "approvals": sorted(
                ({
                    "reviewer": a.reviewer,
                    "approved_at": a.approved_at,
                    "remark": a.remark,
                } for a in request.approvals),
                key=lambda a: (a["reviewer"], a["approved_at"]),
            ),
        }
        payload = json.dumps(
            business, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _build_manifest(
        self, request: SealRequest, content_hash: str, sealed_at: str
    ) -> dict:
        materials = []
        used_names = set()
        for index, material in enumerate(request.materials):
            stored_name = f"{index:02d}-{_SAFE_NAME.sub('_', material.material_id)}"
            if stored_name in used_names:
                raise MaterialError(f"材料标识无法映射为唯一副本名: {material.material_id}")
            used_names.add(stored_name)
            materials.append(
                {
                    "material_id": material.material_id,
                    "sha256": material.sha256,
                    "stored_name": stored_name,
                }
            )
        return {
            "project_id": request.project_id,
            "stage": request.stage,
            "request_id": request.request_id,
            "content_hash": content_hash,
            "sealed_at": sealed_at,
            "materials": materials,
            "approvals": [
                {
                    "reviewer": a.reviewer,
                    "approved_at": a.approved_at,
                    "remark": a.remark,
                }
                for a in request.approvals
            ],
        }

    def _materialize(
        self, request: SealRequest, manifest: dict, staging_dir: str, final_dir: str
    ) -> None:
        """把材料副本与签批清单写入暂存区，校验后原子改名为正式目录。"""
        materials_dir = os.path.join(staging_dir, "materials")
        os.makedirs(materials_dir)
        by_id = {m.material_id: m for m in request.materials}
        for entry in manifest["materials"]:
            material = by_id[entry["material_id"]]
            self._copy_material(
                material.source_path,
                os.path.join(materials_dir, entry["stored_name"]),
                material.sha256,
                entry["material_id"],
            )
        manifest_path = os.path.join(staging_dir, "manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        _fsync_dir(materials_dir)
        _fsync_dir(staging_dir)
        os.makedirs(os.path.dirname(final_dir), exist_ok=True)
        if os.path.isdir(final_dir):
            shutil.rmtree(final_dir)
        os.rename(staging_dir, final_dir)
        _fsync_dir(os.path.dirname(final_dir))

    @staticmethod
    def _copy_material(
        source_path: str, dest_path: str, expected_sha256: str, material_id: str
    ) -> None:
        digest = hashlib.sha256()
        try:
            with open(source_path, "rb") as src:
                with open(dest_path, "wb") as dst:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                        dst.write(chunk)
                    dst.flush()
                    os.fsync(dst.fileno())
        except OSError as exc:
            raise MaterialError(f"材料不可读: {material_id} ({source_path})") from exc
        if digest.hexdigest() != expected_sha256:
            raise MaterialError(
                f"材料实际内容与获批摘要不符: {material_id}"
            )

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def get_release(self, project_id: str, stage: str) -> Optional[EvidencePackage]:
        """读取某阶段证据：要么完整可读，要么返回 None（未放行）。"""
        record = self._store.get_release(project_id, stage)
        if record is None:
            return None
        manifest_path = record["manifest_path"]
        try:
            with open(manifest_path, "r", encoding="utf-8") as fh:
                manifest = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            raise EvidenceIntegrityError(
                f"签批清单缺失或损坏: {project_id}/{stage}"
            ) from exc
        material_dir = os.path.join(os.path.dirname(manifest_path), "materials")
        for entry in manifest.get("materials", []):
            path = os.path.join(material_dir, entry["stored_name"])
            if not os.path.isfile(path):
                raise EvidenceIntegrityError(
                    f"材料副本缺失: {entry['material_id']}"
                )
            if self._sha256_of(path) != entry["sha256"]:
                raise EvidenceIntegrityError(
                    f"材料副本摘要与签批清单不符: {entry['material_id']}"
                )
        return EvidencePackage(
            project_id=project_id,
            stage=stage,
            request_id=record["request_id"],
            sealed_at=record["sealed_at"],
            manifest=manifest,
            _material_dir=material_dir,
        )

    def released_stages(self, project_id: str) -> List[str]:
        """按固定阶段顺序返回已放行阶段（构成连续前缀）。"""
        released = {r["stage"] for r in self._store.list_releases(project_id)}
        return [stage for stage in self._stage_order if stage in released]

    # ------------------------------------------------------------------
    # 路径与校验工具
    # ------------------------------------------------------------------
    def _project_dir(self, project_id: str) -> str:
        return os.path.join(self._root, "projects", project_id)

    def _evidence_dir(self, project_id: str, stage: str) -> str:
        return os.path.join(self._project_dir(project_id), "evidence", stage)

    def _staging_dir(self, project_id: str, stage: str) -> str:
        return os.path.join(
            self._project_dir(project_id),
            "staging",
            f"{stage}-{uuid.uuid4().hex}",
        )

    @staticmethod
    def _sha256_of(path: str) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
