"""封存库异常类型定义。"""


class VaultError(Exception):
    """封存库基础异常。"""


class StageLockedError(VaultError):
    """前序阶段未全部封存，当前阶段不可申请。"""


class UnknownStageError(VaultError):
    """阶段不在固定阶段顺序中。"""


class MaterialError(VaultError):
    """材料缺失、摘要与获批摘要不符或材料集合不匹配。"""


class ApprovalError(VaultError):
    """批准记录未覆盖全部指定审阅人。"""


class ConflictError(VaultError):
    """请求号已被占用或相同请求号携带了不同的业务内容。"""


class EvidenceIntegrityError(VaultError):
    """已保存证据不完整或内容与获批摘要不一致。"""
