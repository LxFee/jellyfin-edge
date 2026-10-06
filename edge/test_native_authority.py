"""Default proxy contract: origin authority and per-caller wire semantics."""
import unittest
import httpx
from control import Policy
from gateway import create_app

class NativeAuthorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_resources_auth_cookies_and_opaque_path(self):
        calls = []
        def origin(r):
            calls.append(r)
            if r.url.path.startswith('/System/'):
                return httpx.Response(401 if 'authorization' not in r.headers else 403)
            return httpx.Response(200, content=b'native', headers=[
                ('set-cookie', 'session=caller; HttpOnly'), ('set-cookie', 'pref=own'),
                ('etag', '"native"')])
        async with httpx.AsyncClient(transport=httpx.MockTransport(origin)) as upstream:
            upstream.cookies.set('ambient', 'must-not-leak')
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(
                    Policy('http://origin'), upstream)), base_url='http://edge') as edge:
                for cookie in ('session=first', 'session=second'):
                    r = await edge.get('/web/future/unknown.wasm?build=new', headers={'Cookie': cookie})
                    self.assertEqual(r.status_code, 200)
                    self.assertEqual(calls[-1].headers['cookie'], cookie)
                    self.assertEqual(r.headers.get_list('set-cookie'), ['session=caller; HttpOnly', 'pref=own'])
                    self.assertNotIn('cache-control', r.headers)
                # Neither forward nor internal authorization can persist cookies.
                self.assertEqual(list(upstream.cookies.jar), [])
                await edge.get('/future', headers={})
                # edge's own jar is caller-owned; use a fresh caller with no cookies.
                async with httpx.AsyncClient(transport=edge._transport, base_url='http://edge') as other:
                    await other.get('/future')
                    self.assertNotIn('cookie', calls[-1].headers)
                r = await edge.get('/future/file%3f%23%252f?opaque=%2f+%23', headers={'Cookie': 'own=yes'})
                self.assertEqual(r.status_code, 200)
                self.assertEqual(calls[-1].url.raw_path, b'/future/file%3f%23%252f?opaque=%2f+%23')
                self.assertEqual((await edge.get('/System/Future')).status_code, 401)
                auth = 'MediaBrowser Client="Original", DeviceId="actual", Token="user", Future="keep"'
                self.assertEqual((await edge.get('/System/Future', headers={'Authorization': auth})).status_code, 403)
                self.assertEqual(calls[-1].headers['authorization'], auth)
                self.assertEqual((await edge.get('/future', headers={'Authorization': 'MediaBrowser Token=malformed'})).status_code, 200)

    async def test_native_web_audio_is_not_media_fallback(self):
        def origin(r):
            return httpx.Response(200, content=b'native', headers={'Content-Type': 'audio/mpeg'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(origin)) as upstream:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(
                    Policy('http://origin', allow_fallback=False), upstream)), base_url='http://edge') as edge:
                self.assertEqual((await edge.get('/web/future/build-sound')).status_code, 200)
                self.assertEqual((await edge.get('/future/media-output')).status_code, 503)

    async def test_enrollment_is_authorized_by_origin(self):
        def origin(r):
            if r.url.path == '/JellyfinEdge/enroll':
                return httpx.Response(200 if r.headers.get('authorization') == 'Bearer registration' else 401,
                                      json={'pending': True})
            return httpx.Response(404)
        async with httpx.AsyncClient(transport=httpx.MockTransport(origin)) as upstream:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(
                    Policy('http://origin'), upstream)), base_url='http://edge') as edge:
                for token, status in [('invalid', 401), ('registration', 200)]:
                    self.assertEqual((await edge.post('/JellyfinEdge/enroll', headers={'Authorization': 'Bearer ' + token}, json={'InstanceId': 'fixture', 'Nonce': 'fixture'})).status_code, status)
