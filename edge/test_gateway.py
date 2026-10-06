import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

import httpx
from control import ControlClient, Mapping, Policy, validate_backend
from gateway import create_app, open_media, rewrite_hls, safe_path


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.trust = patch.dict(os.environ, {'EDGE_ORIGIN_PROXY_TRUST_CONFIRMED': 'true'})
        self.trust.start()
        self.tmp = tempfile.TemporaryDirectory()
        with open(self.tmp.name + '/film.mkv', 'wb') as f:
            f.write(b'0123456789')
        self.remote = True
        self.playback = True
        self.access = 'Full'
        self.valid = True
        self.direct = True
        self.fallback = True
        self.proxy_headers = False
        self.config_valid = True
        self.path = '/media/film.mkv'
        self.requests = []
        self.policy = Policy('http://origin.test')

        def origin(r):
            self.requests.append(r)
            if r.url.path == '/JellyfinEdge/node/heartbeat':
                return httpx.Response(204)
            if r.url.path == '/JellyfinEdge/node/config':
                if r.headers.get('authorization') != 'Bearer node-secret':
                    return httpx.Response(401)
                if not self.config_valid:
                    return httpx.Response(401)
                return httpx.Response(200, json={'NodeId': 'node', 'EnableLocalDirectPlay': True, 'AllowOriginFallback': self.fallback, 'TrustProxyHeaders': self.proxy_headers, 'PathMappings': [{'OriginRoot': '/media', 'EdgeRoot': self.tmp.name}]})
            self.assertNotIn('node-secret', str(r.headers))
            public = r.url.path in ('/Users/AuthenticateByName', '/', '/web/index.html', '/web/main.jellyfin.bundle.js', '/Users/Public', '/Branding/Configuration', '/System/Info/Public')
            if not public:
                import re
                if not re.search(r'Token="[^"]+"', r.headers.get('authorization', '')):
                    return httpx.Response(401)
                if not self.valid:
                    return httpx.Response(401)
                if not self.remote:
                    return httpx.Response(403)
                if r.url.path.startswith(('/Videos/', '/Items/')) and (not self.playback or self.access != 'Full'):
                    return httpx.Response(403)
            if r.url.path == '/Users/Me':
                return httpx.Response(200 if self.valid else 401, json={'Id': 'user', 'Policy': {'EnableRemoteAccess': self.remote, 'EnableMediaPlayback': self.playback}})
            if r.url.path == '/Users/user/Items/item':
                return httpx.Response(200, json={'Id': 'item', 'PlayAccess': self.access})
            if r.url.path == '/Items/item/PlaybackInfo':
                return httpx.Response(200, json={'PlaySessionId': 'session', 'MediaSources': [{'Id': 'source', 'Path': self.path, 'Protocol': 'File', 'SupportsDirectPlay': self.direct, 'SupportsDirectStream': True, 'DirectStreamUrl': '/Videos/item/stream?Static=true&native=1', 'SupportsTranscoding': False}]})
            if r.url.path == '/Users/AuthenticateByName':
                return httpx.Response(200, json={'AccessToken': 'user-token', 'User': {'Id': 'user'}}) if self.remote else httpx.Response(403)
            if r.url.path.endswith('.m3u8'):
                return httpx.Response(200, text='#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key?api_key=user-token"\nseg.ts?api_key=user-token\n', headers={'Content-Type': 'application/vnd.apple.mpegurl'})
            return httpx.Response(200, content=b'native', headers={'Content-Type': 'video/mp4' if r.url.path.startswith('/Videos/') else 'text/plain'})
        self.upstream = httpx.AsyncClient(transport=httpx.MockTransport(origin))
        self.app = create_app(self.policy, self.upstream, 'node-secret')
        self.life = self.app.router.lifespan_context(self.app)
        await self.life.__aenter__()
        await asyncio.sleep(.01)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://edge.test')

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.life.__aexit__(None, None, None)
        await self.upstream.aclose()
        self.tmp.cleanup()
        self.trust.stop()

    async def test_no_local_without_negotiated_session(self):
        r = await self.client.get('/edge/media/item/source?api_key=user-token', headers={'Range': 'bytes=2-5'})
        self.assertEqual(r.content, b'native')
        self.assertEqual(self.requests[-1].headers['range'], 'bytes=2-5')

    async def test_fallback_flag_blocks_native_media(self):
        self.fallback = False
        policy = await ControlClient(self.upstream, 'http://origin.test').config('node-secret')
        app = create_app(policy, self.upstream)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://edge.test') as c:
            for path in ('/Videos/item/stream?Static=true', '/Videos/item/master.m3u8', '/Audio/item/stream', '/edge/media/item/source'):
                r = await c.get(path + ('&' if '?' in path else '?') + 'api_key=user-token')
                self.assertEqual(r.status_code, 503)

    async def test_playback_changes_only_proven_source(self):
        r = await self.client.post('/Items/item/PlaybackInfo?api_key=user-token', json={'UserId': 'attacker'})
        source = r.json()['MediaSources'][0]
        self.assertIn('PlaySessionId=session', source['DirectStreamUrl'])
        self.assertFalse(source['SupportsTranscoding'])
        self.direct = False
        r = await self.client.get('/Items/item/PlaybackInfo?api_key=user-token')
        self.assertEqual(r.json()['MediaSources'][0]['DirectStreamUrl'], '/Videos/item/stream?Static=true&native=1')

    async def test_unpaired_proxy_only(self):
        app = create_app(Policy('http://origin.test', (Mapping('/media', self.tmp.name),), 'fake', True), self.upstream)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://edge.test') as c:
            r = await c.get('/edge/media/item/source?api_key=user-token')
            self.assertEqual(r.content, b'native')
            r = await c.get('/Items/item/PlaybackInfo?api_key=user-token')
            self.assertEqual(r.json()['MediaSources'][0]['DirectStreamUrl'], '/Videos/item/stream?Static=true&native=1')

    async def test_permission_and_missing_token(self):
        self.remote = False
        for path in ('/edge/media/item/source', '/Items/item/PlaybackInfo', '/Videos/item/stream'):
            r = await self.client.get(path + '?api_key=user-token')
            self.assertEqual(r.status_code, 403)
        r = await self.client.get('/edge/media/item/source')
        self.assertEqual(r.status_code, 401)
        self.valid = False
        self.remote = True
        r = await self.client.get('/edge/media/item/source?api_key=user-token')
        self.assertEqual(r.status_code, 403)

    async def test_fallback_and_path_ignored(self):
        self.path = '/etc/passwd'
        r = await self.client.get('/edge/media/item/source?api_key=user-token&path=/etc/passwd&backend=http://evil.test')
        self.assertEqual(r.content, b'native')
        self.assertTrue(all(r.url.host == 'origin.test' for r in self.requests))
        self.assertEqual(self.requests[-1].url.params['MediaSourceId'], 'source')

    async def test_hls_and_control_block(self):
        r = await self.client.get('/Videos/item/master.m3u8?api_key=user-token')
        self.assertIn('/Videos/item/seg.ts?api_key=user-token', r.text)
        self.assertIn('URI="/Videos/item/key?api_key=user-token"', r.text)
        r = await self.client.get('/JellyfinEdge/node/config?api_key=user-token')
        self.assertEqual(r.status_code, 401)
        r = await self.client.get('/Videos/%252e%252e/secret?api_key=user-token')
        self.assertEqual(r.status_code, 200)  # Opaque origin path; never a local file.

    async def test_post_bound_native_local_range(self):
        with patch.dict(os.environ, {'EDGE_ORIGIN_PROXY_TRUST_CONFIRMED': 'true'}):
            app = create_app(self.policy, self.upstream, 'node-secret')
        async with app.router.lifespan_context(app):
            await asyncio.sleep(.01)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://edge.test') as c:
                url = '/Videos/item/stream?Static=true&MediaSourceId=source&PlaySessionId=session&api_key=user-token'
                r = await c.get(url)
                self.assertEqual(r.content, b'native')
                r = await c.post('/Items/item/PlaybackInfo?api_key=user-token', json={'DeviceProfile': {}})
                self.assertIn('PlaySessionId=session', r.json()['MediaSources'][0]['DirectStreamUrl'])
                r = await c.get(url, headers={'Range': 'bytes=2-5'})
                self.assertEqual((r.status_code, r.content), (206, b'2345'))
                r = await c.head(url)
                self.assertEqual(r.headers['content-length'], '10')
                for bad in ('PlaySessionId=other', 'MediaSourceId=other', 'api_key=other'):
                    key = bad.split('=')[0]
                    altered = __import__('re').sub(key + '=[^&]+', bad, url)
                    r = await c.get(altered)
                    self.assertEqual(r.content, b'native')
                r = await c.get(url, headers={'Range': 'bytes=100-'})
                self.assertEqual(r.status_code, 416)
                self.access = 'None'
                r = await c.get(url)
                self.assertEqual(r.status_code, 403)

    async def test_control_revoke_expiry_and_fallback_toggle(self):
        self.fallback = False
        app = create_app(self.policy, self.upstream, 'node-secret', refresh_interval=.03)
        async with app.router.lifespan_context(app):
            await asyncio.sleep(.01)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://edge.test') as c:
                url = '/Videos/item/stream?Static=true&MediaSourceId=source&PlaySessionId=session&api_key=user-token'
                r = await c.get('/Items/item/PlaybackInfo?api_key=user-token')
                self.assertNotIn('PlaySessionId', r.json()['MediaSources'][0]['DirectStreamUrl'])
                self.assertEqual((await c.get(url)).status_code, 503)
                await c.post('/Items/item/PlaybackInfo?api_key=user-token', json={})
                self.assertEqual((await c.get(url)).content, b'0123456789')
                with patch('gateway.time.monotonic', return_value=10**12):
                    self.assertEqual((await c.get(url)).status_code, 503)
                await c.post('/Items/item/PlaybackInfo?api_key=user-token', json={})
                self.config_valid = False
                await asyncio.sleep(.05)
                for path in (url, '/edge/media/item/source?api_key=user-token', '/Videos/item/master.m3u8?api_key=user-token', '/Items/item/PlaybackInfo?api_key=user-token'):
                    self.assertEqual((await c.get(path)).status_code, 503)
                self.config_valid = True
                await asyncio.sleep(.05)
                self.assertEqual((await c.get(url)).status_code, 503)
                await c.post('/Items/item/PlaybackInfo?api_key=user-token', json={})
                self.assertEqual((await c.get(url)).content, b'0123456789')

    async def test_stopped_clears_proof_case_insensitive(self):
        self.fallback = False
        app = create_app(self.policy, self.upstream, 'node-secret')
        async with app.router.lifespan_context(app):
            await asyncio.sleep(.01)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://edge.test') as c:
                r = await c.post('/Items/item/PlaybackInfo?ApiKey=user-token', json={})
                url = r.json()['MediaSources'][0]['DirectStreamUrl']
                self.assertIn('ApiKey=', url)
                self.assertEqual((await c.get(url)).content, b'0123456789')
                await c.post('/Sessions/Playing/Stopped?ApiKey=user-token', json={'playSessionId': 'session', 'itemId': 'item'})
                self.assertEqual((await c.get(url)).status_code, 503)

    async def test_login_and_public_resources(self):
        for path in ('/', '/web/index.html', '/web/main.jellyfin.bundle.js', '/Users/Public', '/Branding/Configuration'):
            r = await self.client.get(path)
            self.assertEqual(r.status_code, 200)
        with patch.dict(os.environ, {'EDGE_ORIGIN_PROXY_TRUST_CONFIRMED': 'false'}):
            untrusted = create_app(self.policy, self.upstream)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=untrusted), base_url='http://edge.test') as c:
            for path in ('/Users/AuthenticateByName', '/Videos/item/stream?api_key=user-token'):
                r = await c.post(path, json={'Username': 'u', 'Pw': 'p'})
                self.assertEqual(r.status_code, 200)  # Native origin authority, no login gate.
        with patch.dict(os.environ, {'EDGE_ORIGIN_PROXY_TRUST_CONFIRMED': 'true'}):
            app = create_app(self.policy, self.upstream)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://edge.test') as c:
            r = await c.post('/Users/AuthenticateByName', json={'Username': 'u', 'Pw': 'p'})
            self.assertEqual(r.json()['AccessToken'], 'user-token')
            self.remote = False
            r = await c.post('/Users/AuthenticateByName', json={'Username': 'u', 'Pw': 'p'})
            self.assertEqual(r.status_code, 403)
            self.assertNotIn('AccessToken', r.text)
            r = await c.get('/JellyfinEdge/node/config')
            self.assertEqual(r.status_code, 401)

    async def test_tokens_normalized_and_peer_rebuilt(self):
        r = await self.client.get('/Videos/item/stream?ApiKey=user-token', headers={'X-MediaBrowser-Token': 'user-token', 'Authorization': 'MediaBrowser Client="test", DeviceId="device", Token="user-token"', 'X-Forwarded-For': 'evil', 'X-Real-IP': 'evil'})
        self.assertEqual(r.status_code, 200)
        sent = self.requests[-1]
        self.assertEqual(sent.url.params['ApiKey'], 'user-token')
        self.assertIn('Token="user-token"', sent.headers['authorization'])
        self.assertEqual(sent.headers['x-mediabrowser-token'], 'user-token')
        self.assertNotIn('x-emby-token', sent.headers)
        self.assertIn('DeviceId="device"', sent.headers['authorization'])
        self.assertNotIn('x-emby-authorization', sent.headers)
        self.assertEqual(sent.headers['x-forwarded-for'], '127.0.0.1')
        self.assertNotIn('cache-control', r.headers)
        for headers in [ {'Authorization': 'Bearer user-token', 'X-Emby-Authorization': 'MediaBrowser Token="other"'}, [('Authorization', 'Bearer user-token'), ('Authorization', 'Bearer other')]]:
            r = await self.client.get('/Videos/item/stream', headers=headers)
            self.assertEqual(r.status_code, 401)

    async def test_media_denial_never_falls_back(self):
        for flag in ('playback', 'access'):
            self.playback = flag != 'playback'
            self.access = 'None' if flag == 'access' else 'Full'
            for path in ('/edge/media/item/source', '/Videos/item/stream', '/Items/item/PlaybackInfo'):
                before = len(self.requests)
                r = await self.client.get(path + '?api_key=user-token')
                self.assertEqual(r.status_code, 403)
                if path.startswith('/edge/'):
                    self.assertFalse(any(x.url.path == '/Videos/item/stream' for x in self.requests[before:]))

    async def test_unknown_source_and_if_range_fallback(self):
        for path, headers in [('/edge/media/item/other', {}), ('/edge/media/item/source', {'If-Range': 'stale'})]:
            r = await self.client.get(path + '?api_key=user-token', headers=headers)
            self.assertEqual(r.content, b'native')


class SecurityTests(unittest.TestCase):
    def test_paths_and_symlinks(self):
        for path in ('/media/../secret', '/media/%252e%252e/secret', '//evil', '/media/a\\b'):
            self.assertFalse(safe_path(path))
        with tempfile.TemporaryDirectory() as root:
            os.symlink('/etc/passwd', root + '/link')
            with self.assertRaises(OSError):
                open_media(Policy('http://origin', (Mapping('/media', root),)), {'Protocol': 'File', 'SupportsDirectPlay': True, 'Path': '/media/link'})

    def test_ssrf_and_hls(self):
        for url in ('file:///etc/passwd', 'http://user:password@host', 'http://host?redirect=evil'):
            with self.assertRaises(ValueError):
                validate_backend(url)
        for url in ('http://evil/key', '//evil/key', '../../secret', 'file:///etc/passwd'):
            with self.assertRaises(ValueError):
                rewrite_hls('#EXTM3U\n' + url, 'http://origin/jellyfin', 'http://origin/jellyfin/video/master.m3u8', 'user-token')


class ControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_pascal_pair_and_snake_config(self):
        def origin(r):
            if r.url.path.endswith('/pair'):
                import json
                self.assertEqual(json.loads(r.content), {'Code': '123', 'Name': 'edge'})
                return httpx.Response(200, json={'NodeId': 'n', 'Token': 'secret'})
            return httpx.Response(200, json={'node_id': 'n', 'enable_local_direct_play': True, 'allow_origin_fallback': False, 'path_mappings': [{'origin_root': '/media', 'edge_root': '/mnt/media'}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(origin)) as c:
            control = ControlClient(c, 'http://origin')
            self.assertEqual((await control.pair('123', 'edge'))['NodeId'], 'n')
            policy = await control.config('secret')
            self.assertTrue(policy.enable_local)
            self.assertFalse(policy.allow_fallback)


if __name__ == '__main__':
    unittest.main()
