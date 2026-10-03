# 阶段放行证据封存库 (release_vault)

工程项目按固定阶段顺序放行：只有完成当前阶段的全部签批，才能进入下一阶段。
每次放行对应一套完整、可离线读取的证据：材料独立副本 + JSON 签批清单（本地目录），
放行状态保存在 SQLite。原始文件被覆盖或删除后，仍能逐字节读回当时批准的内容。

## 目录布局

```
<root>/releases.db                                   # 放行记录（每项目每阶段最多一条）
<root>/projects/<项目>/evidence/<阶段>/manifest.json  # 签批清单（完整批准记录 + 副本映射）
<root>/projects/<项目>/evidence/<阶段>/materials/...  # 必交材料的独立副本
<root>/projects/<项目>/staging/...                   # 封存中的暂存区（崩溃后打开时整体清理）
```

## 用法

```python
from release_vault import Approval, EvidenceVault, Material, SealRequest

vault = EvidenceVault(
    root_dir="/var/vault",
    stage_order=["design", "build", "accept"],
    required_materials={"design": ["spec", "drawing"], ...},
    required_reviewers={"design": ["alice", "bob"], ...},
)

result = vault.seal(SealRequest(
    request_id="req-001",            # 请求号：幂等键
    project_id="proj-1",
    stage="design",
    materials=[Material("spec", "<获批SHA-256>", "/path/to/spec.pdf"), ...],
    approvals=[Approval("alice", "2026-10-01T09:00:00Z"), ...],
))

package = vault.get_release("proj-1", "design")   # 未放行返回 None
data = package.read_material("spec")              # 逐字节读回并校验摘要
stages = vault.released_stages("proj-1")          # 已放行阶段（连续前缀）
```

## 语义

- 第一阶段可直接申请；其余阶段须所有前序阶段封存成功。已放行阶段不可撤回或修改。
- 封存前逐材料复制并核对实际内容与获批 SHA-256；缺失或不符即失败，不留放行状态。
- 副本、签批清单、放行记录三者齐备（暂存区原子改名 + SQLite 提交）才算已放行；
  读取只能得到完整证据或"未放行"。
- 相同请求号 + 相同业务内容（项目、阶段、材料标识及摘要、批准记录，不含原路径）
  重试返回原封存结果，不重新读取原路径、不追加放行记录；内容不同则报冲突。
- 封存中断后重新打开：未成功返回的提交整体撤回（清理暂存区与无记录的残留目录），
  允许重新申请；已成功返回的证据不受影响。

## 测试

```
python3 -m pytest tests/ -q
```
