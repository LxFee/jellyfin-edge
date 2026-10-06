import asyncio
import json
import os
import re
import stat
import time
import hashlib
import ipaddress
from contextlib import asynccontextmanager
from http.cookiejar import DefaultCookiePolicy
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, unquote, quote

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import ConnectionClosed
from starlette.middleware import Middleware


class OriginStreamingResponse(StreamingResponse):
    """Close even if disconnect/cancellation happens before the iterator starts."""
    def __init__(self, *args, close_origin, **kwargs):
        super().__init__(*args, **kwargs)
        self.close_origin = close_origin

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self.close_origin()


def canonical_ip(value):
    # No ports, brackets, zone IDs, obfuscated/unknown values or legacy IPv4 forms.
    if not value or '%' in value or any(c.isspace() for c in value):
        raise ValueError('invalid client IP')
    return str(ipaddress.ip_address(value))


def client_peer(scope, trust_proxy_headers):
    if trust_proxy_headers:
        values = [v.decode('latin-1') for k, v in scope.get('headers', []) if k.lower() == b'x-forwarded-for']
        if len(values) > 1:
            raise ValueError('ambiguous X-Forwarded-For')
        if values:
            # Validate the entire single chain before choosing its leftmost address.
            addresses = [canonical_ip(part.strip(' \t')) for part in values[0].split(',')]
            return addresses[0]
    peer = scope.get('client')
    return canonical_ip(peer[0]) if peer else None


class ClientPeer:
    def __init__(self, app, state):
        self.app, self.state = app, state

    async def __call__(self, scope, receive, send):
        if scope['type'] in ('http', 'websocket'):
            try:
                scope['edge.client_peer'] = client_peer(scope, self.state['policy'].trust_proxy_headers)
            except ValueError:
                if scope['type'] == 'websocket':
                    await send({'type': 'websocket.close', 'code': 1008})
                else:
                    await Response(status_code=400)(scope, receive, send)
                return
        await self.app(scope, receive, send)


class BackendPrefix:
    """Accept the configured Jellyfin BaseUrl without concatenating it twice."""
    def __init__(self, app, backend):
        self.app = app
        self.prefix = urlsplit(backend).path.rstrip('/')

    async def __call__(self, scope, receive, send):
        if scope['type'] in ('http', 'websocket') and self.prefix:
            path = scope.get('path', '')
            raw = scope.get('raw_path', path.encode())
            if (path == self.prefix or path.startswith(self.prefix + '/')) and raw.startswith(self.prefix.encode()):
                scope = dict(scope, path=path[len(self.prefix):] or '/', raw_path=raw[len(self.prefix.encode()):] or b'/')
        await self.app(scope, receive, send)


from control import Policy, ControlClient
from media import MediaGateway, OriginFailure

HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailer', 'transfer-encoding', 'upgrade', 'host', 'content-length'}

class NoAmbientCookies(DefaultCookiePolicy):
    def set_ok(self, cookie, request):
        return False


def media_resource_url(url, base, current, token):
    """Only media payload references, never arbitrary webpage links."""
    if not token or not isinstance(url, str):
        return url
    try:
        child = gateway_url(url, base, current)
    except ValueError:
        return url  # External references retain native semantics, no secret added.
    parsed = urlsplit(child)
    params = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
              if k.lower() not in ('apikey', 'api_key')]
    return parsed.path + '?' + urlencode(params + [('ApiKey', token)])


def native_auth_adapter(request, headers):
    """Modern credentials are opaque; adapt only unambiguous legacy carriers."""
    # Bearer is also the native plugin's node/registration credential. Never
    # reinterpret it as a Jellyfin user or change its authentication scheme.
    if request.headers.get('authorization') or request.headers.get('x-emby-authorization'):
        return headers
    token = token_for(request)
    if token:
        for key in list(headers):
            if key.lower() in ('authorization', 'x-emby-authorization', 'x-emby-token', 'x-mediabrowser-token'):
                del headers[key]
        headers.update(user_auth(token))
    return headers


def token_for(request):
    values = [v for k, v in request.query_params.multi_items() if k.lower() in ('api_key', 'apikey')]
    for key, value in request.headers.items():
        if key.lower() in ('x-emby-token', 'x-mediabrowser-token'):
            values.append(value)
        elif key.lower() in ('authorization', 'x-emby-authorization'):
            if value.lower().startswith('bearer '):
                values.append(value[7:])
            else:
                matches = re.findall(r'\bToken="([^"\r\n]*)"', value, re.I)
                values.extend(matches)
                # Reject unparsed token syntax rather than forwarding it.
                if len(re.findall(r'\bToken\s*=', value, re.I)) != len(matches):
                    return None
    if not values or len(set(values)) != 1 or not values[0] or any(c in values[0] for c in '\r\n"\\'):
        return None
    return values[0]


def user_auth(token):
    if not isinstance(token, str) or any(c in token for c in '\r\n"\\'):
        raise ValueError('invalid token')
    return {'Authorization': f'MediaBrowser Client="JellyfinEdge", Device="Edge", DeviceId="jellyfin-edge", Version="1", Token="{token}"'}


def safe_path_decoded(path):
    # Classify the fully decoded path, never just one unquote layer. Reject
    # URL delimiters as well: concatenating them into a backend URL is unsafe.
    for _ in range(5):
        if any(c in path for c in '\\?#') or any(x in ('.', '..') for x in path.split('/')) or any(ord(c) < 32 or ord(c) == 127 for c in path):
            return None
        decoded = unquote(path)
        if decoded == path:
            return path if path.startswith('/') and not path.startswith('//') else None
        path = decoded
    return None


def safe_path(path):
    return safe_path_decoded(path) is not None


def open_media(policy, source):
    """Open only upstream-authorized paths under explicit mounts, without symlinks."""
    if source.get('Protocol', '').lower() != 'file' or source.get('SupportsDirectPlay') is not True:
        raise ValueError('not proven direct-play compatible')
    path = source.get('Path', '')
    if not isinstance(path, str) or not safe_path(path):
        raise ValueError('unsafe media path')
    for mount in sorted(policy.mappings, key=lambda m: len(m.host), reverse=True):
        root = mount.host.rstrip('/')
        if not path.startswith(root + '/'):
            continue
        parts = path[len(root) + 1:].split('/')
        if not parts or any(not p for p in parts):
            raise ValueError('invalid components')
        fd = os.open(mount.local, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for i, part in enumerate(parts):
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                if i < len(parts) - 1:
                    flags |= os.O_DIRECTORY
                child = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = child
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError('not regular file')
            return fd
        except BaseException:
            os.close(fd)
            raise
    raise ValueError('unmapped path')


def gateway_url(url, base, current):
    resolved = urlsplit(urljoin(current, url))
    trusted = urlsplit(base)
    if (resolved.scheme, resolved.netloc) != (trusted.scheme, trusted.netloc) or resolved.fragment:
        raise ValueError('external upstream URL')
    prefix = trusted.path.rstrip('/')
    if prefix and not resolved.path.startswith(prefix + '/'):
        raise ValueError('URL outside backend base path')
    path = resolved.path[len(prefix):] or '/'
    if not safe_path(path):
        raise ValueError('unsafe URL')
    return path + ('?' + resolved.query if resolved.query else '')


def rewrite_hls(text, base, current, token):
    """Propagate only the caller's verified user token, never a backend credential.

    HLS clients do not inherit parent query auth (or necessarily its headers).
    Validate every child against gateway_url BEFORE attaching any secret.
    """
    if not token:
        raise ValueError('HLS requires verified user token')
    user_auth(token)
    if not text.lstrip().startswith('#EXTM3U'):
        raise ValueError('invalid HLS')

    def child_url(uri):
        child = gateway_url(uri, base, current)
        tokens = [v for k, v in parse_qsl(urlsplit(child).query, keep_blank_values=True)
                  if k.lower() in ('api_key', 'apikey')]
        if tokens:
            if any(value != token for value in tokens):
                raise ValueError('conflicting HLS child token')
            return child  # Preserve original query encoding and matching token.
        return child + ('&' if '?' in child else '?') + urlencode({'ApiKey': token})

    def attribute(match):
        # Parse complete attributes, not URI-looking text inside another value.
        if match['name'] != 'URI':
            return match[0]
        value = match['value']
        if not value.startswith('"') or not value.endswith('"'):
            raise ValueError('invalid HLS URI attribute')
        return match['sep'] + 'URI="' + child_url(value[1:-1]) + '"'

    lines = []
    for line in text.splitlines():
        if line and not line.startswith('#'):
            line = child_url(line.strip())
        elif line.startswith('#'):
            line = re.sub(r'(?P<sep>:|,)(?P<name>[A-Z0-9-]+)=(?P<value>"[^"]*"|[^,]*)', attribute, line)
        lines.append(line)
    return '\n'.join(lines) + '\n'


def create_app(policy: Policy, client=None, node_credential=None, refresh_interval=60, runner=None, cache_dir=None):
    owned = client is None
    client = client or httpx.AsyncClient(follow_redirects=False, timeout=httpx.Timeout(30, read=120), trust_env=False)
    # Control/local authorization requests share the connection pool too. They
    # must never inherit an origin session from an earlier forwarded response.
    client.cookies.clear()
    client.cookies.jar.set_policy(NoAmbientCookies())
    state = {'policy': policy, 'control_ok': False}
    cache_dir = cache_dir or os.environ.get('EDGE_CACHE_DIR') or (str(runner.path.parent / 'cache') if runner else 'state/cache')
    media = MediaGateway(cache_dir, policy.backend, client)
    if media.policy:
        state['policy'] = media.policy
    sessions = {}
    downloads = {'LocalRequests': 0, 'LocalBytes': 0, 'OriginRequests': 0, 'OriginBytes': 0}
    trust_confirmed = os.environ.get('EDGE_ORIGIN_PROXY_TRUST_CONFIRMED') == 'true'

    def binding(token, item, source, session):
        return (hashlib.sha256(token.encode()).hexdigest(), item, source, session)

    def single(request, name):
        values = [v for k, v in request.query_params.multi_items() if k.lower() == name.lower()]
        return values[0] if len(values) == 1 else None

    def is_media(path):
        p = path.lower()
        return bool(re.match(r'^/(?:videos|audio)/[^/]+/(?:stream(?:[./]|$)|(?:master|main)\.m3u8$|hls/)', p)) or (p.startswith('/items/') and p.endswith(('/download', '/file')))

    async def refresh():
        nonlocal node_credential
        while True:
            try:
                if runner:
                    node_credential = await runner.credential(client)
                control = ControlClient(client, runner.url if runner else policy.backend)
                updated = await control.config(node_credential)
                if runner:
                    if any(not runner.permits(m.local) for m in updated.mappings):
                        await runner.heartbeat(client, node_credential, updated, 'ROOT_NOT_ALLOWED')
                        raise ValueError('mapping outside static roots')
                    if updated.new_credential:
                        runner.rotate(updated.new_credential)
                        node_credential = runner.data['Token']
                media.configure(updated, control.exports)
                media.unreachable_until = 0
                if runner:
                    await runner.heartbeat(client, node_credential, updated)
                else:
                    # Legacy readonly Secret remains compatible; rotation cannot be
                    # acknowledged without durable local storage and is not auto-applied.
                    r = await client.post(policy.backend + '/JellyfinEdge/node/heartbeat', headers={'Authorization': 'Bearer ' + node_credential}, json={'Version': 'cache-v2' if updated.enable_cache else 'legacy-v1', 'Revision': updated.revision, 'MediaReadability': 'readable' if updated.enable_cache else 'no-paths' if not updated.mappings else 'readable' if all(os.access(m.local, os.R_OK | os.X_OK) for m in updated.mappings) else 'unreadable', 'ErrorCode': 'ROTATION_REQUIRES_WRITABLE_STATE' if updated.new_credential else ''})
                    r.raise_for_status()
                if updated != state['policy']:
                    sessions.clear()
                state['policy'] = updated
                state['control_ok'] = updated.enabled
            except Exception as error:
                # No exception/request logging: tokens and pairing credentials must not leak.
                state['control_ok'] = False
                sessions.clear()
                if isinstance(error, httpx.HTTPStatusError) and error.response.status_code in (401, 403) and media.policy:
                    from dataclasses import replace
                    revoked = replace(media.policy, enabled=False)
                    media.configure(revoked, [])
                    state['policy'] = revoked
            await asyncio.sleep(refresh_interval)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(refresh()) if node_credential or runner else None
        yield
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if owned:
            await client.aclose()
        media.close()

    async def identity_and_sources(item, token, query, peer=None):
        headers = user_auth(token)
        if peer:
            headers['X-Forwarded-For'] = peer
        r = await client.get(policy.backend + '/Users/Me', headers=headers)
        if r.status_code != 200:
            return None
        user_policy = r.json().get('Policy', {})
        if user_policy.get('EnableRemoteAccess') is not True or user_policy.get('EnableMediaPlayback') is not True:
            return None
        uid = r.json().get('Id')
        if not isinstance(uid, str) or not re.fullmatch(r'[A-Za-z0-9-]+', uid):
            return None
        r = await client.get(policy.backend + f'/Users/{uid}/Items/{item}', headers=headers)
        if r.status_code != 200 or r.json().get('Id') != item or r.json().get('PlayAccess') != 'Full':
            return None
        params = [(k, v) for k, v in query if k.lower() not in ('api_key', 'apikey', 'userid', 'mediasourceid')]
        params.append(('UserId', uid))
        r = await client.get(policy.backend + f'/Items/{item}/PlaybackInfo', headers=headers, params=params)
        if r.status_code != 200:
            return None
        sources = r.json().get('MediaSources', [])
        for source in sources:
            source['_EdgeUserId'] = uid
        return sources

    async def proxy(request, playback=False):
        # Only native media-output seams are subject to origin fallback policy;
        # webpage and metadata routes still belong to the native authorizer.
        media_path = request.url.path.lower()
        if media_path in ('/jellyfinedge/context', '/jellyfinedge/exports') and (not node_credential or not state['control_ok']):
            return Response(status_code=503)
        if node_credential and not state['control_ok'] and (is_media(media_path) or playback):
            return Response(status_code=503)
        if is_media(media_path) and (not state['policy'].allow_fallback or (node_credential and not state['control_ok'])):
            return Response(status_code=503)
        token = token_for(request)
        # Native endpoint authorizes first. Identity checks belong only to local
        # session creation, not ordinary forwarding or admin/plugin routing.
        raw_path = request.scope.get('raw_path', quote(request.scope['path'], safe='/').encode())
        target = policy.backend + raw_path.decode('ascii')
        if request.scope.get('query_string'):
            target += '?' + request.scope['query_string'].decode('ascii')
        request_hop = HOP | {v.strip().lower() for v in request.headers.get('connection', '').split(',') if v.strip()}
        headers = {k: v for k, v in request.headers.items() if k.lower() not in request_hop and not k.lower().startswith(('x-forwarded-', 'x-jellyfin-edge-')) and k.lower() not in ('forwarded', 'x-real-ip')}
        headers = native_auth_adapter(request, headers)
        # Proven proxy context is a registered node identity; callers cannot spoof it.
        if media_path in ('/jellyfinedge/context', '/jellyfinedge/exports') and node_credential and state['control_ok']:
            headers['X-Jellyfin-Edge-Node'] = node_credential
        # Rebuild exactly one canonical IP; never forward a raw incoming chain.
        if request.scope.get('edge.client_peer'):
            headers['X-Forwarded-For'] = request.scope['edge.client_peer']
        content = request.stream()
        if request.method == 'POST' and media_path == '/sessions/playing/stopped':
            payload = await request.body()
            if len(payload) > 65536:
                return Response(status_code=413)
            try:
                stopped = json.loads(payload)
                fields = {k.lower(): v for k, v in stopped.items()}
                stopped_session = fields.get('playsessionid')
                stopped_item = fields.get('itemid')
                digest = hashlib.sha256((token or '').encode()).hexdigest()
                for key in list(sessions):
                    if key[0] == digest and (not stopped_session or key[3] == stopped_session) and (not stopped_item or key[1] == stopped_item):
                        del sessions[key]
            except (ValueError, AttributeError, TypeError):
                return Response(status_code=400)
            content = payload
        forwarded = client.build_request(request.method, target, headers=headers, content=content)
        # Preserve this caller's Cookie, never an ambient origin session.
        if 'cookie' in request.headers:
            forwarded.headers['cookie'] = request.headers['cookie']
        else:
            forwarded.headers.pop('cookie', None)
        upstream = await client.send(forwarded, stream=True)
        if upstream.status_code < 500:
            media.unreachable_until = 0
        closed = False
        async def close_origin():
            nonlocal closed
            if not closed:
                closed = True
                await upstream.aclose()

        # Preserve representation headers only when sending the same wire bytes.
        response_hop = (HOP - {'content-length'}) | {'location'} | {
            v.strip().lower() for v in upstream.headers.get('connection', '').split(',') if v.strip()}
        out_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in response_hop and k.lower() != 'set-cookie'}
        def native_response(response):
            response.raw_headers.extend((b'set-cookie', value.encode('latin-1')) for value in upstream.headers.get_list('set-cookie'))
            return response
        origin_download = re.fullmatch(r'/Items/[A-Za-z0-9-]+/Download', request.url.path, re.I) is not None
        if origin_download:
            downloads['OriginRequests'] += 1
            out_headers['x-jellyfin-edge-transfer'] = 'origin-download'
        if 'location' in upstream.headers:
            location = upstream.headers['location']
            resolved, base = urlsplit(urljoin(target, location)), urlsplit(policy.backend)
            prefix = base.path.rstrip('/')
            if (resolved.scheme, resolved.netloc) == (base.scheme, base.netloc) and (not prefix or resolved.path.startswith(prefix + '/')):
                location = resolved._replace(scheme='', netloc='', path=resolved.path[len(prefix):] or '/').geturl()
            out_headers['location'] = location
        if request.method == 'HEAD' or upstream.status_code in (204, 304):
            await close_origin()
            return native_response(Response(status_code=upstream.status_code, headers=out_headers))
        mime = upstream.headers.get('content-type', '').split(';')[0].strip().lower()
        hls = mime in ('application/vnd.apple.mpegurl', 'application/x-mpegurl', 'audio/mpegurl', 'audio/x-mpegurl')
        # /web is the native webpage/static route, not an origin media-output
        # seam (its future audio/video build assets need no gateway whitelist).
        web_resource = media_path == '/web' or media_path.startswith('/web/')
        media_output = not web_resource and (hls or mime.startswith(('video/', 'audio/')) or mime in ('video/mp2t', 'application/mp4'))
        if media_output and (not state['policy'].allow_fallback or (node_credential and not state['control_ok'])):
            await close_origin()
            return Response(status_code=503)
        if upstream.status_code == 200 and (playback or (hls and token)):
            # These are decoded/transformed bodies, not transparent wire streams.
            for key in ('content-encoding', 'content-length'):
                out_headers.pop(key, None)
            data = bytearray()
            try:
                async for chunk in upstream.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 4 * 1024 * 1024:
                        return Response(status_code=502)
            finally:
                await close_origin()
            if hls:
                try:
                    body = rewrite_hls(data.decode('utf-8'), policy.backend, target, token).encode()
                except (ValueError, UnicodeError):
                    return Response(status_code=502)
            else:
                body = bytes(data)
                token = token_for(request)
                try:
                    doc = json.loads(body)
                    item = request.path_params['path'].split('/')[-2]
                    # Only the actual successful POST response is negotiation
                    # evidence; GET metadata never creates a session proof.
                    session = doc.get('PlaySessionId')
                    authorized = doc.get('MediaSources', []) if request.method == 'POST' and token and state['control_ok'] and state['policy'].enable_local and trust_confirmed and isinstance(session, str) and re.fullmatch(r'[A-Za-z0-9-]+', session) else None
                    identity = None
                    if authorized:
                        identity = await client.get(policy.backend + '/Users/Me', headers={**user_auth(token), **({'X-Forwarded-For': request.scope['edge.client_peer']} if request.scope.get('edge.client_peer') else {})})
                        if identity.status_code != 200 or identity.json().get('Policy', {}).get('EnableRemoteAccess') is not True or identity.json().get('Policy', {}).get('EnableMediaPlayback') is not True:
                            authorized = None
                    now = time.monotonic()
                    for key in list(sessions):
                        if sessions[key][0] <= now:
                            del sessions[key]
                    for source in doc.get('MediaSources', []):
                        # Native media metadata is authoritative. Workers/players
                        # do not inherit SDK headers; adapt only these references.
                        for resource in source.get('MediaAttachments', []) + source.get('MediaStreams', []):
                            if 'DeliveryUrl' in resource:
                                resource['DeliveryUrl'] = media_resource_url(resource['DeliveryUrl'], policy.backend, target, token)
                        candidates = [s for s in authorized or [] if s.get('Id') == source.get('Id') and s.get('Path') == source.get('Path')]
                        native_url = source.get('DirectStreamUrl')
                        if not isinstance(native_url, str):
                            native_url = f'/Videos/{item}/stream?Static=true'
                        native = urlsplit(native_url)
                        static = [v for k, v in parse_qsl(native.query) if k.lower() == 'static']
                        # Only rewrite an already-negotiated original/static media URL,
                        # never a remux, HLS, transcoding or client-incompatible source.
                        if len(candidates) != 1 or source.get('SupportsDirectPlay') is not True or static != ['true'] or '.m3u8' in native.path.lower():
                            continue
                        gateway_url(native_url, policy.backend, target)
                        fd = open_media(state['policy'], candidates[0])
                        os.close(fd)
                        sid = source['Id']
                        if not isinstance(sid, str) or not re.fullmatch(r'[A-Za-z0-9-]+', sid):
                            continue
                        if len(sessions) >= 1024:
                            sessions.pop(next(iter(sessions)))
                        sessions[binding(token, item, sid, session)] = (now + 1800, source.get('Path'), identity.json()['Id'], request.scope.get('edge.client_peer'), now + 21600)
                        source['DirectStreamUrl'] = f'/Videos/{item}/stream?' + urlencode({'Static': 'true', 'MediaSourceId': sid, 'PlaySessionId': session, 'ApiKey': token})
                    body = json.dumps(doc).encode()
                except (ValueError, OSError, KeyError, TypeError):
                    pass  # preserve native negotiation if proof fails
            for key in ('etag', 'content-md5', 'digest', 'accept-ranges'):
                out_headers.pop(key, None)
            out_headers['cache-control'] = 'private, no-store'
            return native_response(Response(body, upstream.status_code, out_headers))
        async def chunks():
            try:
                # Eager MockTransport fixtures may already be consumed; real
                # streaming transports always use raw, undecoded wire chunks.
                async def wire():
                    if upstream.is_stream_consumed:
                        yield upstream.content
                    else:
                        async for chunk in upstream.aiter_raw():
                            yield chunk
                async for chunk in wire():
                    if origin_download:
                        downloads['OriginBytes'] += len(chunk)
                    yield chunk
            finally:
                await close_origin()
        return native_response(OriginStreamingResponse(chunks(), upstream.status_code, out_headers, close_origin=close_origin))

    async def local(request):
        item, sid = request.path_params['item'], request.path_params['source']
        if not all(re.fullmatch(r'[A-Za-z0-9-]+', x) for x in (item, sid)):
            return Response(status_code=400)
        token = token_for(request)
        if not token:
            return Response(status_code=401)
        if node_credential and not state['control_ok']:
            return Response(status_code=503)
        fd = None
        sources = None
        try:
            sources = await identity_and_sources(item, token, request.query_params.multi_items(), request.scope.get('edge.client_peer'))
            if sources is None:
                return Response(status_code=403, headers={'Cache-Control': 'private, no-store'})
            matches = [s for s in sources or [] if s.get('Id') == sid]
            # Proof is ephemeral and bound to actual POST negotiation.
            session = single(request, 'PlaySessionId')
            proof = sessions.get(binding(token, item, sid, session))
            negotiated_session_proven = bool(trust_confirmed and proof and proof[0] > time.monotonic() and len(matches) == 1 and proof[1] == matches[0].get('Path') and proof[2] == matches[0].get('_EdgeUserId') and proof[3] == request.scope.get('edge.client_peer'))
            allowed_params = {'static', 'mediasourceid', 'playsessionid', 'api_key', 'apikey', 'deviceid', 'tag'}
            if any(k.lower() not in allowed_params for k in request.query_params):
                negotiated_session_proven = False
            suffix = request.url.path.rsplit('/stream', 1)[-1]
            if suffix.startswith('.') and matches and suffix[1:].lower() != str(matches[0].get('Container', '')).lower():
                negotiated_session_proven = False
            if negotiated_session_proven and state['control_ok'] and state['policy'].enable_local and len(matches) == 1 and not request.headers.get('if-range'):
                fd = open_media(state['policy'], matches[0])
                sessions[binding(token, item, sid, session)] = (min(time.monotonic() + 1800, proof[4]), *proof[1:])
        except (ValueError, OSError, TypeError):
            if sources is None:
                return Response(status_code=403)
        if fd is None:
            if not state['policy'].allow_fallback or (node_credential and not state['control_ok']):
                return Response(status_code=503)
            # Do not bypass authorization on the fallback: native media endpoint validates token.
            path = f'/Videos/{item}/stream'
            params = [(k, v) for k, v in request.query_params.multi_items() if k.lower() not in ('mediasourceid', 'static')]
            params += [('MediaSourceId', sid), ('Static', 'true')]
            scope = dict(request.scope, path=path, raw_path=path.encode(), query_string=urlencode(params).encode())
            return await proxy(Request(scope, request.receive))
        return file_response(request, fd)

    def file_response(request, fd, extra_headers=None):
        size = os.fstat(fd).st_size
        start, end = 0, size - 1
        status = 200
        headers = {'Accept-Ranges': 'bytes', 'Content-Type': 'application/octet-stream', 'Cache-Control': 'private, no-store'}
        headers.update(extra_headers or {})
        value = request.headers.get('range')
        if value:
            match = re.fullmatch(r'bytes=(\d*)-(\d*)', value)
            if not match or not any(match.groups()):
                os.close(fd)
                return Response(status_code=416, headers={'Content-Range': f'bytes */{size}'})
            a, b = match.groups()
            if a:
                start, end = int(a), min(int(b) if b else size - 1, size - 1)
            else:
                start, end = max(0, size - int(b)), size - 1
            if start > end or start >= size:
                os.close(fd)
                return Response(status_code=416, headers={'Content-Range': f'bytes */{size}'})
            status = 206
            headers['Content-Range'] = f'bytes {start}-{end}/{size}'
        length = max(0, end - start + 1)
        headers['Content-Length'] = str(length)
        local_download = bool(extra_headers and extra_headers.get('X-Jellyfin-Edge-Transfer') == 'local-download')
        if local_download:
            downloads['LocalRequests'] += 1
        if request.method == 'HEAD':
            os.close(fd)
            return Response(status_code=status, headers=headers)
        async def chunks():
            try:
                os.lseek(fd, start, os.SEEK_SET)
                remaining = length
                while remaining:
                    chunk = await asyncio.to_thread(os.read, fd, min(remaining, 256 * 1024))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    if local_download:
                        downloads['LocalBytes'] += len(chunk)
                    yield chunk
            finally:
                os.close(fd)
        return StreamingResponse(chunks(), status, headers)

    async def download(request, item):
        token = token_for(request)
        if not token:
            return Response(status_code=401)
        if not trust_confirmed or (node_credential and not state['control_ok']):
            return Response(status_code=503)
        headers = {**user_auth(token), **({'X-Forwarded-For': request.scope['edge.client_peer']} if request.scope.get('edge.client_peer') else {})}
        user = await client.get(policy.backend + '/Users/Me', headers=headers)
        if user.status_code != 200 or user.json().get('Policy', {}).get('EnableRemoteAccess') is not True or user.json().get('Policy', {}).get('EnableContentDownloading') is not True:
            return Response(status_code=403)
        fd = None
        if node_credential and state['control_ok'] and state['policy'].enable_local:
            authorization = await client.get(policy.backend + '/JellyfinEdge/node/download-authorization/' + item,
                                             headers={**headers, 'X-Jellyfin-Edge-Node': node_credential})
            if authorization.status_code in (401, 403, 404):
                return Response(status_code=authorization.status_code)
            if authorization.status_code != 200:
                return Response(status_code=503)
            try:
                doc = authorization.json()
                if doc.get('ItemId', '').replace('-', '').lower() != item.replace('-', '').lower() or doc.get('NodeId') != state['policy'].node_id:
                    return Response(status_code=503)
                # Official Download selects route itemId, NOT MediaSourceId. Unknown parameters
                # retain official semantics by fallback; never invent a source/session selection.
                known = all(k.lower() in ('apikey', 'api_key') for k in request.query_params)
                if known and not request.headers.get('if-range'):
                    fd = open_media(state['policy'], {'Protocol': 'File', 'SupportsDirectPlay': True, 'Path': doc['Path']})
            except (ValueError, OSError, TypeError, KeyError):
                fd = None
        if fd is None:
            if not state['policy'].allow_fallback:
                return Response(status_code=503)
            return await proxy(request)
        try:
            filename = doc['FileName']
            content_type = doc['ContentType']
            if not isinstance(filename, str) or not filename or any(ord(c) < 32 or ord(c) == 127 for c in filename) or not isinstance(content_type, str) or not re.fullmatch(r'[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+', content_type):
                raise ValueError('unsafe download headers')
            ascii_name = ''.join(c if 32 <= ord(c) < 127 and c not in '\"\\' else '_' for c in filename)
            audit = await client.post(policy.backend + '/JellyfinEdge/node/download-authorization/' + item,
                                      headers={**headers, 'X-Jellyfin-Edge-Node': node_credential}, json={'Path': doc['Path']})
            if audit.status_code != 204:
                os.close(fd)
                return Response(status_code=audit.status_code if audit.status_code in (401, 403, 404) else 503)
            return file_response(request, fd, {'Content-Type': content_type,
                'Content-Disposition': 'attachment; filename="' + ascii_name + '"; filename*=UTF-8\'\'' + quote(filename, safe=''),
                'X-Jellyfin-Edge-Transfer': 'local-download'})
        except (ValueError, TypeError, KeyError, httpx.HTTPError):
            os.close(fd)
            return Response(status_code=503)

    async def dispatch(request):
        path = request.scope['path']
        try:
            exported = await media.exported(request, node_credential)
            if exported is not None:
                return exported
            auth_headers = {k: v for k, v in request.headers.items() if k.lower() in ('authorization', 'x-emby-authorization', 'x-emby-token', 'x-mediabrowser-token', 'cookie')}
            auth_headers = native_auth_adapter(request, auth_headers)
            if request.scope.get('edge.client_peer'):
                auth_headers['X-Forwarded-For'] = request.scope['edge.client_peer']
            cached = await media.native(request, node_credential, auth_headers, state['control_ok'])
            if cached is not None:
                return cached
        except OriginFailure as error:
            return Response(status_code=error.status)
        except (httpx.HTTPError, ValueError):
            return Response(status_code=503)
        if path == '/edge-status':
            return JSONResponse({'ControlReady': state['control_ok'], 'CacheEnabled': state['control_ok'] and state['policy'].enable_cache, 'CachedBytes': media.cache.db.execute('SELECT COALESCE(SUM(size),0) FROM objects').fetchone()[0] if media.cache else 0, 'CacheHits': media.cache.hits if media.cache else 0, 'CacheMisses': media.cache.misses if media.cache else 0, 'ProxyTrustOperatorConfirmed': trust_confirmed, 'ProxyTrustAutomaticallyVerified': False, 'LocalEnabled': state['control_ok'] and state['policy'].enable_local and trust_confirmed, 'LocalDownloadEnabled': state['control_ok'] and state['policy'].enable_local and trust_confirmed, 'Downloads': dict(downloads)})
        native_download = re.fullmatch(r'/Items/([0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/Download', path, re.I)
        if native_download and request.method in ('GET', 'HEAD'):
            try:
                return await download(request, native_download[1])
            except (httpx.HTTPError, ValueError, TypeError):
                return Response(status_code=503)
        native = re.fullmatch(r'/Videos/([A-Za-z0-9-]+)/stream(?:\.[A-Za-z0-9]+)?', path, re.I)
        if native and request.method in ('GET', 'HEAD') and single(request, 'Static') == 'true' and single(request, 'MediaSourceId'):
            scope = dict(request.scope)
            scope['path_params'] = {'item': native[1], 'source': single(request, 'MediaSourceId')}
            return await guarded_local(Request(scope, request.receive))
        try:
            return await proxy(request, path.lower().endswith('/playbackinfo'))
        except httpx.HTTPError:
            return Response(status_code=502)

    async def socket_proxy(websocket):
        # Only the literal native endpoint is exposed. No arbitrary path/query,
        # credential-bearing subprotocol, cookies or incoming forwarding headers
        # are copied to the origin. Its handshake uses modern ApiKey even when
        # older clients supplied api_key (v12 can disable legacy authentication).
        path = websocket.scope['path']
        raw = websocket.scope.get('raw_path', path.encode()).split(b'?', 1)[0]
        token = token_for(websocket)
        # Official Web SDK sends only ApiKey. DeviceId is optional metadata,
        # not an authentication factor; validate it only when supplied.
        devices = [v for k, v in websocket.query_params.multi_items() if k.lower() == 'deviceid']
        device = devices[0] if len(devices) == 1 else None
        peer = websocket.scope.get('edge.client_peer')
        initial_policy = state['policy']

        def gate():
            return (trust_confirmed and bool(peer) and
                    (not node_credential or state['control_ok']) and
                    state['policy'] == initial_policy)

        if (path != '/socket' or raw != b'/socket' or not token or
                (devices and (len(devices) != 1 or not device or len(device) > 256 or
                              any(ord(c) < 32 or ord(c) == 127 for c in device))) or not gate()):
            await websocket.close(code=1008)
            return

        async def verified_user():
            response = await asyncio.wait_for(
                client.get(policy.backend + '/Users/Me',
                           headers={**user_auth(token), 'X-Forwarded-For': peer}), 10)
            if response.status_code != 200:
                return None
            user = response.json()
            uid = user.get('Id')
            if (not isinstance(uid, str) or not re.fullmatch(r'[A-Za-z0-9-]+', uid) or
                    user.get('Policy', {}).get('EnableRemoteAccess') is not True):
                return None
            return uid

        accepted = False
        tasks = []
        close_code = 1011
        try:
            uid = await verified_user()
            if not uid or not gate():
                close_code = 1008
                return
            origin = urlsplit(policy.backend)
            target = origin._replace(scheme='wss' if origin.scheme == 'https' else 'ws',
                                     path=origin.path.rstrip('/') + '/socket',
                                     query=urlencode({'ApiKey': token, **({'deviceId': device} if device else {})})).geturl()
            # websockets 15's asyncio API is deliberately pinned. The library
            # owns Upgrade/Connection/key/version; only canonical client IP is
            # added. Never request or echo caller Sec-WebSocket-Protocol.
            async with websocket_connect(target, additional_headers={'X-Forwarded-For': peer},
                                         proxy=None, user_agent_header=None,
                                         compression=None, open_timeout=10, close_timeout=3,
                                         max_size=4 * 1024 * 1024) as upstream:
                if not gate():
                    close_code = 1008
                    return
                await websocket.accept()
                accepted = True

                async def to_origin():
                    while True:
                        message = await websocket.receive()
                        if message['type'] == 'websocket.disconnect':
                            return 1000
                        if not gate():
                            return 1008
                        data = message.get('bytes')
                        await upstream.send(data if data is not None else message['text'])

                async def to_client():
                    async for message in upstream:
                        if not gate():
                            return 1008
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(message)
                    return 1000

                async def watch_control():
                    # A slow Users/Me must not delay node revocation on an idle
                    # socket. Control still uses the existing refresh cadence.
                    while True:
                        await asyncio.sleep(.1)
                        if not gate():
                            return 1008

                async def watch_identity():
                    # No admin/node credential substitutes for the user; bound
                    # identity and remote permission are rechecked while idle.
                    while True:
                        await asyncio.sleep(max(1, min(refresh_interval, 30)))
                        if await verified_user() != uid or not gate():
                            return 1008

                tasks = [asyncio.create_task(fn()) for fn in
                         (to_origin, to_client, watch_control, watch_identity)]
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                close_code = 1000
                for task in done:
                    code = task.result()
                    if code != 1000:
                        close_code = code
                # Cancel both pumps before closing the upstream context; no
                # abandoned receive task or credential-bearing exception logs.
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            close_code = 1008
        except (ConnectionClosed, WebSocketDisconnect):
            close_code = 1000
        except Exception:
            # Handshake/network errors are deliberately not reflected/logged:
            # their exception strings can contain the origin's token URL.
            close_code = 1011
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await websocket.close(code=close_code if accepted else 1008)
            except (RuntimeError, OSError):
                pass

    async def guarded_local(request):
        try:
            return await local(request)
        except httpx.HTTPError:
            return Response(status_code=502)

    return Starlette(routes=[WebSocketRoute('/socket', socket_proxy), Route('/edge/media/{item}/{source}', guarded_local, methods=['GET', 'HEAD']), Route('/{path:path}', dispatch, methods=['GET', 'HEAD', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])], lifespan=lifespan, middleware=[Middleware(BackendPrefix, backend=policy.backend), Middleware(ClientPeer, state=state)])


def from_env():
    credential = os.environ.get('EDGE_NODE_TOKEN')
    if os.environ.get('EDGE_NODE_TOKEN_FILE'):
        with open(os.environ['EDGE_NODE_TOKEN_FILE'], encoding='utf-8') as f:
            credential = json.load(f)['Token']
    runner = None
    if not credential and os.environ.get('EDGE_ENROLLMENT_TOKEN_FILE'):
        from runner import Runner
        runner = Runner(os.environ.get('EDGE_CONTROL_URL', os.environ['JELLYFIN_BACKEND']), os.environ['EDGE_STATE_FILE'], os.environ['EDGE_ENROLLMENT_TOKEN_FILE'], os.environ.get('EDGE_NODE_NAME', 'edge-runner'), tuple(filter(None, os.environ.get('EDGE_MEDIA_ALLOWED_ROOTS', '').split(':'))))
        # Fail closed even before enrollment has completed.
        credential = runner.data.get('Token') or 'pending'
    return create_app(Policy(os.environ['JELLYFIN_BACKEND']), node_credential=credential, runner=runner)
