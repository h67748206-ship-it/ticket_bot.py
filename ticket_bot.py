import asyncio
import io
import os
import re

import discord
from aiohttp import web
from discord.ext import commands

TOKEN = os.getenv("TOKEN", "")
STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "0") or 0)
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0") or 0)
CATEGORY_ID = int(os.getenv("TICKET_CATEGORY_ID", "0") or 0)
BANNER_URL = os.getenv("BANNER_URL", "")

OPTIONS = [
    ("bot", "Problème sur bot", None, "💬"),
    ("support", "Support", "Contacter l'équipe de support", "💬"),
    ("nitro", "Nitro / Deco", None, "🚀"),
    ("report", "Problème / Report", None, "❗"),
    ("legit", "Rôle Legit", None, "✅"),
    ("shop", "Créer ton shop", None, "🛒"),
]
LABELS = {k: l for k, l, _, _ in OPTIONS}


def slug(text):
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text or "user"


def is_staff(member):
    if member.guild_permissions.administrator:
        return True
    return any(r.id == STAFF_ROLE_ID for r in member.roles)


def owner_of(channel):
    topic = getattr(channel, "topic", None) or ""
    m = re.search(r"ticket_owner:(\d+)", topic)
    return int(m.group(1)) if m else None


async def transcript(channel):
    lines = []
    async for msg in channel.history(limit=None, oldest_first=True):
        ts = msg.created_at.strftime("%d/%m/%Y %H:%M")
        text = msg.content or "[message du bot]"
        for a in msg.attachments:
            text += " [fichier: " + a.url + "]"
        lines.append("[" + ts + "] " + str(msg.author) + " : " + text)
    data = "\n".join(lines).encode("utf-8")
    return discord.File(io.BytesIO(data), filename="transcript-" + channel.name + ".txt")


# ---------------------------------------------------------------- TICKET
class TicketRow(discord.ui.ActionRow):
    @discord.ui.button(label="Prendre en charge", style=discord.ButtonStyle.success,
                       emoji="🙋", custom_id="ticket:claim")
    async def claim(self, interaction, button):
        if not is_staff(interaction.user):
            await interaction.response.send_message("Seul le staff peut faire ça.", ephemeral=True)
            return
        button.disabled = True
        button.label = "Pris par " + interaction.user.display_name
        await interaction.response.edit_message(view=self.view)
        await interaction.channel.send("✅ " + interaction.user.mention + " s'occupe de ce ticket.")

    @discord.ui.button(label="Fermer", style=discord.ButtonStyle.danger,
                       emoji="🔒", custom_id="ticket:close")
    async def close(self, interaction, button):
        owner = owner_of(interaction.channel)
        if not (is_staff(interaction.user) or interaction.user.id == owner):
            await interaction.response.send_message("Tu ne peux pas fermer ce ticket.", ephemeral=True)
            return
        await interaction.response.send_message("🔒 Ticket fermé, suppression dans 5 secondes...")
        channel = interaction.channel
        if LOG_CHANNEL_ID:
            log = interaction.guild.get_channel(LOG_CHANNEL_ID)
            if log:
                try:
                    f = await transcript(channel)
                    embed = discord.Embed(title="Ticket fermé", color=0x5865F2)
                    embed.add_field(name="Salon", value="#" + channel.name)
                    embed.add_field(name="Fermé par", value=interaction.user.mention)
                    if owner:
                        embed.add_field(name="Ouvert par", value="<@" + str(owner) + ">")
                    await log.send(embed=embed, file=f)
                except Exception as e:
                    print("Erreur transcript :", e)
        await asyncio.sleep(5)
        try:
            await channel.delete()
        except discord.HTTPException:
            pass


class TicketView(discord.ui.LayoutView):
    def __init__(self, label="Ticket", member=None):
        super().__init__(timeout=None)
        who = member.mention if member else ""
        container = discord.ui.Container(
            discord.ui.TextDisplay("## 🎫 Ticket — " + label),
            discord.ui.TextDisplay("> Salut " + who + " ! Explique ton problème ou ta demande ici, "
                                   "le staff va te répondre dès que possible."),
            discord.ui.Separator(),
            TicketRow(),
        )
        self.add_item(container)


async def create_ticket(interaction, key):
    guild = interaction.guild
    member = interaction.user
    label = LABELS.get(key, key)

    for ch in guild.text_channels:
        if owner_of(ch) == member.id:
            await interaction.followup.send("Tu as déjà un ticket ouvert : " + ch.mention, ephemeral=True)
            return

    staff_role = guild.get_role(STAFF_ROLE_ID)

    category = guild.get_channel(CATEGORY_ID) if CATEGORY_ID else None
    if not isinstance(category, discord.CategoryChannel):
        category = discord.utils.get(guild.categories, name="Tickets")
        if category is None:
            category = await guild.create_category("Tickets")

    allow = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                        attach_files=True, read_message_history=True)
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member: allow,
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                              manage_channels=True, read_message_history=True),
    }
    if staff_role:
        overwrites[staff_role] = allow

    channel = await guild.create_text_channel(
        name=(slug(key) + "-" + slug(member.name))[:90],
        category=category,
        overwrites=overwrites,
        topic="ticket_owner:" + str(member.id) + " | type:" + label,
    )

    await channel.send(view=TicketView(label, member))
    pings = member.mention + (" " + staff_role.mention if staff_role else "")
    await channel.send(pings, allowed_mentions=discord.AllowedMentions(users=True, roles=True))
    await interaction.followup.send("✅ Ton ticket est ouvert : " + channel.mention, ephemeral=True)


# ---------------------------------------------------------------- PANNEAU
class PanelRow(discord.ui.ActionRow):
    @discord.ui.select(
        custom_id="panel:main",
        placeholder="Choisir le motif de la demande",
        min_values=1, max_values=1,
        options=[discord.SelectOption(label=l, value=k, description=d, emoji=e)
                 for k, l, d, e in OPTIONS],
    )
    async def pick(self, interaction, select):
        await interaction.response.defer(ephemeral=True)
        try:
            await create_ticket(interaction, select.values[0])
        except Exception as e:
            print("Erreur ticket :", e)
            await interaction.followup.send("Erreur : vérifie les permissions du bot.", ephemeral=True)
        try:
            await interaction.message.edit(view=PanelView())
        except discord.HTTPException:
            pass


class PanelView(discord.ui.LayoutView):
    def __init__(self):
        super().__init__(timeout=None)
        items = []
        if BANNER_URL:
            items.append(discord.ui.MediaGallery(discord.MediaGalleryItem(BANNER_URL)))
        items.append(discord.ui.TextDisplay("## ℹ️ Besoin d'aide ?"))
        items.append(discord.ui.TextDisplay("> Choisis une option ci-dessous pour ouvrir un ticket privé."))
        items.append(PanelRow())
        self.add_item(discord.ui.Container(*items))


# ---------------------------------------------------------------- BOT
intents = discord.Intents.default()
intents.message_content = True
intents.members = True


class TicketBot(commands.Bot):
    async def setup_hook(self):
        self.add_view(PanelView())
        self.add_view(TicketView())

        async def home(request):
            ready = self.user is not None
            name = str(self.user) if ready else "démarrage"
            status = "En ligne" if ready else "Démarrage"
            html = ("<!DOCTYPE html><html><head><meta charset='utf-8'>"
                    "<meta name='viewport' content='width=device-width, initial-scale=1'>"
                    "<title>Ticket Bot</title></head>"
                    "<body style='margin:0;min-height:100vh;display:flex;align-items:center;"
                    "justify-content:center;background:#1e1f22;color:#f2f3f5;"
                    "font-family:sans-serif;text-align:center'>"
                    "<div style='background:#2b2d31;padding:32px 40px;border-radius:16px'>"
                    "<h1>🎫 Ticket Bot</h1><p>" + status + " : " + name + "</p>"
                    "<p>Serveurs : " + str(len(self.guilds)) + "</p></div></body></html>")
            return web.Response(text=html, content_type="text/html")

        async def health(request):
            return web.Response(text="ok")

        app = web.Application()
        app.add_routes([web.get("/", home), web.get("/health", health)])
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080")))
        await site.start()


bot = TicketBot(command_prefix="!", intents=intents, help_command=None)


@bot.event
async def on_ready():
    print("Connecté en tant que", bot.user)


@bot.command(name="panel", aliases=["panel_support", "panel_tickets"])
@commands.has_permissions(administrator=True)
async def panel(ctx):
    await ctx.send(view=PanelView())
    try:
        await ctx.message.delete()
    except discord.HTTPException:
        pass


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("Tu dois être administrateur pour faire ça.", delete_after=5)
    elif not isinstance(error, commands.CommandNotFound):
        print("Erreur commande :", error)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Mets ton token dans la variable TOKEN.")
    bot.run(TOKEN)
