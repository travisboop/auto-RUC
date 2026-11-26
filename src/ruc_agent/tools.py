"""One implementation behind CLI and function-call tools."""
import json

from .catalog import ROOT, find_apps, platforms
from .credentials import read_credentials
from .engine import CampusSession, capabilities, error_result, prepare
from .errors import QueryError
from .schema import validate as validate_schema


def definitions():
    return json.loads((ROOT / 'tools/definitions.json').read_text(encoding='utf-8'))


def validate(name, arguments, registry=None):
    definition = next((item for item in definitions() if item['name'] == name), None)
    if definition is None:
        raise QueryError('UNKNOWN_TOOL', '未定义的工具。')
    validate_schema(arguments, definition['parameters'])
    if name == 'ruc_query':
        prepare(arguments['capability'], arguments.get('params', {}), registry)
    elif name == 'ruc_query_booths':
        prepare('quiet_booths', arguments)
    elif name == 'ruc_query_batch':
        for item in arguments['requests']:
            prepare(item['capability'], item.get('params', {}), registry)


class ToolRuntime:
    """One account, one task, one in-memory session across dependent tool calls."""
    def __init__(self, credentials_file=None, username=None, interactive=False, extensions=None, allow_writes=False):
        from .contracts import Registry
        self.registry = Registry(extensions)
        self.credentials_file, self.username, self.interactive = credentials_file, username, interactive
        self.allow_writes = allow_writes
        self.session = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self.session is not None:
            self.session.close()

    def call(self, name, arguments):
        validate(name, arguments, self.registry)
        if name == 'ruc_find_apps':
            return find_apps(**arguments)
        if name == 'ruc_platforms' and not arguments.get('live'):
            return platforms()
        if name == 'ruc_capabilities':
            return capabilities(**arguments, registry=self.registry)
        if name == 'ruc_inspect_source':
            from .source import inspect_source
            return inspect_source(**arguments)
        if name == 'ruc_register_contract':
            result = self.registry.register(arguments['document'])
            if self.session and self.session.browser:
                from .browser import BrowserPolicy
                policy = self.session.browser.policy
                updated = BrowserPolicy(self.registry)
                policy.origins.update(updated.origins)
                policy.documents.update(updated.documents)
                policy.routes.update(updated.routes)
                policy.auth_routes.update(updated.auth_routes)
                policy.rules = updated.rules
                policy.public_only_origins = updated.public_only_origins
            return result
        if name == 'ruc_reset_session':
            self.close()
            metadata = self.session.metadata() if self.session else {}
            self.session = None
            return {'status': 'ok', **metadata}
        if self.session is None:
            self.session = CampusSession(lambda: read_credentials(self.credentials_file, self.username, self.interactive),
                                         self.registry, self.allow_writes)
        session = self.session
        if name == 'ruc_check_login':
            output = session.check_login()
        elif name == 'ruc_authenticate':
            session.legacy_identity.prepare(arguments.get('verification_code'))
            output = {'status': 'ok', 'realm': 'legacy', 'authenticated': True}
        elif name == 'ruc_platforms':
            output = session.platform_inventory()
        elif name == 'ruc_query_booths':
            output = session.query('quiet_booths', arguments)
        elif name == 'ruc_query':
            output = session.query(**arguments)
        elif name == 'ruc_query_batch':
            output = session.batch(arguments['requests'])
        elif name == 'ruc_discover':
            output = session.discover(**arguments)
        elif name == 'ruc_prepare_action':
            output = session.prepare_action(**arguments)
        elif name == 'ruc_commit_action':
            output = session.commit_action(arguments['action_id'])
        elif name == 'ruc_browser_open':
            output = session.get_browser().open(**arguments)
        elif name == 'ruc_browser_snapshot':
            output = session.get_browser().snapshot(**arguments)
        elif name == 'ruc_browser_interact':
            output = session.get_browser().interact(**arguments)
        elif name == 'ruc_browser_network':
            output = session.get_browser().network(**arguments)
        else:
            raise QueryError('UNKNOWN_TOOL', '未定义的工具。')
        output.update(session.metadata())
        return output


def invoke(name, arguments, credentials_file=None, username=None, interactive=False, extensions=None):
    with ToolRuntime(credentials_file, username, interactive, extensions) as runtime:
        output = runtime.call(name, arguments)
    if runtime.session:
        output.update(runtime.session.metadata())
    return output
