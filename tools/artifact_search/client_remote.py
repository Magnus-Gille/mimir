"""Fixed, read-only SSH payload. Never prints credentials or server error bodies."""
import http.client
import json
import os
from pathlib import Path
import shlex
import stat
import sys

KEY_PATH = Path.home() / '.config/mimir-search/runtime.env'
PORT = 8791
LIMIT = 131072


class RemoteError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def private_bytes(path, limit):
    """Open a bounded, owner-only regular file without following its final symlink."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('private file required')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError('file too large')
        return data
    finally:
        os.close(fd)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate field')
        result[key] = value
    return result


def decode(data):
    return json.loads(data, object_pairs_hook=unique_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))


def execute(request):
    try:
        values = []
        for line in private_bytes(KEY_PATH, 65536).decode('utf-8').splitlines():
            if line.strip().startswith('MIMIR_SEARCH_API_KEY='):
                fields = shlex.split(line.strip().split('=', 1)[1], comments=True)
                if len(fields) != 1:
                    raise ValueError('invalid key')
                values.append(fields[0])
        if len(values) != 1 or not 16 <= len(values[0]) <= 4096 or any(not 33 <= ord(c) <= 126 for c in values[0]):
            raise ValueError('invalid key')
        key = values[0]
    except (OSError, UnicodeError, ValueError):
        raise RemoteError('credential_unavailable') from None
    connection = http.client.HTTPConnection('127.0.0.1', PORT, timeout=30)
    try:
        connection.request('POST', '/search', json.dumps(request, allow_nan=False).encode(),
                           {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json',
                            'Accept-Encoding': 'identity'})
        response = connection.getresponse()
        if response.status in (401, 403):
            raise RemoteError('unauthorized')
        if response.status != 200:
            raise RemoteError('retrieval_unavailable')
        if response.getheader('Content-Type', '').split(';')[0].strip() != 'application/json' or response.getheader('Content-Encoding', 'identity') != 'identity':
            raise RemoteError('invalid_response')
        data = response.read(LIMIT + 1)
        if len(data) > LIMIT:
            raise RemoteError('invalid_response')
        return decode(data)
    except (OSError, http.client.HTTPException):
        raise RemoteError('retrieval_unavailable') from None
    except (ValueError, UnicodeError, RecursionError):
        raise RemoteError('invalid_response') from None
    finally:
        connection.close()


def main():
    try:
        raw = sys.stdin.buffer.read(8193)
        if len(raw) > 8192:
            raise RemoteError('invalid_request')
        result = {'ok': True, 'data': execute(decode(raw))}
    except RemoteError as error:
        result = {'ok': False, 'error': error.code}
    except (ValueError, UnicodeError, RecursionError):
        result = {'ok': False, 'error': 'invalid_request'}
    print(json.dumps(result, allow_nan=False))


if __name__ == '__main__':
    main()
