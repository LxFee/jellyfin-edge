"""Package only public plugin artifacts and generate Jellyfin repository metadata."""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--repo', required=True)
parser.add_argument('--tag', required=True)
parser.add_argument('--previous', type=Path)
args = parser.parse_args()
meta = json.loads((ROOT / 'plugin/manifest.json').read_text(encoding='utf-8'))
version = ET.parse(ROOT / 'plugin/Jellyfin.Plugin.Edge.csproj').findtext('./PropertyGroup/Version')
if not re.fullmatch(r'\d+\.\d+\.\d+\.\d+', version or ''):
    raise SystemExit('Plugin version must have four numeric components')
if meta['version'] != version or args.tag != 'v' + version:
    raise SystemExit('Tag, project and manifest versions must match')
if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', args.repo):
    raise SystemExit('Invalid repository')
out = ROOT / 'artifacts/release'
out.mkdir(parents=True, exist_ok=True)
package = out / ('Jellyfin.Edge_' + version + '.zip')
with zipfile.ZipFile(package, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
    for name, path in [('Jellyfin.Plugin.Edge.dll', ROOT / 'plugin/bin/Release/net10.0/Jellyfin.Plugin.Edge.dll'), ('LICENSE', ROOT / 'LICENSE'), ('THIRD_PARTY_NOTICES.md', ROOT / 'THIRD_PARTY_NOTICES.md')]:
        info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        archive.writestr(info, path.read_bytes())
data = package.read_bytes()
(out / 'SHA256SUMS').write_text(hashlib.sha256(data).hexdigest() + '  ' + package.name + '\n', encoding='utf-8')
entry = {k: meta[k] for k in ['guid', 'name', 'category', 'overview', 'description']}
entry['owner'] = args.repo.split('/')[0]
item = {'version': version, 'targetAbi': meta['targetAbi'], 'sourceUrl': f'https://github.com/{args.repo}/releases/download/{args.tag}/{package.name}', 'checksum': hashlib.md5(data, usedforsecurity=False).hexdigest(), 'timestamp': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'changelog': meta['changelog']}
catalog = json.loads(args.previous.read_text(encoding='utf-8')) if args.previous and args.previous.exists() else []
prior = next((p for p in catalog if p['guid'] == entry['guid']), None)
entry['versions'] = [item] + ([v for v in prior['versions'] if v['version'] != version] if prior else [])
entry['versions'].sort(key=lambda v: tuple(map(int, v['version'].split('.'))), reverse=True)
catalog = [entry] + [p for p in catalog if p['guid'] != entry['guid']]
(out / 'manifest.json').write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(f'Packaged {package.name}; contents: Edge DLL and license notices')
