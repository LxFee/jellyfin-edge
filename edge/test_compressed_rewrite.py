import asyncio
import gzip
import unittest

import httpx
from gateway import create_app
import test_gateway


class CompressedRewriteTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_gateway.GatewayTests.asyncSetUp
    asyncTearDown = test_gateway.GatewayTests.asyncTearDown

    async def test_attachment_urls_bind_only_caller_item_source_and_index(self):
        native = self.upstream._transport.handler
        paths = ['/Videos/item/source/Attachments/4', '/Videos/other/source/Attachments/5',
                 'https://evil.test/Videos/item/source/Attachments/6', '/Videos/item/wrong/Attachments/7',
                 '/Videos/item/source/Attachments/99']
        def origin(request):
            response = native(request)
            if request.url.path.endswith('/PlaybackInfo'):
                doc = response.json()
                doc['MediaSources'][0]['MediaAttachments'] = [
                    {'Index': i+4, 'DeliveryUrl': p} for i,p in enumerate(paths)]
                return httpx.Response(200, json=doc)
            return response
        async with httpx.AsyncClient(transport=httpx.MockTransport(origin)) as upstream:
            app = create_app(self.policy, upstream, 'node-secret')
            async with app.router.lifespan_context(app):
                await asyncio.sleep(.01)
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://edge.test') as c:
                    r = await c.post('/Items/item/PlaybackInfo?ApiKey=user-token', json={})
                    attachments = r.json()['MediaSources'][0]['MediaAttachments']
                    self.assertEqual(attachments[0]['DeliveryUrl'], paths[0]+'?ApiKey=user-token')
                    self.assertEqual(attachments[2]['DeliveryUrl'], paths[2])
                    for index in (1, 3, 4):
                        self.assertEqual(attachments[index]['DeliveryUrl'], paths[index] + '?ApiKey=user-token')
                    self.assertEqual((await c.get(paths[0])).status_code, 401)
                    self.assertNotIn('node-secret', r.text)

    async def test_compressed_negotiation_hls_and_index_proof(self):
        native = self.upstream._transport.handler

        def origin(request):
            if request.url.path == '/web/main.jellyfin.bundle.js':
                return httpx.Response(200, content=b'js', headers={'Content-Type': 'application/javascript'})
            response = native(request)
            if request.url.path.endswith('/PlaybackInfo') or request.url.path.endswith('.m3u8'):
                raw = gzip.compress(response.content)
                headers = dict(response.headers)
                headers.update({'Content-Encoding': 'gzip', 'Content-Length': str(len(raw)), 'ETag': '"old-body"'})
                return httpx.Response(response.status_code, headers=headers, stream=httpx.ByteStream(raw))
            if request.url.path == '/web/index.html':
                raw = gzip.compress(b'<script src="main.jellyfin.bundle.js?aaaaaaaaaaaaaaaaaaaa"></script>')
                return httpx.Response(200, headers={'Content-Type': 'text/html', 'Content-Encoding': 'gzip',
                                                   'Content-Length': str(len(raw))}, stream=httpx.ByteStream(raw))
            return response

        async with httpx.AsyncClient(transport=httpx.MockTransport(origin)) as upstream:
            app = create_app(self.policy, upstream, 'node-secret')
            async with app.router.lifespan_context(app):
                await asyncio.sleep(.01)
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://edge.test') as c:
                    r = await c.post('/Items/item/PlaybackInfo?ApiKey=user-token', json={})
                    url = r.json()['MediaSources'][0]['DirectStreamUrl']
                    self.assertIn('PlaySessionId=session', url)
                    self.assertEqual((await c.get(url, headers={'Range': 'bytes=2-5'})).content, b'2345')
                    self.assertNotIn('content-encoding', r.headers)
                    self.assertNotIn('etag', r.headers)
                    self.assertEqual(int(r.headers['content-length']), len(r.content))
                    r = await c.get('/Videos/item/master.m3u8?ApiKey=user-token')
                    self.assertIn('/Videos/item/seg.ts?api_key=user-token', r.text)
                    self.assertNotIn('content-encoding', r.headers)
                    self.assertEqual(int(r.headers['content-length']), len(r.content))
                    r = await c.get('/web/index.html')
                    self.assertEqual(r.headers['content-encoding'], 'gzip')
                    self.assertIn('aaaaaaaaaaaaaaaaaaaa', r.text)
                    r = await c.get('/web/main.jellyfin.bundle.js?aaaaaaaaaaaaaaaaaaaa')
                    self.assertNotIn('cache-control', r.headers)
