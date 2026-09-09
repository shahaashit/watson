"""HTTPS endpoint Flock's Outgoing Webhooks POST to.

One endpoint serves all channels — routing is by the `?token=<token>` query
param (each Flock webhook has its own token, mapped to a channel by the
`add` CLI). See app/services/flock_webhook_client.py for the receive logic.

This endpoint sits under /api like the rest of the app; expose it to Flock
via ngrok (or similar). Watson binds to 127.0.0.1, so a public tunnel is
mandatory — Flock's servers can't reach localhost.
"""
import logging

from fastapi import APIRouter, Depends, Request

from ..db import get_db
from ..services import flock_webhook_client

log = logging.getLogger("watson.flock_webhook.route")

router = APIRouter(prefix="/api", tags=["flock_webhook"])


@router.post("/flock/webhook")
async def flock_webhook(request: Request, conn=Depends(get_db)):
    """Accept a Flock Outgoing Webhook POST. Never 4xx / 5xx unless the
    request is genuinely malformed — Flock retries on non-2xx, and a hot
    retry loop hides real problems under a mountain of duplicate attempts.
    """
    token = request.query_params.get("token", "")
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        log.warning("flock webhook: non-JSON body from %s", request.client)
        return {"ok": False, "reason": "invalid_json"}
    result = flock_webhook_client.receive(conn, token, payload)
    return result


@router.delete("/flock/webhook/mentions")
def dismiss_mentions(channel_jid: str, conn=Depends(get_db)):
    """Clear cached mentions for a channel — the "Dismiss" action on the
    Today card. Keeps the webhook token registered so subsequent incoming
    messages still land; only clears the current batch from view."""
    n = flock_webhook_client.dismiss_channel_mentions(conn, channel_jid)
    return {"cleared": n, "channel_jid": channel_jid}


@router.get("/flock/webhook/channels")
def list_registered_channels(conn=Depends(get_db)):
    """Debug helper — returns every registered token's channel + last-seen
    timestamp. Useful to confirm 'did my message hit Watson at all?' from
    the UI without tailing server.log."""
    return {
        "channels": [
            {
                "token_prefix": r["token"][:8],
                "channel_name": r["channel_name"],
                "channel_jid": r["channel_jid"],
                "created_at": r["created_at"],
                "last_seen_at": r["last_seen_at"],
            }
            for r in flock_webhook_client.list_channels(conn)
        ]
    }
