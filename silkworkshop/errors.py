"""领域错误：HTTP 层据此映射状态码。"""


class DomainError(Exception):
    """所有领域错误的基类。"""

    status = 400
    code = "domain_error"


class NotFound(DomainError):
    status = 404
    code = "not_found"


class ValidationError(DomainError):
    status = 422
    code = "validation"


class Conflict(DomainError):
    status = 409
    code = "conflict"


class ConsentError(DomainError):
    """授权缺失或已撤回，禁止新的展示。"""

    status = 403
    code = "consent_denied"
