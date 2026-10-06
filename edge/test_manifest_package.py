"""The deployed gateway must not depend on a pinned native web manifest."""
import pathlib
import unittest

class PackageTests(unittest.TestCase):
    def test_no_runtime_manifest_dependency(self):
        root = pathlib.Path(__file__).parent
        self.assertNotIn('native-web-manifest', (root / 'Dockerfile').read_text())
        self.assertNotIn('NATIVE_WEB_MANIFEST', (root / 'gateway.py').read_text())
