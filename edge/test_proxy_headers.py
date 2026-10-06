import asyncio
import unittest

import httpx
from control import ControlClient, Policy
from gateway import create_app
import test_gateway


class ProxyHeadersTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_gateway.GatewayTests.asyncSetUp
    asyncTearDown = test_gateway.GatewayTests.asyncTearDown

    async def test_on_identity_forwarding_login_and_binding(self):
        self.proxy_headers = True
        app = create_app(self.policy, self.upstream, 'node-secret', refresh_interval=.03)
        async with app.router.lifespan_context(app):
            await asyncio.sleep(.01)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://edge.test') as c:
                headers = {'X-Forwarded-For': '2001:0db8::1, 10.0.0.2', 'Forwarded': 'for=evil', 'X-Real-IP': 'evil', 'X-Forwarded-Host': 'evil'}
                before = len(self.requests)
                r = await c.post('/Items/item/PlaybackInfo?ApiKey=user-token', json={}, headers=headers)
                url = r.json()['MediaSources'][0]['DirectStreamUrl']
                self.assertEqual((await c.get(url, headers=headers)).content, b'0123456789')
                for sent in (r for r in self.requests[before:] if not r.url.path.startswith('/JellyfinEdge/node/')):
                    self.assertEqual(sent.headers['x-forwarded-for'], '2001:db8::1')
                    for name in ('forwarded', 'x-real-ip', 'x-forwarded-host'):
                        self.assertNotIn(name, sent.headers)
                changed = {'X-Forwarded-For': '192.0.2.9, 10.0.0.2'}
                self.assertEqual((await c.get(url, headers=changed)).content, b'native')
                before = len(self.requests)
                await c.post('/Users/AuthenticateByName', json={}, headers=changed)
                login = [r for r in self.requests[before:] if r.url.path == '/Users/AuthenticateByName']
                self.assertEqual(len(login), 1)
                self.assertEqual(login[0].headers['x-forwarded-for'], '192.0.2.9')
                self.proxy_headers = False
                await asyncio.sleep(.05)
                before = len(self.requests)
                self.assertEqual((await c.get(url, headers=headers)).content, b'native')
                media_requests = [r for r in self.requests[before:] if not r.url.path.startswith('/JellyfinEdge/node/')]
                self.assertTrue(media_requests)
                self.assertEqual(media_requests[-1].headers['x-forwarded-for'], '127.0.0.1')

    async def test_malformed_duplicate_and_multihop(self):
        app = create_app(Policy('http://origin.test', trust_proxy_headers=True), self.upstream)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://edge.test') as c:
            for value in ('evil', '', '192.0.2.1,', ',192.0.2.1', '192.0.2.1, unknown', '127.1', '010.0.0.1', '192.0.2.1:80', '[::1]', 'fe80::1%eth0', '2001:db8:::1', '192.0.2.1\tfoo'):
                before = len(self.requests)
                r = await c.get('/System/Info/Public', headers={'X-Forwarded-For': value})
                self.assertEqual(r.status_code, 400, value)
                self.assertEqual(len(self.requests), before)
            r = await c.get('/System/Info/Public', headers=[('X-Forwarded-For', '192.0.2.1'), ('x-forwarded-for', '192.0.2.2')])
            self.assertEqual(r.status_code, 400)
            for headers, expected in [({}, '127.0.0.1'), ({'X-Forwarded-For': '192.0.2.1, 2001:db8::2, 10.0.0.1'}, '192.0.2.1')]:
                self.assertEqual((await c.get('/System/Info/Public', headers=headers)).status_code, 200)
                self.assertEqual(self.requests[-1].headers['x-forwarded-for'], expected)

    async def test_off_ignores_spoof_and_ambiguity(self):
        for headers in ({'X-Forwarded-For': '192.0.2.99'}, [('X-Forwarded-For', 'evil'), ('X-Forwarded-For', '192.0.2.1')]):
            r = await self.client.get('/System/Info/Public', headers=headers)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(self.requests[-1].headers['x-forwarded-for'], '127.0.0.1')

    async def test_control_default_pascal_snake_and_strict_bool(self):
        for key in ('TrustProxyHeaders', 'trust_proxy_headers'):
            for value in (None, False, True, 'true', 1):
                data = {'NodeId': 'n'}
                if value is not None:
                    data[key] = value
                async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=data))) as c:
                    control = ControlClient(c, 'http://origin')
                    if value is not None and type(value) is not bool:
                        with self.assertRaises(ValueError):
                            await control.config('secret')
                    else:
                        self.assertEqual((await control.config('secret')).trust_proxy_headers, value is True)
