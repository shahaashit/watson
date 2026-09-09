"""One-time Google Calendar OAuth shared by the Settings UI and CLI."""

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
            raise GoogleOAuthError("Google Calendar client configuration is missing.")
        client_config = json.loads(client_config_json)
        flow = InstalledAppFlow.from_client_config(
            client_config, gcal_client.SCOPES
        )
        credentials = flow.run_local_server(
            port=0,
            prompt="consent",
            open_browser=True,
        )
        secret_store.set_secret("google.authorized_user", credentials.to_json())
    except GoogleOAuthError:
        raise
    except Exception:
        raise GoogleOAuthError("Google Calendar authorization failed.") from None


def main() -> None:
    run_oauth_flow()


if __name__ == "__main__":
    main()
