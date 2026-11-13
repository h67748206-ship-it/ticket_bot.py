"""
Bot de tickets (discord.py 2.x), un seul fichier.

Variables d'environnement :
  TOKEN               -> token du bot (obligatoire)
  STAFF_ROLE_ID       -> ID du rôle staff qui voit les tickets (obligatoire)
  LOG_CHANNEL_ID      -> ID du salon où partent les transcripts (optionnel)
  TICKET_CATEGORY_ID  -> ID de la catégorie où créer les tickets (optionnel,
                         sinon le bot crée une catégorie "Tickets")
  PORT                -> port du mini site web (Render le met tout seul)

Commande (admin uniquement) :
  !panel   -> poste le panneau unique « Besoin d'aide ? » avec le menu
              « Choisir le motif de la demande » (Problème sur bot, Support,
              Nitro / Deco, Problème / Report, Rôle Legit, Créer ton shop)

Installation : pip install -U discord.py
"""

import asyncio
import io
import os
import re
from datetime import datetime, timezone

import discord
from aiohttp import web
from discord.ext import commands

# ------------------------------------------------------------------ CONFIG
TOKEN = os.getenv("TOKEN", "")
STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "0"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))
TICKET_CATEGORY_ID = int(os.getenv("TICKET_CATEGORY_ID", "0"))
PREFIX = "!"
COLOR = 0x5865F2

# Panneau unique : toutes les options dans un seul menu
PANEL_OPTIONS = [
    # key, label, description, emoji
    ("bot", "Problème sur bot", None, "💬"),
    ("support", "Support", "Contacter l'équipe de support", "💬"),
    ("nitro", "Nitro / Deco", None, "🚀"),
    ("report", "Problème / Report", None, "❗"),
    ("legit", "Rôle Legit", None, "✅"),
    ("shop", "Créer ton shop", None, "🛒"),
]

LABELS = {k: l for k, l, _, _ in PANEL_OPTIONS}


# ------------------------------------------------------------------ OUTILS
def slug(text: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text or "user"


def is_staff(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    return any(r.id == STAFF_ROLE_ID for r in member.roles)


def ticket_owner_id(channel: discord.abc.GuildChannel):
    """Le topic du salon contient 'ticket_owner:<id>'."""
    topic = getattr(channel, "topic", None) or ""
    m = re.search(r"ticket_owner:(\d+)", topic)
    return int(m.group(1)) if m else None


async def build_transcript(channel: discord.TextChannel) -> discord.File:
    lines = [f"Transcript de #{channel.name} — {datetime.now(timezone.utc):%d/%m/%Y %H:%M} UTC", "=" * 60]
    async for msg in channel.history(limit=None, oldest_first=True):
        ts = msg.created_at.strftime("%d/%m/%Y %H:%M")
        content = msg.content or ""
        if msg.embeds and not content:
            content = "[embed]"
        for a in msg.attachments:
            content += f" [fichier: {a.url}]"
        lines.append(f"[{ts}] {msg.author} : {content}")
    data = "\n".join(lines).encode("utf-8")
    return discord.File(io.BytesIO(data), filename=f"transcript-{channel.name}.txt")


# ------------------------------------------------------------------ BOUTONS DANS LE TICKET
class TicketControls(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Prendre en charge", style=discord.ButtonStyle.success,
                       emoji="🙋", custom_id="ticket:claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("Seul le staff peut faire ça.", ephemeral=True)
        button.disabled = True
        button.label = f"Pris par {interaction.user.display_name}"
        await interaction.response.edit_message(view=self)
        await interaction.channel.send(f"✅ {interaction.user.mention} s'occupe de ce ticket.")

    @discord.ui.button(label="Fermer", style=discord.ButtonStyle.danger,
                       emoji="🔒", custom_id="ticket:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        owner = ticket_owner_id(interaction.channel)
        if not (is_staff(interaction.user) or interaction.user.id == owner):
            return await interaction.response.send_message("Tu ne peux pas fermer ce ticket.", ephemeral=True)

        await interaction.response.send_message("🔒 Ticket fermé, suppression dans 5 secondes...")
        channel = interaction.channel

        # transcript dans le salon de logs
        if LOG_CHANNEL_ID:
            log = interaction.guild.get_channel(LOG_CHANNEL_ID)
            if log:
                try:
                    f = await build_transcript(channel)
                    embed = discord.Embed(title="Ticket fermé", color=COLOR,
                                          timestamp=datetime.now(timezone.utc))
                    embed.add_field(name="Salon", value=f"#{channel.name}")
                    embed.add_field(name="Fermé par", value=interaction.user.mention)
                    if owner:
                        embed.add_field(name="Ouvert par", value=f"<@{owner}>")
                    await log.send(embed=embed, file=f)
                except Exception as e:
                    print("Erreur transcript :", e)

        await asyncio.sleep(5)
        try:
            await channel.delete(reason=f"Ticket fermé par {interaction.user}")
        except discord.HTTPException:
            pass


# ------------------------------------------------------------------ CRÉATION DU TICKET
async def create_ticket(interaction: discord.Interaction, key: str):
    guild = interaction.guild
    member = interaction.user
    label = LABELS.get(key, key)

    # 1 seul ticket ouvert par membre
    for ch in guild.text_channels:
        if ticket_owner_id(ch) == member.id:
            return await interaction.followup.send(
                f"Tu as déjà un ticket ouvert : {ch.mention}", ephemeral=True)

    staff_role = guild.get_role(STAFF_ROLE_ID)

    # catégorie
    category = guild.get_channel(TICKET_CATEGORY_ID) if TICKET_CATEGORY_ID else None
    if not isinstance(category, discord.CategoryChannel):
        category = discord.utils.get(guild.categories, name="Tickets")
        if category is None:
            category = await guild.create_category("Tickets")

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                            attach_files=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                              manage_channels=True, read_message_history=True),
    }
    if staff_role:
        overwrites[staff_role] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, attach_files=True, read_message_history=True)

    channel = await guild.create_text_channel(
        name=f"{slug(key)}-{slug(member.name)}"[:90],
        category=category,
        overwrites=overwrites,
        topic=f"ticket_owner:{member.id} | type:{label}",
        reason=f"Ticket ouvert par {member}",
    )

    embed = discord.Embed(
        title=f"🎫 Ticket — {label}",
        description=(f"Salut {member.mention} ! Explique ton problème ou ta demande ici, "
                     f"le staff va te répondre dès que possible."),
        color=COLOR,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text=f"Ticket de {member}")
    await channel.send(content=member.mention, embed=embed, view=TicketControls())

    # ping du staff (séparé pour ne pas spammer le membre)
    if staff_role:
        await channel.send(staff_role.mention, allowed_mentions=discord.AllowedMentions(roles=True))

    await interaction.followup.send(f"✅ Ton ticket est ouvert : {channel.mention}", ephemeral=True)


# ------------------------------------------------------------------ PANNEAUX (MENUS)
def make_options(options):
    return [discord.SelectOption(label=l, value=k, description=d, emoji=e)
            for k, l, d, e in options]


class PanelSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(custom_id="panel:main", placeholder="Choisir le motif de la demande",
                         options=make_options(PANEL_OPTIONS), min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await create_ticket(interaction, self.values[0])
        # reset du menu pour pouvoir re-sélectionner la même option
        await interaction.message.edit(view=Panel())


class Panel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(PanelSelect())


# ------------------------------------------------------------------ BOT
intents = discord.Intents.default()
intents.message_content = True
intents.members = True


class TicketBot(commands.Bot):
    async def setup_hook(self):
        # vues persistantes (marchent même après un redémarrage)
        self.add_view(Panel())
        self.add_view(TicketControls())

        # mini site web pour Render / cron-job.org
        async def home(_):
            ready = self.user is not None
            name = str(self.user) if ready else "démarrage..."
            guilds = len(self.guilds) if ready else 0
            ping = round(self.latency * 1000) if ready and self.latency == self.latency else 0
            html = f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ticket Bot</title>
<style>
  body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
       background:#1e1f22;color:#f2f3f5;font-family:system-ui,sans-serif}}
  .card{{background:#2b2d31;border-radius:16px;padding:32px 40px;text-align:center;
        box-shadow:0 8px 30px rgba(0,0,0,.4);max-width:90vw}}
  h1{{margin:0 0 8px;font-size:26px}}
  .dot{{display:inline-block;width:10px;height:10px;border-radius:50%;
       background:{"#23a55a" if ready else "#f0b232"};margin-right:8px}}
  .row{{margin:6px 0;color:#b5bac1}}
  b{{color:#f2f3f5}}
</style></head><body>
<div class="card">
  <h1>🎫 Ticket Bot</h1>
  <div class="row"><span class="dot"></span>{"En ligne" if ready else "Démarrage"}</div>
  <div class="row">Bot : <b>{name}</b></div>
  <div class="row">Serveurs : <b>{guilds}</b> &nbsp;•&nbsp; Ping : <b>{ping} ms</b></div>
</div></body></html>"""
            return web.Response(text=html, content_type="text/html")

        async def health(_):
            return web.Response(text="ok")

        app = web.Application()
        app.add_routes([web.get("/", home), web.get("/health", health)])
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080")))
        await site.start()


bot = TicketBot(command_prefix=PREFIX, intents=intents, help_command=None)


@bot.event
async def on_ready():
    print(f"Connecté en tant que {bot.user} ({bot.user.id})")


@bot.command(name="panel", aliases=["panel_support", "panel_tickets"])
@commands.has_permissions(administrator=True)
async def panel(ctx: commands.Context):
    embed = discord.Embed(
        title="ℹ️ Besoin d'aide ?",
        description="> Choisis une option ci-dessous pour ouvrir un ticket privé.",
        color=COLOR,
    )
    await ctx.send(embed=embed, view=Panel())
    try:
        await ctx.message.delete()
    except discord.HTTPException:
        pass


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("Tu dois être administrateur pour faire ça.", delete_after=5)
    elif isinstance(error, commands.CommandNotFound):
        pass
    else:
        print("Erreur commande :", error)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("⚠️ Mets ton token dans la variable d'environnement TOKEN.")
    bot.run(TOKEN)
