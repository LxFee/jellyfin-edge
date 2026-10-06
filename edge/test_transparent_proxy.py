"""Native authorizer and real compressed wire fixtures, not a Users/Me-only gate."""
import gzip
import hashlib
import json
import re
import unittest
import zlib

import brotli
import httpx
from control import Policy
from gateway import create_app


class NativeProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.payload = ('native browser resource 中文 ' * 1000).encode()
        self.encoding = 'gzip'
        self.status = 200

        def origin(r):
            self.calls.append(r)
            auth = r.headers.get('authorization', '')
            token = re.search(r'Token="([^"]+)"', auth)
            token = token[1] if token else None
            # Simulate native token/remote policy/elevation at the actual endpoint.
            if not r.url.path.startswith('/web/'):
                if token not in ('user', 'admin', 'remote-denied'):
                    return httpx.Response(401, json={'error': 'native authentication'})
                if token == 'remote-denied' and r.headers.get('x-forwarded-for') == '203.0.113.9':
                    return httpx.Response(403, json={'error': 'native remote policy'})
                if r.url.path == '/System/Configuration' and token != 'admin':
                    return httpx.Response(403, json={'error': 'native elevation'})
                if r.url.path == '/Users/Me':
                    return httpx.Response(200, json={'Id': token, 'Policy': {'EnableRemoteAccess': token != 'remote-denied'}})
                return httpx.Response(200, json={'Items': [], 'authorizer': 'native'})
            encode = {'gzip': gzip.compress, 'deflate': zlib.compress, 'br': brotli.compress,
                      'future': lambda b: b}[self.encoding]
            body = encode(self.payload)
            return httpx.Response(self.status, stream=httpx.ByteStream(body), headers={
                'Content-Encoding': self.encoding, 'Content-Length': str(len(body)),
                'Content-Type': 'application/javascript; charset=utf-8', 'ETag': '"wire"',
                'Vary': 'Accept-Encoding', 'Set-Cookie': 'private-session=secret' if self.status == 500 else ''})

        self.origin = httpx.AsyncClient(transport=httpx.MockTransport(origin))
        self.edge = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(
            Policy('http://origin.test', trust_proxy_headers=True), self.origin)), base_url='http://edge.test')

    async def asyncTearDown(self):
        await self.edge.aclose()
        await self.origin.aclose()

    async def test_native_authorizer_same_status_body_single_call(self):
        for path, token, expected in [('/Items', 'user', 200), ('/Items', 'invalid', 401),
                ('/Items', 'remote-denied', 403), ('/System/Configuration', 'user', 403),
                ('/System/Configuration', 'admin', 200)]:
            h = {'Authorization': f'MediaBrowser DeviceId="native-device", Token="{token}"',
                 'X-Forwarded-For': '203.0.113.9'}
            direct = await self.origin.get('http://origin.test' + path, headers=h)
            self.calls.clear()
            edge = await self.edge.get(path, headers=h)
            self.assertEqual((edge.status_code, edge.json()), (expected, direct.json()))
            self.assertEqual([r.url.path for r in self.calls], [path])
            self.assertIn('DeviceId="native-device"', self.calls[0].headers['authorization'])
        self.calls.clear()
        for _ in range(10):
            self.assertEqual((await self.edge.get('/Items?ApiKey=user')).status_code, 200)
        self.assertEqual(len(self.calls), 10)  # old blanket identity lookup: 20

    async def test_wire_encoding_length_charset_etag_and_errors(self):
        for encoding, decode in [('gzip', gzip.decompress), ('deflate', zlib.decompress),
                                 ('br', brotli.decompress), ('future', lambda b: b)]:
            self.encoding = encoding
            async with self.edge.stream('GET', '/web/main.jellyfin.bundle.js',
                    headers={'Accept-Encoding': encoding}) as r:
                raw = b''.join([b async for b in r.aiter_raw()])
                self.assertEqual(self.calls[-1].headers['accept-encoding'], encoding)
                self.assertEqual(r.headers['content-encoding'], encoding)
                self.assertEqual(int(r.headers['content-length']), len(raw))
                self.assertEqual(r.headers['content-type'], 'application/javascript; charset=utf-8')
                self.assertEqual(r.headers['etag'], '"wire"')
                self.assertEqual(hashlib.sha256(decode(raw)).digest(), hashlib.sha256(self.payload).digest())
                self.assertEqual(r.headers.get('set-cookie'), '')
            self.assertNotIn('cookie', self.calls[-1].headers)
        self.encoding = 'gzip'
        self.status = 500
        async with self.edge.stream('GET', '/web/main.jellyfin.bundle.js') as r:
            raw = b''.join([b async for b in r.aiter_raw()])
            self.assertEqual(r.status_code, 500)
            self.assertEqual(gzip.decompress(raw), self.payload)

    async def test_head_and_304_have_headers_no_body(self):
        for method, status in [('HEAD', 200), ('GET', 304)]:
            self.status = status
            r = await self.edge.request(method, '/web/main.jellyfin.bundle.js',
                                        headers={'If-None-Match': '"wire"'})
            self.assertEqual(r.content, b'')
            self.assertEqual(r.headers['content-encoding'], 'gzip')
            self.assertEqual(r.headers['etag'], '"wire"')
            self.assertEqual(self.calls[-1].headers['if-none-match'], '"wire"')
