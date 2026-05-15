# ---------------------------------------------------------------------------
# web_server.py  –  Async HTTP server with Server-Sent Events (SSE)
# ---------------------------------------------------------------------------
# Uses Server-Sent Events rather than WebSockets deliberately:
#   • SSE is plain HTTP chunked transfer – no SHA-1/crypto dependency
#   • Standard MicroPython firmware ships with the required libraries
#   • The browser's EventSource API reconnects automatically
#   • Data flow is one-way (Pico → phone), which is all we need
#
# Endpoints
# ---------
#   GET /              → serves index.html from the Pico W flash filesystem
#   GET /events        → SSE stream, pushes JSON state at BROADCAST_HZ
#   GET /health        → plain-text "OK" (useful for connection check)
#   GET /baro?qnh=X    → set QNH to X hPa  (returns "OK")
#   GET /baro?cal_ft=X → calibrate baro to X ft MSL using current pressure
#   GET /<file>        → serves any other file from the flash filesystem
#                        (terrain.js, sw.js, manifest.webmanifest, icons …)
# ---------------------------------------------------------------------------

import uasyncio as asyncio
import ujson
import uos

_MIME = {
    'html':        'text/html; charset=utf-8',
    'js':          'application/javascript',
    'css':         'text/css',
    'json':        'application/json',
    'png':         'image/png',
    'ico':         'image/x-icon',
    'webmanifest': 'application/manifest+json',
    'txt':         'text/plain',
}


async def _handle_root(writer):
    """Stream index.html in 2 KB chunks to avoid loading 100 KB into RAM."""
    try:
        size = uos.stat('index.html')[6]
    except OSError:
        err = (b'<!DOCTYPE html><html><body>'
               b'<h2>index.html not found on Pico W flash.</h2>'
               b'<p>Copy iphone_display/index.html to the Pico W filesystem.</p>'
               b'</body></html>')
        await _send_headers(writer, '200 OK', 'text/html; charset=utf-8',
                            f'Content-Length: {len(err)}\r\nConnection: close\r\n')
        writer.write(err)
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        return

    await _send_headers(writer, '200 OK', 'text/html; charset=utf-8',
                        f'Content-Length: {size}\r\nConnection: close\r\n')
    with open('index.html', 'rb') as f:
        while True:
            chunk = f.read(2048)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    writer.close()
    await writer.wait_closed()


def _parse_qs(qs):
    """Parse 'key=val&key2=val2' query string → dict.  No URL-decode needed
    for the simple numeric values we accept on /baro."""
    params = {}
    if not qs:
        return params
    for pair in qs.split('&'):
        if '=' in pair:
            k, v = pair.split('=', 1)
            params[k.strip()] = v.strip()
    return params


async def _send_headers(writer, status, content_type, extra=''):
    header = (
        f'HTTP/1.1 {status}\r\n'
        f'Content-Type: {content_type}\r\n'
        'Access-Control-Allow-Origin: *\r\n'
        f'{extra}'
        '\r\n'
    )
    writer.write(header.encode())
    await writer.drain()



async def _handle_health(writer):
    body = b'OK'
    await _send_headers(writer, '200 OK', 'text/plain',
                        f'Content-Length: {len(body)}\r\nConnection: close\r\n')
    writer.write(body)
    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def _handle_baro(writer, params, state):
    """
    GET /baro?qnh=1014.2       – set QNH (hPa); value range 800–1100 enforced
    GET /baro?cal_ft=1500      – request barometer calibration to 1500 ft MSL
                                  (main.py sensor_loop processes _cal_ft flag)
    Both parameters may be combined in one request.
    """
    if 'qnh' in params:
        try:
            qnh = float(params['qnh'])
            if 800.0 <= qnh <= 1100.0:
                state['baro_hpa'] = round(qnh, 2)
        except ValueError:
            pass

    if 'cal_ft' in params:
        try:
            state['_cal_ft'] = float(params['cal_ft'])
        except ValueError:
            pass

    body = b'OK'
    await _send_headers(writer, '200 OK', 'text/plain',
                        f'Content-Length: {len(body)}\r\nConnection: close\r\n')
    writer.write(body)
    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def _handle_sse(writer, state):
    """Keep the connection open and stream JSON state as SSE events."""
    await _send_headers(
        writer, '200 OK', 'text/event-stream',
        'Cache-Control: no-cache\r\nConnection: keep-alive\r\n'
    )
    interval_ms = 1000 // state.get('_broadcast_hz', 10)
    try:
        while True:
            # Build a shallow copy, skipping internal (_-prefixed) keys
            payload = {k: v for k, v in state.items() if not k.startswith('_')}
            event = 'data: ' + ujson.dumps(payload) + '\n\n'
            writer.write(event.encode())
            await writer.drain()
            await asyncio.sleep_ms(interval_ms)
    except Exception:
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def _handle_trim(writer, params, state):
    """
    GET /trim?pitch=X&roll=Y&yaw=Z
    Any combination; values in degrees.  Ranges enforced: ±10° pitch/roll, ±180° yaw.
    Sets _save_trims flag so sensor_loop persists values to flash.
    """
    changed = False
    if 'pitch' in params:
        try:
            v = float(params['pitch'])
            if -10.0 <= v <= 10.0:
                state['pitch_trim'] = round(v, 1)
                changed = True
        except ValueError:
            pass
    if 'roll' in params:
        try:
            v = float(params['roll'])
            if -10.0 <= v <= 10.0:
                state['roll_trim'] = round(v, 1)
                changed = True
        except ValueError:
            pass
    if 'yaw' in params:
        try:
            v = float(params['yaw'])
            if -180.0 <= v <= 180.0:
                state['yaw_trim'] = round(v, 1)
                changed = True
        except ValueError:
            pass
    if changed:
        state['_save_trims'] = True
    body = b'OK'
    await _send_headers(writer, '200 OK', 'text/plain',
                        f'Content-Length: {len(body)}\r\nConnection: close\r\n')
    writer.write(body)
    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def _handle_magcal(writer, params, state):
    """
    GET /magcal?action=get          → JSON {corrections:[...], active:bool}
    GET /magcal?action=set&t=v,...  → store 36 comma-separated floats
    GET /magcal?action=clear        → remove deviation table
    """
    action = params.get('action', 'get')

    if action == 'set':
        try:
            t_str = params.get('t', '')
            vals = [float(x) for x in t_str.split(',') if x.strip()]
            print(f'magcal: received {len(vals)} values (url_t_len={len(t_str)})')
            if len(vals) == 36:
                state['_magdev'] = vals
                state['_save_magdev'] = True
                print('magcal: stored 36-pt table')
            else:
                print(f'magcal: REJECTED — need 36, got {len(vals)}')
        except Exception as e:
            print(f'magcal set error: {e}')
        body = b'OK'
        await _send_headers(writer, '200 OK', 'text/plain',
                            f'Content-Length: {len(body)}\r\nConnection: close\r\n')
        writer.write(body)

    elif action == 'clear':
        state['_magdev'] = []
        state['_save_magdev'] = True
        body = b'OK'
        await _send_headers(writer, '200 OK', 'text/plain',
                            f'Content-Length: {len(body)}\r\nConnection: close\r\n')
        writer.write(body)

    else:  # 'get'
        table = state.get('_magdev', [])
        body = ujson.dumps({'corrections': table, 'active': len(table) == 36}).encode()
        await _send_headers(writer, '200 OK', 'application/json',
                            f'Content-Length: {len(body)}\r\nConnection: close\r\n')
        writer.write(body)

    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def _handle_sdp_zero(writer, state):
    """GET /sdp_zero → flag the sensor_loop to capture a new SDP31 zero
    offset on the next tick.  Use this after installation, or after a
    temperature swing during a long ground hold, to null out any drift
    in the differential-pressure reading.  Aircraft must be stationary
    with no airflow into the pitot tube."""
    state['_sdp_zero'] = True
    body = b'OK'
    await _send_headers(writer, '200 OK', 'text/plain',
                        f'Content-Length: {len(body)}\r\nConnection: close\r\n')
    writer.write(body)
    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def _handle_404(writer):
    body = b'Not Found'
    await _send_headers(writer, '404 Not Found', 'text/plain',
                        f'Content-Length: {len(body)}\r\nConnection: close\r\n')
    writer.write(body)
    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def _serve_static(writer, filename):
    """Serve any file that exists on the Pico flash filesystem."""
    if '..' in filename:  # block directory traversal
        await _handle_404(writer)
        return
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    ctype = _MIME.get(ext, 'application/octet-stream')
    try:
        size = uos.stat(filename)[6]
    except OSError:
        await _handle_404(writer)
        return
    await _send_headers(writer, '200 OK', ctype,
                        f'Content-Length: {size}\r\nConnection: close\r\n')
    with open(filename, 'rb') as f:
        while True:
            chunk = f.read(2048)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    writer.close()
    await writer.wait_closed()


async def _client_handler(reader, writer, state):
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=5)
    except Exception:
        writer.close()
        return

    # Drain remaining request headers
    try:
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=3)
            if line in (b'\r\n', b'\n', b''):
                break
    except Exception:
        pass

    try:
        method, full_path, *_ = request_line.decode().split()
    except Exception:
        writer.close()
        return

    if method != 'GET':
        await _handle_404(writer)
        return

    # Split path and query string
    if '?' in full_path:
        path, qs = full_path.split('?', 1)
    else:
        path, qs = full_path, ''
    params = _parse_qs(qs)

    if path in ('/', '/index.html'):
        await _handle_root(writer)
    elif path == '/events':
        await _handle_sse(writer, state)
    elif path == '/health':
        await _handle_health(writer)
    elif path == '/baro':
        await _handle_baro(writer, params, state)
    elif path == '/trim':
        await _handle_trim(writer, params, state)
    elif path == '/magcal':
        await _handle_magcal(writer, params, state)
    elif path == '/sdp_zero':
        await _handle_sdp_zero(writer, state)
    else:
        await _serve_static(writer, path.lstrip('/'))


async def start_server(state, port=80):
    """
    Start the HTTP server.  Pass the shared state dict; it will be read
    directly each time an SSE event is built.
    """
    async def handler(reader, writer):
        await _client_handler(reader, writer, state)

    server = await asyncio.start_server(handler, '0.0.0.0', port)
    print(f'HTTP server listening on port {port}')
    await server.wait_closed()
