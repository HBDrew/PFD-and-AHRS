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
#   GET /          → serves index.html from the Pico W flash filesystem
#   GET /events    → SSE stream, pushes JSON state at BROADCAST_HZ
#   GET /health    → plain-text "OK" (useful for connection check)
# ---------------------------------------------------------------------------

import uasyncio as asyncio
import ujson

_INDEX_CACHE = None   # loaded once from flash


def _load_index():
    global _INDEX_CACHE
    if _INDEX_CACHE is not None:
        return _INDEX_CACHE
    try:
        with open('index.html', 'r') as f:
            _INDEX_CACHE = f.read()
    except OSError:
        _INDEX_CACHE = (
            '<!DOCTYPE html><html><body>'
            '<h2>index.html not found on Pico W flash.</h2>'
            '<p>Copy display/index.html to the root of the Pico W filesystem.</p>'
            '</body></html>'
        )
    return _INDEX_CACHE


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


async def _handle_root(writer):
    html = _load_index()
    await _send_headers(writer, '200 OK', 'text/html; charset=utf-8',
                        f'Content-Length: {len(html)}\r\nConnection: close\r\n')
    writer.write(html.encode())
    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def _handle_health(writer):
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
            # Build a shallow copy without internal keys
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


async def _handle_404(writer):
    body = b'Not Found'
    await _send_headers(writer, '404 Not Found', 'text/plain',
                        f'Content-Length: {len(body)}\r\nConnection: close\r\n')
    writer.write(body)
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
        method, path, *_ = request_line.decode().split()
    except Exception:
        writer.close()
        return

    if method != 'GET':
        await _handle_404(writer)
        return

    if path in ('/', '/index.html'):
        await _handle_root(writer)
    elif path == '/events':
        await _handle_sse(writer, state)
    elif path == '/health':
        await _handle_health(writer)
    else:
        await _handle_404(writer)


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
