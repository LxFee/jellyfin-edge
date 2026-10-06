"""Operator-only pairing helper. Writes secret to a private file, never stdout."""
import asyncio
import getpass
import json
import os
import sys
import httpx
from control import ControlClient


async def main():
    if len(sys.argv) != 3:
        raise SystemExit('Usage: python pair.py NODE_NAME NEW_SECRET_FILE')
    code = getpass.getpass('Pairing code: ')
    async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
        try:
            result = await ControlClient(client, os.environ['JELLYFIN_BACKEND']).pair(code, sys.argv[1])
            token = result.get('Token', result.get('token'))
            node = result.get('NodeId', result.get('node_id'))
            if not isinstance(token, str) or not token or not node:
                raise ValueError('invalid response')
            fd = os.open(sys.argv[2], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as f:
                json.dump({'NodeId': node, 'Token': token}, f)
        except Exception:
            raise SystemExit('Pairing failed (details suppressed to protect credentials)') from None


if __name__ == '__main__':
    asyncio.run(main())
