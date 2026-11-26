"""Keep authentication material out of returned business records and diagnostics."""
import re

SECRET_KEY = re.compile(r'password|passwd|token|ticket|secret|hmac|authorization|cookie|session|csrf|^(?:authkey|jwt(?:Header)?)$', re.I)


def redact(value):
    if isinstance(value, dict):
        return {key: '<redacted>' if SECRET_KEY.search(key) else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value
