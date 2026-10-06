"""Images and metadata inherit native auth/cache/cookie semantics."""
import unittest
import httpx
from control import Policy
from gateway import create_app

ITEM = 'aa03b53b58abdbcaaafe2fd211a96134'
class PublicImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_images_branding_and_private_routes_match_origin(self):
        calls = []
        def origin(r):
            calls.append(r)
            if 'Token="revoked"' in r.headers.get('authorization', ''):
                return httpx.Response(401)
            if r.url.path.startswith(('/Branding/', f'/Items/{ITEM}/Images/')):
                return httpx.Response(200, content=b'image', headers={'Content-Type': 'image/png', 'Cache-Control': 'public, max-age=86400', 'ETag': '"original"', 'Set-Cookie': 'own=caller'})
            return httpx.Response(401)
        async with httpx.AsyncClient(transport=httpx.MockTransport(origin)) as upstream:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(Policy('http://origin', allow_fallback=False), upstream)), base_url='http://edge') as edge:
                for path in (f'/Items/{ITEM}/Images/Primary', f'/Items/{ITEM}/Images/FutureType', '/Branding/Configuration', '/Branding/Css', '/Items', '/System/Configuration', '/Users/Me'):
                    for method in ('GET', 'HEAD'):
                        h = {'Cookie': 'caller=own', 'If-None-Match': '"original"'}
                        direct = await upstream.request(method, 'http://origin' + path, headers=h)
                        calls.clear()
                        r = await edge.request(method, path, headers=h)
                        self.assertEqual(r.status_code, direct.status_code)
                        self.assertEqual(r.content, b'' if method == 'HEAD' else direct.content)
                        self.assertEqual(r.headers.get('cache-control'), direct.headers.get('cache-control'))
                        self.assertEqual(r.headers.get_list('set-cookie'), direct.headers.get_list('set-cookie'))
                        self.assertEqual(len(calls), 1)
                        self.assertEqual(calls[0].headers['cookie'], 'caller=own')
                self.assertEqual((await edge.get(f'/Items/{ITEM}/Images/Primary?ApiKey=revoked')).status_code, 401)
