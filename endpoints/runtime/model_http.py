"""Build async transports without reloading the TLS trust store per request."""
from __future__ import annotations

import httpx


# httpx honors SSL_CERT_FILE/SSL_CERT_DIR when creating this context. Loading the
# trust store once also avoids synchronous certificate I/O inside each deadline.
_TLS_CONTEXT = httpx.create_ssl_context()


def async_http_client(timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, verify=_TLS_CONTEXT)
