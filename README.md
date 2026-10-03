# 阶段放行证据封存库（release_vault）

工程项目按固定阶段顺序推进，只有完成当前阶段全部签批才能进入下一阶段。
本库由上层直接调用，负责把每次放行封存为一套完整、可离线读取的证据：

- 放行状态保存在 SQLite（`releases.db`）。
- 封存材料副本与可离线查看的 JSON 签批清单（`manifest.json`）保存在本地目录 `evidence/<项目>/<阶段>/`。
- 只有材料副本、签批清单、放行记录三者完整落盘，阶段才显示为已放行。

## 目录结构

```
<root>/
  releases.db                 # 放行记录（SQLite，WAL + FULL sync）
  evidence/<项目>/<阶段>/
    materials/<材料标识>       # 必交材料的独立文件副本
    manifest.json             # 签批清单：完整批准记录 + 材料摘要 + 副本相对路径
  staging/                    # 封存中的暂存区（重开时自动清理）
```

## 使用示例

```python
from release_vault import (
    ApprovalRecord, MaterialSubmission, ReleaseVault, SealRequest, StageSpec,
)

stages = [
    StageSpec("design", ("spec", "diagram")),
    StageSpec("build", ("binary",)),
]
vault = ReleaseVault("./vault_root", "proj-1", stages)

result = vault.seal(SealRequest(
    request_id="req-001",
    project_id="proj-1",
    stage="design",
    materials=(
        MaterialSubmission("spec", "/path/spec.pdf", "<获批SHA-256>"),
        MaterialSubmission("diagram", "/path/diagram.png", "<获批SHA-256>"),
    ),
    approvals=(
        ApprovalRecord("alice", "approve", "<spec的SHA-256>", "2026-10-01T00:00:00Z"),
        ApprovalRecord("bob", "approve", "<diagram的SHA-256>", "2026-10-01T01:00:00Z"),
    ),
))

view = vault.get_evidence("design")   # 未放行返回 None；已放行返回完整证据
print(vault.released_stages())        # 已放行阶段的连续前缀
vault.close()
```

## 行为保证

- **阶段顺序**：第一阶段可直接封存；其余阶段须所有前序阶段封存成功。
- **唯一放行**：每阶段最多一套有效证据和一条放行记录，不支持撤回或修改。
- **摘要核验**：副本实际内容必须与获批 SHA-256 一致，缺失或不符即拒绝放行。
- **原子可见**：先写副本与清单并 fsync，原子改名到证据目录，最后提交放行记录；
  读取只能得到完整证据或"未放行"，不会看到中间状态。
- **幂等重试**：相同业务内容（项目、阶段、材料标识及摘要、批准记录，不含原路径）
  再次提交返回原封存结果，不重读原路径、不追加放行记录。
- **崩溃恢复**：中断后重新打开，暂存区与无放行记录的孤儿证据目录被整体清理，
  可重新申请；已成功返回的证据不受影响。

## 运行测试

```bash
python3 -m unittest discover -s tests -v
```
