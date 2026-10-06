"""Empty and malformed native Authorization is the origin's decision."""
import unittest
import httpx
from control import Policy
from gateway import create_app

class LogoutMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_modern_headers_opaque_even_after_logout(self):
        calls = []
        def origin(r):
            calls.append(r)
            return httpx.Response(200 if r.url.path.startswith('/Branding/') else 401, json={})
        async with httpx.AsyncClient(transport=httpx.MockTransport(origin)) as upstream:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(Policy('http://origin'), upstream)), base_url='http://edge') as edge:
                for auth in ('MediaBrowser Client="Web", DeviceId="web", Token=""', 'MediaBrowser Token=unquoted', 'MediaBrowser Token="", Token="invalid"'):
                    for path, status in [('/Branding/Configuration', 200), ('/Users/Me', 401)]:
                        r = await edge.get(path, headers={'Authorization': auth})
                        self.assertEqual(r.status_code, status)
                        self.assertEqual(calls[-1].headers['authorization'], auth)
