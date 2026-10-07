#!/usr/bin/env python3
"""Offline Python + /bin/sh entrypoint tests; no real Jellyfin or network."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("external_web", ROOT / "prepare-external-web.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
URL = "https://scripts.example.com/extension.js"


class ExternalWebTests(unittest.TestCase):
    def test_validation(self):
        for value in [URL, "https://example.com/a.js?a=1&b=2", "https://[::1]:443/a.js"]:
            self.assertEqual(module.validate_url(value), value)
        for value in ["http://example.com/a.js", "//example.com/a", "https://", "https://user:pass@example.com/a", "https://example.com/'", 'https://example.com/"', "https://example.com/<script>", "https://example.com/`", "https://example.com/\\x", "https://example.com/\na", "https://example.com/ a", "https://example.com/%xx", "https://example.com:99999/a", "https://example.com:bad/a"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                module.validate_url(value)

    def test_copy_idempotence_and_readonly_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "official"
            source.mkdir()
            original = '<html><head><title>Official</title></head><body></body></html>'
            (source / "index.html").write_text(original)
            (source / "asset.js").write_text("original")
            (source / "index.html").chmod(0o444)
            target = root / "cache/edge-web"
            try:
                for url in [URL, URL, "https://example.com/a.js?a=1&b=2"]:
                    module.prepare(source, target, url)
                    result = (target / "index.html").read_text()
                    self.assertEqual(result.count('id="jellyfin-edge-external-script"'), 1)
                    self.assertEqual((source / "index.html").read_text(), original)
                    self.assertEqual((target / "asset.js").read_text(), "original")
                self.assertIn("a=1&amp;b=2", result)
                self.assertNotIn(URL, result)
                with self.assertRaises(ValueError):
                    module.prepare(source, source, URL)
                (source / "index.html").chmod(0o644)
                (source / "index.html").write_text("no head")
                with self.assertRaises(ValueError):
                    module.prepare(source, target, URL)
            finally:
                (source / "index.html").chmod(0o644)

    def test_shell_entrypoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plugin = root / "plugin"
            plugin.mkdir()
            for name in ["manifest.json", "Jellyfin.Plugin.Edge.dll"]:
                (plugin / name).write_text("fixture")
            server = root / "server"
            server.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
            server.chmod(0o755)
            script = (ROOT / "host-entrypoint.sh").read_text().replace('/opt/jellyfin-edge/plugin', str(plugin)).replace('/usr/local/lib/jellyfin-edge/prepare-external-web.py', str(ROOT / 'prepare-external-web.py')).replace('/usr/bin/python3', sys.executable).replace('exec /jellyfin/jellyfin', 'exec ' + str(server))
            entry = root / "entry.sh"
            entry.write_text(script)
            source = root / "web"
            source.mkdir()
            (source / "index.html").write_text('<head></head>')
            env = dict(os.environ, JELLYFIN_DATA_DIR=str(root / "data"), JELLYFIN_CACHE_DIR=str(root / "cache"), JELLYFIN_WEB_DIR=str(source))
            env.pop('JELLYFIN_EXTERNAL_SCRIPT_URL', None)
            def run(*args):
                return subprocess.run(['/bin/sh', str(entry), *args], env=env, capture_output=True, text=True)
            r = run('--foo', 'two words', '--webdir', '/original')
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.splitlines(), ['--foo', 'two words', '--webdir', '/original'])
            self.assertFalse((root / 'cache').exists())
            manifest = root / 'data/plugins/Jellyfin.Edge_2.0.0.1/manifest.json'
            manifest.write_text('disabled')
            env['JELLYFIN_EXTERNAL_SCRIPT_URL'] = URL
            for _ in range(2):
                r = run('--foo', 'two words')
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stdout.splitlines(), ['--foo', 'two words', '--webdir', str(root / 'cache/edge-web')])
            self.assertEqual(manifest.read_text(), 'disabled')
            for arg in ['--webdir', '--webdir=/other', '-w', '-w/other']:
                self.assertNotEqual(run(arg).returncode, 0)
            env['JELLYFIN_EXTERNAL_SCRIPT_URL'] = 'http://example.com/a'
            self.assertNotEqual(run().returncode, 0)
            env['JELLYFIN_EXTERNAL_SCRIPT_URL'] = URL
            bad_cache = root / 'not-a-directory'
            bad_cache.write_text('blocked')
            env['JELLYFIN_CACHE_DIR'] = str(bad_cache)
            r = run()
            self.assertNotEqual(r.returncode, 0)
            self.assertEqual(r.stdout, '')  # Never silently start the server.


if __name__ == '__main__':
    unittest.main()
