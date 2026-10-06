"""Future native resources require no gateway release or manifest approval."""
import unittest
import httpx
from control import Policy
from gateway import create_app

class NativeResourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_resources_and_encoded_paths_use_origin_authority(self):
        calls = []
        def origin(r):
            calls.append(r)
            return httpx.Response(301 if r.url.path == '/web' else 200,
                                  headers={'Location': '/web/'} if r.url.path == '/web' else {}, content=b'official')
        async with httpx.AsyncClient(transport=httpx.MockTransport(origin)) as upstream:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(Policy('http://origin.test'), upstream)), base_url='http://edge.test') as edge:
                for path in ('/web/future.wasm?build=next', '/web/new/api/resource', '/future/native', '/web/libraries/%6cibarchive.wasm', '/web/index.html%3fApiKey=fixture', '/web/index.html%23fragment', '/web'):
                    calls.clear()
                    r = await edge.get(path)
                    self.assertEqual(r.status_code, 301 if path == '/web' else 200)
                    self.assertEqual(len(calls), 1)
                    if path == '/web':
                        self.assertEqual(r.headers['location'], '/web/')
