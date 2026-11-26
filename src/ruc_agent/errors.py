"""Errors safe to return to an agent without response bodies or credentials."""


class QueryError(Exception):
    def __init__(self, code, message, details=None):
        self.code = code
        self.details = details
        super().__init__(message)
