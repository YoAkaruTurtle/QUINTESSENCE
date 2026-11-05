"""The two web tools: DeepSeek Search and Fetch.

The host calls these on the model's behalf; the model never sees the endpoint or
the key.  Everything here is shaped by what the live API actually does, not by
what the documentation promises:

* **Errors are mapped by HTTP status and ``type``.**  The API returns
  ``error.code == "invalid_request_error"`` for authentication failures as well
  as for bad parameters, so branching on ``code`` would file a missing key under
  "you sent bad JSON".
* **Only 408 / 429 / 5xx are retried**, at most three times, with a short
  exponential backoff.  A 4xx that is not 408/429 will not change on its own.
* **A fetch stores page text only when asked to** (``capture="evidence"``), and
  even then bounded, because the text is what makes a later ``external_report``
  quote mechanically checkable.  A metadata-only fetch keeps a hash and a
  length, never the body.
* **The host decides which URL is fetchable.**  ``WebFetchPolicy`` runs before
  the provider sees the request: https only, no credentials in the URL, no IP
  literals, no local/special-use names, no non-default port and an optional
  host allowlist.  The provider never becomes a way around that policy.
* **A whole call has a deadline.**  ``total_timeout`` bounds retries as well as
  the individual request, so a tool call cannot outlive the host's per-tool
  wall budget.

The API key is read from the environment by the host and passed in; it is never
formatted into a message, a return value or an exception.
"""

from __future__ import annotations

import ipaddress
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from ..foundation.jsonutil import sha256_hex

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_TIMEOUT_SECONDS = 30.0

#: The environment variable the host reads the web credential from.  The
#: configuration names this variable; it never stores a key.
DEFAULT_API_KEY_ENV = "DEEPSEEK_API_KEY"

#: A host that opted into web tools but supplied no credential must say so
#: instead of pretending a search happened.
WEB_NOT_CONFIGURED = "web_not_configured"

#: A captured page body is capped here; a longer page is truncated and the
#: truncation is recorded, because a silently cut quote would be unverifiable.
MAX_CAPTURE_BYTES = 64 * 1024

#: Retries only for transient statuses.
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
MAX_ATTEMPTS = 3

WEB_BAD_REQUEST = "web_bad_request"
WEB_UNAUTHORIZED = "web_unauthorized"
WEB_PAYMENT_REQUIRED = "web_payment_required"
WEB_FORBIDDEN = "web_forbidden"
WEB_NOT_FOUND = "web_not_found"
WEB_RATE_LIMITED = "web_rate_limited"
WEB_TIMEOUT = "web_timeout"
WEB_SERVER_ERROR = "web_server_error"
WEB_TRANSPORT_ERROR = "web_transport_error"


class WebToolError(RuntimeError):
    """A web call that failed in a way the model may need to see.

    ``code`` is stable and safe: it never contains the API key, the request URL
    with credentials, or a raw provider body.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _error_for(status: int) -> tuple[str, str]:
    if status == 400:
        return WEB_BAD_REQUEST, "the search or fetch request was rejected as invalid"
    if status == 401:
        return WEB_UNAUTHORIZED, "the web credentials were rejected"
    if status == 402:
        return WEB_PAYMENT_REQUIRED, "the account has insufficient balance"
    if status == 403:
        return WEB_FORBIDDEN, "the fetch was rejected by the security policy"
    if status == 404:
        return WEB_NOT_FOUND, "no usable page content was returned"
    if status == 408:
        return WEB_TIMEOUT, "the request timed out"
    if status == 413:
        return WEB_BAD_REQUEST, "the request body was too large"
    if status == 429:
        return WEB_RATE_LIMITED, "the search or fetch rate limit was exceeded"
    if 500 <= status < 600:
        return WEB_SERVER_ERROR, "the web service reported a server error"
    return WEB_TRANSPORT_ERROR, f"unexpected status {status}"


#: Special-use names that must never be fetched through the tool.  ``https``
#: alone does not make ``localhost`` or a metadata endpoint safe.
_LOCAL_SUFFIXES = (
    "localhost",
    "local",
    "internal",
    "lan",
    "home.arpa",
    "test",
    "invalid",
    "example",
    "onion",
)


@dataclass(frozen=True)
class WebFetchPolicy:
    """Host-owned egress policy for ``web.fetch``.

    The model names a URL; this policy decides whether the host is willing to
    hand it to the web provider.  ``allowed_hosts`` is empty by default, which
    means "any public https host" -- it still refuses http, credentials, IP
    literals, local/special-use names and non-default ports.  A non-empty
    allowlist narrows that to the named hosts and their subdomains.
    """

    allowed_hosts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        normalised: list[str] = []
        for entry in self.allowed_hosts:
            if not isinstance(entry, str):
                raise ValueError("allowed_hosts entries must be strings")
            host = entry.strip().lower().rstrip(".")
            if host.startswith("."):
                host = host[1:]
            if (
                not host
                or "/" in host
                or ":" in host
                or "@" in host
                or "." not in host
            ):
                raise ValueError(f"allowed host {entry!r} is not a public DNS name")
            normalised.append(host)
        object.__setattr__(
            self, "allowed_hosts", tuple(sorted(set(normalised)))
        )

    def check(self, url: str) -> str:
        """Return the URL the host will fetch, or raise ``PermissionError``.

        The refusal message is safe to show: it names the rule, never the key,
        the request headers or a provider body.
        """

        target = str(url or "").strip()
        if not target:
            raise PermissionError("web.fetch requires a URL")
        if len(target) > 8192:
            raise PermissionError("web.fetch refused an over-long URL")
        try:
            parts = urlsplit(target)
            port = parts.port
        except ValueError:
            raise PermissionError("web.fetch refused an unparsable URL") from None
        if parts.scheme.lower() != "https":
            raise PermissionError("web.fetch only allows https URLs")
        if parts.username is not None or parts.password is not None:
            raise PermissionError("web.fetch refuses URLs that carry credentials")
        host = (parts.hostname or "").lower().rstrip(".")
        if not host:
            raise PermissionError("web.fetch requires a host name")
        if not host.isascii():
            raise PermissionError("web.fetch requires an ASCII host name")
        if port not in (None, 443):
            raise PermissionError("web.fetch only allows the default https port")
        if _is_ip_literal(host):
            raise PermissionError("web.fetch refuses an IP-literal host")
        if _is_local_name(host):
            raise PermissionError("web.fetch refuses a local or special-use host")
        if self.allowed_hosts and not any(
            host == allowed or host.endswith("." + allowed)
            for allowed in self.allowed_hosts
        ):
            raise PermissionError(
                "web.fetch refused a host outside the configured allowlist"
            )
        return target


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _is_local_name(host: str) -> bool:
    if "." not in host:
        return True
    return any(host == suffix or host.endswith("." + suffix) for suffix in _LOCAL_SUFFIXES)


class DeepSeekWebClient:
    """Search and fetch against ``api.deepseek.com`` with bounded retries."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        total_timeout: float | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        transport: Callable[[urllib.request.Request, float], tuple[int, bytes]] | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("an API key is required")
        if total_timeout is not None and total_timeout <= 0:
            raise ValueError("total_timeout must be positive")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = float(timeout)
        self._total_timeout = None if total_timeout is None else float(total_timeout)
        self._sleep = sleep
        self._clock = clock
        self._transport = transport

    # ------------------------------------------------------------------ util

    def _post(self, path: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        started = self._clock()
        last: WebToolError | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            remaining = self._remaining(started)
            if remaining is not None and remaining <= 0:
                raise last or WebToolError(WEB_TIMEOUT, "the web request timed out")
            request = urllib.request.Request(
                f"{self._base_url}{path}",
                data=body,
                method="POST",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
            try:
                status, raw = self._send(
                    request,
                    self._timeout
                    if remaining is None
                    else max(0.001, min(self._timeout, remaining)),
                )
            except TimeoutError:
                last = WebToolError(WEB_TIMEOUT, "the web request timed out")
                if attempt < MAX_ATTEMPTS and self._can_retry(started, attempt):
                    continue
                raise last
            except urllib.error.URLError as exc:
                if isinstance(exc.reason, TimeoutError):
                    last = WebToolError(WEB_TIMEOUT, "the web request timed out")
                    if attempt < MAX_ATTEMPTS and self._can_retry(started, attempt):
                        continue
                    raise last from None
                raise WebToolError(
                    WEB_TRANSPORT_ERROR, f"the web request did not complete: {exc.reason}"
                ) from None

            if status == 200:
                try:
                    return json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise WebToolError(
                        WEB_TRANSPORT_ERROR, "the web service returned unreadable JSON"
                    ) from None

            code, message = _error_for(status)
            last = WebToolError(code, message)
            if status in RETRYABLE_STATUS and attempt < MAX_ATTEMPTS and self._can_retry(
                started, attempt
            ):
                continue
            raise last
        raise last or WebToolError(WEB_TRANSPORT_ERROR, "the web request failed")

    def _remaining(self, started: float) -> float | None:
        if self._total_timeout is None:
            return None
        return self._total_timeout - (self._clock() - started)

    def _can_retry(self, started: float, attempt: int) -> bool:
        """Sleep before a retry only while the whole call still has budget."""

        remaining = self._remaining(started)
        if remaining is not None and remaining <= 0:
            return False
        delay = 0.2 * attempt
        self._sleep(delay if remaining is None else min(delay, remaining))
        return True

    def _send(
        self, request: urllib.request.Request, timeout: float
    ) -> tuple[int, bytes]:
        if self._transport is not None:
            return self._transport(request, timeout)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()
        except TimeoutError:
            raise

    # ----------------------------------------------------------------- calls

    def search(
        self, *, query: str, count: int = 5, need_content: bool = False
    ) -> Mapping[str, Any]:
        """Run one search; returns the provider payload unchanged."""

        text = str(query or "").strip()
        if not text:
            raise WebToolError(WEB_BAD_REQUEST, "query must not be empty")
        return self._post(
            "/v1/tools/search",
            {"query": text, "count": max(1, min(int(count), 50)), "need_content": bool(need_content)},
        )

    def fetch(
        self, *, url: str, capture: str = "metadata"
    ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        """Fetch one page.

        Returns ``(summary, event_payload)``.  The summary always carries the
        content hash; the event payload carries the text only when
        ``capture="evidence"``, and then bounded by :data:`MAX_CAPTURE_BYTES`.
        """

        if capture not in ("metadata", "evidence"):
            raise WebToolError(WEB_BAD_REQUEST, "capture must be 'metadata' or 'evidence'")
        target = str(url or "").strip()
        if not target:
            raise WebToolError(WEB_BAD_REQUEST, "url must not be empty")
        payload = self._post("/v1/tools/fetch", {"url": target})
        title = str(payload.get("title") or "")
        text = str(payload.get("content") or "")
        truncated = False
        encoded = text.encode("utf-8")
        if len(encoded) > MAX_CAPTURE_BYTES:
            text = encoded[:MAX_CAPTURE_BYTES].decode("utf-8", errors="ignore")
            truncated = True
        digest = sha256_hex(text)
        fetched_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        summary = {
            "url": target,
            "title": title,
            "content_sha256": digest,
            "content_type": "text/markdown",
            "captured": capture == "evidence",
            "truncated": truncated,
            "text_len": len(text),
        }
        event_payload: dict[str, Any] = {
            "url": target,
            "title": title,
            "fetched_at": fetched_at,
            "status": 200,
            "content_type": "text/markdown",
            "content_sha256": digest,
            "text_len": len(text),
            "truncated": truncated,
        }
        if capture == "evidence":
            event_payload["text"] = text
        return summary, event_payload


__all__ = [
    "DEFAULT_API_KEY_ENV",
    "DeepSeekWebClient",
    "MAX_ATTEMPTS",
    "MAX_CAPTURE_BYTES",
    "RETRYABLE_STATUS",
    "WEB_BAD_REQUEST",
    "WEB_FORBIDDEN",
    "WEB_NOT_CONFIGURED",
    "WEB_NOT_FOUND",
    "WEB_PAYMENT_REQUIRED",
    "WEB_RATE_LIMITED",
    "WEB_SERVER_ERROR",
    "WEB_TIMEOUT",
    "WEB_TRANSPORT_ERROR",
    "WEB_UNAUTHORIZED",
    "WebFetchPolicy",
    "WebToolError",
]
