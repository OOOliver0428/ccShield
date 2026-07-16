"""HTTP API package.

Owns the FastAPI router, the LOCAL_TOKEN + Host-guard middleware, and the
auth-flow route handlers. Imported by :mod:`app.main` to wire the
``/api`` sub-app into the live FastAPI instance.

The package surface is intentionally narrow:

- :mod:`app.api.middleware` — ``local_token_middleware`` (auth + host guard).
- :mod:`app.api.auth_routes` — QR-poll / status / manual-cookie endpoints.
- :mod:`app.api.router` — ``api_router`` aggregator for all API routes.

The package itself holds no logic; it exists so ``from app.api import
api_router`` resolves to a single, stable import path.
"""
