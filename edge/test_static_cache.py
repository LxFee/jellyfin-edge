"""Native cache context, no gateway static-resource inventory or fallback TTL."""
import unittest
import httpx
from control import Policy
from gateway import create_app

class NativeCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_origin_headers_for_html_build_resources_and_user_api(self):
        for path in ('/web/index.html', '/web/future.wasm?v=next', '/web/new/font.any', '/Users/Me'):
            for cache in (None, 'public, max-age=600', 'private, no-cache', 'no-store'):
                def origin(r):
                    headers = {'ETag': '"origin"', 'Last-Modified': 'Wed, 01 Jan 2025 00:00:00 GMT'}
                    if cache is not None:
                        headers['Cache-Control'] = cache
                    return httpx.Response(304 if r.headers.get('if-none-match') else 200,
                                          content=b'' if r.headers.get('if-none-match') else b'native', headers=headers)
                async with httpx.AsyncClient(transport=httpx.MockTransport(origin)) as upstream:
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(Policy('http://origin'), upstream)), base_url='http://edge') as edge:
                        for headers in ({}, {'If-None-Match': '"origin"', 'Cookie': 'caller=own'}):
                            r = await edge.get(path, headers=headers)
                            self.assertEqual(r.headers.get('cache-control'), cache)
                            self.assertEqual(r.headers['etag'], '"origin"')
                            self.assertEqual(r.status_code, 304 if headers else 200)
