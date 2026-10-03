"""阶段放行证据封存库自动化测试。"""

import hashlib
import json
import os

import pytest

from release_vault import (
    Approval,
    ApprovalError,
    ConflictError,
    EvidenceIntegrityError,
    EvidenceVault,
    Material,
    MaterialError,
    SealRequest,
    StageLockedError,
    UnknownStageError,
)

STAGES = ["design", "build", "accept"]
REQUIRED_MATERIALS = {
    "design": ["spec", "drawing"],
    "build": ["report"],
    "accept": ["certificate"],
}
REQUIRED_REVIEWERS = {
    "design": ["alice", "bob"],
    "build": ["carol"],
    "accept": ["dave"],
}
APPROVALS = {
    "design": [Approval("alice", "2026-10-01T09:00:00Z"), Approval("bob", "2026-10-01T10:00:00Z", "ok")],
    "build": [Approval("carol", "2026-10-02T09:00:00Z")],
    "accept": [Approval("dave", "2026-10-03T09:00:00Z")],
}


def make_vault(root):
    return EvidenceVault(
        root_dir=str(root),
        stage_order=list(STAGES),
        required_materials=dict(REQUIRED_MATERIALS),
        required_reviewers=dict(REQUIRED_REVIEWERS),
    )


def write_materials(tmp_path, stage, tag=""):
    """为某阶段生成必交材料原文件，返回 Material 列表。"""
    src_dir = tmp_path / f"src-{stage}{tag}"
    src_dir.mkdir(exist_ok=True)
    materials = []
    for material_id in REQUIRED_MATERIALS[stage]:
        content = f"{stage}/{material_id}/{tag}".encode()
        path = src_dir / material_id
        path.write_bytes(content)
        materials.append(
            Material(
                material_id=material_id,
                sha256=hashlib.sha256(content).hexdigest(),
                source_path=str(path),
            )
        )
    return materials


def make_request(tmp_path, stage, request_id, project="proj-1", tag=""):
    return SealRequest(
        request_id=request_id,
        project_id=project,
        stage=stage,
        materials=write_materials(tmp_path, stage, tag),
        approvals=list(APPROVALS[stage]),
    )


def seal_through(vault, tmp_path, stage, project="proj-1"):
    """依次封存到指定阶段（含），返回各阶段 SealResult。"""
    results = {}
    for s in STAGES[: STAGES.index(stage) + 1]:
        results[s] = vault.seal(make_request(tmp_path, s, f"req-{project}-{s}", project))
    return results


class TestSealFlow:
    def test_seal_stages_in_order(self, tmp_path):
        vault = make_vault(tmp_path / "vault")
        results = seal_through(vault, tmp_path, "accept")
        assert vault.released_stages("proj-1") == STAGES
        for stage in STAGES:
            package = vault.get_release("proj-1", stage)
            assert package is not None
            assert package.manifest == results[stage].manifest
        vault.close()

    def test_first_stage_can_seal_directly(self, tmp_path):
        vault = make_vault(tmp_path / "vault")
        result = vault.seal(make_request(tmp_path, "design", "req-1"))
        assert result.stage == "design"
        assert vault.released_stages("proj-1") == ["design"]
        vault.close()

    def test_later_stage_blocked_until_prior_sealed(self, tmp_path):
        vault = make_vault(tmp_path / "vault")
        with pytest.raises(StageLockedError):
            vault.seal(make_request(tmp_path, "build", "req-1"))
        assert vault.get_release("proj-1", "build") is None
        vault.seal(make_request(tmp_path, "design", "req-2"))
        with pytest.raises(StageLockedError):
            vault.seal(make_request(tmp_path, "accept", "req-3"))
        vault.close()

    def test_unknown_stage_rejected(self, tmp_path):
        vault = make_vault(tmp_path / "vault")
        with pytest.raises(UnknownStageError):
            vault.seal(SealRequest("req-1", "proj-1", "nope"))
        vault.close()

    def test_released_stage_cannot_be_modified(self, tmp_path):
        vault = make_vault(tmp_path / "vault")
        vault.seal(make_request(tmp_path, "design", "req-1"))
        with pytest.raises(ConflictError):
            vault.seal(make_request(tmp_path, "design", "req-other"))
        assert vault.released_stages("proj-1") == ["design"]
        vault.close()


class TestValidation:
    def test_missing_material_rejected(self, tmp_path):
        vault = make_vault(tmp_path / "vault")
        request = make_request(tmp_path, "design", "req-1")
        request.materials.pop()
        with pytest.raises(MaterialError):
            vault.seal(request)
        assert vault.get_release("proj-1", "design") is None
        vault.close()

    def test_digest_mismatch_rejected(self, tmp_path):
        vault = make_vault(tmp_path / "vault")
        request = make_request(tmp_path, "design", "req-1")
        bad = request.materials[0]
        request.materials[0] = Material(bad.material_id, "0" * 64, bad.source_path)
        with pytest.raises(MaterialError):
            vault.seal(request)
        assert vault.get_release("proj-1", "design") is None
        vault.close()

    def test_missing_source_file_rejected(self, tmp_path):
        vault = make_vault(tmp_path / "vault")
        request = make_request(tmp_path, "design", "req-1")
        os.remove(request.materials[0].source_path)
        with pytest.raises(MaterialError):
            vault.seal(request)
        assert vault.get_release("proj-1", "design") is None
        vault.close()

    def test_missing_reviewer_rejected(self, tmp_path):
        vault = make_vault(tmp_path / "vault")
        request = make_request(tmp_path, "design", "req-1")
        request.approvals.pop()
        with pytest.raises(ApprovalError):
            vault.seal(request)
        assert vault.get_release("proj-1", "design") is None
        vault.close()

    def test_manifest_keeps_full_approvals_and_maps_copies(self, tmp_path):
        vault = make_vault(tmp_path / "vault")
        result = vault.seal(make_request(tmp_path, "design", "req-1"))
        manifest = result.manifest
        assert {a["reviewer"] for a in manifest["approvals"]} == {"alice", "bob"}
        evidence_dir = tmp_path / "vault" / "projects" / "proj-1" / "evidence" / "design"
        on_disk = json.loads((evidence_dir / "manifest.json").read_text())
        assert on_disk == manifest
        for entry in manifest["materials"]:
            copy_path = evidence_dir / "materials" / entry["stored_name"]
            assert copy_path.is_file()
            assert hashlib.sha256(copy_path.read_bytes()).hexdigest() == entry["sha256"]
        vault.close()


class TestIdempotency:
    def test_same_request_returns_original_result(self, tmp_path):
        vault = make_vault(tmp_path / "vault")
        request = make_request(tmp_path, "design", "req-1")
        first = vault.seal(request)
        second = vault.seal(request)
        assert second == first
        assert len(vault._store.list_releases("proj-1")) == 1
        vault.close()

    def test_retry_with_same_content_after_source_deleted(self, tmp_path):
        vault = make_vault(tmp_path / "vault")
        request = make_request(tmp_path, "design", "req-1")
        originals = {m.material_id: (m.source_path, open(m.source_path, "rb").read()) for m in request.materials}
        first = vault.seal(request)
        for path, _ in originals.values():
            os.remove(path)
        retry = vault.seal(request)
        assert retry == first
        assert len(vault._store.list_releases("proj-1")) == 1
        package = vault.get_release("proj-1", "design")
        for material_id, (_, content) in originals.items():
            assert package.read_material(material_id) == content
        vault.close()

    def test_same_request_id_different_content_conflicts(self, tmp_path):
        vault = make_vault(tmp_path / "vault")
        vault.seal(make_request(tmp_path, "design", "req-1"))
        changed = make_request(tmp_path, "design", "req-1", tag="changed")
        with pytest.raises(ConflictError):
            vault.seal(changed)
        vault.close()

    def test_request_id_reused_on_other_stage_conflicts(self, tmp_path):
        vault = make_vault(tmp_path / "vault")
        vault.seal(make_request(tmp_path, "design", "req-1"))
        with pytest.raises(ConflictError):
            vault.seal(make_request(tmp_path, "build", "req-1"))
        vault.close()


class TestCrashRecovery:
    def test_interrupted_seal_leaves_no_partial_state(self, tmp_path):
        root = tmp_path / "vault"
        vault = make_vault(root)
        design_result = vault.seal(make_request(tmp_path, "design", "req-design"))
        design_bytes = {
            m.material_id: open(m.source_path, "rb").read()
            for m in write_materials(tmp_path, "design")
        }

        original_copy = EvidenceVault._copy_material
        calls = {"n": 0}

        def crashing_copy(source_path, dest_path, expected_sha256, material_id):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated crash")
            return original_copy(source_path, dest_path, expected_sha256, material_id)

        vault._copy_material = crashing_copy
        build_request = make_request(tmp_path, "build", "req-build")
        with pytest.raises(RuntimeError):
            vault.seal(build_request)
        vault.close()

        # 重新打开：未成功返回的提交整体撤回，可重试
        reopened = make_vault(root)
        assert reopened.get_release("proj-1", "build") is None
        assert reopened.released_stages("proj-1") == ["design"]
        assert not (root / "projects" / "proj-1" / "staging").exists()

        result = reopened.seal(build_request)
        assert result.request_id == "req-build"
        assert len(reopened._store.list_releases("proj-1")) == 2
        assert reopened.released_stages("proj-1") == ["design", "build"]

        # 已成功返回的证据仍可完整读取
        package = reopened.get_release("proj-1", "design")
        assert package.manifest == design_result.manifest
        for material_id, content in design_bytes.items():
            data = package.read_material(material_id)
            assert data == content
            assert hashlib.sha256(data).hexdigest() == hashlib.sha256(content).hexdigest()
        reopened.close()

    def test_orphan_evidence_dir_rolled_back_on_open(self, tmp_path):
        root = tmp_path / "vault"
        vault = make_vault(root)
        vault.seal(make_request(tmp_path, "design", "req-design"))
        vault.close()

        # 模拟崩溃残留：无放行记录的正式目录与暂存目录
        orphan = root / "projects" / "proj-1" / "evidence" / "build"
        (orphan / "materials").mkdir(parents=True)
        (orphan / "materials" / "00-report").write_bytes(b"partial")
        staging = root / "projects" / "proj-1" / "staging" / "build-abc"
        staging.mkdir(parents=True)
        (staging / "junk").write_bytes(b"junk")

        reopened = make_vault(root)
        assert reopened.get_release("proj-1", "build") is None
        assert not orphan.exists()
        assert not (root / "projects" / "proj-1" / "staging").exists()
        # 撤回后允许重新申请
        reopened.seal(make_request(tmp_path, "build", "req-build"))
        assert reopened.released_stages("proj-1") == ["design", "build"]
        reopened.close()

    def test_released_stages_form_contiguous_prefix(self, tmp_path):
        root = tmp_path / "vault"
        vault = make_vault(root)
        seal_through(vault, tmp_path, "build")
        vault.close()
        reopened = make_vault(root)
        released = reopened.released_stages("proj-1")
        assert released == STAGES[:2]
        assert released == STAGES[: len(released)]
        reopened.close()


class TestEvidenceIntegrity:
    def test_tampered_copy_detected(self, tmp_path):
        root = tmp_path / "vault"
        vault = make_vault(root)
        vault.seal(make_request(tmp_path, "design", "req-1"))
        copy_path = (
            root / "projects" / "proj-1" / "evidence" / "design" / "materials" / "00-spec"
        )
        copy_path.write_bytes(b"tampered")
        with pytest.raises(EvidenceIntegrityError):
            vault.get_release("proj-1", "design")
        vault.close()

    def test_unreleased_stage_reads_as_none(self, tmp_path):
        vault = make_vault(tmp_path / "vault")
        assert vault.get_release("proj-1", "accept") is None
        vault.close()

    def test_projects_are_isolated(self, tmp_path):
        vault = make_vault(tmp_path / "vault")
        vault.seal(make_request(tmp_path, "design", "req-a", project="proj-a"))
        assert vault.get_release("proj-b", "design") is None
        assert vault.released_stages("proj-b") == []
        vault.seal(make_request(tmp_path, "design", "req-b", project="proj-b"))
        assert vault.released_stages("proj-b") == ["design"]
        vault.close()
