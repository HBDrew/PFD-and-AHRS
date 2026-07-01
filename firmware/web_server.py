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


# SSE client tracking. The iPhone WiFi handoffs / page refreshes /
# background-suspends leave Pico-side connections orphaned — writer.drain()
# then blocks indefinitely waiting for ACKs from a peer that's gone, the
# socket pool fills, and new connections silently fail (iPhone sees "no
# link"). We cap concurrent SSE clients and time-out drain() to kill dead
# connections fast enough that the pool stays available for the live client.
_SSE_MAX_CLIENTS     = 2
_SSE_DRAIN_TIMEOUT_S = 2.0
_sse_client_count    = 0


async def _handle_sse(writer, state):
    """Keep the connection open and stream JSON state as SSE events."""
    global _sse_client_count

    # Refuse new connections when the pool is saturated rather than letting
    # them quietly hang. iPhone gets a clean 503 it can retry against once
    # the existing zombies drain out.
    if _sse_client_count >= _SSE_MAX_CLIENTS:
        body = b'SSE busy'
        try:
            await _send_headers(writer, '503 Service Unavailable', 'text/plain',
                                f'Content-Length: {len(body)}\r\nConnection: close\r\n')
            writer.write(body)
            try:
                await asyncio.wait_for(writer.drain(), 1.0)
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        print(f'[SSE] refused — {_sse_client_count}/{_SSE_MAX_CLIENTS} clients active')
        return

    _sse_client_count += 1
    print(f'[SSE] client connected ({_sse_client_count}/{_SSE_MAX_CLIENTS} active)')
    try:
        await _send_headers(
            writer, '200 OK', 'text/event-stream',
            'Cache-Control: no-cache\r\nConnection: keep-alive\r\n'
        )
        interval_ms = 1000 // state.get('_broadcast_hz', 10)
        while True:
            # Build a shallow copy, skipping internal (_-prefixed) keys
            payload = {k: v for k, v in state.items() if not k.startswith('_')}
            event = 'data: ' + ujson.dumps(payload) + '\n\n'
            writer.write(event.encode())
            try:
                # Bounded drain so dead peers don't pin the task. Without
                # this, a backgrounded iPhone or dropped WiFi leaves the
                # task in a permanent wait — sockets accumulate, pool fills.
                await asyncio.wait_for(writer.drain(), _SSE_DRAIN_TIMEOUT_S)
            except Exception:
                # Any failure (timeout, broken pipe, reset) → peer is gone.
                # Break the loop and let the finally block release the slot.
                break
            await asyncio.sleep_ms(interval_ms)
    except Exception as e:
        # Most likely a normal client disconnect — keep the log quiet.
        if 'ECONNRESET' not in str(e) and 'BROKEN' not in str(e).upper():
            print(f'[SSE] client error: {e}')
    finally:
        _sse_client_count = max(0, _sse_client_count - 1)
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
        print(f'[SSE] client closed ({_sse_client_count}/{_SSE_MAX_CLIENTS} active)')


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


async def _handle_align(writer, params, state):
    """
    GET /align?pitch=X&roll=Y
    Input-side axis alignment (raw gyro/accel/mag rotation applied BEFORE the
    Mahony filter).  Same offsets settable over USB serial via $ALIGN; this
    HTTP route lets a WiFi-only display (Pi Zero on the AP) push them too — the
    display's LEVEL / flight-zero button uses it to re-reference the sensors so
    the current orientation reads level.  Range: ±10° each.  Persists to flash
    via the _save_orient flag (same store as orientation/mounting/$ALIGN).
    """
    changed = False
    for pkey, skey in (('pitch', 'pitch_align'), ('roll', 'roll_align')):
        if pkey in params:
            try:
                v = float(params[pkey])
                if v < -10.0:
                    v = -10.0
                elif v > 10.0:
                    v = 10.0
                state[skey] = round(v, 2)
                changed = True
            except ValueError:
                pass
    if changed:
        state['_save_orient'] = True
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


async def _handle_magoff(writer, params, state):
    """
    GET /magoff?action=get               → JSON {mx_off, my_off, mz_off, active}
    GET /magoff?action=set&v=mx,my,mz    → store hard-iron offsets (3 floats)
    GET /magoff?action=clear             → zero the offsets
    """
    action = params.get('action', 'get')

    if action == 'set':
        try:
            v_str = params.get('v', '')
            vals = [float(x) for x in v_str.split(',') if x.strip()]
            if len(vals) == 3:
                state['_mag_offset'] = (vals[0], vals[1], vals[2])
                state['_save_magcal'] = True
                print(f'magoff: stored {vals[0]:.1f},{vals[1]:.1f},{vals[2]:.1f}')
            else:
                print(f'magoff: REJECTED — need 3, got {len(vals)}')
        except Exception as e:
            print(f'magoff set error: {e}')
        body = b'OK'
        await _send_headers(writer, '200 OK', 'text/plain',
                            f'Content-Length: {len(body)}\r\nConnection: close\r\n')
        writer.write(body)

    elif action == 'clear':
        state['_mag_offset'] = (0.0, 0.0, 0.0)
        state['_save_magcal'] = True
        state['_magtumble_active'] = False
        body = b'OK'
        await _send_headers(writer, '200 OK', 'text/plain',
                            f'Content-Length: {len(body)}\r\nConnection: close\r\n')
        writer.write(body)

    elif action == 'tumble_start':
        state['_magtumble_active'] = True
        state['_magtumble_min'] = [None, None, None]
        state['_magtumble_max'] = [None, None, None]
        body = b'OK'
        await _send_headers(writer, '200 OK', 'text/plain',
                            f'Content-Length: {len(body)}\r\nConnection: close\r\n')
        writer.write(body)

    elif action == 'tumble_finish':
        mn = state.get('_magtumble_min', [None]*3)
        mx = state.get('_magtumble_max', [None]*3)
        if state.get('_magtumble_active') and all(v is not None for v in mn):
            off = (0.5*(mn[0]+mx[0]), 0.5*(mn[1]+mx[1]), 0.5*(mn[2]+mx[2]))
            state['_mag_offset'] = off
            state['_save_magcal'] = True
        state['_magtumble_active'] = False
        body = b'OK'
        await _send_headers(writer, '200 OK', 'text/plain',
                            f'Content-Length: {len(body)}\r\nConnection: close\r\n')
        writer.write(body)

    else:  # 'get'
        o = state.get('_mag_offset', (0.0, 0.0, 0.0))
        active = any(abs(v) > 1e-6 for v in o)
        body = ujson.dumps({'mx_off': o[0], 'my_off': o[1], 'mz_off': o[2],
                             'active': active}).encode()
        await _send_headers(writer, '200 OK', 'application/json',
                            f'Content-Length: {len(body)}\r\nConnection: close\r\n')
        writer.write(body)

    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def _handle_sdp_zero(writer, state):
    """GET /sdp_zero → flag the sensor_loop to capture a new SDP33 zero
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
    elif path == '/align':
        await _handle_align(writer, params, state)
    elif path == '/magcal':
        await _handle_magcal(writer, params, state)
    elif path == '/magoff':
        await _handle_magoff(writer, params, state)
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
