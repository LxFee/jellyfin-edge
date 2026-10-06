"""Real TCP/ASGI WebSocket relay tests; HTTP identity/control use MockTransport.

No production tokens, clusters or browser modifications are required.
"""
import asyncio
import json
import os
import socket
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx
import uvicorn
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import InvalidStatus, ConnectionClosed

from control import Policy
from gateway import create_app


class WebSocketWireTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.valid = self.remote = self.control = True
        self.uid = 'independent-user'
        self.seen = []
        self.identities = []
        self.origin_messages = []
        self.origin_closed = asyncio.Event()
        self.origin_active = set()
        self.reject_origin = False

        async def origin(ws):
            self.origin_active.add(ws)
            try:
                await ws.send(json.dumps({'MessageType': 'ForceKeepAlive'}))
                async for message in ws:
                    self.origin_messages.append(message)
                    if isinstance(message, bytes):
                        await ws.send(message)
                    else:
                        await ws.send(json.dumps({'MessageType': 'KeepAlive'}))
            finally:
                self.origin_active.discard(ws)
                self.origin_closed.set()

        def handshake(ws, request):
            self.seen.append(request)
            if self.reject_origin:
                return ws.respond(403, 'denied')

        self.origin = await serve(origin, '127.0.0.1', 0, process_request=handshake)
        port = self.origin.sockets[0].getsockname()[1]

        def http_origin(request):
            if request.url.path == '/JellyfinEdge/node/heartbeat':
                return httpx.Response(204)
            if request.url.path == '/JellyfinEdge/node/config':
                self.assertEqual(request.headers['authorization'], 'Bearer fake-node-credential')
                return httpx.Response(200 if self.control else 401, json={
                    'NodeId': 'node', 'TrustProxyHeaders': True})
            self.identities.append(request)
            self.assertEqual(request.url.path, '/Users/Me')
            if 'Token="fake-user-token"' not in request.headers['authorization']:
                return httpx.Response(401)
            return httpx.Response(200 if self.valid else 401, json={
                'Id': self.uid, 'Policy': {'EnableRemoteAccess': self.remote}})

        self.http = httpx.AsyncClient(transport=httpx.MockTransport(http_origin))
        with patch.dict(os.environ, {'EDGE_ORIGIN_PROXY_TRUST_CONFIRMED': 'true'}):
            self.app = create_app(Policy(f'http://127.0.0.1:{port}'), self.http,
                                  'fake-node-credential', refresh_interval=.03)
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(128)
        self.base = f'ws://127.0.0.1:{self.sock.getsockname()[1]}'
        # Uvicorn must NOT consume XFF before gateway's own policy validation.
        self.server = uvicorn.Server(uvicorn.Config(self.app, log_level='critical',
                                                   proxy_headers=False, ws='websockets'))
        self.task = asyncio.create_task(self.server.serve(sockets=[self.sock]))
        for _ in range(100):
            if self.server.started:
                break
            await asyncio.sleep(.01)
        self.assertTrue(self.server.started)

    async def asyncTearDown(self):
        self.server.should_exit = True
        await asyncio.wait_for(self.task, 5)
        self.origin.close()
        await self.origin.wait_closed()
        await self.http.aclose()
        self.sock.close()

    async def denied(self, path, headers=None, subprotocols=None):
        with self.assertRaises(InvalidStatus) as failure:
            async with connect(self.base + path, additional_headers=headers,
                               subprotocols=subprotocols, proxy=None):
                self.fail('unexpected authenticated connection')
        self.assertEqual(failure.exception.response.status_code, 403)

    async def test_modern_user_keepalive_binary_and_safe_forwarding(self):
        async with connect(self.base + '/socket?ApiKey=fake-user-token&deviceId=independent-device&unknown=fake-secret',
                           additional_headers={'X-Forwarded-For': '203.0.113.77, 198.51.100.9',
                                               'Cookie': 'fake-cookie', 'X-Real-IP': 'evil',
                                               'Authorization': 'Bearer fake-user-token'},
                           subprotocols=['fake-secret'], proxy=None) as ws:
            self.assertIsNone(ws.subprotocol)
            self.assertNotIn('fake-secret', str(ws.response.headers))
            self.assertEqual(json.loads(await ws.recv())['MessageType'], 'ForceKeepAlive')
            await ws.send('{"MessageType":"KeepAlive"}')
            self.assertEqual(json.loads(await ws.recv())['MessageType'], 'KeepAlive')
            await ws.send(b'binary-payload')
            self.assertEqual(await ws.recv(), b'binary-payload')
            sent = self.seen[-1]
            query = parse_qs(urlsplit(sent.path).query)
            self.assertEqual(query, {'ApiKey': ['fake-user-token'], 'deviceId': ['independent-device']})
            self.assertEqual(sent.headers['X-Forwarded-For'], '203.0.113.77')
            for header in ('Cookie', 'Authorization', 'X-Emby-Token', 'Sec-WebSocket-Protocol', 'X-Real-IP'):
                self.assertNotIn(header, sent.headers)
            self.assertEqual(self.identities[-1].headers['X-Forwarded-For'], '203.0.113.77')
            self.assertNotIn('fake-node-credential', str(sent.headers) + sent.path)
        await asyncio.wait_for(self.origin_closed.wait(), 2)

    async def test_official_sdk_apikey_only_keepalive(self):
        async with connect(self.base + '/socket?ApiKey=fake-user-token', proxy=None) as ws:
            self.assertEqual(json.loads(await ws.recv())['MessageType'], 'ForceKeepAlive')
            await ws.send('{"MessageType":"KeepAlive"}')
            self.assertEqual(json.loads(await ws.recv())['MessageType'], 'KeepAlive')
            self.assertEqual(parse_qs(urlsplit(self.seen[-1].path).query),
                             {'ApiKey': ['fake-user-token']})
            self.assertTrue(self.identities)
        for field in ('valid', 'remote'):
            setattr(self, field, False)
            await self.denied('/socket?ApiKey=fake-user-token')
            setattr(self, field, True)

    async def test_missing_conflicting_and_subprotocol_tokens_denied(self):
        for path, headers, protocols in [
            ('/socket', None, None),
            ('/socket?ApiKey=invalid', None, None),
            ('/socket?deviceId=d', None, None),
            ('/socket?deviceId=d', None, ['fake-user-token']),
            ('/socket?ApiKey=fake-user-token&deviceId=d', {'Authorization': 'Bearer other'}, None),
            ('/socket?ApiKey=fake-user-token&ApiKey=other&deviceId=d', None, None),
            ('/socket?ApiKey=&deviceId=d', None, None),
        ]:
            await self.denied(path, headers, protocols)
        self.assertEqual(self.seen, [])

    async def test_identity_remote_policy_and_user_id_required(self):
        for field, value in [('valid', False), ('remote', False), ('uid', None), ('uid', '../admin')]:
            old = getattr(self, field)
            setattr(self, field, value)
            await self.denied('/socket?ApiKey=fake-user-token&deviceId=d')
            setattr(self, field, old)
        self.assertEqual(self.seen, [])

    async def test_paths_device_and_malformed_peer_denied(self):
        for path in ['/other', '/socket/', '/SOCKET', '/%73ocket', '/edge/socket',
                     '/socket?ApiKey=fake-user-token&deviceId=a&deviceId=b']:
            await self.denied(path)
        await self.denied('/socket?ApiKey=fake-user-token&deviceId=d', {'X-Forwarded-For': 'evil'})
        await self.denied('/socket?ApiKey=fake-user-token&deviceId=d',
                          [('X-Forwarded-For', '203.0.113.1'), ('X-Forwarded-For', '203.0.113.2')])
        self.assertEqual(self.seen, [])

    async def test_legacy_query_normalized_to_modern_origin(self):
        async with connect(self.base + '/socket?api_key=fake-user-token&deviceId=d', proxy=None) as ws:
            await ws.recv()
            self.assertEqual(parse_qs(urlsplit(self.seen[-1].path).query),
                             {'ApiKey': ['fake-user-token'], 'deviceId': ['d']})

    async def test_revoked_node_closes_idle_socket_and_denies_new_handshake(self):
        async with connect(self.base + '/socket?ApiKey=fake-user-token&deviceId=d', proxy=None) as ws:
            await ws.recv()
            self.control = False
            with self.assertRaises(ConnectionClosed):
                await asyncio.wait_for(ws.recv(), 3)
            self.assertEqual(ws.close_code, 1008)
        await asyncio.wait_for(self.origin_closed.wait(), 2)
        await self.denied('/socket?ApiKey=fake-user-token&deviceId=d')

    async def test_revoked_user_closes_idle_socket(self):
        async with connect(self.base + '/socket?ApiKey=fake-user-token&deviceId=d', proxy=None) as ws:
            await ws.recv()
            self.valid = False
            with self.assertRaises(ConnectionClosed):
                await asyncio.wait_for(ws.recv(), 3)
            self.assertEqual(ws.close_code, 1008)
        await asyncio.wait_for(self.origin_closed.wait(), 2)

    async def test_remote_permission_change_closes_idle_socket(self):
        async with connect(self.base + '/socket?ApiKey=fake-user-token&deviceId=d', proxy=None) as ws:
            await ws.recv()
            self.remote = False
            with self.assertRaises(ConnectionClosed):
                await asyncio.wait_for(ws.recv(), 3)
            self.assertEqual(ws.close_code, 1008)
        await asyncio.wait_for(self.origin_closed.wait(), 2)

    async def test_origin_close_cleans_up_downstream(self):
        async with connect(self.base + '/socket?ApiKey=fake-user-token&deviceId=d', proxy=None) as ws:
            await ws.recv()
            upstream = next(iter(self.origin_active))
            await upstream.close(code=1000, reason='fake-secret-not-forwarded')
            with self.assertRaises(ConnectionClosed):
                await asyncio.wait_for(ws.recv(), 2)
            self.assertEqual(ws.close_reason, '')
        self.assertFalse(self.origin_active)

    async def test_control_failure_denies_before_origin_contact(self):
        self.control = False
        await asyncio.sleep(.08)
        await self.denied('/socket?ApiKey=fake-user-token&deviceId=d')
        self.assertEqual(self.seen, [])

    async def test_origin_handshake_denied_before_downstream_accept(self):
        self.reject_origin = True
        await self.denied('/socket?ApiKey=fake-user-token&deviceId=d')

    async def test_proxy_trust_false_ignores_spoofed_xff(self):
        # Control config refresh is paused by removing the app's node credential
        # via a separate server, rather than overriding the production policy.
        await self.task_stop()
        with patch.dict(os.environ, {'EDGE_ORIGIN_PROXY_TRUST_CONFIRMED': 'true'}):
            app = create_app(Policy(self.http_backend()), self.http)
        self.server = uvicorn.Server(uvicorn.Config(app, log_level='critical', proxy_headers=False))
        self.task = asyncio.create_task(self.server.serve(sockets=[self.sock]))
        while not self.server.started:
            await asyncio.sleep(.01)
        async with connect(self.base + '/socket?ApiKey=fake-user-token&deviceId=d',
                           additional_headers={'X-Forwarded-For': 'evil'}, proxy=None) as ws:
            await ws.recv()
            self.assertEqual(self.seen[-1].headers['X-Forwarded-For'], '127.0.0.1')

    def http_backend(self):
        return f'http://127.0.0.1:{self.origin.sockets[0].getsockname()[1]}'

    async def task_stop(self):
        self.server.should_exit = True
        await asyncio.wait_for(self.task, 5)
        # Uvicorn closes the listening socket on shutdown; make a fresh one.
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(128)
        self.base = f'ws://127.0.0.1:{self.sock.getsockname()[1]}'

    async def test_operator_trust_unconfirmed_denies(self):
        await self.task_stop()
        with patch.dict(os.environ, {'EDGE_ORIGIN_PROXY_TRUST_CONFIRMED': 'false'}):
            app = create_app(Policy(self.http_backend()), self.http)
        self.server = uvicorn.Server(uvicorn.Config(app, log_level='critical', proxy_headers=False))
        self.task = asyncio.create_task(self.server.serve(sockets=[self.sock]))
        while not self.server.started:
            await asyncio.sleep(.01)
        await self.denied('/socket?ApiKey=fake-user-token&deviceId=d')
        self.assertEqual(self.seen, [])
