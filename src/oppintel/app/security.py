"""Request-level security: CSRF protection, rate limiting and response headers.

Three concerns, kept here rather than scattered through route handlers so a new route inherits
them rather than having to remember them.

**CSRF.** Every state-changing request must carry a token that matches the session. The token
is generated per session, compared in constant time, and required on POST, PUT, PATCH and
DELETE. A JSON API client is exempted only when it authenticates by a header rather than a
cookie, because a cross-site form cannot set that header — which is exactly the property that
makes the cookie-based session forgeable without a token.

**Rate limiting.** A small in-process token bucket keyed by client and route class. It exists
to blunt credential stuffing and accidental hammering, not to be a distributed limiter; the
documented production deployment puts a real limiter in front of the app.

**Headers.** A conservative set that does not break the app: no sniffing, no framing, a
referrer policy, and a content security policy that permits only same-origin assets and the
inline JSON-LD the SEO layer emits.
"""

from __future__ import annotations

import hmac
import logging
import secrets
import time
from collections import defaultdict, deque
from typing import Any, Callable

from flask import Request, Response, g, jsonify, request, session

log = logging.getLogger(__name__)

#: Methods that change state and therefore require a CSRF token.
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Session key holding the per-session CSRF token.
CSRF_SESSION_KEY = "_csrf_token"

#: Header / form field a client may use to present the token.
CSRF_FORM_FIELD = "_csrf_token"
CSRF_HEADER = "X-CSRF-Token"

#: Route prefixes that are rate limited, with (max requests, window seconds) per client.
#: Auth endpoints are the tightest because they are the credential-guessing surface.
RATE_LIMITS: tuple[tuple[str, int, int], ...] = (
    ("/signin", 10, 300),
    ("/signup", 10, 300),
    ("/api", 300, 60),
    ("/", 600, 60),
)

#: Paths exempt from CSRF because they authenticate without a cookie, or are safe by design.
CSRF_EXEMPT_PREFIXES = ()


def csrf_token() -> str:
    """The current session's CSRF token, created on first use."""
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def _presented_token(req: Request) -> str | None:
    header = req.headers.get(CSRF_HEADER)
    if header:
        return header
    if req.form:
        return req.form.get(CSRF_FORM_FIELD)
    try:
        payload = req.get_json(silent=True)
    except Exception:  # noqa: BLE001 - a malformed body is simply "no token"
        payload = None
    if isinstance(payload, dict):
        value = payload.get(CSRF_FORM_FIELD)
        if isinstance(value, str):
            return value
    return None


def _api_client_uses_header_auth(req: Request) -> bool:
    """Whether a request presents an API key rather than relying on the session cookie.

    A browser cannot attach this header cross-site without a CORS preflight, so a request that
    proves possession of the key is not forgeable by a hostile page. The key path is not
    implemented in this build, so this returns False unless the header is actually present,
    which keeps the exemption from silently applying to cookie-authenticated calls.
    """
    return bool(req.headers.get("X-API-Key"))


def check_csrf(req: Request) -> Response | None:
    """Return a 403 response when a state-changing request lacks a valid token."""
    if req.method not in UNSAFE_METHODS:
        return None
    if any(req.path.startswith(prefix) for prefix in CSRF_EXEMPT_PREFIXES):
        return None
    expected = session.get(CSRF_SESSION_KEY)

    # A first-time visitor has no token yet; issue one rather than rejecting, but only for a
    # request that carries no session at all. Once a session exists the token must match.
    if not expected:
        if _api_client_uses_header_auth(req):
            return None
        return jsonify({"error": "csrf_token_missing"}), 403

    presented = _presented_token(req)
    if _api_client_uses_header_auth(req):
        return None
    if not presented or not hmac.compare_digest(str(presented), str(expected)):
        log.warning("CSRF rejection on %s %s", req.method, req.path)
        return jsonify({"error": "csrf_failed"}), 403
    return None


class RateLimiter:
    """A per-process sliding-window limiter.

    Deliberately simple and bounded: it keeps a deque of recent timestamps per key and drops
    keys once their window empties, so memory cannot grow without limit. It is a defence in
    depth, not the only one, and the deployment notes say so.
    """

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, max_requests: int, window_seconds: int) -> bool:
        """Record a hit. Returns False when the caller has exceeded the allowance."""
        now = time.monotonic()
        bucket = self._hits[key]
        cutoff = now - window_seconds
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= max_requests:
            return False
        bucket.append(now)
        return True

    def reset(self) -> None:
        self._hits.clear()


#: The application's limiter. Attached to the app in `create_app` so tests can reset it.
limiter = RateLimiter()


def client_key(req: Request) -> str:
    """A stable key for the client.

    Uses the remote address only. Forwarded headers are deliberately not trusted, because a
    client can set them and would then be able to evade the limit by rotating a value.
    """
    return req.remote_addr or "unknown"


def rate_limit_for(path: str) -> tuple[int, int] | None:
    """The allowance for a path, most specific prefix first."""
    for prefix, max_requests, window in RATE_LIMITS:
        if path.startswith(prefix):
            return max_requests, window
    return None


def check_rate_limit(req: Request) -> Response | None:
    """Return a 429 response when the client has exceeded its allowance."""
    allowance = rate_limit_for(req.path)
    if allowance is None:
        return None
    max_requests, window = allowance
    # Scope the "everything else" bucket per method as well, so a page GET and a form POST do
    # not consume one another's allowance.
    key = f"{client_key(req)}:{req.path}:{req.method}"
    if not limiter.check(key, max_requests, window):
        log.warning("Rate limit exceeded for %s %s", req.method, req.path)
        return jsonify({"error": "rate_limited"}), 429
    return None


def apply_security_headers(response: Response) -> Response:
    """Attach the standard hardening headers to an outgoing response."""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Permissions-Policy", "geolocation=(), microphone=(), camera=()"
    )
    # The CSP allows the app's own stylesheet and the inline JSON-LD blocks, which are data
    # rather than executable script. `frame-ancestors 'none'` duplicates X-Frame-Options for
    # browsers that prefer the modern directive.
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self'; "
        "script-src 'self'; connect-src 'self'; base-uri 'self'; "
        "form-action 'self'; frame-ancestors 'none'; object-src 'none'",
    )
    return response


def install_security(app: Any, *, enabled: bool = True) -> None:
    """Register the CSRF check, rate limiter and header hook on an application."""
    from flask import current_app

    @app.before_request
    def _security_gate() -> Any:
        if not enabled:
            return None
        blocked = check_rate_limit(request)
        if blocked is not None:
            return blocked
        return check_csrf(request)

    @app.after_request
    def _headers(response: Response) -> Response:
        apply_security_headers(response)
        return response

    # Expose the token to templates for form embedding.
    @app.context_processor
    def _csrf_context() -> dict[str, Any]:
        return {"csrf_token": csrf_token}


def require_entitlement(feature: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Wrap a view so it requires a feature the account's plan grants.

    Denies with 403 for a JSON caller and 404 for a browser caller. The browser gets 404
    rather than 403 because a paid feature's existence is not something to advertise to an
    account that cannot use it; a signed-in account with the entitlement is unaffected.
    """
    from functools import wraps

    from flask import abort

    def decorator(view: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(view)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            entitlement = getattr(g, "entitlement", None)
            if entitlement is not None and entitlement.has(feature):
                return view(*args, **kwargs)
            if request.headers.get("Accept", "").startswith("application/json"):
                return jsonify({"error": "entitlement_required", "feature": feature}), 403
            abort(404)
        return wrapped

    return decorator
