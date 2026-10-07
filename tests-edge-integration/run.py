"""Isolated real Jellyfin/Edge HTTP checks. Never print credentials or export URLs."""
import argparse
import json
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
import uuid
from urllib.parse import urljoin, urlsplit
import httpx

parser = argparse.ArgumentParser()
parser.add_argument('--host-image', required=True)
parser.add_argument('--gateway-image', required=True)
args = parser.parse_args()
run = 'edge-release-check-' + secrets.token_hex(5)
host, gateway, network = run+'-host', run+'-gateway', run+'-net'
resources = []
PID = '5ed3af2c-a62a-4fc1-b470-cefa5820ac92'
client = httpx.Client(timeout=120, trust_env=False, follow_redirects=False)

def docker(*args):
    result = subprocess.run(['docker', *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError('Isolated Docker operation failed; no private output printed')
    return result.stdout.decode().strip()

def wait(check, seconds=120):
    end = time.monotonic()+seconds
    while time.monotonic()<end:
        try:
            if check(): return
        except (httpx.HTTPError, ValueError, KeyError):
            pass
        time.sleep(1)
    raise RuntimeError('Isolated service readiness timed out')

def api(method,path,**kwargs):
    r=client.request(method, origin+path, **kwargs)
    if r.status_code>=400:
        raise RuntimeError(f'Test API returned {r.status_code} at {path.split("?")[0]}')
    return r.json() if r.content else None

def check(name, condition):
    if not condition: raise AssertionError(name)
    print('PASS '+name,flush=True)

try:
    docker('network','create',network)
    resources.append(('network',network))
    docker('run','-d','--name',host,'--network',network,'-p','127.0.0.1::8096',args.host_image)
    resources.append(('container',host))
    origin='http://'+docker('port',host,'8096/tcp').splitlines()[0]
    wait(lambda: client.get(origin+'/Startup/User').status_code==200)
    password=secrets.token_urlsafe(30)
    api('POST','/Startup/Configuration',json={'UICulture':'en-US','MetadataCountryCode':'US','PreferredMetadataLanguage':'en'})
    api('POST','/Startup/User',json={'Name':'release-demo','Password':password})
    api('POST','/Startup/RemoteAccess',json={'EnableRemoteAccess':True,'EnableAutomaticPortMapping':False})
    api('POST','/Startup/Complete',json={})
    authorization='MediaBrowser Client="ReleaseCheck", Device="Test", DeviceId="release-check", Version="2"'
    auth=api('POST','/Users/AuthenticateByName',headers={'Authorization':authorization},json={'Username':'release-demo','Pw':password})
    token=auth['AccessToken']
    client.headers['Authorization']=authorization+', Token="'+token+'"'
    check('Edge plugin loaded in official Jellyfin',any(p['Id'].replace('-','')==PID.replace('-','') for p in api('GET','/Plugins')))
    check('direct host context keeps native downloads',api('GET','/JellyfinEdge/context')['FromProxy'] is False)
    docker('exec',host,'mkdir','-p','/release-media')
    docker('exec',host,'/usr/lib/jellyfin-ffmpeg/ffmpeg','-hide_banner','-loglevel','error','-y','-f','lavfi','-i','testsrc2=size=320x180:rate=24','-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','8','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','/release-media/Edge Demo (2026).mp4')
    api('POST','/Library/VirtualFolders',params={'name':'Release synthetic','collectionType':'movies','refreshLibrary':'true'},json={'LibraryOptions':{'PathInfos':[{'Path':'/release-media'}],'EnableInternetProviders':False,'EnableRealtimeMonitor':False,'TypeOptions':[{'Type':'Movie','MetadataFetchers':[],'ImageFetchers':[]}]}})
    items=[]
    def indexed():
        nonlocal_items=api('GET','/Items',params={'recursive':'true','includeItemTypes':'Movie','fields':'MediaSources','userId':auth['User']['Id']})['Items']
        items[:] = nonlocal_items
        return bool(items and items[0].get('MediaSources'))
    wait(indexed)
    item=items[0]
    source=item['MediaSources'][0]['Id']
    enrollment=api('POST','/JellyfinEdge/admin/enrollment-token/rotate')
    node=api('POST','/JellyfinEdge/enroll',headers={'Authorization':'Bearer '+enrollment['Token']},json={'InstanceId':uuid.uuid4().hex,'Nonce':secrets.token_hex(32),'Name':'Release synthetic node','Version':'2.0.0'})
    docker('run','-d','--name',gateway,'--network',network,'-p','127.0.0.1::8080','-e','JELLYFIN_BACKEND=http://'+host+':8096','-e','EDGE_NODE_TOKEN_FILE=/tmp/node.json','-e','EDGE_CACHE_DIR=/tmp/edge-cache','--entrypoint','sleep',args.gateway_image,'infinity')
    resources.append(('container',gateway))
    edge='http://'+docker('port',gateway,'8080/tcp').splitlines()[0]
    config=api('GET','/Plugins/'+PID+'/Configuration')
    setting=next(n for n in config['Nodes'] if n['NodeId']==node['NodeId'])
    setting.update(Enabled=True,PublicUrl=edge,EnableCache=True,CacheMaxBytes=67108864,CacheBlockBytes=65536,AllowOriginFallback=True,EnableLocalDirectPlay=False,Mappings=[])
    config.update(DefaultNodeId=node['NodeId'],MaxHeight=360,VideoBitRate=1000000,AudioBitRate=128000)
    api('POST','/Plugins/'+PID+'/Configuration',json=config)
    with tempfile.TemporaryDirectory() as private:
        credential=Path(private)/'node.json'
        credential.write_text(json.dumps({'Token':node['Token']}),encoding='utf-8')
        credential.chmod(0o600)
        docker('cp',str(credential),gateway+':/tmp/node.json')
    docker('exec','--user','root',gateway,'chown','10001:10001','/tmp/node.json')
    docker('exec','--user','root',gateway,'chmod','600','/tmp/node.json')
    docker('exec','-d',gateway,'uvicorn','gateway:from_env','--factory','--host','0.0.0.0','--port','8080','--no-access-log','--no-proxy-headers','--log-level','critical')
    wait(lambda: client.get(edge+'/edge-status').json().get('ControlReady') is True)
    check('registered node applies HTTP cache configuration',True)
    proxy_context=client.get(edge+'/JellyfinEdge/context')
    check('proxy context is established by registered node identity',proxy_context.status_code==200 and proxy_context.json()['FromProxy'] is True)
    original=client.get(origin+'/Items/'+item['Id']+'/Download')
    check('official original download succeeds',original.status_code==200 and len(original.content)>0)
    cold=client.get(edge+'/Items/'+item['Id']+'/Download')
    warm=client.get(edge+'/Items/'+item['Id']+'/Download')
    check('cold and warm cached downloads preserve bytes',cold.status_code==warm.status_code==200 and cold.content==warm.content==original.content)
    ranged=client.get(edge+'/Items/'+item['Id']+'/Download',headers={'Range':'bytes=10-29'})
    check('cached Range response preserves selected bytes',ranged.status_code==206 and ranged.content==original.content[10:30])
    head=client.head(edge+'/Items/'+item['Id']+'/Download')
    check('HEAD preserves length without body',head.status_code==200 and not head.content and int(head.headers['Content-Length'])==len(original.content))
    def exported(operation):
        return api('POST','/JellyfinEdge/exports',json={'ItemId':item['Id'],'MediaSourceId':source,'Operation':operation,'SubtitleIndex':-1})
    download=exported('download')
    public_client=httpx.Client(timeout=120,trust_env=False)
    downloaded=public_client.get(download['Url'])
    check('anonymous scoped download matches original',downloaded.status_code==200 and downloaded.content==original.content)
    share=exported('share')
    playlist=public_client.get(share['Url'])
    check('sharing returns HLS without account or node credentials',playlist.status_code==200 and '#EXTM3U' in playlist.text and token not in playlist.text and node['Token'] not in playlist.text)
    segments=[line for line in playlist.text.splitlines() if line and not line.startswith('#')]
    check('HLS contains media segments',bool(segments))
    first=public_client.get(urljoin(share['Url'],segments[0]))
    again=public_client.get(urljoin(share['Url'],segments[0]))
    check('share segments are repeatable and cached',first.status_code==again.status_code==200 and first.content==again.content and len(first.content)>0)
    docker('stop',host)
    offline=public_client.get(download['Url'])
    check('complete scoped original cache remains available offline',offline.status_code==200 and offline.content==original.content)
    public_client.close()
    print('PASS real host/gateway HTTP integration',flush=True)
finally:
    client.close()
    for kind,name in reversed(resources):
        subprocess.run(['docker', 'rm' if kind=='container' else 'network', '-f' if kind=='container' else 'rm', name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    print('PASS isolated resources cleaned',flush=True)
