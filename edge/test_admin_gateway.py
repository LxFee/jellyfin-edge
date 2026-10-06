"""Native RequiresElevation and plugin signatures remain origin-owned."""
import json
import unittest
import httpx
from control import Policy
from gateway import create_app

NODE = '01234567-89ab-cdef-0123-456789abcdef'
class AdminGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_admin_remote_and_library_authority(self):
        calls = []
        def origin(r):
            calls.append(r)
            auth = r.headers.get('authorization', '')
            if 'Token="admin"' in auth:
                return httpx.Response(200, json={'native': True}, headers={'Cache-Control': 'no-store'})
            if 'Token="remote-disabled"' in auth:
                return httpx.Response(403)
            if 'Token="user"' in auth:
                return httpx.Response(404 if r.url.path.startswith('/Users/user/Items/') else 403)
            return httpx.Response(401)
        async with httpx.AsyncClient(transport=httpx.MockTransport(origin)) as upstream:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(Policy('http://origin'), upstream)), base_url='http://edge') as edge:
                for path in (f'/JellyfinEdge/admin/nodes/{NODE}/revoke', '/JellyfinEdge/admin/status', '/System/Configuration', f'/Plugins/{NODE}/Configuration', '/Users/user/Items/restricted'):
                    for token in (None, 'admin', 'user', 'remote-disabled', 'invalid'):
                        h = {'Authorization': f'MediaBrowser Client="Native", Token="{token}"'} if token else {}
                        direct = await upstream.post('http://origin' + path, headers=h, json={'sent': 'unchanged'})
                        calls.clear()
                        r = await edge.post(path, headers=h, json={'sent': 'unchanged'})
                        self.assertEqual((r.status_code, r.content), (direct.status_code, direct.content))
                        self.assertEqual(len(calls), 1)
                        self.assertEqual(json.loads(calls[0].content), {'sent': 'unchanged'})
                        self.assertEqual(calls[0].headers.get('authorization'), h.get('Authorization'))

    async def test_plugin_bearer_not_reinterpreted_and_unknown_route_not_blocked(self):
        calls = []
        def origin(r):
            calls.append(r)
            return httpx.Response(200 if r.headers.get('authorization') == 'Bearer native-node' else 401)
        async with httpx.AsyncClient(transport=httpx.MockTransport(origin)) as upstream:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(Policy('http://origin'), upstream)), base_url='http://edge') as edge:
                for path in ('/JellyfinEdge/node/config', '/JellyfinEdge/enroll', '/JellyfinEdge/future'):
                    for token, status in [('native-node', 200), ('invalid', 401)]:
                        r = await edge.get(path, headers={'Authorization': 'Bearer ' + token})
                        self.assertEqual(r.status_code, status)
                        self.assertEqual(calls[-1].headers['authorization'], 'Bearer ' + token)
