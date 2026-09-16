"""Google Calendar OAuth, shared by the Settings UI and CLI."""

import json

from ..services import gcal_client, secret_store


class GoogleOAuthError(RuntimeError):
    """An OAuth flow failed without exposing credential material."""


def run_oauth_flow() -> None:
    """Run Google's loopback flow and store its authorized-user JSON.

    Both inputs and outputs remain behind Watson's Keychain boundary. Any
    third-party exception is replaced because OAuth libraries may include
    pieces of the client configuration in their error text.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    try:
        client_config_json = secret_store.get_secret("google.client_config")
        if not client_config_json:
            raise GoogleOAuthError("Google client configuration is missing.")
        client_config = json.loads(client_config_json)
        flow = InstalledAppFlow.from_client_config(
            client_config, gcal_client.OAUTH_SCOPES
        )
        def accept_granted_scopes(response):
            # OAuthlib otherwise rejects Google's granular (partial) consent
            # before credentials can be returned. Use only the token response's
            # actual scope, and keep this adjustment local to this OAuth session.
            granted = response.json().get('scope')
            if isinstance(granted, str):
                flow.oauth2session.scope = granted.split()
            elif isinstance(granted, list) and all(isinstance(scope, str) for scope in granted):
                flow.oauth2session.scope = granted
            return response
        flow.oauth2session.register_compliance_hook('access_token_response', accept_granted_scopes)
        credentials = flow.run_local_server(
            port=0,
            prompt="consent",
            open_browser=True,
        )
        secret_store.set_secret("google.authorized_user", gcal_client.credential_json(credentials, require_granted=True))
    except GoogleOAuthError:
        raise
    except Exception:
        raise GoogleOAuthError("Google authorization failed.") from None


def main() -> None:
    run_oauth_flow()


if __name__ == "__main__":
    main()
