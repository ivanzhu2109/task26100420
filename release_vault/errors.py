"""阶段放行证据封存库的异常定义。"""


class VaultError(Exception):
    """封存库基础异常。"""


class UnknownStageError(VaultError):
    """阶段不在项目固定阶段顺序中。"""


class StageOrderError(VaultError):
    """前序阶段未全部封存，当前阶段不能放行。"""


class StageAlreadySealedError(VaultError):
    """阶段已放行，不允许重复封存或修改。"""


class MaterialMissingError(VaultError):
    """必交材料缺失或源文件不可读。"""


class DigestMismatchError(VaultError):
    """材料实际内容与获批 SHA-256 摘要不符。"""


class ApprovalIncompleteError(VaultError):
    """批准记录不完整或与材料清单不绑定。"""
