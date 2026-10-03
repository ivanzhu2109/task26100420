"""阶段放行证据封存库自动化测试。"""
import hashlib
import json
import os
import shutil
import tempfile
import unittest

from release_vault import (
    ApprovalRecord,
    DigestMismatchError,
    MaterialMissingError,
    MaterialSubmission,
    ReleaseVault,
    SealRequest,
    StageAlreadySealedError,
    StageOrderError,
    StageSpec,
)

STAGES = [
    StageSpec("design", ("spec", "diagram")),
    StageSpec("build", ("binary",)),
    StageSpec("release", ("notes",)),
]


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        digest.update(handle.read())
    return digest.hexdigest()


class VaultTestBase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="vault-")
        self.src = tempfile.mkdtemp(prefix="src-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.src, ignore_errors=True)
        self.files = {}

    def make_file(self, name, content):
        path = os.path.join(self.src, name)
        with open(path, "wb") as handle:
            handle.write(content)
        self.files[name] = (path, hashlib.sha256(content).hexdigest(), content)
        return path

    def digest_of(self, name):
        return self.files[name][1]

    def content_of(self, name):
        return self.files[name][2]

    def open_vault(self):
        return ReleaseVault(self.root, "proj", STAGES)

    def build_request(self, stage, request_id="req-1"):
        if stage == "design":
            materials = (
                MaterialSubmission("spec", self.files["spec"][0], self.digest_of("spec")),
                MaterialSubmission(
                    "diagram", self.files["diagram"][0], self.digest_of("diagram")
                ),
            )
            approvals = (
                ApprovalRecord("alice", "approve", self.digest_of("spec"), "2026-10-01T00:00:00Z", "ok"),
                ApprovalRecord("bob", "approve", self.digest_of("diagram"), "2026-10-01T01:00:00Z"),
            )
        elif stage == "build":
            materials = (
                MaterialSubmission("binary", self.files["binary"][0], self.digest_of("binary")),
            )
            approvals = (
                ApprovalRecord("carol", "approve", self.digest_of("binary"), "2026-10-02T00:00:00Z"),
            )
        else:
            materials = (
                MaterialSubmission("notes", self.files["notes"][0], self.digest_of("notes")),
            )
            approvals = (
                ApprovalRecord("dave", "approve", self.digest_of("notes"), "2026-10-03T00:00:00Z"),
            )
        return SealRequest(request_id, "proj", stage, materials, approvals)

    def prepare(self, *names):
        contents = {
            "spec": b"spec-content-v1",
            "diagram": b"diagram-bytes",
            "binary": b"\x00\x01\x02binary",
            "notes": b"release notes text",
        }
        for name in names:
            self.make_file(name, contents[name])

    def assert_evidence_matches(self, view, stage):
        self.assertIsNotNone(view)
        self.assertEqual(view.stage, stage)
        with open(view.manifest_path, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        self.assertEqual(manifest["stage"], stage)
        self.assertEqual(len(manifest["approvals"]), len(view.approvals))
        for material in view.materials:
            self.assertTrue(os.path.isfile(material.stored_path))
            self.assertEqual(sha256_of(material.stored_path), material.sha256)
        manifest_digests = {m["sha256"] for m in manifest["materials"]}
        self.assertEqual(manifest_digests, {m.sha256 for m in view.materials})


class InterruptRecoveryTest(VaultTestBase):
    """封存中断并重新打开后的行为。"""

    def test_crash_then_reopen_and_retry(self):
        self.prepare("spec", "diagram", "binary")

        # 第一阶段完整封存成功。
        vault = self.open_vault()
        result1 = vault.seal(self.build_request("design", "req-design"))
        self.assertFalse(result1.already_sealed)
        self.assertEqual(vault.release_count(), 1)
        vault.close()

        # 模拟第二阶段封存中断：写清单时崩溃，留下暂存区。
        vault = self.open_vault()
        original = ReleaseVault._write_manifest
        def boom(*args, **kwargs):
            raise RuntimeError("simulated crash")
        ReleaseVault._write_manifest = boom
        try:
            with self.assertRaises(RuntimeError):
                vault.seal(self.build_request("build", "req-build"))
        finally:
            ReleaseVault._write_manifest = original
        vault.close()
        # 暂存痕迹确实存在过。
        self.assertFalse(os.listdir(os.path.join(self.root, "staging"))
                         if os.path.isdir(os.path.join(self.root, "staging")) else [])

        # 再模拟一次崩溃在证据目录改名之后、放行记录提交之前：制造孤儿证据目录。
        orphan = os.path.join(self.root, "evidence", "proj", "build")
        os.makedirs(os.path.join(orphan, "materials"))
        with open(os.path.join(orphan, "manifest.json"), "w") as handle:
            json.dump({"stage": "build"}, handle)

        # 重新打开：中断痕迹被清理。
        vault = self.open_vault()
        self.assertFalse(os.path.exists(orphan))
        # 已放行阶段证据完整可读，未放行阶段返回 None，看不到中间状态。
        self.assert_evidence_matches(vault.get_evidence("design"), "design")
        self.assertIsNone(vault.get_evidence("build"))
        self.assertEqual(vault.released_stages(), ["design"])
        self.assertEqual(vault.release_count(), 1)

        # 重试第二阶段：整体完成，不产生重复放行记录。
        result2 = vault.seal(self.build_request("build", "req-build"))
        self.assertFalse(result2.already_sealed)
        self.assertEqual(vault.release_count(), 2)
        vault.close()

        # 再次重新打开：两阶段证据均完整，构成连续前缀。
        vault = self.open_vault()
        self.assert_evidence_matches(vault.get_evidence("design"), "design")
        self.assert_evidence_matches(vault.get_evidence("build"), "build")
        self.assertIsNone(vault.get_evidence("release"))
        self.assertEqual(vault.released_stages(), ["design", "build"])
        self.assertEqual(vault.release_count(), 2)

        # 第一阶段此前成功返回的证据仍可逐字节读回。
        view = vault.get_evidence("design")
        by_id = {m.material_id: m for m in view.materials}
        with open(by_id["spec"].stored_path, "rb") as handle:
            self.assertEqual(handle.read(), self.content_of("spec"))
        with open(by_id["diagram"].stored_path, "rb") as handle:
            self.assertEqual(handle.read(), self.content_of("diagram"))
        vault.close()


class IdempotentRetryTest(VaultTestBase):
    """成功后删除原文件，以相同业务内容重试。"""

    def test_retry_after_source_deleted(self):
        self.prepare("spec", "diagram")
        vault = self.open_vault()
        request = self.build_request("design", "req-design")
        result = vault.seal(request)
        self.assertEqual(vault.release_count(), 1)

        # 覆盖并删除原文件。
        for name in ("spec", "diagram"):
            path = self.files[name][0]
            with open(path, "wb") as handle:
                handle.write(b"overwritten-garbage")
            os.unlink(path)

        # 相同业务内容重试（请求号可不同、原路径已不存在）。
        retry = SealRequest(
            "req-design-retry", "proj", "design", request.materials, request.approvals
        )
        result2 = vault.seal(retry)
        self.assertTrue(result2.already_sealed)
        self.assertEqual(result2.request_id, "req-design")
        self.assertEqual(result2.sealed_at, result.sealed_at)
        self.assertEqual(result2.manifest_path, result.manifest_path)
        self.assertEqual(
            {m.material_id: m.sha256 for m in result2.materials},
            {m.material_id: m.sha256 for m in result.materials},
        )
        self.assertEqual(vault.release_count(), 1)

        # 获批材料仍可逐字节读回。
        view = vault.get_evidence("design")
        by_id = {m.material_id: m for m in view.materials}
        with open(by_id["spec"].stored_path, "rb") as handle:
            self.assertEqual(handle.read(), self.content_of("spec"))
        with open(by_id["diagram"].stored_path, "rb") as handle:
            self.assertEqual(handle.read(), self.content_of("diagram"))
        vault.close()

        # 重新打开后结果不变。
        vault = self.open_vault()
        self.assertEqual(vault.release_count(), 1)
        self.assertEqual(vault.released_stages(), ["design"])
        self.assert_evidence_matches(vault.get_evidence("design"), "design")
        vault.close()


class ValidationTest(VaultTestBase):
    """校验类行为。"""

    def test_stage_order_enforced(self):
        self.prepare("binary")
        vault = self.open_vault()
        with self.assertRaises(StageOrderError):
            vault.seal(self.build_request("build"))
        self.assertEqual(vault.release_count(), 0)
        vault.close()

    def test_digest_mismatch_rejected(self):
        self.prepare("spec", "diagram")
        vault = self.open_vault()
        request = self.build_request("design")
        wrong = "0" * 64
        bad = SealRequest(
            request.request_id,
            request.project_id,
            request.stage,
            (
                MaterialSubmission("spec", request.materials[0].source_path, wrong),
                request.materials[1],
            ),
            (
                ApprovalRecord("alice", "approve", wrong, "2026-10-01T00:00:00Z"),
                request.approvals[1],
            ),
        )
        with self.assertRaises(DigestMismatchError):
            vault.seal(bad)
        self.assertEqual(vault.release_count(), 0)
        self.assertIsNone(vault.get_evidence("design"))
        vault.close()

    def test_missing_material_rejected(self):
        self.prepare("spec")
        vault = self.open_vault()
        request = SealRequest(
            "req-x",
            "proj",
            "design",
            (MaterialSubmission("spec", self.files["spec"][0], self.digest_of("spec")),),
            (ApprovalRecord("alice", "approve", self.digest_of("spec"), "2026-10-01T00:00:00Z"),),
        )
        with self.assertRaises(MaterialMissingError):
            vault.seal(request)
        self.assertEqual(vault.release_count(), 0)
        vault.close()

    def test_different_content_after_seal_rejected(self):
        self.prepare("spec", "diagram")
        vault = self.open_vault()
        vault.seal(self.build_request("design"))
        changed = self.build_request("design", "req-other")
        changed = SealRequest(
            changed.request_id,
            changed.project_id,
            changed.stage,
            changed.materials,
            (
                ApprovalRecord("alice", "approve", self.digest_of("spec"), "2026-10-09T00:00:00Z"),
                changed.approvals[1],
            ),
        )
        with self.assertRaises(StageAlreadySealedError):
            vault.seal(changed)
        self.assertEqual(vault.release_count(), 1)
        vault.close()

    def test_full_flow_three_stages(self):
        self.prepare("spec", "diagram", "binary", "notes")
        vault = self.open_vault()
        vault.seal(self.build_request("design", "r1"))
        vault.seal(self.build_request("build", "r2"))
        vault.seal(self.build_request("release", "r3"))
        self.assertEqual(vault.released_stages(), ["design", "build", "release"])
        self.assertEqual(vault.release_count(), 3)
        for stage in ("design", "build", "release"):
            self.assert_evidence_matches(vault.get_evidence(stage), stage)
        vault.close()


if __name__ == "__main__":
    unittest.main()
