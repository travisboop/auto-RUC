"""Explicit credential inputs; no implicit search for accounts or saved sessions."""
import getpass
import json
import os
from pathlib import Path
import stat
import sys
import warnings

from .errors import QueryError


def read_credentials(filename=None, username=None, interactive=True):
    filename = filename or os.environ.get('RUC_CREDENTIALS_FILE')
    if filename:
        path = Path(filename).expanduser()
        descriptor = None
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 16384:
                raise QueryError('CREDENTIALS_INVALID', '凭据文件须为小于 16 KB 的普通 JSON 文件。')
            if os.name == 'posix' and (info.st_mode & 0o077 or info.st_uid != os.getuid()):
                raise QueryError('CREDENTIALS_PERMISSIONS', '凭据文件须由当前用户持有，权限设为 600。')
            with os.fdopen(descriptor, encoding='utf-8') as stream:
                descriptor = None
                values = json.load(stream)
        except (OSError, ValueError, UnicodeError):
            raise QueryError('CREDENTIALS_INVALID', '无法读取凭据文件中的 JSON。') from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if not isinstance(values, dict) or set(values) != {'username', 'password'} or not all(
            isinstance(values.get(key), str) and values[key] for key in ['username', 'password']
        ):
            raise QueryError('CREDENTIALS_INVALID', '凭据 JSON 仅需非空的 username 和 password。')
        if username and username != values['username']:
            raise QueryError('CREDENTIALS_INVALID', '命令行学号与凭据文件不一致。')
        return values['username'], values['password']
    if not interactive:
        raise QueryError('CREDENTIALS_REQUIRED', '请提供 credentials-file 或 RUC_CREDENTIALS_FILE。')
    try:
        if not username:
            print('学号：', end='', file=sys.stderr, flush=True)
            username = input().strip()
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            password = getpass.getpass('密码（输入不显示）：')
    except (EOFError, getpass.GetPassWarning):
        raise QueryError('CREDENTIALS_REQUIRED', '请在本机终端输入，或指定凭据文件。') from None
    if not username or not password:
        raise QueryError('CREDENTIALS_REQUIRED', '需要学号和密码。')
    return username, password


def configure_credentials(filename):
    """User-run setup: hidden terminal input into a new, explicitly named file."""
    path = Path(filename).expanduser().absolute()
    package = Path(__file__).resolve().parents[2]
    if package == path.resolve() or package in path.resolve().parents:
        raise QueryError('CREDENTIALS_INVALID', '真实凭据必须保存到连接包目录之外。')
    if not sys.stdin.isatty():
        raise QueryError('CREDENTIALS_REQUIRED', '请在本机终端运行 configure，密码输入会隐藏。')
    print('学号：', end='', file=sys.stderr, flush=True)
    username = input().strip()
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        try:
            password = getpass.getpass('密码（输入不显示）：')
        except (EOFError, getpass.GetPassWarning):
            raise QueryError('CREDENTIALS_REQUIRED', '未取得终端隐藏输入的密码。') from None
    if not username or not password:
        raise QueryError('CREDENTIALS_REQUIRED', '学号和密码不能为空。')
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump({'username': username, 'password': password}, stream, ensure_ascii=False)
    except FileExistsError:
        raise QueryError('CREDENTIALS_EXISTS', '目标文件已存在，请指定新的包外文件路径。') from None
    except OSError:
        raise QueryError('CREDENTIALS_INVALID', '无法写入指定的凭据文件。') from None
    finally:
        password = None
    return {'status': 'ok', 'credentialsFile': str(path), 'loginAttempted': False}
