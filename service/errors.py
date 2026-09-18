"""统一错误类型：所有业务校验与冲突都以 ApiError 抛出，由 HTTP 层转成 JSON。"""


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details if details is not None else {}

    def to_dict(self):
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}


def bad_request(code: str, message: str, details=None) -> ApiError:
    return ApiError(400, code, message, details)


def not_found(code: str, message: str, details=None) -> ApiError:
    return ApiError(404, code, message, details)


def conflict(code: str, message: str, details=None) -> ApiError:
    return ApiError(409, code, message, details)
