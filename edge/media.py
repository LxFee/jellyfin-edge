"""Scoped export distribution and the original-file cache seam."""
import hashlib
import hmac
import json
import re
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import httpx
from starlette.responses import Response, StreamingResponse

from cache import DiskCache
from control import Mapping, Policy
from runner import atomic_state


def field(doc, name, default=None):
    return doc.get(name, doc.get(name[0].lower() + name[1:], default))


class OriginFailure(Exception):
    def __init__(self, status=503):
        self.status = status if status in (400, 401, 403, 404, 409, 416) else 503


async def bounded(client, url, headers, params=None, limit=64 * 1024 * 1024):
    async with client.stream('GET', url, headers={**headers, 'Accept-Encoding': 'identity'}, params=params) as response:
        if response.status_code != 200:
            raise OriginFailure(response.status_code)
        data = bytearray()
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > limit:
                raise OriginFailure(502)
        return bytes(data)


def byte_range(request, size, etag):
    value = request.headers.get('range')
    if request.headers.get('if-range') and request.headers['if-range'] != etag:
        value = None
    if not value:
        return 0, size - 1, 200
    match = re.fullmatch(r'bytes=(\d*)-(\d*)', value)
    if not match or not any(match.groups()):
        raise OriginFailure(416)
    first, last = match.groups()
    if first:
        start, end = int(first), min(int(last) if last else size - 1, size - 1)
    else:
        start, end = max(0, size - int(last)), size - 1
    if start > end or start >= size:
        raise OriginFailure(416)
    return start, end, 206


class MediaGateway:
    def __init__(self, root, backend, client):
        self.root, self.backend, self.client = Path(root), backend, client
        self.cache = None
        self.grants = {}
        self.policy = None
        self.unreachable_until = 0
        self.completeness = {}
        path = self.root / 'confirmed.json'
        if path.exists():
            saved = json.loads(path.read_text())
            saved_policy = saved['policy']
            saved_policy['mappings'] = tuple(Mapping(**entry) for entry in saved_policy.get('mappings', []))
            candidate = Policy(**saved_policy)
            if candidate.backend != backend:
                raise ValueError('cached configuration belongs to another main server')
            self.configure(candidate, saved['grants'], persist=False)

    def configure(self, policy, grants, persist=True):
        # Legacy mount-only policies have no cache namespace or export contract.
        if not policy.enable_cache and not self.cache and not policy.source_namespace:
            return
        old = self.policy
        self.cache = self.cache or DiskCache(self.root / 'objects', policy.cache_max_bytes)
        self.cache.configure(policy.cache_max_bytes)
        if old and (old.cache_epoch != policy.cache_epoch or old.source_namespace != policy.source_namespace or old.node_id != policy.node_id):
            self.cache.clear()
        self.policy = policy
        self.grants = {field(g, 'Id'): g for g in grants if field(g, 'NodeId') == policy.node_id}
        if persist:
            public_policy = asdict(policy)
            public_policy['new_credential'] = None
            atomic_state(self.root / 'confirmed.json', {'policy': public_policy, 'grants': list(self.grants.values())})

    def complete(self, key, inspect):
        known = self.completeness.get(key)
        if known is None or known[0] != self.cache.inventory:
            known = (self.cache.inventory, inspect())
            if len(self.completeness) >= 1024:
                self.completeness.pop(next(iter(self.completeness)))
            self.completeness[key] = known
        return known[1]

    def save_grant(self, grant):
        if self.grants.get(field(grant, 'Id')) == grant:
            return
        self.grants[field(grant, 'Id')] = grant
        self.configure(self.policy, list(self.grants.values()))

    def block_key(self, identity, index):
        # Block geometry is part of the identity; a size change never reuses a differently aligned block.
        return f"file:{field(identity, 'CacheKey')}:{self.policy.cache_epoch}:{self.policy.cache_block_bytes}:{index}"

    async def cached(self, key, fetch, expected=None, limit=64 * 1024 * 1024):
        if self.policy.enable_cache:
            return await self.cache.get(key, fetch, expected, limit)
        data = await fetch()
        if not isinstance(data, bytes) or len(data) > limit or (expected is not None and len(data) != expected):
            raise OriginFailure()
        return data

    def complete_file(self, identity):
        size, block = field(identity, 'Size'), self.policy.cache_block_bytes
        return self.complete(self.block_key(identity, 0), lambda: all(self.cache.contains(self.block_key(identity, i), min(block, size - i * block)) for i in range((size + block - 1) // block)))

    async def original(self, request, identity, origin, headers, params=None, download=False, offline=False, before_send=None):
        size, key = field(identity, 'Size'), field(identity, 'CacheKey')
        if type(size) is not int or size < 0 or not isinstance(key, str) or not re.fullmatch('[0-9a-f]{64}', key):
            raise OriginFailure()
        etag = '"' + key + '"'
        out = {'Content-Type': field(identity, 'ContentType', 'application/octet-stream'), 'ETag': etag,
               'Accept-Ranges': 'bytes', 'Cache-Control': 'private, no-store', 'X-Jellyfin-Edge-Transfer': 'file-cache' if self.policy.enable_cache else 'file-proxy'}
        if download:
            filename = field(identity, 'FileName', 'video')
            ascii_name = ''.join(c if 32 <= ord(c) < 127 and c not in '\\"' else '_' for c in filename)
            out['Content-Disposition'] = 'attachment; filename="' + ascii_name + '"; filename*=UTF-8\'\'' + quote(filename, safe='')
        if request.headers.get('if-none-match') in (etag, '*'):
            return Response(status_code=304, headers=out)
        try:
            start, end, status = byte_range(request, size, etag)
        except OriginFailure:
            return Response(status_code=416, headers={'Content-Range': f'bytes */{size}'})
        out['Content-Length'] = str(max(0, end - start + 1))
        if status == 206:
            out['Content-Range'] = f'bytes {start}-{end}/{size}'
        if offline and not self.complete_file(identity):
            raise OriginFailure()
        if request.method == 'HEAD' or size == 0:
            return Response(status_code=status, headers=out)
        block = self.policy.cache_block_bytes

        async def get(index):
            lo, hi = index * block, min(size, (index + 1) * block) - 1
            async def fetch():
                if offline:
                    raise OriginFailure()
                async with self.client.stream('GET', origin, params=params, headers={**headers, 'Range': f'bytes={lo}-{hi}', 'Accept-Encoding': 'identity'}) as response:
                    if response.status_code != 206 or response.headers.get('content-range') != f'bytes {lo}-{hi}/{size}' or response.headers.get('etag') != etag:
                        raise OriginFailure(response.status_code)
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > hi - lo + 1:
                            raise OriginFailure(502)
                    return bytes(data)
            return await self.cached(self.block_key(identity, index), fetch, hi - lo + 1)

        # Authorize/fill the first missing block before response headers are committed.
        first = await get(start // block)
        if before_send:
            await before_send()
        async def chunks():
            for index in range(start // block, end // block + 1):
                data = first if index == start // block else await get(index)
                lo = max(0, start - index * block)
                hi = min(len(data), end - index * block + 1)
                yield data[lo:hi]
        return StreamingResponse(chunks(), status_code=status, headers=out)

    async def native(self, request, credential, auth_headers, ready):
        if not ready or not self.policy or not self.policy.enable_cache or not self.policy.enabled or not credential:
            return None
        path = request.url.path
        download = re.fullmatch(r'/Items/([0-9a-f-]{32,36})/Download', path, re.I)
        stream = re.fullmatch(r'/(?:Videos|Audio)/([0-9a-f-]{32,36})/stream(?:\.[A-Za-z0-9]+)?', path, re.I)
        if not download and not (stream and request.query_params.get('Static', request.query_params.get('static')) == 'true'):
            return None
        allowed = {'apikey', 'api_key'} if download else {'apikey', 'api_key', 'static', 'mediasourceid', 'playsessionid', 'deviceid', 'tag'}
        if request.method not in ('GET', 'HEAD') or any(k.lower() not in allowed for k in request.query_params):
            return None
        if len([k for k, _ in request.query_params.multi_items() if k.lower() == 'mediasourceid']) > 1:
            return None
        item = (download or stream)[1]
        params = {'operation': 'download' if download else 'play'}
        source = next((v for k, v in request.query_params.multi_items() if k.lower() == 'mediasourceid'), None)
        if source and not download:
            params['mediaSourceId'] = source
        headers = {**auth_headers, 'X-Jellyfin-Edge-Node': credential}
        endpoint = self.backend + '/JellyfinEdge/node/files/' + item
        metadata = await self.client.get(endpoint, headers=headers, params=params, timeout=httpx.Timeout(5, connect=2))
        if metadata.status_code in (401, 403, 404):
            return Response(status_code=metadata.status_code, headers={'Cache-Control': 'private, no-store'})
        if metadata.status_code != 200:
            return None
        identity = metadata.json()
        if stream and '.' in path.rsplit('/', 1)[1] and path.rsplit('.', 1)[1].lower() != Path(field(identity, 'FileName')).suffix[1:].lower():
            return None
        params['cacheKey'] = field(identity, 'CacheKey')
        async def audit():
            response = await self.client.post(endpoint + '/audit', headers=headers, params={'cacheKey': params['cacheKey']})
            if response.status_code != 204:
                raise OriginFailure(response.status_code)
        return await self.original(request, identity, endpoint + '/bytes', headers, params, download=bool(download), before_send=audit if download else None)

    def playlist_key(self, grant):
        return f"hls:{field(grant, 'ArtifactKey')}:{self.policy.cache_epoch}:playlist"

    def segment_key(self, grant, segment):
        return f"hls:{field(grant, 'ArtifactKey')}:{self.policy.cache_epoch}:{segment['index']}:{segment['runtimeTicks']}:{segment['actualSegmentLengthTicks']}"

    @staticmethod
    def parse_playlist(data):
        text = data.decode('utf-8')
        if not text.startswith('#EXTM3U') or '#EXT-X-ENDLIST' not in text:
            raise OriginFailure(502)
        segments = []
        for line in text.splitlines():
            if not line or line.startswith('#'):
                # TS-only exports have no alternate playlists, keys or initialization maps.
                if 'URI=' in line:
                    raise OriginFailure(502)
                continue
            uri = urlsplit(line.strip())
            match = re.fullmatch(r'hls1/main/(\d+)\.ts', uri.path)
            if not match or uri.scheme or uri.netloc or uri.fragment:
                raise OriginFailure(502)
            query = {k.lower(): v for k, v in parse_qs(uri.query).items()}
            try:
                runtime = query['runtimeticks']
                duration = query['actualsegmentlengthticks']
                if len(runtime) != 1 or len(duration) != 1:
                    raise ValueError()
                segments.append({'index': int(match[1]), 'runtimeTicks': int(runtime[0]), 'actualSegmentLengthTicks': int(duration[0])})
            except (KeyError, ValueError):
                raise OriginFailure(502)
        if not segments:
            raise OriginFailure(502)
        return text, segments

    async def exported(self, request, credential):
        match = re.fullmatch(r'/edge/v1/exports/([0-9a-f]{32})/(file|index\.m3u8|segments/(\d+)\.ts)', request.url.path)
        if not match:
            return None
        if request.method not in ('GET', 'HEAD'):
            return Response(status_code=405)
        token_values = request.query_params.getlist('media_token')
        if len(token_values) != 1 or not re.fullmatch('[0-9a-f]{64}', token_values[0]):
            return Response(status_code=401)
        if not self.policy or not self.policy.enabled:
            return Response(status_code=503)
        ident, asset, ordinal = match[1], match[2], match[3]
        token, offline = token_values[0], False
        endpoint = self.backend + '/JellyfinEdge/node/exports/' + ident
        headers = {'Authorization': 'Bearer ' + (credential or ''), 'X-Jellyfin-Edge-Media': token}
        try:
            if time.monotonic() < self.unreachable_until:
                raise httpx.ConnectError('origin probe backoff')
            response = await self.client.get(endpoint, headers=headers, timeout=httpx.Timeout(3, connect=1))
            if response.status_code in (401, 403, 404):
                self.grants.pop(ident, None)
                self.configure(self.policy, list(self.grants.values()))
                return Response(status_code=response.status_code)
            if response.status_code != 200:
                offline = True
                self.unreachable_until = time.monotonic() + 10
            else:
                grant = response.json()
                if field(grant, 'NodeId') != self.policy.node_id:
                    return Response(status_code=403)
                self.save_grant(grant)
        except httpx.HTTPError:
            offline = True
            if time.monotonic() >= self.unreachable_until:
                self.unreachable_until = time.monotonic() + 10
        grant = self.grants.get(ident)
        if grant is None or field(grant, 'Revoked', False) or not hmac.compare_digest(field(grant, 'TokenHash', ''), hashlib.sha256(token.encode()).hexdigest()):
            return Response(status_code=401)
        expires = field(grant, 'ExpiresAt')
        if expires and datetime.fromisoformat(expires.replace('Z', '+00:00')).timestamp() <= time.time():
            return Response(status_code=410)
        if offline and not self.policy.enable_cache:
            return Response(status_code=503)
        try:
            mode = field(field(grant, 'Output'), 'Mode')
            if mode == 'original':
                if asset != 'file':
                    return Response(status_code=404)
                return await self.original(request, field(grant, 'File'), endpoint + '/bytes', headers, download=field(grant, 'Operation') == 'download', offline=offline)
            if asset == 'file':
                return Response(status_code=404)
            playlist_key = self.playlist_key(grant)
            async def fetch_playlist():
                if offline:
                    raise OriginFailure()
                data = await bounded(self.client, endpoint + '/playlist', headers, limit=4 * 1024 * 1024)
                self.parse_playlist(data)
                return data
            data = await self.cached(playlist_key, fetch_playlist, limit=4 * 1024 * 1024)
            text, segments = self.parse_playlist(data)
            if offline and not self.complete(playlist_key, lambda: all(self.cache.contains(self.segment_key(grant, s)) for s in segments)):
                raise OriginFailure()
            if ordinal is None:
                lines, index = [], 0
                for line in text.splitlines():
                    if line and not line.startswith('#'):
                        line = 'segments/' + str(index) + '.ts?' + urlencode({'media_token': token})
                        index += 1
                    lines.append(line)
                body = ('\n'.join(lines) + '\n').encode()
                out = {'Content-Type': 'application/vnd.apple.mpegurl', 'Cache-Control': 'private, no-store', 'Content-Length': str(len(body))}
                return Response(b'' if request.method == 'HEAD' else body, headers=out)
            index = int(ordinal)
            if index >= len(segments):
                return Response(status_code=404)
            segment = segments[index]
            async def fetch_segment():
                if offline:
                    raise OriginFailure()
                return await bounded(self.client, endpoint + '/segments/' + str(segment['index']), headers, params={k: v for k, v in segment.items() if k != 'index'})
            body = await self.cached(self.segment_key(grant, segment), fetch_segment)
            out = {'Content-Type': 'video/mp2t', 'Cache-Control': 'private, no-store', 'Content-Length': str(len(body)), 'X-Jellyfin-Edge-Transfer': 'share-cache' if self.policy.enable_cache else 'share-proxy'}
            return Response(b'' if request.method == 'HEAD' else body, headers=out)
        except (OriginFailure, ValueError) as error:
            return Response(status_code=error.status if isinstance(error, OriginFailure) else 503)
        except httpx.HTTPError:
            return Response(status_code=503)

    def close(self):
        if self.cache:
            self.cache.close()
