"""Classify only documented provider codes; never expose response text."""

_WORKSPACE_DENIED = frozenset(
    ['OAUTH_023', 'OAUTH_026', 'OAUTH_027']
    + [f'OAUTH_{number:03}' for number in range(29, 46)]
)


def workspace_denied(response) -> bool:
    if response is None or response.status_code not in (401, 403):
        return False
    try:
        body = response.json()
        code = body.get('ECODE') if isinstance(body, dict) else None
        return isinstance(code, str) and code in _WORKSPACE_DENIED
    except (ValueError, TypeError):
        return False
