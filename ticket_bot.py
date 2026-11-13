"""
ticket_bot.py — Bot de tickets (discord.py 2.x), un seul fichier.

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
        member: discord.Permissihttps://github.com/h67748206-ship-it/ticket_bot.py/tree/mainonOverwrite(view_channel=True, send_messages=True,
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
