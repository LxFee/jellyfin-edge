"""Download authorization is independent of PlaybackInfo and playback permissions."""
import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch
import httpx
from control import Policy
from gateway import create_app

ITEM = 'a' * 32
ALT = 'b' * 32

class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.trust = patch.dict(os.environ, {'EDGE_ORIGIN_PROXY_TRUST_CONFIRMED': 'true'})
        self.trust.start()
        self.tmp = tempfile.TemporaryDirectory()
        for name, data in [('中文.mp4', b'0123456789'), ('alt.mp4', b'alternative')]:
            with open(self.tmp.name + '/' + name, 'wb') as f: f.write(data)
        self.permission = True
        self.authorization = 200
        self.fallback = False
        self.path = '/media/中文.mp4'
        self.calls = []
        self.control_valid = True
        self.local_enabled = True
        def origin(r):
            self.calls.append(r)
            if r.url.path == '/JellyfinEdge/node/heartbeat':
                return httpx.Response(204)
            if r.url.path == '/JellyfinEdge/node/config':
                if not self.control_valid:
                    return httpx.Response(401)
                return httpx.Response(200, json={'NodeId': 'node', 'EnableLocalDirectPlay': self.local_enabled,
                    'AllowOriginFallback': self.fallback, 'PathMappings': [{'OriginRoot': '/media', 'EdgeRoot': self.tmp.name}]})
            if r.url.path == '/Users/Me':
                return httpx.Response(200, json={'Id': 'user', 'Policy': {'EnableRemoteAccess': True,
                    'EnableMediaPlayback': False, 'EnableContentDownloading': self.permission}})
            if '/download-authorization/' in r.url.path:
                self.assertEqual(r.headers['x-jellyfin-edge-node'], 'node-secret')
                self.assertIn('Token="user-token"', r.headers['authorization'])
                if r.method == 'POST':
                    self.assertIn('Path', __import__('json').loads(r.content))
                    return httpx.Response(204 if self.authorization == 200 else self.authorization)
                item = r.url.path.rsplit('/', 1)[1]
                return httpx.Response(self.authorization, json={'NodeId': 'node', 'ItemId': item,
                    'Path': '/media/alt.mp4' if item == ALT else self.path,
                    'FileName': '中文.mp4', 'ContentType': 'video/mp4'})
            if r.url.path.endswith('/Download'): return httpx.Response(200, content=b'official-origin')
            raise AssertionError('unexpected origin request (no playback negotiation allowed)')
        self.upstream = httpx.AsyncClient(transport=httpx.MockTransport(origin))
        self.app = create_app(Policy('http://origin.test'), self.upstream, 'node-secret', refresh_interval=.02)
        self.life = self.app.router.lifespan_context(self.app)
        await self.life.__aenter__()
        await asyncio.sleep(.01)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://edge.test')
    async def asyncTearDown(self):
        await self.client.aclose(); await self.life.__aexit__(None, None, None)
        await self.upstream.aclose(); self.tmp.cleanup(); self.trust.stop()
    def url(self, item=ITEM): return '/Items/' + item + '/Download?ApiKey=user-token'
    async def test_original_range_head_and_utf8_without_playback(self):
        r = await self.client.get(self.url())
        self.assertEqual((r.status_code, r.content), (200, b'0123456789'))
        self.assertEqual(r.headers['x-jellyfin-edge-transfer'], 'local-download')
        self.assertEqual(r.headers['content-type'], 'video/mp4')
        self.assertIn("filename*=UTF-8''%E4%B8%AD%E6%96%87.mp4", r.headers['content-disposition'])
        for value, expected in [('bytes=2-5', b'2345'), ('bytes=-3', b'789'), ('bytes=7-', b'789')]:
            r = await self.client.get(self.url(), headers={'Range': value})
            self.assertEqual((r.status_code, r.content), (206, expected))
        r = await self.client.head(self.url())
        self.assertEqual((r.status_code, r.content, r.headers['content-length']), (200, b'', '10'))
        self.assertFalse(any(r.url.path.endswith('/Download') for r in self.calls))
        status = (await self.client.get('/edge-status')).json()['Downloads']
        self.assertEqual(status['OriginBytes'], 0)
        self.assertEqual(status['OriginRequests'], 0)
        self.assertEqual(status['LocalBytes'], 20)
    async def test_exact_alternative_item_and_no_source_query_invention(self):
        r = await self.client.get(self.url(ALT))
        self.assertEqual(r.content, b'alternative')
        r = await self.client.get(self.url() + '&MediaSourceId=' + ALT)
        self.assertEqual(r.status_code, 503)
    async def test_denied_anonymous_revoked_and_hidden(self):
        self.assertEqual((await self.client.get('/Items/' + ITEM + '/Download')).status_code, 401)
        self.permission = False
        self.assertEqual((await self.client.get(self.url())).status_code, 403)
        self.permission = True
        for denied in [401, 403, 404]:
            self.authorization = denied
            self.assertEqual((await self.client.get(self.url())).status_code, denied)
        self.assertFalse(any(r.url.path.endswith('/Download') for r in self.calls))
    async def test_ranges_and_path_security(self):
        for value in ['bytes=100-', 'bytes=0-1,3-4', 'bytes=-0', 'bytes=a-b']:
            r = await self.client.get(self.url(), headers={'Range': value})
            self.assertEqual(r.status_code, 416)
            self.assertEqual(r.headers['content-range'], 'bytes */10')
        os.symlink('/etc/passwd', self.tmp.name + '/link')
        for path in ['/etc/passwd', '/media/../etc/passwd', '/media/link', '/media/missing']:
            self.path = path
            self.assertEqual((await self.client.get(self.url())).status_code, 503)
    async def test_local_read_disabled_obeys_fallback(self):
        self.local_enabled = False
        await asyncio.sleep(.06)
        r = await self.client.get(self.url())
        self.assertEqual(r.status_code, 503)
        self.fallback = True
        await asyncio.sleep(.06)
        r = await self.client.get(self.url())
        self.assertEqual((r.status_code, r.content), (200, b'official-origin'))
        self.assertEqual(r.headers['x-jellyfin-edge-transfer'], 'origin-download')
        self.assertFalse(any('/download-authorization/' in r.url.path for r in self.calls))

    async def test_origin_unavailable_no_local_bytes(self):
        await self.upstream.aclose()
        # MockTransport itself remains callable after close, httpx correctly rejects client use.
        # Network outage behavior is tested with a real transport exception instead.
        def outage(r):
            raise httpx.ConnectError('origin unavailable', request=r)
        upstream = httpx.AsyncClient(transport=httpx.MockTransport(outage))
        app = create_app(Policy('http://origin.test'), upstream, 'node-secret')
        async with app.router.lifespan_context(app):
            await asyncio.sleep(.01)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://edge.test') as c:
                r = await c.get(self.url())
                self.assertEqual(r.status_code, 503)
                counters = (await c.get('/edge-status')).json()['Downloads']
                self.assertEqual(counters['LocalBytes'], 0)
                self.assertEqual(counters['OriginBytes'], 0)
        await upstream.aclose()

    async def test_control_revocation_fails_closed_even_with_fallback(self):
        self.fallback = True
        self.control_valid = False
        await asyncio.sleep(.06)
        r = await self.client.get(self.url())
        self.assertEqual(r.status_code, 503)
        self.assertFalse(any(r.url.path.endswith('/Download') for r in self.calls))

    async def test_missing_file_fallback_is_official_endpoint(self):
        self.path = '/media/missing'
        self.fallback = True
        await asyncio.sleep(.05)
        r = await self.client.get(self.url())
        self.assertEqual((r.status_code, r.content), (200, b'official-origin'))
        downloads = [r for r in self.calls if r.url.path.endswith('/Download')]
        self.assertEqual(len(downloads), 1)
        self.assertEqual(downloads[0].url.path, '/Items/' + ITEM + '/Download')
        self.assertNotIn('x-jellyfin-edge-node', downloads[0].headers)
