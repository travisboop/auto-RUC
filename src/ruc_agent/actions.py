"""Session-local, expiring, single-use action plans. No implicit retry."""
from copy import deepcopy
import hashlib
import json
import secrets
from time import monotonic

from .errors import QueryError


class ActionPlans:
    def __init__(self):
        self.pending = {}

    def prepare(self, payload):
        self.pending = {k: v for k, v in self.pending.items() if v[0] > monotonic()}
        if len(self.pending) >= 32:
            raise QueryError('TOO_MANY_ACTIONS', '待提交操作超过 32 项；先提交或重置任务会话。')
        frozen = deepcopy(payload)
        token = secrets.token_urlsafe(24)
        digest = hashlib.sha256(json.dumps(frozen, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        self.pending[token] = (monotonic() + 300, frozen)
        return {'status': 'prepared', 'actionId': token, 'expiresInSeconds': 300,
                'digest': digest, 'preview': deepcopy(frozen), 'submitted': False}

    def take(self, token):
        plan = self.pending.pop(token, None)
        if plan is None:
            raise QueryError('ACTION_NOT_FOUND', '操作不存在或已经提交；不会重复发送。')
        expires, payload = plan
        if monotonic() >= expires:
            raise QueryError('ACTION_EXPIRED', '预览已过期，须重新读取当前状态并生成操作。')
        return payload

    def clear(self):
        self.pending.clear()
