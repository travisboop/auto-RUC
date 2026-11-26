import argparse
import json
from pathlib import Path
import sys

from . import __version__
from .booths import QueryError
from .tools import definitions, error_result, invoke
from .credentials import configure_credentials


def main(argv=None):
    parser = argparse.ArgumentParser(description='人大平台连接包：全新认证、能力发现、配置式查询和操作。')
    parser.add_argument('--extensions', help='额外 JSON 契约目录；不自动搜索本机目录')
    parser.add_argument('--version', action='version', version=__version__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ['check-login', 'booths']:
        command = commands.add_parser(name)
        command.add_argument('--credentials-file', help='本机凭据 JSON 路径；也可用 RUC_CREDENTIALS_FILE')
        command.add_argument('--username', help='终端登录学号；密码隐藏输入')
        if name == 'booths':
            for key in ['date', 'campus', 'venue', 'type', 'from', 'to']:
                command.add_argument('--' + key)
            command.add_argument('--minutes', type=int)
    apps = commands.add_parser('find-apps', help='检索 2026-11-25 应用目录快照')
    apps.add_argument('keyword')
    apps.add_argument('--limit', type=int, default=20)
    caps = commands.add_parser('capabilities', help='发现能力、参数和验证范围')
    caps.add_argument('keyword', nargs='?')
    query = commands.add_parser('query', help='查询指定能力；参数 JSON 见 capabilities')
    query.add_argument('capability')
    query.add_argument('--params', default='{}', help='能力参数 JSON 对象')
    query.add_argument('--credentials-file')
    query.add_argument('--username')
    query.add_argument('--all-pages', action='store_true', help='按契约从第一页收集完整记录')
    query.add_argument('--max-pages', type=int, default=100)
    batch = commands.add_parser('batch', help='批量查询，共用本次新认证')
    batch.add_argument('--requests-file', required=True, help='包内或本机 JSON 查询数组')
    batch.add_argument('--credentials-file')
    batch.add_argument('--username')
    setup = commands.add_parser('configure', help='终端隐藏输入，创建包外凭据 JSON')
    setup.add_argument('--credentials-file', default=str(Path.home() / '.config/ruc/credentials.json'))
    platform_command = commands.add_parser('platforms', help='列出各平台的接入状态')
    platform_command.add_argument('--live', action='store_true', help='读取当前账号的平台目录与覆盖范围')
    platform_command.add_argument('--credentials-file')
    commands.add_parser('tools', help='输出 function tool 定义')
    args = vars(parser.parse_args(argv))
    extensions = args.pop('extensions')
    command = args.pop('command')
    try:
        if command == 'tools':
            output = definitions()
        elif command == 'configure':
            output = configure_credentials(args['credentials_file'])
        else:
            file = args.pop('credentials_file', None)
            username = args.pop('username', None)
            arguments = {k: v for k, v in args.items() if v is not None}
            if command == 'query':
                try:
                    arguments['params'] = json.loads(arguments['params'])
                except ValueError:
                    raise QueryError('INVALID_ARGUMENT', '--params 须为 JSON 对象。') from None
            elif command == 'batch':
                try:
                    with Path(arguments['requests_file']).open(encoding='utf-8') as stream:
                        raw = stream.read(65537)
                    if len(raw) > 65536:
                        raise ValueError()
                    arguments = {'requests': json.loads(raw)}
                except (OSError, ValueError, UnicodeError):
                    raise QueryError('INVALID_ARGUMENT', '查询文件须为不超过 64 KB 的 JSON 数组。') from None
            name = {'check-login': 'ruc_check_login', 'booths': 'ruc_query_booths',
                    'find-apps': 'ruc_find_apps', 'platforms': 'ruc_platforms',
                    'capabilities': 'ruc_capabilities', 'query': 'ruc_query', 'batch': 'ruc_query_batch'}[command]
            output = invoke(name, arguments, file, username, interactive=True, extensions=extensions)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 2 if isinstance(output, dict) and output['status'] == 'partial' else 0
    except QueryError as error:
        print(json.dumps(error_result(error), ensure_ascii=False))
        return 1
    except KeyboardInterrupt:
        print(json.dumps({'status': 'error', 'code': 'CANCELLED', 'message': '操作已取消。'}, ensure_ascii=False))
        return 130


def tool_main(argv=None):
    parser = argparse.ArgumentParser(description='从 stdin 读取一次 JSON tool call，向 stdout 返回一次 JSON。')
    parser.add_argument('--list', action='store_true')
    parser.add_argument('--credentials-file')
    parser.add_argument('--extensions')
    args = parser.parse_args(argv)
    try:
        if args.list:
            result = definitions()
        else:
            raw = sys.stdin.read(65537)
            if len(raw) > 65536:
                raise QueryError('INVALID_ARGUMENT', '工具输入超过 64 KB。')
            try:
                call = json.loads(raw)
            except ValueError:
                raise QueryError('INVALID_ARGUMENT', '输入须为 JSON tool call。') from None
            if not isinstance(call, dict) or set(call) != {'name', 'arguments'} or not isinstance(call['name'], str):
                raise QueryError('INVALID_ARGUMENT', '输入仅需 name 和 arguments。')
            if call['name'] in {'ruc_prepare_action', 'ruc_commit_action', 'ruc_browser_open', 'ruc_browser_snapshot',
                               'ruc_browser_interact', 'ruc_browser_network', 'ruc_register_contract', 'ruc_authenticate'}:
                raise QueryError('PERSISTENT_SESSION_REQUIRED', '此工具需要连续会话；使用 scripts/serve.py 的 JSONL 或 MCP 模式。')
            result = invoke(call['name'], call['arguments'], args.credentials_file, extensions=args.extensions)
        print(json.dumps(result, ensure_ascii=False))
        return 2 if isinstance(result, dict) and result['status'] == 'partial' else 0
    except QueryError as error:
        print(json.dumps(error_result(error), ensure_ascii=False))
        return 1
    except KeyboardInterrupt:
        return 130
