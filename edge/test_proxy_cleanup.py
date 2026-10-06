"""ASGI cancellation/error ownership without buffering the origin stream."""
import asyncio
import unittest

import httpx
from control import Policy
from gateway import create_app


class CountingStream(httpx.AsyncByteStream):
    def __init__(self, fail=False):
        self.reads = 0
        self.closes = 0
        self.fail = fail

    async def __aiter__(self):
        for _ in range(100):
            self.reads += 1
            if self.fail:
                raise httpx.ReadError('origin disconnected')
            yield b'x' * 4096

    async def aclose(self):
        self.closes += 1


class CleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_before_body_origin_error_and_backpressure(self):
        for mode in ('before-body', 'client-send-error', 'origin-error'):
            stream = CountingStream(fail=mode == 'origin-error')
            async with httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda r: httpx.Response(200, stream=stream))) as origin:
                app = create_app(Policy('http://origin.test'), origin)
                scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'},
                         'http_version': '1.1', 'method': 'GET', 'scheme': 'http',
                         'path': '/web/main.jellyfin.bundle.js', 'raw_path': b'/web/main.jellyfin.bundle.js',
                         'query_string': b'', 'headers': [], 'client': ('127.0.0.1', 1000),
                         'server': ('edge.test', 80)}
                async def receive():
                    return {'type': 'http.request', 'body': b'', 'more_body': False}
                async def send(message):
                    if mode == 'before-body' and message['type'] == 'http.response.start':
                        raise asyncio.CancelledError()
                    if mode == 'client-send-error' and message['type'] == 'http.response.body':
                        self.assertEqual(stream.reads, 1)  # no eager origin buffering
                        raise OSError('client disconnected')
                try:
                    await app(scope, receive, send)
                except (asyncio.CancelledError, OSError, httpx.ReadError):
                    pass
                except Exception as e:
                    # Starlette reports send-side OSError as ClientDisconnect.
                    self.assertEqual(type(e).__name__, 'ClientDisconnect')
                self.assertEqual(stream.closes, 1, mode)
                self.assertLessEqual(stream.reads, 1, mode)
