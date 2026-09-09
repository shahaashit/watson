"""Redacted error reporting for third-party and model boundaries.

Exception messages and arguments are untrusted: SDKs commonly include request
headers, tokens, URLs, or response bodies in them.  Callers may retain the
service name, a sanitized exception class name, and a generic recovery hint.
"""

_MESSAGES = {
    "anthropic": "Classification failed. Capture saved for manual review.",
    "clickup": "ClickUp request failed. Check the integration and try again.",
    "gitlab": "GitLab request failed. Check the integration and try again.",
    "google-calendar": (
        "Google Calendar request failed. Reconnect the integration and try again."
    ),
    "flock": "Flock request failed. Reconnect the integration and try again.",
}
_KNOWN_ERROR_TYPES = frozenset({
    "CircuitOpenError",
    "ConnectionError",
    "ConnectTimeout",
    "ExternalError",
    "FlockRefreshError",
    "GoogleCredentialError",
    "HTTPError",
    "JSONDecodeError",
    "OSError",
    "ProxyError",
    "ReadTimeout",
    "RequestException",
    "RuntimeError",
    "SecretStoreError",
    "SSLError",
    "Timeout",
    "TimeoutError",
    "TypeError",
    "ValueError",
})


def source_for_step(name: str) -> str:
    prefix = name.removesuffix("_sync")
    return {
        "gcal": "google-calendar",
        "clickup": "clickup",
        "gitlab": "gitlab",
        "flock": "flock",
    }.get(prefix, "external-service")


def exception_type(error: BaseException) -> str:
    name = type(error).__name__
    return name if name in _KNOWN_ERROR_TYPES else "ExternalError"


def message(source: str) -> str:
    return _MESSAGES.get(
        source, "External service request failed. Check the integration and try again."
    )


def details(source: str, error: BaseException) -> dict[str, str]:
    return {
        "source": source,
        "error_type": exception_type(error),
        "error": message(source),
    }


def log_failure(logger, operation: str, source: str, error: BaseException) -> None:
    """Log metadata only; deliberately omit exception text, args, and traceback."""
    logger.warning(
        "%s failed (source=%s, error_type=%s)",
        operation,
        source,
        exception_type(error),
    )
