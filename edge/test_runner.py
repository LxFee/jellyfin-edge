import json
import os
import tempfile
import unittest
from pathlib import Path
import httpx
from runner import Runner, atomic_state


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_retry_rotation_and_no_rejoin(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'state' / 'node.json'
            enrollment = Path(d) / 'enrollment'
            enrollment.write_text('A' * 64)
            roots = [d]
            calls = []
            def origin(request):
                calls.append(json.loads(request.content))
                return httpx.Response(200, json={'NodeId': '12345678-1234-1234-1234-123456789012', 'Token': 'B' * 64})
            async with httpx.AsyncClient(transport=httpx.MockTransport(origin)) as c:
                runner = Runner('https://main.internal', path, enrollment, 'test', roots)
                identity = runner.data.copy()
                # Simulate an enrollment response lost before local durable commit.
                await runner.credential(c)
                atomic_state(path, identity)
                retry = Runner('https://main.internal', path, enrollment, 'test', roots)
                await retry.credential(c)
                self.assertEqual(calls[0], calls[1])
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
                retry.rotate('C' * 64)
                enrollment.unlink()
                restarted = Runner('https://main.internal', path, enrollment, 'test', roots)
                self.assertEqual(await restarted.credential(c), 'C' * 64)
                self.assertEqual(len(calls), 2)
                self.assertEqual(restarted.data['InstanceId'], identity['InstanceId'])
                self.assertFalse(restarted.permits('/etc'))
                self.assertTrue(restarted.permits(d))

    async def test_corrupt_state_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'node.json'
            path.write_text('corrupt')
            with self.assertRaises(ValueError):
                Runner('https://main.internal', path, '/missing', 'test', [d])
