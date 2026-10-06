import asyncio
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from cache import DiskCache
from control import Policy
from gateway import create_app
from media import MediaGateway

ITEM = '1' * 32
NODE = '12345678-1234-1234-1234-123456789012'
EXPORT = 'e' * 32
TOKEN = 'a' * 64


class CacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_baseurl_is_forwarded_once_and_raw_query_preserved(self):
        seen = []
        async def origin(request):
            seen.append(str(request.url))
            return httpx.Response(200, content=b'opaque')
        async with httpx.AsyncClient(transport=httpx.MockTransport(origin)) as upstream:
            app = create_app(Policy('http://main.test/jf'), upstream)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://node.test') as client:
                await client.get('/jf/web/a.js?x=a%2Fb&x=a+b')
                await client.get('/web/a.js?x=a%2Fb&x=a+b')
        self.assertEqual(seen, ['http://main.test/jf/web/a.js?x=a%2Fb&x=a+b'] * 2)
    async def test_coalescing_failure_quota_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = DiskCache(directory, 10)
            calls = 0
            async def fetch():
                nonlocal calls
                calls += 1
                await asyncio.sleep(.02)
                return b'12345'
            result = await asyncio.gather(*(cache.get('one', fetch, 5) for _ in range(20)))
            self.assertEqual(result, [b'12345'] * 20)
            self.assertEqual(calls, 1)
            self.assertEqual(cache.locks, {})
            async def bad(): return b'short'
            with self.assertRaises(ValueError): await cache.get('bad', bad, 10)
            self.assertFalse(cache.contains('bad'))
            await cache.get('two', fetch, 5)
            cache.read('one')
            await cache.get('three', fetch, 5)
            self.assertFalse(cache.contains('two'))
            self.assertTrue(cache.contains('one'))
            cache.close()
            restored = DiskCache(directory, 10)
            self.assertEqual(restored.read('one'), b'12345')
            restored.clear()
            self.assertFalse(restored.contains('one'))
            restored.close()

    async def test_different_misses_run_concurrently_and_clear_discards_inflight_fill(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = DiskCache(directory, 100)
            started = set()
            release = asyncio.Event()
            async def fill(key):
                started.add(key)
                await release.wait()
                return b'bytes'
            tasks = [asyncio.create_task(cache.get(key, lambda key=key: fill(key))) for key in ('a', 'b')]
            await asyncio.sleep(.01)
            self.assertEqual(started, {'a', 'b'})
            cache.clear()
            release.set()
            await asyncio.gather(*tasks)
            self.assertFalse(cache.contains('a'))
            self.assertFalse(cache.contains('b'))
            cache.close()


class MediaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.content = b'0123456789' * 15000
        self.version = '1' * 64
        self.offline = False
        self.revoked = False
        self.denied = False
        self.calls = []
        self.policy = Policy('http://main.test', node_id=NODE, enabled=True, enable_cache=True,
                             cache_block_bytes=65536, cache_max_bytes=1000000, source_namespace='ns')
        self.grant = {'Id': EXPORT, 'NodeId': NODE, 'TokenHash': hashlib.sha256(TOKEN.encode()).hexdigest(),
                      'ExpiresAt': None, 'Operation': 'share', 'ArtifactKey': 'f' * 64,
                      'File': self.identity(), 'Output': {'Mode': 'hls'}, 'Revoked': False}

        async def origin(request):
            self.calls.append(request)
            if self.offline:
                raise httpx.ConnectError('offline', request=request)
            path = request.url.path
            if path == '/JellyfinEdge/node/config':
                return httpx.Response(200, json={'NodeId': NODE, 'EnableCache': True, 'CacheBlockBytes': 65536,
                    'CacheMaxBytes': 1000000, 'Enabled': True, 'AllowOriginFallback': True, 'SourceNamespace': 'ns', 'Exports': [] if self.revoked else [self.grant]})
            if path == '/JellyfinEdge/node/heartbeat': return httpx.Response(204)
            if path.startswith('/JellyfinEdge/node/files/'):
                self.assertEqual(request.headers['x-jellyfin-edge-node'], 'node-only')
                if self.denied or 'denied' in request.headers.get('authorization', ''): return httpx.Response(403)
                if path.endswith('/audit'): return httpx.Response(204)
                if path.endswith('/bytes'):
                    self.assertEqual(request.url.params['cacheKey'], self.version)
                    await asyncio.sleep(.005)
                    lo, hi = map(int, request.headers['range'][6:].split('-'))
                    return httpx.Response(206, content=self.content[lo:hi + 1], headers={'Content-Range': f'bytes {lo}-{hi}/{len(self.content)}', 'ETag': '"' + self.version + '"'})
                return httpx.Response(200, json=self.identity())
            if path == '/JellyfinEdge/node/exports/' + EXPORT:
                self.assertEqual(request.headers['authorization'], 'Bearer node-only')
                self.assertEqual(request.headers['x-jellyfin-edge-media'], TOKEN)
                return httpx.Response(404 if self.revoked else 200, json=self.grant)
            if path == '/JellyfinEdge/node/exports/' + EXPORT + '/bytes':
                lo, hi = map(int, request.headers['range'][6:].split('-'))
                return httpx.Response(206, content=self.content[lo:hi + 1], headers={'Content-Range': f'bytes {lo}-{hi}/{len(self.content)}', 'ETag': '"' + self.version + '"'})
            if path.endswith('/playlist'):
                return httpx.Response(200, content=b'#EXTM3U\n#EXT-X-PLAYLIST-TYPE:VOD\n#EXTINF:6,\nhls1/main/0.ts?runtimeTicks=0&actualSegmentLengthTicks=60000000\n#EXTINF:2,\nhls1/main/1.ts?runtimeTicks=60000000&actualSegmentLengthTicks=20000000\n#EXT-X-ENDLIST\n')
            if '/segments/' in path:
                return httpx.Response(200, content=b'transcoded-' + path[-1:].encode())
            if path.endswith('/main.m3u8'):
                return httpx.Response(200, content=b'#EXTM3U\n#EXTINF:2,\nhls/main/0.ts\n', headers={'Content-Type': 'application/vnd.apple.mpegurl'})
            if path == '/JellyfinEdge/exports':
                self.assertEqual(request.headers['x-jellyfin-edge-node'], 'node-only')
                return httpx.Response(200, json={'Url': 'scoped'})
            return httpx.Response(200, content=b'normal')
        self.origin = httpx.AsyncClient(transport=httpx.MockTransport(origin))
        self.app = create_app(self.policy, self.origin, 'node-only', refresh_interval=10, cache_dir=self.directory.name)
        self.life = self.app.router.lifespan_context(self.app)
        await self.life.__aenter__()
        await asyncio.sleep(.02)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(self.app), base_url='http://edge.test')

    def identity(self):
        return {'CacheKey': self.version, 'Size': len(self.content), 'FileName': 'video.mp4', 'ContentType': 'video/mp4'}

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.life.__aexit__(None, None, None)
        await self.origin.aclose()
        self.directory.cleanup()

    def native(self, token='allowed'):
        return '/Videos/' + ITEM + '/stream?Static=true&MediaSourceId=' + ITEM + '&ApiKey=' + token

    def share(self, asset='index.m3u8'):
        return '/edge/v1/exports/' + EXPORT + '/' + asset + '?media_token=' + TOKEN

    def byte_calls(self): return [r for r in self.calls if r.url.path.endswith('/bytes')]

    async def test_native_range_coalescing_cross_user_authorization_and_version_change(self):
        response = await asyncio.gather(*(self.client.get(self.native(str(i)), headers={'Range': 'bytes=100-199'}) for i in range(12)))
        self.assertTrue(all(r.status_code == 206 and r.content == self.content[100:200] for r in response))
        self.assertEqual(len(self.byte_calls()), 1)
        denied = await self.client.get(self.native('denied'), headers={'Range': 'bytes=100-199'})
        self.assertEqual(denied.status_code, 403)
        self.version = '2' * 64
        self.content = b'x' * len(self.content)
        changed = await self.client.get(self.native(), headers={'Range': 'bytes=100-199'})
        self.assertEqual(changed.content, b'x' * 100)
        self.assertEqual(len(self.byte_calls()), 2)

    async def test_download_head_suffix_conditional_and_audit(self):
        url = '/Items/' + ITEM + '/Download?ApiKey=allowed'
        head = await self.client.head(url)
        self.assertEqual((head.status_code, head.content, head.headers['content-length']), (200, b'', str(len(self.content))))
        self.assertEqual(self.byte_calls(), [])
        suffix = await self.client.get(url, headers={'Range': 'bytes=-10'})
        self.assertEqual((suffix.status_code, suffix.content), (206, self.content[-10:]))
        self.assertIn('attachment', suffix.headers['content-disposition'])
        self.assertEqual(len([r for r in self.calls if r.url.path.endswith('/audit')]), 1)
        conditional = await self.client.get(url, headers={'If-None-Match': '"' + self.version + '"'})
        self.assertEqual(conditional.status_code, 304)
        bad = await self.client.get(url, headers={'Range': 'bytes=900000-'})
        self.assertEqual(bad.status_code, 416)

    async def test_normal_transcodes_are_never_cached_and_node_context_cannot_be_spoofed(self):
        url = '/Videos/' + ITEM + '/main.m3u8?ApiKey=allowed'
        for _ in range(2): self.assertEqual((await self.client.get(url)).status_code, 200)
        self.assertEqual(len([r for r in self.calls if r.url.path.endswith('/main.m3u8')]), 2)
        await self.client.post('/JellyfinEdge/exports', headers={'X-Jellyfin-Edge-Node': 'forged'})

    async def test_cache_disabled_keeps_online_exports_and_does_not_serve_offline(self):
        directory = Path(self.directory.name) / 'disabled'
        gateway = MediaGateway(directory, self.policy.backend, self.origin)
        gateway.configure(replace(self.policy, enable_cache=False), [self.grant])
        for _ in range(2):
            self.assertEqual((await gateway.exported(httpx_request(self.share()), 'node-only')).status_code, 200)
            response = await gateway.exported(httpx_request(self.share('segments/0.ts')), 'node-only')
            self.assertEqual(response.body, b'transcoded-0')
            self.assertEqual(response.headers['x-jellyfin-edge-transfer'], 'share-proxy')
        self.assertEqual(len([r for r in self.calls if '/segments/' in r.url.path]), 2)
        self.assertEqual(gateway.cache.db.execute('SELECT COUNT(*) FROM objects').fetchone()[0], 0)
        self.offline = True
        self.assertEqual((await gateway.exported(httpx_request(self.share()), 'node-only')).status_code, 503)
        gateway.close()
        restored = MediaGateway(directory, self.policy.backend, self.origin)
        self.assertFalse(restored.policy.enable_cache)
        self.assertEqual((await restored.exported(httpx_request(self.share()), 'node-only')).status_code, 503)
        restored.close()

    async def test_hls_cache_offline_restart_revocation_and_no_account_token_in_playlist(self):
        playlist = await self.client.get(self.share())
        self.assertEqual(playlist.status_code, 200)
        self.assertNotIn('ApiKey', playlist.text)
        self.assertNotIn('node-only', playlist.text)
        self.assertIn('media_token=' + TOKEN, playlist.text)
        for i in range(2):
            for _ in range(2): self.assertEqual((await self.client.get(self.share('segments/' + str(i) + '.ts'))).content, b'transcoded-' + str(i).encode())
        self.assertEqual(len([r for r in self.calls if '/segments/' in r.url.path]), 2)
        self.offline = True
        self.assertEqual((await self.client.get(self.share())).status_code, 200)
        self.assertEqual((await self.client.get(self.share('segments/1.ts'))).content, b'transcoded-1')
        restored = MediaGateway(self.directory.name, self.policy.backend, self.origin)
        self.assertEqual(len(restored.grants), 1)
        self.assertEqual((await restored.exported(httpx_request(self.share()), 'node-only')).status_code, 200)
        restored.close()
        self.offline = False
        await self.client.get('/System/Info/Public')
        self.revoked = True
        self.assertEqual((await self.client.get(self.share())).status_code, 404)
        self.offline = True
        self.assertEqual((await self.client.get(self.share())).status_code, 401)

    async def test_export_original_offline_only_after_complete_fill_and_expiry(self):
        self.grant['Output']['Mode'] = 'original'
        self.assertEqual((await self.client.get(self.share('file'), headers={'Range': 'bytes=0-9'})).content, self.content[:10])
        self.offline = True
        self.assertEqual((await self.client.get(self.share('file'), headers={'Range': 'bytes=0-9'})).status_code, 503)
        self.offline = False
        await self.client.get('/System/Info/Public')
        self.assertEqual((await self.client.get(self.share('file'))).content, self.content)
        self.offline = True
        self.assertEqual((await self.client.get(self.share('file'))).content, self.content)
        self.grant['ExpiresAt'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        # Simulate confirmed expiring grant before going offline.
        self.offline = False
        await self.client.get('/System/Info/Public')
        self.assertEqual((await self.client.get(self.share('file'))).status_code, 410)
        self.offline = True
        self.assertEqual((await self.client.get(self.share('file'))).status_code, 410)


def httpx_request(url):
    from starlette.requests import Request
    path, query = url.split('?', 1)
    return Request({'type': 'http', 'method': 'GET', 'path': path, 'query_string': query.encode(), 'headers': [], 'scheme': 'http', 'server': ('edge.test', 80)})
