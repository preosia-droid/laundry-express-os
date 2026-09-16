"""Same-origin site/API server. Static files never include source or databases."""
import json
import mimetypes
import os
from pathlib import Path
from hosted import production_backend

PUBLIC = Path(__file__).parent / 'public'
SECURITY = [('X-Content-Type-Options', 'nosniff'), ('Referrer-Policy', 'no-referrer'),
    ('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
    ('Cache-Control', 'no-store')]


class Website:
    def __init__(self, api, origin):
        self.api, self.origin = api, origin.rstrip('/')

    def __call__(self, env, start):
        path, method = env.get('PATH_INFO', '/'), env.get('REQUEST_METHOD', 'GET')
        def reply(status, body, kind='application/json'):
            start(status, SECURITY + [('Content-Type', kind), ('Content-Length', str(len(body)))])
            return [body if method != 'HEAD' else b'']
        if path == '/health' and method in ('GET', 'HEAD'):
            return reply('200 OK', b'{"status":"ok"}')
        if method == 'POST':
            origin = env.get('HTTP_ORIGIN')
            if origin and origin != self.origin:
                return reply('403 Forbidden', b'{"error":"Origin denied"}')
            if not env.get('CONTENT_TYPE', '').lower().startswith('application/json'):
                return reply('415 Unsupported Media Type', b'{"error":"JSON required"}')
            def secure_start(status, headers):
                start(status, [(k,v) for k,v in headers if k.lower() not in ('cache-control','x-content-type-options')] + SECURITY)
            return self.api(env, secure_start)
        if method not in ('GET', 'HEAD'):
            return reply('405 Method Not Allowed', b'{"error":"Method not allowed"}')
        relative = 'index.html' if path == '/' else 'owner.html' if path in ('/owner','/owner/') else path.lstrip('/')
        file = (PUBLIC / relative).resolve()
        if not file.is_relative_to(PUBLIC.resolve()) or not file.is_file() or file.suffix not in ('.html','.js','.css','.png','.jpg','.jpeg','.webp','.svg','.ico'):
            return reply('404 Not Found', b'{"error":"Not found"}')
        return reply('200 OK', file.read_bytes(), mimetypes.guess_type(file.name)[0] or 'application/octet-stream')


def create_app():
    return Website(production_backend(), os.environ['PUBLIC_ORIGIN'])


if __name__ == '__main__':
    import argparse
    from wsgiref.simple_server import make_server
    from hosted import LocalCloudBackend
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', required=True)
    parser.add_argument('--port', type=int, default=8790)
    args = parser.parse_args()
    with make_server('127.0.0.1', args.port, Website(LocalCloudBackend(args.db), f'http://127.0.0.1:{args.port}')) as server:
        server.serve_forever()
