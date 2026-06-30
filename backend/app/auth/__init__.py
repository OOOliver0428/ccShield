"""Auth state machine package.

This package owns the runtime representation of "is the user logged in,
and if not, why?". It is intentionally tiny — a single state machine
plus a callback registry — because auth state is global to the process
and is read by many subsystems (the FastAPI routes, the WS push layer,
the cookie-refresh coordinator).

Public surface lives in :mod:`app.auth.session`.
"""

from app.auth.session import (
    AuthSession,
    AuthState,
    NotAuthenticatedError,
    auth_session,
)

__all__ = [
    "AuthSession",
    "AuthState",
    "NotAuthenticatedError",
    "auth_session",
]
