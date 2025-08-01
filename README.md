# Gaslight for Highlight

Gaslight is a service for collecting telemetry data from the [Highlight](https://highlight.io) Javascript SDK ([highlight.run](https://www.npmjs.com/package/highlight.run)), which can later be uploaded to a real Highlight backend instance. This is useful for airgapped web applications.

> [!TIP]
> Highlight has been acquired and may undergo API changes. Gaslight is not yet compatible with the LaunchDarkly JavaScript Observability SDK.


## Collection

Gaslight can be served by any ASGI server. If the frontend is accessed over HTTPS, Gaslight must also be served securely.

    gunicorn gaslight:app \
        --worker-class uvicorn.workers.UvicornWorker \
        --bind 0.0.0.0:8082 \
        --certfile server.crt --keyfile server.key  `# for HTTPS only`

For local development, the `gaslight serve` command will serve the application with `uvicorn` over HTTP.

By default, Gaslight allows cross-origin requests from anywhere unless the `CORS_ORIGIN` environment variable is appropriately set (`;` delimited).

The frontend should be configured to point to the Gaslight like so:

```javascript
H.init("a0b1c2d3", {
    backendUrl: 'http://gaslight.host:8082/public',
    otlpEndpoint: 'http://gaslight.host:8082/otel',
    // ...
})
```

Telemetry data is stored in the SQLite database `gaslight.db` in the current working directory.


## Forwarding

To push the stored data to the real Highlight backend, use:

    gaslight push \
        --graph-endpoint http://highlight.host:8082/public \
        --otlp-endpoint http://highlight.host:4317/

The OTLP endpoint must use HTTP(S), and not gRPC. If omitted, the hosted highlight.io endpoints are used.


## Security

No validation is performed on the stored requests. Assume that any client that can access Gaslight can effectively also access your Highlight instance.
