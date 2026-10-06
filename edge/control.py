"""Control-plane protocol client; never uses a Jellyfin administrator key."""
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
import httpx


def validate_backend(url: str) -> str:
    p = urlsplit(url)
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password or p.query or p.fragment:
        raise ValueError('backend must be a trusted http(s) origin, optionally with a base path')
    if any(x in p.path for x in ('..', '\\', '%')):
        raise ValueError('invalid backend base path')
    return url.rstrip('/')


@dataclass(frozen=True)
class Mapping:
    host: str
    local: str


@dataclass(frozen=True)
class Policy:
    backend: str
    mappings: tuple[Mapping, ...] = ()
    node_id: str | None = None
    enable_local: bool = False
    allow_fallback: bool = True
    trust_proxy_headers: bool = False
    enabled: bool = True
    revision: str = ''
    new_credential: str | None = None
    enable_cache: bool = False
    cache_max_bytes: int = 50 * 1024 * 1024 * 1024
    cache_block_bytes: int = 2 * 1024 * 1024
    cache_epoch: int = 0
    source_namespace: str = ''

    def __post_init__(self):
        validate_backend(self.backend)
        for m in self.mappings:
            if not Path(m.host).is_absolute() or not Path(m.local).is_absolute():
                raise ValueError('mount roots must be absolute')


class ControlClient:
    def __init__(self, client: httpx.AsyncClient, backend: str):
        self.client = client
        self.backend = validate_backend(backend)

    async def pair(self, code: str, node_name: str) -> dict:
        r = await self.client.post(self.backend + '/JellyfinEdge/pair', json={'Code': code, 'Name': node_name})
        r.raise_for_status()
        return r.json()

    async def config(self, credential: str) -> Policy:
        r = await self.client.get(self.backend + '/JellyfinEdge/node/config', headers={'Authorization': 'Bearer ' + credential}, timeout=httpx.Timeout(5, connect=2))
        r.raise_for_status()
        data = r.json()
        # The control plane cannot silently redirect this node to a new origin.
        if data.get('backend', self.backend).rstrip('/') != self.backend:
            raise ValueError('backend migration requires explicit operator trust')
        def field(obj, pascal, snake, default=None):
            return obj.get(pascal, obj.get(snake, default))
        mounts = field(data, 'PathMappings', 'path_mappings', [])
        mappings = tuple(Mapping(field(x, 'OriginRoot', 'origin_root'), field(x, 'EdgeRoot', 'edge_root')) for x in mounts)
        node_id = field(data, 'NodeId', 'node_id')
        if not isinstance(node_id, str) or not node_id:
            raise ValueError('missing NodeId')
        enable = field(data, 'EnableLocalDirectPlay', 'enable_local_direct_play', False)
        fallback = field(data, 'AllowOriginFallback', 'allow_origin_fallback', True)
        trust = field(data, 'TrustProxyHeaders', 'trust_proxy_headers', False)
        if not isinstance(enable, bool) or not isinstance(fallback, bool) or not isinstance(trust, bool):
            raise ValueError('policy flags must be booleans')
        enabled = field(data, 'Enabled', 'enabled', True)
        revision = field(data, 'Revision', 'revision', '')
        candidate = field(data, 'NewCredential', 'new_credential')
        if not isinstance(enabled, bool) or not isinstance(revision, str) or len(revision) > 64:
            raise ValueError('invalid configuration metadata')
        cache = field(data, 'EnableCache', 'enableCache', False)
        quota = field(data, 'CacheMaxBytes', 'cacheMaxBytes', 50 * 1024 * 1024 * 1024)
        block = field(data, 'CacheBlockBytes', 'cacheBlockBytes', 2 * 1024 * 1024)
        epoch = field(data, 'CacheEpoch', 'cacheEpoch', 0)
        namespace = field(data, 'SourceNamespace', 'sourceNamespace', '')
        if not isinstance(cache, bool) or type(quota) is not int or type(block) is not int or not 65536 <= block <= 16777216 or quota < block or type(epoch) is not int or epoch < 0 or not isinstance(namespace, str):
            raise ValueError('invalid cache configuration')
        # Node-private grants are handled by MediaGateway, not public status/config.
        self.exports = field(data, 'Exports', 'exports', []) or []
        return Policy(self.backend, mappings, node_id, enable, fallback, trust, enabled, revision, candidate, cache, quota, block, epoch, namespace)
