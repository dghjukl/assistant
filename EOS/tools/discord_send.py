"""Tool: Discord Send — push a message to a Discord channel from a tool call."""
from __future__ import annotations
import httpx

_bot_token: str | None = None
_target_channel_id: int | None = None


def configure(bot_token: str, channel_id: int | None = None) -> None:
    global _bot_token, _target_channel_id
    _bot_token = bot_token
    _target_channel_id = channel_id


async def send_message(content: str, channel_id: int | None = None) -> str:
    token = _bot_token
    cid   = channel_id or _target_channel_id
    if not token or not cid:
        return "Discord send tool not configured (no token or channel_id)."
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.post(
                f"https://discord.com/api/v10/channels/{cid}/messages",
                headers={
                    "Authorization": f"Bot {token}",
                    "Content-Type": "application/json",
                },
                json={"content": content[:2000]},
            )
            resp.raise_for_status()
            return f"Message sent to channel {cid}."
        except Exception as exc:
            return f"Failed to send Discord message: {exc}"
