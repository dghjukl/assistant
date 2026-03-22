"""
EOS — Discord Bot Interface
Primary remote interface. Text messages in Discord → Qwen3 → Discord reply.

Features
--------
- Mentions in guild channels trigger full process_turn() (same pipeline as WebUI)
- DM messages always processed (no mention required)
- Image attachments routed through topology's vision service
- Rate limit safe: all sends go through _safe_send()
- Admin commands: !status !autonomy !identity !remember !memory !reflect
                  !initiative !queue !investigate !help
- Bus publishing: each turn emits a DISCORD_MESSAGE signal
- Turn notifiers: notify_turn() propagated to reflection pipeline + initiative engine
- Bot status queryable via get_bot_status() for server.py admin API

Lifecycle
---------
discord_bot.start(topology, cfg, tracer, bus) is called as an asyncio background
task from server.py's startup_event.  The task runs for the process lifetime.
Calling discord_bot.stop() requests a clean disconnect.
"""
from __future__ import annotations

import asyncio
import io
import logging
import time
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

import discord
from discord.ext import commands
from PIL import Image

from core.memory import log_interaction
from services.retrieval import remember
from core.autonomy import can

if TYPE_CHECKING:
    from runtime.topology import RuntimeTopology

logger = logging.getLogger("eos.discord")

# ── Bot setup ─────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# ── Injected runtime state ────────────────────────────────────────────────────

_topology: "RuntimeTopology | None" = None
_cfg:      dict = {}
_tracer    = None
_bus       = None

# Turn notifier callbacks injected from server.py
_turn_notifiers: list[Callable[[], None]] = []

# Lifecycle state
_connected:    bool  = False
_guilds_count: int   = 0
_start_time:   float = 0.0
_turns_handled: int  = 0
_errors:       int   = 0


def inject(
    topology: "RuntimeTopology",
    cfg: dict,
    tracer=None,
    bus=None,
    turn_notifiers: list[Callable[[], None]] | None = None,
) -> None:
    """Inject runtime dependencies. Called before bot.start()."""
    global _topology, _cfg, _tracer, _bus, _turn_notifiers
    _topology        = topology
    _cfg             = cfg
    _tracer          = tracer
    _bus             = bus
    _turn_notifiers  = turn_notifiers or []


def get_bot_status() -> dict[str, Any]:
    """Return bot status for the admin API."""
    return {
        "enabled":       _cfg.get("discord", {}).get("enabled", False),
        "connected":     _connected,
        "guilds":        _guilds_count,
        "uptime_seconds": round(time.time() - _start_time, 1) if _start_time else 0,
        "turns_handled": _turns_handled,
        "errors":        _errors,
        "user":          str(bot.user) if bot.user else None,
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_token() -> str:
    disc_cfg  = _cfg.get("discord", {})
    cred_file = Path(disc_cfg.get("credential_file", "AI personal files/Discord.txt"))
    if not cred_file.is_file():
        raise FileNotFoundError(f"Discord token file not found: {cred_file}")
    for line in cred_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and not line.startswith("http"):
            return line
    raise ValueError(f"Could not find Discord token in {cred_file}")


def _split_message(text: str, limit: int = 1900) -> list[str]:
    """Split a long message into Discord-safe chunks."""
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        chunks.append(text[:limit])
        text = text[limit:]
    return chunks


async def _safe_send(
    target: discord.abc.Messageable,
    content: str,
    *,
    reference: discord.Message | None = None,
) -> None:
    """Send a message, handling Discord rate limits and HTTP errors gracefully."""
    global _errors
    for chunk in _split_message(content):
        try:
            if reference:
                await target.send(chunk, reference=reference)
                reference = None  # only reference the first chunk
            else:
                await target.send(chunk)
        except discord.errors.RateLimited as exc:
            wait = exc.retry_after + 0.1
            logger.warning("Discord rate limited — sleeping %.1fs", wait)
            await asyncio.sleep(wait)
            try:
                await target.send(chunk)
            except Exception as exc2:
                logger.error("Discord send failed after rate limit retry: %s", exc2)
                _errors += 1
        except discord.errors.Forbidden:
            logger.warning("No permission to send in channel")
            _errors += 1
            return
        except discord.errors.HTTPException as exc:
            logger.error("Discord HTTP error: %s", exc)
            _errors += 1
            return
        except Exception as exc:
            logger.error("Discord send error: %s", exc)
            _errors += 1
            return


def _notify_turns() -> None:
    """Fire all registered turn notifier callbacks."""
    for fn in _turn_notifiers:
        try:
            fn()
        except Exception:
            pass


def _publish_message_signal(content: str, author: str, guild: str | None) -> None:
    """Publish a DISCORD_MESSAGE signal to the bus (best-effort)."""
    if not _bus:
        return
    try:
        from runtime.signal_bus import SignalEnvelope, SEVERITY_INFO
        _bus.publish(SignalEnvelope(
            source="discord_interface",
            signal_type="discord_message",
            severity=SEVERITY_INFO,
            confidence=0.9,
            payload={
                "author":  author,
                "guild":   guild,
                "preview": content[:100],
            },
        ))
    except Exception:
        pass


# ── Bot events ────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    global _connected, _guilds_count, _start_time
    _connected    = True
    _guilds_count = len(bot.guilds)
    _start_time   = time.time()
    logger.info("Discord bot connected as %s (%s) in %d guild(s)",
                bot.user, bot.user.id, _guilds_count)

    # Sync the discord_send tool with the first usable channel
    try:
        from tools.discord_send import configure
        token = _get_token()
        for guild in bot.guilds:
            for channel in guild.text_channels:
                if channel.permissions_for(guild.me).send_messages:
                    configure(token, channel.id)
                    logger.info(
                        "Discord send tool → #%s in '%s'",
                        channel.name, guild.name,
                    )
                    break
            break
    except Exception as exc:
        logger.warning("Could not configure send tool: %s", exc)


@bot.event
async def on_disconnect():
    global _connected
    _connected = False
    logger.warning("Discord bot disconnected")


@bot.event
async def on_guild_join(guild: discord.Guild):
    global _guilds_count
    _guilds_count = len(bot.guilds)
    logger.info("Joined guild: %s", guild.name)


@bot.event
async def on_message(message: discord.Message):
    global _turns_handled, _errors
    if message.author == bot.user:
        return

    disc_cfg = _cfg.get("discord", {})

    # Guild: only respond to explicit mentions (configurable)
    if message.guild:
        if disc_cfg.get("respond_only_to_mentions", True):
            if bot.user not in message.mentions:
                await bot.process_commands(message)
                return
        if disc_cfg.get("ignore_bots", True) and message.author.bot:
            return

    # Strip bot mention from content
    content = message.content
    if bot.user:
        content = content.replace(f"<@{bot.user.id}>", "").strip()
        content = content.replace(f"<@!{bot.user.id}>", "").strip()

    # Process image attachments via vision service
    if message.attachments and _topology and _topology.vision_available:
        for attachment in message.attachments:
            if any(attachment.filename.lower().endswith(ext)
                   for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp")):
                try:
                    img_bytes   = await attachment.read()
                    img         = Image.open(io.BytesIO(img_bytes)).convert("RGB")
                    from services.vision import describe
                    description = await describe(
                        img, _topology,
                        prompt=content or "Describe what you see in this image.",
                    )
                    content = (
                        f"[Image: {description}]\n{content}"
                        if content
                        else f"[Image: {description}]"
                    )
                except Exception as exc:
                    logger.warning("Vision processing failed: %s", exc)

    # Let command handler check for ! commands first
    await bot.process_commands(message)

    # Don't process as chat if it was a command
    if message.content.startswith("!"):
        return

    if not content:
        return

    # ── Full cognitive turn ───────────────────────────────────────────────
    _publish_message_signal(
        content,
        str(message.author),
        message.guild.name if message.guild else None,
    )

    async with message.channel.typing():
        try:
            from runtime.orchestrator import process_turn
            response = await process_turn(
                _topology, content, _cfg,
                tracer=_tracer,
                bus=_bus,
            )
            _turns_handled += 1
            _notify_turns()
        except Exception as exc:
            logger.error("Discord turn failed: %s", exc)
            response = "Sorry, something went wrong on my end."
            _errors += 1

    await _safe_send(message.channel, response, reference=message)


# ── Admin commands ────────────────────────────────────────────────────────────

@bot.command(name="help")
async def cmd_help(ctx):
    """List all available commands."""
    lines = [
        "**EOS Commands**",
        "`!status`       — Entity status (name, identity, interactions)",
        "`!identity`     — Full identity domain breakdown",
        "`!autonomy <dim> on|off` — Toggle autonomy dimension",
        "`!remember <text>` — Explicitly store something in memory",
        "`!memory <query>` — Search memory for relevant items",
        "`!reflect`      — Trigger an immediate identity eval cycle",
        "`!initiative`   — Show initiative queue status",
        "`!queue`        — Alias for !initiative",
        "`!investigate <title>` — Start a new investigation",
        "`!help`         — This message",
    ]
    await _safe_send(ctx, "\n".join(lines))


@bot.command(name="status")
async def cmd_status(ctx):
    """Show entity status."""
    try:
        from core.entity import get_status
        s = get_status(_cfg)
        lines = [
            "**Entity Status**",
            f"Name: {s.get('name') or '*(unnamed)*'}",
            f"Identity: {s.get('identity_stable_domains', 0)}/{s.get('total_domains', 6)} domains stable",
            f"Interactions: {s.get('interaction_count', 0)}",
        ]
        if _topology:
            top = _topology.status_summary()
            lines.append(f"Mode: {top.get('deployment_mode', '?')} "
                         f"| Vision: {'✓' if top.get('vision_available') else '✗'}")
        await _safe_send(ctx, "\n".join(lines))
    except Exception as exc:
        await _safe_send(ctx, f"Status error: {exc}")


@bot.command(name="identity")
async def cmd_identity(ctx):
    """Show current identity state."""
    try:
        from core.memory import get_identity_state
        state = get_identity_state()
        lines = ["**Identity State**"]
        for domain, data in state.items():
            conf   = f"{data.get('confidence', 0):.0%}"
            answer = data.get("answer", "") or "*(not formed)*"
            if len(answer) > 90:
                answer = answer[:90] + "…"
            lines.append(f"**{domain}** ({conf}): {answer}")
        await _safe_send(ctx, "\n".join(lines))
    except Exception as exc:
        await _safe_send(ctx, f"Identity error: {exc}")


@bot.command(name="autonomy")
async def cmd_autonomy(ctx, dimension: str = "", state: str = ""):
    """Toggle autonomy dimension. Usage: !autonomy <dimension> on|off"""
    if not dimension or not state:
        from core.autonomy import get_full_profile
        profile = get_full_profile()
        lines = ["**Autonomy Profile**"]
        for dim, data in profile.get("dimensions", {}).items():
            icon = "✓" if data.get("enabled") else "✗"
            lines.append(f"{icon} `{dim}`")
        await _safe_send(ctx, "\n".join(lines))
        return
    try:
        from core.autonomy import set_dimension
        enabled = state.lower() in ("on", "true", "1", "yes")
        set_dimension(dimension, enabled)
        await _safe_send(ctx, f"Autonomy `{dimension}` → `{'enabled' if enabled else 'disabled'}`")
    except ValueError as exc:
        await _safe_send(ctx, f"Error: {exc}")


@bot.command(name="remember")
async def cmd_remember(ctx, *, text: str = ""):
    """Explicitly store something in memory."""
    if not text:
        await _safe_send(ctx, "Usage: `!remember <text to remember>`")
        return
    try:
        from tools.memory_query import save_memory
        result = await save_memory(text, source="discord_explicit")
        await _safe_send(ctx, result or "Stored.")
    except Exception as exc:
        await _safe_send(ctx, f"Memory error: {exc}")


@bot.command(name="memory")
async def cmd_memory(ctx, *, query: str = ""):
    """Search memory. Usage: !memory <query>"""
    if not query:
        await _safe_send(ctx, "Usage: `!memory <search query>`")
        return
    try:
        from core.memory import search_memory
        results = search_memory(query, top_k=5)
        if not results:
            await _safe_send(ctx, "No relevant memories found.")
            return
        lines = [f"**Memory search:** `{query}`"]
        for i, r in enumerate(results[:5], 1):
            text = r.get("text", "")[:120]
            score = r.get("score", 0.0)
            lines.append(f"{i}. ({score:.2f}) {text}")
        await _safe_send(ctx, "\n".join(lines))
    except Exception as exc:
        await _safe_send(ctx, f"Memory search error: {exc}")


@bot.command(name="reflect")
async def cmd_reflect(ctx):
    """Trigger an immediate identity evaluation cycle."""
    if _topology is None:
        await _safe_send(ctx, "Topology not ready.")
        return
    await _safe_send(ctx, "Triggering identity reflection cycle… (this runs in the background)")
    try:
        asyncio.create_task(
            _run_reflection_background(ctx)
        )
    except Exception as exc:
        await _safe_send(ctx, f"Reflect error: {exc}")


async def _run_reflection_background(ctx) -> None:
    """Background identity eval with Discord reply on completion."""
    try:
        from core.identity import run_evaluation_cycle
        results = await run_evaluation_cycle(
            primary_endpoint=_topology.primary_endpoint(),
            signal_bus=_bus,
            cfg=_cfg,
        )
        stable = results.get("stable_domains", [])
        cycle  = results.get("cycle", 0)
        await _safe_send(
            ctx,
            f"Reflection cycle {cycle} complete. "
            f"Stable domains: {', '.join(stable) if stable else 'none yet'}."
        )
    except Exception as exc:
        logger.error("Background reflect failed: %s", exc)
        await _safe_send(ctx, f"Reflection failed: {exc}")


@bot.command(name="initiative", aliases=["queue"])
async def cmd_initiative(ctx):
    """Show the initiative queue."""
    # Try to get from server's global engine
    try:
        # Import server module to access _initiative_engine
        import importlib
        srv = importlib.import_module("webui.server")
        engine = getattr(srv, "_initiative_engine", None)
        if engine is None:
            await _safe_send(ctx, "Initiative engine not available.")
            return
        from core.autonomy import can
        queue = engine.get_queue()
        enabled = can("initiative")
        if not queue:
            await _safe_send(
                ctx,
                f"Initiative queue is empty. "
                f"Engine: {'enabled' if enabled else 'disabled (autonomy gate)'}."
            )
            return
        lines = [
            f"**Initiative Queue** ({'enabled' if enabled else 'disabled'}) "
            f"— {len(queue)} item(s)"
        ]
        for item in queue[:5]:
            status = item.get("status", "?")
            itype  = item.get("initiative_type", "?")
            prio   = item.get("priority", "?")
            lines.append(f"• `{itype}` [{prio}] — {status}")
        await _safe_send(ctx, "\n".join(lines))
    except Exception as exc:
        await _safe_send(ctx, f"Initiative error: {exc}")


@bot.command(name="investigate")
async def cmd_investigate(ctx, *, title: str = ""):
    """Create a new investigation. Usage: !investigate <title>"""
    if not title:
        # List existing investigations
        try:
            import importlib
            srv = importlib.import_module("webui.server")
            engine = getattr(srv, "_investigation_engine", None)
            if engine is None:
                await _safe_send(ctx, "Investigation engine not available.")
                return
            items = engine.list(limit=5)
            if not items:
                await _safe_send(ctx, "No investigations yet. Use `!investigate <title>` to create one.")
                return
            lines = ["**Recent Investigations**"]
            for inv in items:
                lines.append(
                    f"• `{inv['investigation_id']}` — {inv['title']} [{inv['status']}]"
                )
            await _safe_send(ctx, "\n".join(lines))
        except Exception as exc:
            await _safe_send(ctx, f"Investigation list error: {exc}")
        return

    try:
        import importlib
        srv = importlib.import_module("webui.server")
        engine = getattr(srv, "_investigation_engine", None)
        if engine is None:
            await _safe_send(ctx, "Investigation engine not available.")
            return
        inv = engine.create(title=title, created_by="discord")
        iid = inv["investigation_id"]
        await _safe_send(
            ctx,
            f"Investigation created: `{iid}`\n"
            f"**{title}**\n"
            f"Use the admin panel to run evidence passes, or wait for the engine "
            f"to pick it up automatically when initiative is enabled."
        )
    except Exception as exc:
        await _safe_send(ctx, f"Investigation create error: {exc}")


# ── Entry point ───────────────────────────────────────────────────────────────

async def start(
    topology: "RuntimeTopology",
    cfg: dict,
    tracer=None,
    bus=None,
    turn_notifiers: list[Callable[[], None]] | None = None,
) -> None:
    """Start the Discord bot.

    Designed to run as an asyncio background task from server.py startup_event.
    Loops forever until cancelled or bot.close() is called.

    If the bot token is missing or Discord is disabled in cfg, returns silently.
    """
    global _errors

    disc_cfg = cfg.get("discord", {})
    if not disc_cfg.get("enabled", False):
        logger.info("Discord bot disabled in config — skipping start")
        return

    inject(topology, cfg, tracer, bus, turn_notifiers)

    try:
        token = _get_token()
    except (FileNotFoundError, ValueError) as exc:
        logger.warning("Discord bot token unavailable: %s — bot will not start", exc)
        return

    logger.info("Starting Discord bot…")
    try:
        await bot.start(token)
    except discord.errors.LoginFailure as exc:
        logger.error("Discord login failed (bad token?): %s", exc)
        _errors += 1
    except asyncio.CancelledError:
        logger.info("Discord bot task cancelled — shutting down")
        await bot.close()
    except Exception as exc:
        logger.error("Discord bot crashed: %s", exc)
        _errors += 1


async def stop() -> None:
    """Gracefully close the Discord bot connection."""
    global _connected
    try:
        await bot.close()
        _connected = False
        logger.info("Discord bot closed")
    except Exception as exc:
        logger.warning("Discord bot close error: %s", exc)
