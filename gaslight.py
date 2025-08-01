#!/usr/bin/env python3
import json
import os
import sqlite3
import sys
import urllib.request
import urllib.error
import warnings

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response


app = FastAPI()


# Persistent store
db = sqlite3.connect('gaslight.db')
db.execute(
    'CREATE TABLE IF NOT EXISTS requests (destination TEXT, request BLOB)'
)
db.commit()


def save_to_db(dest, blob):
    if not isinstance(blob, (bytes, bytearray)):
        blob = json.dumps(blob).encode()

    db.execute(
        'INSERT INTO requests(destination, request) VALUES (?, ?)',
        (dest, sqlite3.Binary(blob)),
    )
    db.commit()


# CORS middleware to allow requests from the frontend server.
#
# Quite honestly, this was a pain to get working. The OpenTelemetry data is
# submitted using navigator.sendBeacon(), and since the browser automatically
# includes cookies with that request, we need to allow credentials. And
# allowing credentials means we can't use a wildcard for the allowed origins.
@app.middleware('http')
async def permissive_cors(request: Request, call_next):
    origin = request.headers.get('origin')

    is_cors_preflight = (
        request.method == 'OPTIONS'
        and origin is not None
        and request.headers.get('access-control-request-method') is not None
    )

    # Reject requests from disallowed origins
    allowed_origins = os.environ.get('CORS_ORIGIN', '*').split(';')
    if allowed_origins != ['*'] and origin not in allowed_origins:
        return Response('Disallowed CORS origin', status_code=400)

    # Handle CORS preflight requests
    if is_cors_preflight:
        return Response(
            status_code=200,
            headers={
                # Required because cookies are sent with beacons
                'Access-Control-Allow-Credentials': 'true',
                'Access-Control-Allow-Headers': request.headers.get(
                    'access-control-request-headers', '*'
                ),
                'Access-Control-Allow-Origin': request.headers.get(
                    'origin', '*'
                ),
                'Access-Control-Allow-Methods': 'POST, OPTIONS',
            },
        )

    # Pass the request to the next handler, then add the CORS headers that must
    # be present on every response, not only preflight responses.
    response = await call_next(request)
    response.headers['Vary'] = 'Origin'  # our response varies by origin

    if origin:
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Credentials'] = 'true'

    return response


@app.post('/public')
async def graphql_request(request: Request):
    payload = await request.json()
    query = payload.get('query', '')
    op = payload.get('operationName')
    v = payload.get('variables', {})

    if query.lower().startswith('mutation '):
        save_to_db('gql', payload)

    # We try to return a response that matches the expected schema so we don't
    # break the JavaScript SDK. The return types are defined in schema.graphqls.
    if op == 'initializeSession':
        # See type InitializeSessionResponse in the GraphQL schema
        out = {
            'secure_id': v.get('session_secure_id'),
            'project_id': v.get('organization_verbose_id'),
        }
    elif op == 'identifySession':
        out = v.get('session_secure_id')
    elif op == 'addSessionProperties':
        out = v.get('session_secure_id')
    elif op in ('pushPayload', 'PushPayload'):
        out = v.get('payload_id', 0)
    elif op in ('pushPayloadCompressed', 'PushPayloadCompressed'):
        # The schema says this returns Any, but it's actually null on success
        out = None
    elif op == 'pushMetrics':
        out = len(v.get('metrics', []))
    elif op == 'addSessionFeedback':
        out = v.get('session_secure_id')
    else:
        warnings.warn(f'Unexpected operation from client: {op}')
        out = None
    return JSONResponse({'data': {op: out}})


@app.post('/otel/v1/{signal}')
async def otlp_request(request: Request, signal: str):
    payload = await request.json()
    save_to_db(f'otel:/v1/{signal}', payload)

    # The "partialSuccess" field is required; empty means full success.
    return JSONResponse({'partialSuccess': {}}, status_code=200)


def cmd_serve(args):
    try:
        import uvicorn
    except ImportError:
        sys.exit(
            'Missing dependency: uvicorn is not installed. '
            'Please install it or use another ASGI server.'
        )

    uvicorn.run(app, host=args.host, port=args.port)


def cmd_push(args):
    endpoints = {
        'gql': args.graph_endpoint,
        'otel': args.otlp_endpoint,
    }

    # Enumerate the stored requests in the database
    c = db.cursor()
    c.execute(
        'SELECT rowid, destination, request FROM requests ORDER BY rowid ASC'
    )

    failure = None
    processed = 0
    for rowid, dest, blob in c.fetchall():
        # Expand 'gql' and 'otel' endpoints to the new URLs
        endkind, _, endpath = dest.partition(':')
        endpoint = endpoints.get(endkind)
        if endpoint and endpath:
            endpoint = f'{endpoint.rstrip("/")}{endpath}'

        # Forward the request to the Highlight backend
        req = urllib.request.Request(
            endpoint,
            headers={'Content-Type': 'application/json'},
            data=blob,
        )

        try:
            with urllib.request.urlopen(req) as resp:
                code = resp.getcode()

            if 200 <= code < 300:
                processed += 1

                # Remove the row from the database after successful push
                c.execute('DELETE FROM requests WHERE rowid = ?', (rowid,))
                db.commit()
            else:
                failure = (rowid, f'HTTP error {code}')
                break
        except urllib.error.URLError as e:
            failure = (rowid, str(e.reason))
            break

    print(f'Pushed {processed} requests to Highlight')

    if failure is not None:
        print(
            f'Failed to push data for row {failure[0]}: {failure[1]}',
            file=sys.stderr,
        )
        sys.exit(1)


def main():
    import argparse

    parser = argparse.ArgumentParser(prog='gaslight')
    subparsers = parser.add_subparsers(dest='command', required=True)

    serve = subparsers.add_parser('serve', help='Run the ASGI server')
    serve.add_argument('--host', default='0.0.0.0', help='Host to bind')
    serve.add_argument('--port', type=int, default=8082, help='Port to bind')
    serve.add_argument(
        '--graph-endpoint', default='/public', help='GraphQL endpoint path'
    )
    serve.add_argument(
        '--otlp-endpoint', default='/otel', help='OTLP endpoint path'
    )

    push = subparsers.add_parser(
        'push', help='Push data to the Highlight backend'
    )
    push.add_argument('--graph-endpoint', default='https://pub.highlight.io')
    push.add_argument('--otlp-endpoint', default='https://otel.highlight.io')

    args = parser.parse_args()

    if args.command == 'serve':
        cmd_serve(args)
    elif args.command == 'push':
        cmd_push(args)


if __name__ == '__main__':
    main()
