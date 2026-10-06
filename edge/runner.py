"""Outbound runner identity; no public management listener or user/admin credential."""
import json
import os
import secrets
import uuid
from pathlib import Path

VERSION = 'runner-cache-v2'


def atomic_state(path, data):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + secrets.token_hex(8))
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if hasattr(os, 'O_DIRECTORY'):
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


class Runner:
    def __init__(self, control_url, state_file, enrollment_file, name, allowed_roots):
        from control import validate_backend
        self.url = validate_backend(control_url)
        self.path = Path(state_file)
        self.enrollment_file = enrollment_file
        self.name = name
        self.allowed_roots = tuple(Path(x).resolve() for x in allowed_roots)
        if any(str(x) == '/' for x in self.allowed_roots):
            raise ValueError('explicit restricted media roots required')
        if self.path.exists():
            self.data = json.loads(self.path.read_text())  # Corrupt state fails closed.
            uuid.UUID(self.data['InstanceId'])
            if len(self.data['Nonce']) != 64:
                raise ValueError('invalid runner identity')
        else:
            self.data = {'InstanceId': str(uuid.uuid4()), 'Nonce': secrets.token_hex(32)}
            atomic_state(self.path, self.data)  # Before any network attempt.

    def permits(self, root):
        path = Path(root)
        # Resolve for containment, but do not permit a configured symlink root.
        return path.is_absolute() and not path.is_symlink() and any(path.resolve() == r or r in path.resolve().parents for r in self.allowed_roots)

    async def credential(self, client):
        if self.data.get('Token'):
            return self.data['Token']  # Never automatically rejoin after rejection/revoke.
        token = Path(self.enrollment_file).read_text().strip()
        response = await client.post(self.url + '/JellyfinEdge/enroll', headers={'Authorization': 'Bearer ' + token}, json={
            'InstanceId': self.data['InstanceId'], 'Nonce': self.data['Nonce'], 'Name': self.name, 'Version': VERSION})
        response.raise_for_status()
        result = response.json()
        node = result.get('NodeId', result.get('nodeId'))
        credential = result.get('Token', result.get('token'))
        uuid.UUID(node)
        if not isinstance(credential, str) or len(credential) != 64:
            raise ValueError('invalid enrollment response')
        self.data.update(NodeId=node, Token=credential)
        atomic_state(self.path, self.data)
        return credential

    def rotate(self, token):
        if not isinstance(token, str) or len(token) != 64:
            raise ValueError('invalid rotation response')
        self.data['Token'] = token
        atomic_state(self.path, self.data)  # Persist BEFORE using/acknowledging.

    async def heartbeat(self, client, credential, policy, error=''):
        readable = 'readable' if policy.enable_cache else 'no-paths' if not policy.mappings else 'readable' if all(self.permits(m.local) and os.access(m.local, os.R_OK | os.X_OK) for m in policy.mappings) else 'unreadable'
        response = await client.post(self.url + '/JellyfinEdge/node/heartbeat', headers={'Authorization': 'Bearer ' + credential}, json={
            'Version': VERSION, 'Revision': policy.revision, 'MediaReadability': readable, 'ErrorCode': error or ('MEDIA_UNREADABLE' if readable == 'unreadable' else '')})
        response.raise_for_status()
