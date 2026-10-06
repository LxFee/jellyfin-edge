"""Native Jellyfin HLS: parent has ApiKey, children retain only transcode params."""
import os
import re
import unittest
from unittest.mock import patch
from urllib.parse import parse_qsl, urlsplit

import httpx
from control import Policy
from gateway import create_app


class HLSAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.trust = patch.dict(os.environ, {'EDGE_ORIGIN_PROXY_TRUST_CONFIRMED': 'true'})
        self.trust.start()
        self.requests = []
        self.valid = True
        self.master = ('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=192000\n'
                       'main.m3u8?VideoBitrate=64000&AudioBitrate=128000&PlaySessionId=session\n')
        self.main = ('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key?keyid=1"\n'
                     '#EXT-X-MAP:URI="init.mp4?tag=a%26b",BYTERANGE="100@0"\n'
                     '#EXTINF:3,\nhls1/segment.ts?PlaySessionId=session\n')

        def origin(request):
            self.requests.append(request)
            self.assertEqual(request.url.host, 'origin.test')
            if not self.valid:
                return httpx.Response(401)
            if 'Token="user-token"' not in request.headers.get('authorization', ''):
                return httpx.Response(401)
            self.assertNotIn('node-secret', str(request.headers))
            self.assertNotIn('admin-secret', str(request.headers))
            # Original query is preserved; the native origin authorizes it.
            if request.url.path == '/jellyfin/Users/Me':
                return httpx.Response(200 if self.valid else 401, json={
                    'Id': 'user', 'Policy': {'EnableRemoteAccess': True, 'EnableMediaPlayback': True}})
            if request.url.path == '/jellyfin/Users/user/Items/item':
                return httpx.Response(200, json={'PlayAccess': 'Full'})
            if request.url.path.endswith('.m3u8'):
                text = self.master if request.url.path.endswith('/master.m3u8') else self.main
                return httpx.Response(200, text=text, headers={'Content-Type': 'application/vnd.apple.mpegurl', 'Cache-Control': 'public'})
            return httpx.Response(200, content=b'native')

        self.upstream = httpx.AsyncClient(transport=httpx.MockTransport(origin))
        app = create_app(Policy('http://origin.test/jellyfin'), self.upstream)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://edge.test')

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.upstream.aclose()
        self.trust.stop()

    def children(self, text):
        return [line for line in text.splitlines() if line and not line.startswith('#')] + re.findall(r'URI="([^"]*)"', text)

    async def test_native_master_main_segments_without_inherited_headers(self):
        for query, headers in [('ApiKey=user-token', {}), ('aPiKeY=user-token', {}),
                               ('VideoBitrate=64000', {'Authorization': 'MediaBrowser Client="Web", Token="user-token"'})]:
            with self.subTest(query=query):
                # Without rewrite, a standard hls.js child has neither token nor parent headers.
                unauthenticated = await self.client.get('/Videos/item/main.m3u8?VideoBitrate=64000')
                self.assertEqual(unauthenticated.status_code, 401)
                master = await self.client.get('/Videos/item/master.m3u8?' + query, headers=headers)
                self.assertEqual(master.status_code, 200)
                child = self.children(master.text)[0]
                self.assertEqual(dict(parse_qsl(urlsplit(child).query))['VideoBitrate'], '64000')
                main = await self.client.get(child)  # Deliberately no headers.
                self.assertEqual(main.status_code, 200)
                self.assertIn('BYTERANGE="100@0"', main.text)
                for uri in self.children(main.text):
                    self.assertEqual([v for k, v in parse_qsl(urlsplit(uri).query) if k.lower() in ('apikey', 'api_key')], ['user-token'])
                    response = await self.client.get(uri)
                    self.assertEqual(response.status_code, 200)
                    if 'mpegurl' in response.headers.get('content-type', ''):
                        self.assertEqual(response.headers['cache-control'], 'private, no-store')
                self.assertEqual(master.headers['cache-control'], 'private, no-store')
                self.assertEqual(main.headers['cache-control'], 'private, no-store')

    async def test_existing_token_preserved_conflict_rejected(self):
        for key in ('ApiKey', 'api_key', 'aPiKeY'):
            for token in ('user-token', 'other', 'admin-secret', 'node-secret', ''):
                with self.subTest(key=key, token=token):
                    self.master = '#EXTM3U\nmain.m3u8?' + key + '=' + token + '&tag=one&tag=two\n'
                    response = await self.client.get('/Videos/item/master.m3u8?ApiKey=user-token')
                    self.assertEqual(response.status_code, 200 if token == 'user-token' else 502)
                    if token == 'user-token':
                        pairs = parse_qsl(urlsplit(self.children(response.text)[0]).query)
                        self.assertEqual(pairs, [(key, token), ('tag', 'one'), ('tag', 'two')])
                    else:
                        self.assertNotIn(token or 'user-token', response.text)
                    if response.status_code == 200:
                        self.assertEqual(response.headers['cache-control'], 'private, no-store')
        self.master = '#EXTM3U\nmain.m3u8?ApiKey=user-token&api_key=other\n'
        self.assertEqual((await self.client.get('/Videos/item/master.m3u8?ApiKey=user-token')).status_code, 502)

    async def test_all_uri_attributes_and_absolute_same_origin(self):
        self.master = ('#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",URI="audio.m3u8?tag=1"\n'
                       '#EXT-X-I-FRAME-STREAM-INF:BANDWIDTH=1,URI="http://origin.test/jellyfin/Videos/item/iframe.m3u8"\n'
                       '#EXT-X-SESSION-KEY:METHOD=AES-128,URI="/jellyfin/Videos/item/key"\n'
                       'http://origin.test/jellyfin/Videos/item/main.m3u8?backend=http%3A%2F%2Fevil.test\n')
        response = await self.client.get('/Videos/item/master.m3u8?ApiKey=user-token')
        self.assertEqual(response.status_code, 200)
        for uri in self.children(response.text):
            self.assertTrue(uri.startswith('/Videos/item/'))
            self.assertEqual(dict(parse_qsl(urlsplit(uri).query))['ApiKey'], 'user-token')
            self.assertEqual((await self.client.get(uri)).status_code, 200)
        self.assertTrue(all(r.url.host == 'origin.test' for r in self.requests))

    async def test_query_encoding_and_complete_attribute_parsing(self):
        self.master = ('#EXTM3U\n#EXT-X-MEDIA:NAME="label,URI=not-a-resource",TYPE=AUDIO,'
                       'URI="audio.m3u8?tag=a%26b&tag=&Api%4Bey=user%2Dtoken"\n')
        response = await self.client.get('/Videos/item/master.m3u8?ApiKey=user-token')
        self.assertEqual(response.status_code, 200)
        self.assertIn('NAME="label,URI=not-a-resource"', response.text)
        self.assertIn('URI="/Videos/item/audio.m3u8?tag=a%26b&tag=&Api%4Bey=user%2Dtoken"', response.text)
        self.master = '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI=key\n'
        response = await self.client.get('/Videos/item/master.m3u8?ApiKey=user-token')
        self.assertEqual(response.status_code, 502)
        self.assertNotIn('user-token', response.text)

    async def test_unsafe_children_and_unverified_parent_fail_closed(self):
        for uri in ('https://evil.test/main.m3u8', '//evil.test/key',
                    'http://origin.test:81/jellyfin/key', 'http://user@origin.test/jellyfin/key',
                    '/outside/key', '../%252e%252e/secret', 'key#fragment', 'file:///etc/passwd'):
            for entry in (uri, '#EXT-X-KEY:METHOD=AES-128,URI="' + uri + '"'):
                with self.subTest(uri=entry):
                    self.master = '#EXTM3U\n' + entry + '\n'
                    response = await self.client.get('/Videos/item/master.m3u8?ApiKey=user-token')
                    self.assertEqual(response.status_code, 502)
                    self.assertNotIn('user-token', response.text)
        self.valid = False
        before = len(self.requests)
        response = await self.client.get('/Videos/item/master.m3u8?ApiKey=user-token')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(len(self.requests), before + 1)
