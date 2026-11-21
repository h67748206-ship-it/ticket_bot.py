# -*- coding: utf-8 -*-
"""
(=) Sanctions — bot de modération complet (préfixe "=" + slash commands)
Un seul fichier : sanctions_bot.py
Nécessite discord.py >= 2.6 (pour les embeds Components V2)

Variables d'environnement :
  TOKEN       -> token du bot (obligatoire)
  OWNER_ID    -> ton ID Discord (optionnel, owner global du bot)
  PORT        -> fourni par Render (mini site web pour le keep-alive)
"""
import os
import re
import time
import sqlite3
import asyncio
from datetime import timedelta
from typing import Optional, Literal

import discord
from discord.ext import commands
from aiohttp import web

TOKEN = os.getenv("TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
DB_PATH = os.getenv("DB_PATH", "sanctions.db")
COLOR = 0x2B2D31
HAS_V2 = hasattr(discord.ui, "LayoutView")

# ════════════════════════════════════════════════════════════
#  BASE DE DONNÉES (SQLite)
# ════════════════════════════════════════════════════════════
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
conn.executescript("""
CREATE TABLE IF NOT EXISTS settings(guild_id INTEGER, key TEXT, value TEXT, PRIMARY KEY(guild_id,key));
CREATE TABLE IF NOT EXISTS owners(guild_id INTEGER, user_id INTEGER, PRIMARY KEY(guild_id,user_id));
CREATE TABLE IF NOT EXISTS sanctions(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, user_id INTEGER, mod_id INTEGER,
    type TEXT, reason TEXT, created REAL, ends REAL, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS blacklist(
    guild_id INTEGER, user_id INTEGER, reason TEXT, mod_id INTEGER, created REAL,
    PRIMARY KEY(guild_id,user_id));
CREATE TABLE IF NOT EXISTS protected(guild_id INTEGER, user_id INTEGER, PRIMARY KEY(guild_id,user_id));
CREATE TABLE IF NOT EXISTS absences(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, user_id INTEGER, reason TEXT,
    ends REAL, status TEXT DEFAULT 'pending', created REAL, decided_by INTEGER);
CREATE TABLE IF NOT EXISTS tribunal(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, target_id INTEGER, requester_id INTEGER,
    reason TEXT, proof TEXT, status TEXT DEFAULT 'pending', created REAL, decided_by INTEGER);
CREATE TABLE IF NOT EXISTS blrank(
    guild_id INTEGER, user_id INTEGER, reason TEXT, mod_id INTEGER, created REAL, PRIMARY KEY(guild_id,user_id));
CREATE TABLE IF NOT EXISTS role_rules(
    guild_id INTEGER, role_id INTEGER, type TEXT, other_id INTEGER DEFAULT 0,
    PRIMARY KEY(guild_id,role_id,type,other_id));
CREATE TABLE IF NOT EXISTS access(guild_id INTEGER, command TEXT, level INTEGER, PRIMARY KEY(guild_id,command));
CREATE TABLE IF NOT EXISTS access_roles(guild_id INTEGER, role_id INTEGER, level INTEGER, PRIMARY KEY(guild_id,role_id));
""")
conn.commit()


def db_run(sql, params=()):
    cur = conn.execute(sql, params)
    conn.commit()
    return cur.lastrowid


def db_one(sql, params=()):
    return conn.execute(sql, params).fetchone()


def db_all(sql, params=()):
    return conn.execute(sql, params).fetchall()


def get_setting(guild_id, key, default=None):
    r = db_one("SELECT value FROM settings WHERE guild_id=? AND key=?", (guild_id, key))
    return r["value"] if r else default


def set_setting(guild_id, key, value):
    db_run("INSERT INTO settings(guild_id,key,value) VALUES(?,?,?) "
           "ON CONFLICT(guild_id,key) DO UPDATE SET value=excluded.value", (guild_id, key, str(value)))


# ════════════════════════════════════════════════════════════
#  OUTILS
# ════════════════════════════════════════════════════════════
def emb(desc=None, title=None, color=COLOR):
    return discord.Embed(title=title, description=desc, color=color)


async def ok(ctx, text):
    return await ctx.send(embed=emb(f"✅ {text}", color=0x57F287))


async def ko(ctx, text):
    return await ctx.send(embed=emb(f"❌ {text}", color=0xED4245))


def tsd(x, style="R"):
    return f"<t:{int(x)}:{style}>"


DUR_RE = re.compile(r"^(\d+)\s*([smhdj])$", re.I)


def parse_duration(s: str):
    m = DUR_RE.match(s.strip())
    if not m:
        return None
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "j": 86400}[m[2].lower()]
    return timedelta(seconds=int(m[1]) * mult)


async def is_owner(ctx):
    if not ctx.guild:
        return False
    if OWNER_ID and ctx.author.id == OWNER_ID:
        return True
    if ctx.author.id == ctx.guild.owner_id:
        return True
    return db_one("SELECT 1 FROM owners WHERE guild_id=? AND user_id=?",
                  (ctx.guild.id, ctx.author.id)) is not None


def user_level(guild_id, member):
    ids = [r.id for r in member.roles]
    if not ids:
        return 0
    q = ",".join("?" * len(ids))
    r = db_one(f"SELECT MAX(level) m FROM access_roles WHERE guild_id=? AND role_id IN ({q})", (guild_id, *ids))
    return r["m"] or 0


async def access_decision(ctx):
    """True = autorisé par règle d'accès, False = refusé, None = pas de règle."""
    if not ctx.guild or not ctx.command:
        return None
    row = db_one("SELECT level FROM access WHERE guild_id=? AND command=?",
                 (ctx.guild.id, ctx.command.qualified_name))
    if not row:
        return None
    if row["level"] < 0:
        return True
    if await is_owner(ctx):
        return True
    return user_level(ctx.guild.id, ctx.author) >= row["level"]


async def global_access(ctx):
    if await access_decision(ctx) is False:
        raise commands.CheckFailure("Niveau d'accès insuffisant pour cette commande.")
    return True


def perm(**perms):
    """Owner = accès total ; règle `acces` si définie ; sinon permissions Discord."""
    async def pred(ctx):
        if not ctx.guild:
            return False
        if await is_owner(ctx):
            return True
        d = await access_decision(ctx)
        if d is True:
            return True
        if d is False:
            raise commands.CheckFailure("Niveau d'accès insuffisant pour cette commande.")
        p = ctx.author.guild_permissions
        if p.administrator or all(getattr(p, k) for k in perms):
            return True
        raise commands.MissingPermissions(list(perms))
    return commands.check(pred)


def owner_only():
    async def pred(ctx):
        if await is_owner(ctx):
            return True
        raise commands.CheckFailure("Commande réservée aux owners.")
    return commands.check(pred)


async def can_act(ctx, target):
    if not isinstance(target, discord.Member):
        return True
    if target.id == ctx.guild.owner_id or target == ctx.guild.me:
        return False
    if ctx.guild.me.top_role <= target.top_role:
        return False
    if await is_owner(ctx):
        return True
    return ctx.author.top_role > target.top_role


async def send_log(bot, guild, embed):
    kind = (embed.title or "").lower()
    cid = get_setting(guild.id, f"logch:{kind}") or get_setting(guild.id, "logs")
    custom = get_setting(guild.id, f"logtitle:{kind}")
    if custom:
        embed.title = custom
    if not cid:
        return
    ch = guild.get_channel(int(cid))
    if ch:
        try:
            await ch.send(embed=embed)
        except discord.HTTPException:
            pass


def rank_roles(guild):
    """Rôles de la plage de rangs (de rank_top à rank_bottom incluse)."""
    t, b = get_setting(guild.id, "rank_top"), get_setting(guild.id, "rank_bottom")
    if not t or not b:
        return []
    rt, rb = guild.get_role(int(t)), guild.get_role(int(b))
    if not rt or not rb:
        return []
    lo, hi = sorted((rt.position, rb.position))
    return [r for r in guild.roles if lo <= r.position <= hi and not r.is_default() and not r.managed]


def log_embed(title, ctx, target, reason=None, extra=None):
    e = emb(title=title, color=COLOR)
    e.add_field(name="Cible", value=f"{target} (`{target.id}`)")
    e.add_field(name="Modérateur", value=f"{ctx.author.mention}")
    if reason:
        e.add_field(name="Motif", value=reason[:1000], inline=False)
    if extra:
        e.add_field(name="Infos", value=extra, inline=False)
    return e


async def try_dm(user, text):
    try:
        await user.send(text)
    except discord.HTTPException:
        pass


def record(ctx, user_id, typ, reason, ends=None):
    return db_run("INSERT INTO sanctions(guild_id,user_id,mod_id,type,reason,created,ends) "
                  "VALUES(?,?,?,?,?,?,?)",
                  (ctx.guild.id, user_id, ctx.author.id, typ, reason, time.time(), ends))


# ════════════════════════════════════════════════════════════
#  AIDE (/help · =help) — Components V2
# ════════════════════════════════════════════════════════════
def C(cmd, desc):
    return f"`/{cmd}` · `={cmd}` — {desc}"


HELP = {
    "absences": ("ABSENCES", "Demandes d'absence staff et suivi", [
        C("absence set", "Déposer une demande d'absence à valider"),
        C("absence reset", "Annuler une absence en cours"),
        C("absences", "Consulter les demandes et décisions d'absence."),
    ]),
    "sanctions": ("SANCTIONS", "Avertissements, mutes, bans, blacklist", [
        C("avert", "Consigner un avertissement motivé dans le dossier du membre."),
        C("avertissements", "Examiner les avertissements actifs et numéros de dossier."),
        C("remove avert", "Retirer un avertissement avec son numéro de dossier"),
        C("tempmute", "Appliquer un timeout Discord avec une durée et un motif."),
        C("unmute", "Lever le timeout Discord actif d'un membre."),
        C("mutelist", "Afficher les dossiers de timeout dont la date de fin est future."),
        C("ban", "Bannir un membre et choisir la purge des messages récents."),
        C("unban", "Retirer un bannissement avec l'identifiant Discord cible."),
        C("unbanall", "Ouvrir une confirmation avant le débannissement global."),
        C("banlist", "Examiner les bannissements actifs du serveur."),
        C("baninfo", "Lire le motif Discord et le dossier de ban d'un identifiant."),
        C("sanctions", "Afficher les sanctions récentes consignées pour un membre."),
        C("bl add", "Ajouter à la blacklist et appliquer le ban"),
        C("bl info", "Consulter le statut blacklist d'un utilisateur"),
        C("bl list", "Lister les membres blacklistés"),
        C("bl remove", "Retirer un utilisateur de la blacklist"),
        C("protect add", "Protéger un utilisateur contre le ban"),
        C("protect info", "Voir si un utilisateur est protégé"),
        C("protect list", "Afficher la liste des utilisateurs protégés"),
        C("protect remove", "Retirer la protection anti-ban"),
        C("tribunal", "Configurer le Tribunal ou soumettre une demande de bannissement."),
    ]),
    "salons": ("SALONS", "Gestion des salons textuels et vocaux", [
        C("clear", "Supprimer un nombre cible de messages récents du salon."),
        C("lock", "Bloquer l'envoi de messages pour @everyone dans ce salon."),
        C("unlock", "Restaurer la permission d'envoi de messages de @everyone."),
        C("hide", "Masquer ce salon à @everyone sans modifier les autres accès."),
        C("unhide", "Rendre ce salon visible à @everyone."),
        C("lockall", "Verrouiller tous les salons textuels du serveur."),
        C("unlockall", "Déverrouiller tous les salons textuels du serveur."),
        C("hideall", "Cacher tous les salons textuels du serveur."),
        C("unhideall", "Rendre visibles tous les salons textuels du serveur."),
        C("renew", "Cloner ce salon puis supprimer l'original ; messages perdus."),
        C("nick", "Modifier ou retirer le surnom d'un membre."),
        C("mv", "Déplacer un membre connecté vers un autre salon vocal."),
        C("pv", "Fermer/rouvrir temporairement un vocal aux nouvelles connexions."),
    ]),
    "roles": ("RÔLES", "Attribution et retrait de rôles", [
        C("addrole", "Attribuer un rôle à un membre avec contrôle de hiérarchie."),
        C("removerole", "Retirer un rôle attribué à un membre."),
        C("rank", "Attribuer un rôle de rang configuré à un membre."),
        C("derank", "Retirer du membre les rôles inclus dans la plage configurée."),
        C("blrank add", "Derank et ajouter à la blacklist de rang"),
        C("blrank list", "Lister les blacklists de rang"),
        C("blrank remove", "Retirer la blacklist de rang"),
        C("limitrole", "Bloquer des rôles, définir les autorisations et lier des rôles."),
    ]),
    "infos": ("INFOS", "Profils, serveur et utilitaires", [
        C("snipe", "Consulter le dernier message supprimé encore en cache."),
        C("vc", "Afficher les statistiques vocales et serveur."),
        C("find", "Trouver un membre présent dans un salon vocal."),
        C("pic", "Ouvrir l'avatar d'un membre en résolution originale."),
        C("banner", "Ouvrir la bannière de profil publique d'un membre."),
        C("user", "Examiner le profil, les rôles et les dates d'un membre."),
        C("serveur info", "Afficher la fiche complète du serveur"),
        C("serveur pic", "Afficher l'icône du serveur"),
        C("serveur banner", "Afficher la bannière du serveur"),
        C("serveurs", "Lister les serveurs de cette instance du bot."),
        C("create emoji", "Créer un emoji à partir d'une image"),
        C("create salon", "Créer un salon texte"),
        C("create role", "Créer un rôle"),
        C("rolemembers", "Lister les membres portant un rôle et compter les résultats."),
    ]),
    "config": ("CONFIGURATION", "Réglages du bot et du serveur", [
        C("setup status", "Afficher l'état complet de la configuration du serveur"),
        C("setup logs", "Configurer le salon des logs de modération"),
        C("setup roles", "Configurer le rôle staff de modération"),
        C("setup tribunal", "Configurer le Tribunal ou définir son salon de demandes"),
        C("acces panneau", "Ouvrir le panneau des niveaux et permissions"),
        C("acces role", "Associe un rôle à un niveau"),
        C("acces commande", "Définit le niveau requis pour une commande"),
        C("acces public", "Autorise ou interdit une commande de consultation à tous"),
        C("setup absence", "Configurer le salon des demandes et le rôle valideur"),
        C("setup limites", "Définir les plafonds d'avertissements avant auto-mute"),
        C("owner add", "Ajouter un owner"),
        C("owner list", "Afficher la liste des owners"),
        C("owner remove", "Retirer un owner"),
        C("setup rangs", "Définir la plage de rôles de rang"),
        C("prefix", "Changer le préfixe textuel des commandes."),
        C("customlogs", "Personnaliser le salon et le titre d'un type de journal."),
        C("customvc", "Personnaliser le message d'information de /vc."),
        C("profil", "Ouvrir le panneau ou modifier le profil global du bot."),
    ]),
}
HELP_ORDER = ["absences", "sanctions", "salons", "roles", "infos", "config"]
HELP_LABEL = {"absences": "Absences", "sanctions": "Sanctions", "salons": "Salons",
              "roles": "Rôles", "infos": "Infos", "config": "Configuration"}


def help_page(key):
    if key == "accueil":
        body = ("Bot de modération complet.\n\nUtilise le menu ci-dessous pour parcourir "
                "les catégories. Toutes les commandes marchent en **slash** (`/`) et avec le "
                "**préfixe** (`=` par défaut).")
        return "AIDE", "Accueil", body
    title, over, cmds = HELP[key]
    return title, over, "\n".join(cmds)


class HelpView(discord.ui.LayoutView if HAS_V2 else discord.ui.View):
    def __init__(self, author_id, page="accueil"):
        super().__init__(timeout=600)
        self.author_id = author_id
        self.page = page
        if HAS_V2:
            self.build()

    def build(self):
        self.clear_items()
        title, over, body = help_page(self.page)
        c = discord.ui.Container(accent_colour=COLOR)
        c.add_item(discord.ui.TextDisplay(f"# {title}"))
        c.add_item(discord.ui.Separator())
        c.add_item(discord.ui.TextDisplay(f"## ESPACE\n**Vue d'ensemble**\n{over}"))
        c.add_item(discord.ui.Separator())
        c.add_item(discord.ui.TextDisplay(f"### Commandes de cette page\n{body}"))
        c.add_item(discord.ui.Separator())

        row1 = discord.ui.ActionRow()
        sel = discord.ui.Select(
            placeholder="Choisir une catégorie",
            options=[discord.SelectOption(label=HELP_LABEL[k], value=k, default=(k == self.page))
                     for k in HELP_ORDER])
        sel.callback = self.on_select
        row1.add_item(sel)
        c.add_item(row1)

        row2 = discord.ui.ActionRow()
        home = discord.ui.Button(label="Accueil", style=discord.ButtonStyle.secondary)
        home.callback = self.on_home
        row2.add_item(home)
        c.add_item(row2)
        self.add_item(c)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Ce menu n'est pas pour toi.", ephemeral=True)
            return False
        return True

    async def on_select(self, interaction):
        self.page = interaction.data["values"][0]
        self.build()
        await interaction.response.edit_message(view=self)

    async def on_home(self, interaction):
        self.page = "accueil"
        self.build()
        await interaction.response.edit_message(view=self)


# ════════════════════════════════════════════════════════════
#  BOT
# ════════════════════════════════════════════════════════════
async def get_prefix(bot, message):
    p = get_setting(message.guild.id, "prefix", "=") if message.guild else "="
    return commands.when_mentioned_or(p)(bot, message)


class SanctionsBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=get_prefix, intents=discord.Intents.all(),
                         help_command=None, case_insensitive=True)
        self.snipes = {}

    async def setup_hook(self):
        for cog in (Aide, Sanctions, Blacklist, Salons, Roles, Infos, Config, Absences, Tribunal, Acces):
            await self.add_cog(cog(self))
        self.add_check(global_access)
        self.add_dynamic_items(AbsBtn, TribBtn)
        await self.tree.sync()
        # mini site web (keep-alive Render)
        app = web.Application()
        app.router.add_get("/", lambda r: web.Response(text="(=) Sanctions — en ligne"))
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080"))).start()

    async def on_ready(self):
        print(f"Connecté : {self.user} ({len(self.guilds)} serveurs)")
        await self.change_presence(activity=discord.Game(name=get_setting(0, "status", "=help")))

    async def on_command_error(self, ctx, error):
        error = getattr(error, "original", error)
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.MissingPermissions):
            return await ko(ctx, "Tu n'as pas la permission d'utiliser cette commande.")
        if isinstance(error, commands.CheckFailure):
            return await ko(ctx, str(error) or "Accès refusé.")
        if isinstance(error, commands.MissingRequiredArgument):
            return await ko(ctx, f"Argument manquant : `{error.param.name}`.")
        if isinstance(error, (commands.BadArgument, commands.BadUnionArgument)):
            return await ko(ctx, "Argument invalide. Vérifie la mention / l'ID / la valeur.")
        if isinstance(error, discord.Forbidden):
            return await ko(ctx, "Je n'ai pas les permissions nécessaires (hiérarchie des rôles ?).")
        print("Erreur:", repr(error))
        await ko(ctx, f"Erreur : `{error}`")

    async def on_member_update(self, before, after):
        if before.roles == after.roles:
            return
        if db_one("SELECT 1 FROM blrank WHERE guild_id=? AND user_id=?", (after.guild.id, after.id)):
            bad = [r for r in rank_roles(after.guild) if r in after.roles]
            if bad:
                try:
                    await after.remove_roles(*bad, reason="Blacklist de rang")
                except discord.HTTPException:
                    pass

    async def on_message_delete(self, m):
        if m.guild and not m.author.bot:
            self.snipes[m.channel.id] = (m.author, m.content, m.attachments, time.time())

    async def on_member_join(self, member):
        if db_one("SELECT 1 FROM blacklist WHERE guild_id=? AND user_id=?", (member.guild.id, member.id)):
            try:
                await member.guild.ban(member, reason="Blacklist")
            except discord.HTTPException:
                pass


# ════════════════════════════════════════════════════════════
#  COG : AIDE
# ════════════════════════════════════════════════════════════
class Aide(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="help", description="Afficher l'aide du bot")
    async def help_cmd(self, ctx):
        if HAS_V2:
            await ctx.send(view=HelpView(ctx.author.id))
        else:
            e = emb("\n".join(c for k in HELP_ORDER for c in HELP[k][2])[:4000], "AIDE")
            await ctx.send(embed=e)


# ════════════════════════════════════════════════════════════
#  COG : SANCTIONS
# ════════════════════════════════════════════════════════════
class ConfirmUnbanAll(discord.ui.View):
    def __init__(self, ctx):
        super().__init__(timeout=60)
        self.ctx = ctx

    async def interaction_check(self, i):
        return i.user.id == self.ctx.author.id

    @discord.ui.button(label="Confirmer", style=discord.ButtonStyle.danger)
    async def yes(self, i, b):
        await i.response.edit_message(content="⏳ Débannissement en cours…", embed=None, view=None)
        n = 0
        async for entry in self.ctx.guild.bans(limit=None):
            try:
                await self.ctx.guild.unban(entry.user, reason=f"unbanall par {self.ctx.author}")
                n += 1
            except discord.HTTPException:
                pass
        await i.edit_original_response(content=f"✅ {n} membre(s) débanni(s).")

    @discord.ui.button(label="Annuler", style=discord.ButtonStyle.secondary)
    async def no(self, i, b):
        await i.response.edit_message(content="Annulé.", embed=None, view=None)


class Sanctions(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ── avertissements ──
    @commands.hybrid_command(name="avert", description="Consigner un avertissement")
    @perm(moderate_members=True)
    async def avert(self, ctx, member: discord.Member, *, reason: str = "Aucun motif"):
        if not await can_act(ctx, member):
            return await ko(ctx, "Hiérarchie insuffisante.")
        sid = record(ctx, member.id, "warn", reason)
        await try_dm(member, f"⚠️ Avertissement sur **{ctx.guild.name}** : {reason}")
        await ok(ctx, f"{member.mention} averti (dossier **#{sid}**) — {reason}")
        await send_log(self.bot, ctx.guild, log_embed("Avertissement", ctx, member, reason, f"Dossier #{sid}"))
        # auto-mute si limite atteinte
        limit = int(get_setting(ctx.guild.id, "warn_limit", 0) or 0)
        if limit:
            n = db_one("SELECT COUNT(*) c FROM sanctions WHERE guild_id=? AND user_id=? AND type='warn' AND active=1",
                       (ctx.guild.id, member.id))["c"]
            if n >= limit:
                mins = int(get_setting(ctx.guild.id, "auto_mute_min", 60))
                try:
                    await member.timeout(timedelta(minutes=mins), reason=f"{n} avertissements")
                    record(ctx, member.id, "mute", f"Auto-mute ({n} avertissements)",
                           time.time() + mins * 60)
                    await ctx.send(embed=emb(f"🔇 {member.mention} mute automatiquement {mins} min ({n} avertissements)."))
                except discord.HTTPException:
                    pass

    @commands.hybrid_command(name="avertissements", description="Voir les avertissements d'un membre")
    @perm(moderate_members=True)
    async def avertissements(self, ctx, member: discord.Member):
        rows = db_all("SELECT * FROM sanctions WHERE guild_id=? AND user_id=? AND type='warn' AND active=1 "
                      "ORDER BY id DESC LIMIT 25", (ctx.guild.id, member.id))
        if not rows:
            return await ctx.send(embed=emb(f"Aucun avertissement actif pour {member.mention}."))
        txt = "\n".join(f"**#{r['id']}** {tsd(r['created'], 'd')} — {r['reason']} (par <@{r['mod_id']}>)" for r in rows)
        await ctx.send(embed=emb(txt[:4000], f"Avertissements de {member}"))

    @commands.hybrid_group(name="remove", description="Retirer un élément", invoke_without_command=True)
    @perm(moderate_members=True)
    async def remove(self, ctx):
        await ko(ctx, "Utilise `remove avert <numéro>`.")

    @remove.command(name="avert", description="Retirer un avertissement par numéro de dossier")
    @perm(moderate_members=True)
    async def remove_avert(self, ctx, dossier: int):
        r = db_one("SELECT * FROM sanctions WHERE id=? AND guild_id=? AND type='warn' AND active=1",
                   (dossier, ctx.guild.id))
        if not r:
            return await ko(ctx, "Dossier introuvable.")
        db_run("UPDATE sanctions SET active=0 WHERE id=?", (dossier,))
        await ok(ctx, f"Avertissement **#{dossier}** retiré.")

    # ── mutes ──
    @commands.hybrid_command(name="tempmute", description="Timeout Discord avec durée (ex: 10m, 2h, 1d)")
    @perm(moderate_members=True)
    async def tempmute(self, ctx, member: discord.Member, duree: str, *, reason: str = "Aucun motif"):
        d = parse_duration(duree)
        if not d:
            return await ko(ctx, "Durée invalide. Exemples : `30s`, `10m`, `2h`, `1d`.")
        if d > timedelta(days=28):
            return await ko(ctx, "Maximum 28 jours.")
        if not await can_act(ctx, member):
            return await ko(ctx, "Hiérarchie insuffisante.")
        await member.timeout(d, reason=f"{reason} | {ctx.author}")
        sid = record(ctx, member.id, "mute", reason, time.time() + d.total_seconds())
        await try_dm(member, f"🔇 Tu as été mute sur **{ctx.guild.name}** ({duree}) : {reason}")
        await ok(ctx, f"{member.mention} mute pendant **{duree}** (dossier #{sid}) — {reason}")
        await send_log(self.bot, ctx.guild, log_embed("Tempmute", ctx, member, reason, f"Durée : {duree}"))

    @commands.hybrid_command(name="unmute", description="Lever le timeout d'un membre")
    @perm(moderate_members=True)
    async def unmute(self, ctx, member: discord.Member):
        await member.timeout(None)
        db_run("UPDATE sanctions SET active=0 WHERE guild_id=? AND user_id=? AND type='mute'",
               (ctx.guild.id, member.id))
        await ok(ctx, f"{member.mention} n'est plus mute.")
        await send_log(self.bot, ctx.guild, log_embed("Unmute", ctx, member))

    @commands.hybrid_command(name="mutelist", description="Afficher les mutes en cours")
    @perm(moderate_members=True)
    async def mutelist(self, ctx):
        rows = db_all("SELECT * FROM sanctions WHERE guild_id=? AND type='mute' AND active=1 AND ends>? "
                      "ORDER BY ends", (ctx.guild.id, time.time()))
        if not rows:
            return await ctx.send(embed=emb("Aucun mute en cours."))
        txt = "\n".join(f"<@{r['user_id']}> — fin {tsd(r['ends'])} — {r['reason']}" for r in rows[:30])
        await ctx.send(embed=emb(txt[:4000], "Mutes en cours"))

    # ── bans ──
    @commands.hybrid_command(name="ban", description="Bannir un membre (purge optionnelle 0-7 jours)")
    @perm(ban_members=True)
    async def ban(self, ctx, user: discord.User, purge: Optional[int] = 0, *, reason: str = "Aucun motif"):
        if db_one("SELECT 1 FROM protected WHERE guild_id=? AND user_id=?", (ctx.guild.id, user.id)):
            return await ko(ctx, "Cet utilisateur est **protégé** contre le ban.")
        member = ctx.guild.get_member(user.id)
        if member and not await can_act(ctx, member):
            return await ko(ctx, "Hiérarchie insuffisante.")
        purge = max(0, min(7, purge or 0))
        await try_dm(user, f"🔨 Tu as été banni de **{ctx.guild.name}** : {reason}")
        await ctx.guild.ban(user, reason=f"{reason} | {ctx.author}", delete_message_seconds=purge * 86400)
        sid = record(ctx, user.id, "ban", reason)
        await ok(ctx, f"**{user}** banni (dossier #{sid}) — {reason}")
        await send_log(self.bot, ctx.guild, log_embed("Ban", ctx, user, reason, f"Purge : {purge}j"))

    @commands.hybrid_command(name="unban", description="Débannir un utilisateur (ID)")
    @perm(ban_members=True)
    async def unban(self, ctx, user: discord.User):
        try:
            await ctx.guild.unban(user, reason=f"par {ctx.author}")
        except discord.NotFound:
            return await ko(ctx, "Cet utilisateur n'est pas banni.")
        db_run("UPDATE sanctions SET active=0 WHERE guild_id=? AND user_id=? AND type='ban'", (ctx.guild.id, user.id))
        await ok(ctx, f"**{user}** débanni.")
        await send_log(self.bot, ctx.guild, log_embed("Unban", ctx, user))

    @commands.hybrid_command(name="unbanall", description="Débannir tout le monde (avec confirmation)")
    @perm(administrator=True)
    async def unbanall(self, ctx):
        await ctx.send(embed=emb("⚠️ Débannir **tous** les membres bannis du serveur ?", color=0xFEE75C),
                       view=ConfirmUnbanAll(ctx))

    @commands.hybrid_command(name="banlist", description="Lister les bannissements actifs")
    @perm(ban_members=True)
    async def banlist(self, ctx):
        await ctx.defer()
        bans = [b async for b in ctx.guild.bans(limit=None)]
        if not bans:
            return await ctx.send(embed=emb("Aucun bannissement actif."))
        lines = [f"`{b.user.id}` **{b.user}** — {(b.reason or 'n/a')[:60]}" for b in bans[:25]]
        e = emb("\n".join(lines), f"Bannissements ({len(bans)})")
        if len(bans) > 25:
            e.set_footer(text=f"… et {len(bans) - 25} autres")
        await ctx.send(embed=e)

    @commands.hybrid_command(name="baninfo", description="Infos de ban d'un identifiant")
    @perm(ban_members=True)
    async def baninfo(self, ctx, user: discord.User):
        try:
            b = await ctx.guild.fetch_ban(user)
        except discord.NotFound:
            return await ko(ctx, "Cet utilisateur n'est pas banni.")
        e = emb(title=f"Ban de {user}")
        e.add_field(name="Motif Discord", value=b.reason or "n/a", inline=False)
        r = db_one("SELECT * FROM sanctions WHERE guild_id=? AND user_id=? AND type='ban' ORDER BY id DESC",
                   (ctx.guild.id, user.id))
        if r:
            e.add_field(name="Dossier", value=f"#{r['id']} par <@{r['mod_id']}> {tsd(r['created'])}\n{r['reason']}",
                        inline=False)
        await ctx.send(embed=e)

    @commands.hybrid_command(name="sanctions", description="Sanctions récentes d'un membre")
    @perm(moderate_members=True)
    async def sanctions(self, ctx, user: discord.User):
        rows = db_all("SELECT * FROM sanctions WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 15",
                      (ctx.guild.id, user.id))
        if not rows:
            return await ctx.send(embed=emb(f"Aucune sanction pour **{user}**."))
        txt = "\n".join(f"**#{r['id']}** `{r['type']}` {tsd(r['created'], 'd')} — {r['reason']} (<@{r['mod_id']}>)"
                        for r in rows)
        await ctx.send(embed=emb(txt[:4000], f"Sanctions de {user}"))


# ════════════════════════════════════════════════════════════
#  COG : BLACKLIST & PROTECT
# ════════════════════════════════════════════════════════════
class Blacklist(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_group(name="bl", description="Gestion de la blacklist", invoke_without_command=True)
    @perm(ban_members=True)
    async def bl(self, ctx):
        await ko(ctx, "Sous-commandes : `add`, `info`, `list`, `remove`.")

    @bl.command(name="add", description="Blacklister et bannir")
    @perm(ban_members=True)
    async def bl_add(self, ctx, user: discord.User, *, reason: str = "Aucun motif"):
        if db_one("SELECT 1 FROM protected WHERE guild_id=? AND user_id=?", (ctx.guild.id, user.id)):
            return await ko(ctx, "Cet utilisateur est **protégé**.")
        member = ctx.guild.get_member(user.id)
        if member and not await can_act(ctx, member):
            return await ko(ctx, "Hiérarchie insuffisante.")
        db_run("INSERT OR REPLACE INTO blacklist VALUES(?,?,?,?,?)",
               (ctx.guild.id, user.id, reason, ctx.author.id, time.time()))
        await ctx.guild.ban(user, reason=f"Blacklist : {reason}")
        record(ctx, user.id, "blacklist", reason)
        await ok(ctx, f"**{user}** blacklisté et banni.")
        await send_log(self.bot, ctx.guild, log_embed("Blacklist", ctx, user, reason))

    @bl.command(name="info", description="Statut blacklist d'un utilisateur")
    @perm(ban_members=True)
    async def bl_info(self, ctx, user: discord.User):
        r = db_one("SELECT * FROM blacklist WHERE guild_id=? AND user_id=?", (ctx.guild.id, user.id))
        if not r:
            return await ctx.send(embed=emb(f"**{user}** n'est pas blacklisté."))
        await ctx.send(embed=emb(f"**{user}** est blacklisté depuis {tsd(r['created'])}\n"
                                 f"Par <@{r['mod_id']}> — {r['reason']}"))

    @bl.command(name="list", description="Lister les blacklistés")
    @perm(ban_members=True)
    async def bl_list(self, ctx):
        rows = db_all("SELECT * FROM blacklist WHERE guild_id=? ORDER BY created DESC LIMIT 30", (ctx.guild.id,))
        if not rows:
            return await ctx.send(embed=emb("Blacklist vide."))
        await ctx.send(embed=emb("\n".join(f"<@{r['user_id']}> (`{r['user_id']}`) — {r['reason']}" for r in rows)[:4000],
                                 "Blacklist"))

    @bl.command(name="remove", description="Retirer de la blacklist (et débannir)")
    @perm(ban_members=True)
    async def bl_remove(self, ctx, user: discord.User):
        db_run("DELETE FROM blacklist WHERE guild_id=? AND user_id=?", (ctx.guild.id, user.id))
        try:
            await ctx.guild.unban(user)
        except discord.HTTPException:
            pass
        await ok(ctx, f"**{user}** retiré de la blacklist.")

    @commands.hybrid_group(name="protect", description="Protection anti-ban", invoke_without_command=True)
    @owner_only()
    async def protect(self, ctx):
        await ko(ctx, "Sous-commandes : `add`, `info`, `list`, `remove`.")

    @protect.command(name="add", description="Protéger un utilisateur contre le ban")
    @owner_only()
    async def protect_add(self, ctx, user: discord.User):
        db_run("INSERT OR IGNORE INTO protected VALUES(?,?)", (ctx.guild.id, user.id))
        await ok(ctx, f"**{user}** est maintenant protégé.")

    @protect.command(name="info", description="Voir si un utilisateur est protégé")
    @owner_only()
    async def protect_info(self, ctx, user: discord.User):
        p = db_one("SELECT 1 FROM protected WHERE guild_id=? AND user_id=?", (ctx.guild.id, user.id))
        await ctx.send(embed=emb(f"**{user}** : {'🛡️ protégé' if p else 'non protégé'}"))

    @protect.command(name="list", description="Liste des utilisateurs protégés")
    @owner_only()
    async def protect_list(self, ctx):
        rows = db_all("SELECT user_id FROM protected WHERE guild_id=?", (ctx.guild.id,))
        await ctx.send(embed=emb("\n".join(f"<@{r['user_id']}>" for r in rows) or "Aucun utilisateur protégé.",
                                 "Utilisateurs protégés"))

    @protect.command(name="remove", description="Retirer la protection anti-ban")
    @owner_only()
    async def protect_remove(self, ctx, user: discord.User):
        db_run("DELETE FROM protected WHERE guild_id=? AND user_id=?", (ctx.guild.id, user.id))
        await ok(ctx, f"Protection retirée pour **{user}**.")


# ════════════════════════════════════════════════════════════
#  COG : SALONS
# ════════════════════════════════════════════════════════════
class Salons(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _set(self, ctx, channels, **kw):
        everyone = ctx.guild.default_role
        n = 0
        for ch in channels:
            ow = ch.overwrites_for(everyone)
            for k, v in kw.items():
                setattr(ow, k, v)
            try:
                await ch.set_permissions(everyone, overwrite=ow)
                n += 1
            except discord.HTTPException:
                pass
        return n

    @commands.hybrid_command(name="clear", description="Supprimer des messages récents")
    @perm(manage_messages=True)
    async def clear(self, ctx, nombre: commands.Range[int, 1, 1000]):
        await ctx.defer(ephemeral=True)
        extra = 1 if ctx.interaction is None else 0
        deleted = await ctx.channel.purge(limit=nombre + extra)
        await ctx.send(embed=emb(f"🧹 {max(0, len(deleted) - extra)} message(s) supprimé(s)."), delete_after=5)

    @commands.hybrid_command(name="lock", description="Bloquer l'envoi de messages pour @everyone")
    @perm(manage_channels=True)
    async def lock(self, ctx):
        await self._set(ctx, [ctx.channel], send_messages=False)
        await ok(ctx, "Salon verrouillé 🔒")

    @commands.hybrid_command(name="unlock", description="Restaurer l'envoi de messages")
    @perm(manage_channels=True)
    async def unlock(self, ctx):
        await self._set(ctx, [ctx.channel], send_messages=None)
        await ok(ctx, "Salon déverrouillé 🔓")

    @commands.hybrid_command(name="hide", description="Masquer ce salon à @everyone")
    @perm(manage_channels=True)
    async def hide(self, ctx):
        await self._set(ctx, [ctx.channel], view_channel=False)
        await ok(ctx, "Salon masqué 🙈")

    @commands.hybrid_command(name="unhide", description="Rendre ce salon visible")
    @perm(manage_channels=True)
    async def unhide(self, ctx):
        await self._set(ctx, [ctx.channel], view_channel=None)
        await ok(ctx, "Salon visible 👀")

    @commands.hybrid_command(name="lockall", description="Verrouiller tous les salons textuels")
    @perm(administrator=True)
    async def lockall(self, ctx):
        await ctx.defer()
        n = await self._set(ctx, ctx.guild.text_channels, send_messages=False)
        await ok(ctx, f"{n} salon(s) verrouillé(s).")

    @commands.hybrid_command(name="unlockall", description="Déverrouiller tous les salons textuels")
    @perm(administrator=True)
    async def unlockall(self, ctx):
        await ctx.defer()
        n = await self._set(ctx, ctx.guild.text_channels, send_messages=None)
        await ok(ctx, f"{n} salon(s) déverrouillé(s).")

    @commands.hybrid_command(name="hideall", description="Cacher tous les salons textuels")
    @perm(administrator=True)
    async def hideall(self, ctx):
        await ctx.defer()
        n = await self._set(ctx, ctx.guild.text_channels, view_channel=False)
        await ok(ctx, f"{n} salon(s) masqué(s).")

    @commands.hybrid_command(name="unhideall", description="Rendre visibles tous les salons textuels")
    @perm(administrator=True)
    async def unhideall(self, ctx):
        await ctx.defer()
        n = await self._set(ctx, ctx.guild.text_channels, view_channel=None)
        await ok(ctx, f"{n} salon(s) visible(s).")

    @commands.hybrid_command(name="renew", description="Cloner ce salon et supprimer l'original")
    @perm(manage_channels=True)
    async def renew(self, ctx):
        ch = ctx.channel
        pos = ch.position
        new = await ch.clone(reason=f"renew par {ctx.author}")
        await ch.delete()
        await new.edit(position=pos)
        await new.send(embed=emb("♻️ Salon recréé."))

    @commands.hybrid_command(name="nick", description="Modifier ou retirer le surnom d'un membre")
    @perm(manage_nicknames=True)
    async def nick(self, ctx, member: discord.Member, *, pseudo: Optional[str] = None):
        if not await can_act(ctx, member):
            return await ko(ctx, "Hiérarchie insuffisante.")
        await member.edit(nick=pseudo)
        await ok(ctx, f"Surnom de {member.mention} : **{pseudo or 'réinitialisé'}**")

    @commands.hybrid_command(name="mv", description="Déplacer un membre vers un autre vocal")
    @perm(move_members=True)
    async def mv(self, ctx, member: discord.Member, salon: discord.VoiceChannel):
        if not member.voice:
            return await ko(ctx, "Ce membre n'est pas en vocal.")
        await member.move_to(salon)
        await ok(ctx, f"{member.mention} déplacé vers {salon.mention}.")

    @commands.hybrid_command(name="pv", description="Fermer/rouvrir ton vocal aux nouvelles connexions")
    @perm(manage_channels=True)
    async def pv(self, ctx):
        if not ctx.author.voice or not ctx.author.voice.channel:
            return await ko(ctx, "Tu dois être dans un salon vocal.")
        ch = ctx.author.voice.channel
        ow = ch.overwrites_for(ctx.guild.default_role)
        closed = ow.connect is False
        ow.connect = None if closed else False
        await ch.set_permissions(ctx.guild.default_role, overwrite=ow)
        await ok(ctx, f"{ch.mention} : {'rouvert 🔓' if closed else 'fermé aux nouvelles connexions 🔒'}")

    @commands.hybrid_command(name="voicemove", description="Répartir les membres d'une catégorie de vocaux")
    @perm(move_members=True)
    async def voicemove(self, ctx, categorie: discord.CategoryChannel):
        vcs = categorie.voice_channels
        if not vcs:
            return await ko(ctx, "Aucun vocal dans cette catégorie.")
        members = [m for vc in vcs for m in vc.members]
        await ctx.defer()
        for i, m in enumerate(members):
            try:
                await m.move_to(vcs[i % len(vcs)])
            except discord.HTTPException:
                pass
        await ok(ctx, f"{len(members)} membre(s) répartis.")


# ════════════════════════════════════════════════════════════
#  COG : RÔLES
# ════════════════════════════════════════════════════════════
class Roles(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _check(self, ctx, member, role):
        """Hiérarchie + règles limitrole. Retourne la liste des rôles liés, ou None si refusé."""
        if role >= ctx.guild.me.top_role:
            await ko(ctx, "Ce rôle est au-dessus de moi.")
            return None
        owner = await is_owner(ctx)
        if not owner and role >= ctx.author.top_role:
            await ko(ctx, "Ce rôle est au-dessus de toi.")
            return None
        if not await can_act(ctx, member):
            await ko(ctx, "Hiérarchie insuffisante.")
            return None
        rules = db_all("SELECT type, other_id FROM role_rules WHERE guild_id=? AND role_id=?", (ctx.guild.id, role.id))
        if not owner and any(r["type"] == "block" for r in rules):
            await ko(ctx, "Ce rôle est **bloqué** (limitrole).")
            return None
        allow = [r["other_id"] for r in rules if r["type"] == "allow"]
        if allow and not owner and not any(x.id in allow for x in ctx.author.roles):
            await ko(ctx, "Tu n'as pas le rôle requis pour attribuer celui-ci (limitrole).")
            return None
        linked = [ctx.guild.get_role(r["other_id"]) for r in rules if r["type"] == "link"]
        return [x for x in linked if x]

    @commands.hybrid_command(name="addrole", description="Attribuer un rôle à un membre")
    @perm(manage_roles=True)
    async def addrole(self, ctx, member: discord.Member, role: discord.Role):
        linked = await self._check(ctx, member, role)
        if linked is None:
            return
        await member.add_roles(role, *linked, reason=f"par {ctx.author}")
        extra = f" (+ {' '.join(x.mention for x in linked)})" if linked else ""
        await ok(ctx, f"{role.mention} ajouté à {member.mention}{extra}.")

    @commands.hybrid_command(name="removerole", description="Retirer un rôle à un membre")
    @perm(manage_roles=True)
    async def removerole(self, ctx, member: discord.Member, role: discord.Role):
        linked = await self._check(ctx, member, role)
        if linked is None:
            return
        await member.remove_roles(role, *linked, reason=f"par {ctx.author}")
        await ok(ctx, f"{role.mention} retiré à {member.mention}.")

    # ── rangs ──
    @commands.hybrid_command(name="rank", description="Attribuer un rôle de rang à un membre")
    @perm(manage_roles=True)
    async def rank(self, ctx, member: discord.Member, role: discord.Role):
        rr = rank_roles(ctx.guild)
        if not rr:
            return await ko(ctx, "Plage de rangs non configurée (`setup rangs`).")
        if role not in rr:
            return await ko(ctx, "Ce rôle n'est pas dans la plage de rangs.")
        if db_one("SELECT 1 FROM blrank WHERE guild_id=? AND user_id=?", (ctx.guild.id, member.id)):
            return await ko(ctx, "Ce membre est en **blacklist de rang**.")
        linked = await self._check(ctx, member, role)
        if linked is None:
            return
        old = [r for r in rr if r in member.roles and r != role]
        if old:
            await member.remove_roles(*old, reason=f"rank par {ctx.author}")
        await member.add_roles(role, *linked, reason=f"rank par {ctx.author}")
        await ok(ctx, f"{member.mention} a maintenant le rang {role.mention}.")

    @commands.hybrid_command(name="derank", description="Retirer tous les rôles de la plage de rangs")
    @perm(manage_roles=True)
    async def derank(self, ctx, member: discord.Member):
        if not await can_act(ctx, member):
            return await ko(ctx, "Hiérarchie insuffisante.")
        rr = [r for r in rank_roles(ctx.guild) if r in member.roles and r < ctx.guild.me.top_role]
        if not rr:
            return await ko(ctx, "Aucun rôle de rang à retirer.")
        await member.remove_roles(*rr, reason=f"derank par {ctx.author}")
        await ok(ctx, f"{len(rr)} rôle(s) retiré(s) à {member.mention}.")
        await send_log(self.bot, ctx.guild, log_embed("Derank", ctx, member))

    @commands.hybrid_group(name="blrank", description="Blacklist de rang", invoke_without_command=True)
    @perm(manage_roles=True)
    async def blrank(self, ctx):
        await ko(ctx, "Sous-commandes : `add`, `list`, `remove`.")

    @blrank.command(name="add", description="Derank et blacklist de rang")
    @perm(manage_roles=True)
    async def blrank_add(self, ctx, member: discord.Member, *, reason: str = "Aucun motif"):
        if not await can_act(ctx, member):
            return await ko(ctx, "Hiérarchie insuffisante.")
        db_run("INSERT OR REPLACE INTO blrank VALUES(?,?,?,?,?)",
               (ctx.guild.id, member.id, reason, ctx.author.id, time.time()))
        rr = [r for r in rank_roles(ctx.guild) if r in member.roles and r < ctx.guild.me.top_role]
        if rr:
            await member.remove_roles(*rr, reason="blrank")
        await ok(ctx, f"{member.mention} derank et en blacklist de rang — {reason}")
        await send_log(self.bot, ctx.guild, log_embed("Blrank", ctx, member, reason))

    @blrank.command(name="list", description="Lister les blacklists de rang")
    @perm(manage_roles=True)
    async def blrank_list(self, ctx):
        rows = db_all("SELECT * FROM blrank WHERE guild_id=? ORDER BY created DESC LIMIT 30", (ctx.guild.id,))
        if not rows:
            return await ctx.send(embed=emb("Aucune blacklist de rang."))
        await ctx.send(embed=emb("\n".join(f"<@{r['user_id']}> — {r['reason']}" for r in rows)[:4000],
                                 "Blacklists de rang"))

    @blrank.command(name="remove", description="Retirer la blacklist de rang")
    @perm(manage_roles=True)
    async def blrank_remove(self, ctx, user: discord.User):
        db_run("DELETE FROM blrank WHERE guild_id=? AND user_id=?", (ctx.guild.id, user.id))
        await ok(ctx, f"Blacklist de rang retirée pour **{user}**.")

    # ── limitrole ──
    @commands.hybrid_group(name="limitrole", description="Bloquer, restreindre et lier des rôles",
                           invoke_without_command=True)
    @owner_only()
    async def limitrole(self, ctx):
        await self.limitrole_liste(ctx)

    @limitrole.command(name="bloquer", description="Interdire l'attribution d'un rôle via le bot")
    @owner_only()
    async def limitrole_bloquer(self, ctx, role: discord.Role):
        db_run("INSERT OR IGNORE INTO role_rules VALUES(?,?,?,0)", (ctx.guild.id, role.id, "block"))
        await ok(ctx, f"{role.mention} est **bloqué**.")

    @limitrole.command(name="autoriser", description="Seul ce rôle autorisé pourra attribuer le rôle")
    @owner_only()
    async def limitrole_autoriser(self, ctx, role: discord.Role, autorise: discord.Role):
        db_run("INSERT OR IGNORE INTO role_rules VALUES(?,?,?,?)", (ctx.guild.id, role.id, "allow", autorise.id))
        await ok(ctx, f"{autorise.mention} peut attribuer {role.mention}.")

    @limitrole.command(name="lier", description="Donner un rôle lié en même temps que le rôle")
    @owner_only()
    async def limitrole_lier(self, ctx, role: discord.Role, lie: discord.Role):
        db_run("INSERT OR IGNORE INTO role_rules VALUES(?,?,?,?)", (ctx.guild.id, role.id, "link", lie.id))
        await ok(ctx, f"{role.mention} donnera aussi {lie.mention}.")

    @limitrole.command(name="retirer", description="Supprimer toutes les règles d'un rôle")
    @owner_only()
    async def limitrole_retirer(self, ctx, role: discord.Role):
        db_run("DELETE FROM role_rules WHERE guild_id=? AND role_id=?", (ctx.guild.id, role.id))
        await ok(ctx, f"Règles supprimées pour {role.mention}.")

    @limitrole.command(name="liste", description="Voir les règles de rôles")
    @owner_only()
    async def limitrole_liste(self, ctx):
        rows = db_all("SELECT * FROM role_rules WHERE guild_id=? ORDER BY role_id", (ctx.guild.id,))
        if not rows:
            return await ctx.send(embed=emb("Aucune règle."))
        lab = {"block": "🚫 bloqué", "allow": "🔑 autorisé à", "link": "🔗 lié à"}
        txt = "\n".join(
            f"<@&{r['role_id']}> — {lab[r['type']]}" + (f" <@&{r['other_id']}>" if r["other_id"] else "")
            for r in rows)
        await ctx.send(embed=emb(txt[:4000], "Règles de rôles"))


# ════════════════════════════════════════════════════════════
#  COG : INFOS
# ════════════════════════════════════════════════════════════
class Infos(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="snipe", description="Dernier message supprimé du salon")
    @perm(manage_messages=True)
    async def snipe(self, ctx):
        s = self.bot.snipes.get(ctx.channel.id)
        if not s:
            return await ko(ctx, "Rien à sniper.")
        author, content, atts, t = s
        e = emb(content or "*(pas de texte)*", color=COLOR)
        e.set_author(name=str(author), icon_url=author.display_avatar.url)
        e.set_footer(text=f"Supprimé il y a {int(time.time() - t)}s")
        if atts:
            e.add_field(name="Pièces jointes", value="\n".join(a.filename for a in atts))
        await ctx.send(embed=e)

    @commands.hybrid_command(name="vc", description="Statistiques vocales et serveur")
    async def vc(self, ctx):
        g = ctx.guild
        in_voice = sum(len(v.members) for v in g.voice_channels)
        online = sum(1 for m in g.members if m.status != discord.Status.offline)
        custom = get_setting(g.id, "vc_msg")
        if custom:
            txt = (custom.replace("{serveur}", g.name).replace("{membres}", str(g.member_count))
                   .replace("{online}", str(online)).replace("{vocal}", str(in_voice))
                   .replace("{boosts}", str(g.premium_subscription_count)))
            return await ctx.send(embed=emb(txt))
        e = emb(title=f"Stats de {g.name}")
        e.add_field(name="Membres", value=str(g.member_count))
        e.add_field(name="En ligne", value=str(online))
        e.add_field(name="En vocal", value=str(in_voice))
        e.add_field(name="Boosts", value=str(g.premium_subscription_count))
        await ctx.send(embed=e)

    @commands.hybrid_command(name="find", description="Trouver un membre dans un vocal")
    async def find(self, ctx, member: discord.Member):
        if member.voice and member.voice.channel:
            await ctx.send(embed=emb(f"{member.mention} est dans {member.voice.channel.mention}"))
        else:
            await ko(ctx, "Ce membre n'est dans aucun vocal.")

    @commands.hybrid_command(name="pic", description="Avatar d'un membre en haute résolution")
    async def pic(self, ctx, user: Optional[discord.User] = None):
        user = user or ctx.author
        e = emb(title=f"Avatar de {user}")
        e.set_image(url=user.display_avatar.replace(size=4096).url)
        await ctx.send(embed=e)

    @commands.hybrid_command(name="banner", description="Bannière de profil d'un membre")
    async def banner(self, ctx, user: Optional[discord.User] = None):
        user = await self.bot.fetch_user((user or ctx.author).id)
        if not user.banner:
            return await ko(ctx, "Cet utilisateur n'a pas de bannière.")
        e = emb(title=f"Bannière de {user}")
        e.set_image(url=user.banner.url)
        await ctx.send(embed=e)

    @commands.hybrid_command(name="user", description="Infos sur un membre")
    async def user(self, ctx, member: Optional[discord.Member] = None):
        m = member or ctx.author
        e = emb(title=str(m))
        e.set_thumbnail(url=m.display_avatar.url)
        e.add_field(name="ID", value=str(m.id))
        e.add_field(name="Compte créé", value=tsd(m.created_at.timestamp(), "D"))
        e.add_field(name="A rejoint", value=tsd(m.joined_at.timestamp(), "D") if m.joined_at else "?")
        roles = [r.mention for r in reversed(m.roles) if r != ctx.guild.default_role]
        e.add_field(name=f"Rôles ({len(roles)})", value=" ".join(roles)[:1000] or "Aucun", inline=False)
        await ctx.send(embed=e)

    @commands.hybrid_group(name="serveur", description="Infos du serveur", invoke_without_command=True)
    async def serveur(self, ctx):
        await ko(ctx, "Sous-commandes : `info`, `pic`, `banner`.")

    @serveur.command(name="info", description="Fiche complète du serveur")
    async def serveur_info(self, ctx):
        g = ctx.guild
        e = emb(g.description, g.name)
        if g.icon:
            e.set_thumbnail(url=g.icon.url)
        e.add_field(name="Owner", value=f"<@{g.owner_id}>")
        e.add_field(name="Membres", value=str(g.member_count))
        e.add_field(name="Salons", value=f"{len(g.text_channels)} texte / {len(g.voice_channels)} vocal")
        e.add_field(name="Rôles", value=str(len(g.roles)))
        e.add_field(name="Boost", value=f"Niv. {g.premium_tier} ({g.premium_subscription_count})")
        e.add_field(name="Créé", value=tsd(g.created_at.timestamp(), "D"))
        await ctx.send(embed=e)

    @serveur.command(name="pic", description="Icône du serveur")
    async def serveur_pic(self, ctx):
        if not ctx.guild.icon:
            return await ko(ctx, "Pas d'icône.")
        e = emb(title=ctx.guild.name)
        e.set_image(url=ctx.guild.icon.replace(size=4096).url)
        await ctx.send(embed=e)

    @serveur.command(name="banner", description="Bannière du serveur")
    async def serveur_banner(self, ctx):
        if not ctx.guild.banner:
            return await ko(ctx, "Pas de bannière.")
        e = emb(title=ctx.guild.name)
        e.set_image(url=ctx.guild.banner.url)
        await ctx.send(embed=e)

    @commands.hybrid_command(name="serveurs", description="Serveurs de cette instance du bot")
    @owner_only()
    async def serveurs(self, ctx):
        txt = "\n".join(f"**{g.name}** — {g.member_count} membres (`{g.id}`)" for g in self.bot.guilds)
        await ctx.send(embed=emb(txt[:4000], f"{len(self.bot.guilds)} serveur(s)"))

    @commands.hybrid_command(name="rolemembers", description="Membres portant un rôle")
    async def rolemembers(self, ctx, role: discord.Role):
        ms = role.members
        txt = "\n".join(m.mention for m in ms[:60]) or "Aucun membre."
        await ctx.send(embed=emb(txt, f"{role.name} — {len(ms)} membre(s)"))

    @commands.hybrid_group(name="create", description="Créer des éléments", invoke_without_command=True)
    @perm(manage_guild=True)
    async def create(self, ctx):
        await ko(ctx, "Sous-commandes : `emoji`, `salon`, `role`.")

    @create.command(name="emoji", description="Créer un emoji à partir d'une image")
    @perm(manage_expressions=True)
    async def create_emoji(self, ctx, nom: str, image: discord.Attachment):
        e = await ctx.guild.create_custom_emoji(name=nom, image=await image.read())
        await ok(ctx, f"Emoji créé : {e}")

    @create.command(name="salon", description="Créer un salon texte")
    @perm(manage_channels=True)
    async def create_salon(self, ctx, nom: str):
        ch = await ctx.guild.create_text_channel(nom)
        await ok(ctx, f"Salon créé : {ch.mention}")

    @create.command(name="role", description="Créer un rôle")
    @perm(manage_roles=True)
    async def create_role(self, ctx, nom: str, couleur: Optional[discord.Colour] = None):
        r = await ctx.guild.create_role(name=nom, colour=couleur or discord.Colour.default())
        await ok(ctx, f"Rôle créé : {r.mention}")


# ════════════════════════════════════════════════════════════
#  COG : CONFIGURATION
# ════════════════════════════════════════════════════════════
class Config(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_group(name="setup", description="Configuration du serveur", invoke_without_command=True)
    @owner_only()
    async def setup_group(self, ctx):
        await ko(ctx, "Sous-commandes : `status`, `logs`, `roles`, `absence`, `tribunal`, `rangs`, `limites`.")

    @setup_group.command(name="logs", description="Salon des logs de modération")
    @owner_only()
    async def setup_logs(self, ctx, salon: discord.TextChannel):
        set_setting(ctx.guild.id, "logs", salon.id)
        await ok(ctx, f"Logs de modération → {salon.mention}")

    @setup_group.command(name="roles", description="Rôle staff de modération")
    @owner_only()
    async def setup_roles(self, ctx, role: discord.Role):
        set_setting(ctx.guild.id, "staff_role", role.id)
        await ok(ctx, f"Rôle staff → {role.mention}")

    @setup_group.command(name="absence", description="Salon des demandes d'absence + rôle valideur")
    @owner_only()
    async def setup_absence(self, ctx, salon: discord.TextChannel, valideur: discord.Role):
        set_setting(ctx.guild.id, "abs_channel", salon.id)
        set_setting(ctx.guild.id, "abs_role", valideur.id)
        await ok(ctx, f"Absences → {salon.mention} (valideur : {valideur.mention})")

    @setup_group.command(name="rangs", description="Plage de rôles de rang (du plus haut au plus bas)")
    @owner_only()
    async def setup_rangs(self, ctx, haut: discord.Role, bas: discord.Role):
        set_setting(ctx.guild.id, "rank_top", haut.id)
        set_setting(ctx.guild.id, "rank_bottom", bas.id)
        n = len(rank_roles(ctx.guild))
        await ok(ctx, f"Plage de rangs : {haut.mention} → {bas.mention} ({n} rôle(s)).")

    @setup_group.command(name="tribunal", description="Salon des demandes du Tribunal + rôle juge")
    @owner_only()
    async def setup_tribunal(self, ctx, salon: discord.TextChannel, juge: discord.Role):
        set_setting(ctx.guild.id, "trib_channel", salon.id)
        set_setting(ctx.guild.id, "trib_role", juge.id)
        await ok(ctx, f"Tribunal → {salon.mention} (juges : {juge.mention})")

    @setup_group.command(name="limites", description="Avertissements avant auto-mute (0 = désactivé)")
    @owner_only()
    async def setup_limites(self, ctx, avertissements: commands.Range[int, 0, 50],
                            minutes_mute: commands.Range[int, 1, 40320] = 60):
        set_setting(ctx.guild.id, "warn_limit", avertissements)
        set_setting(ctx.guild.id, "auto_mute_min", minutes_mute)
        await ok(ctx, f"Auto-mute : {avertissements or 'désactivé'} avert. → {minutes_mute} min")

    @setup_group.command(name="status", description="État de la configuration")
    @owner_only()
    async def setup_status(self, ctx):
        g = ctx.guild

        def ch(k):
            v = get_setting(g.id, k)
            return f"<#{v}>" if v else "❌ non défini"

        def ro(k):
            v = get_setting(g.id, k)
            return f"<@&{v}>" if v else "❌ non défini"

        txt = (f"**Préfixe** : `{get_setting(g.id, 'prefix', '=')}`\n"
               f"**Logs** : {ch('logs')}\n**Rôle staff** : {ro('staff_role')}\n"
               f"**Tribunal** : {ch('trib_channel')} / juges {ro('trib_role')}\n"
               f"**Absences** : {ch('abs_channel')} / valideur {ro('abs_role')}\n"
               f"**Auto-mute** : {get_setting(g.id, 'warn_limit', 0)} avert. → {get_setting(g.id, 'auto_mute_min', 60)} min\n"
               f"**Owners** : {db_one('SELECT COUNT(*) c FROM owners WHERE guild_id=?', (g.id,))['c']}\n"
               f"**Protégés** : {db_one('SELECT COUNT(*) c FROM protected WHERE guild_id=?', (g.id,))['c']}\n"
               f"**Blacklistés** : {db_one('SELECT COUNT(*) c FROM blacklist WHERE guild_id=?', (g.id,))['c']}")
        await ctx.send(embed=emb(txt, "Configuration"))

    @commands.hybrid_group(name="owner", description="Gestion des owners", invoke_without_command=True)
    @owner_only()
    async def owner(self, ctx):
        await ko(ctx, "Sous-commandes : `add`, `list`, `remove`.")

    @owner.command(name="add", description="Ajouter un owner")
    @owner_only()
    async def owner_add(self, ctx, user: discord.User):
        db_run("INSERT OR IGNORE INTO owners VALUES(?,?)", (ctx.guild.id, user.id))
        await ok(ctx, f"**{user}** est maintenant owner.")

    @owner.command(name="list", description="Liste des owners")
    @owner_only()
    async def owner_list(self, ctx):
        rows = db_all("SELECT user_id FROM owners WHERE guild_id=?", (ctx.guild.id,))
        txt = f"👑 <@{ctx.guild.owner_id}> (propriétaire du serveur)\n" + "\n".join(f"<@{r['user_id']}>" for r in rows)
        await ctx.send(embed=emb(txt, "Owners"))

    @owner.command(name="remove", description="Retirer un owner")
    @owner_only()
    async def owner_remove(self, ctx, user: discord.User):
        db_run("DELETE FROM owners WHERE guild_id=? AND user_id=?", (ctx.guild.id, user.id))
        await ok(ctx, f"**{user}** n'est plus owner.")

    @commands.hybrid_command(name="customlogs", description="Personnaliser salon et titre d'un type de log")
    @owner_only()
    async def customlogs(self, ctx, type: Literal["avertissement", "tempmute", "unmute", "ban", "unban",
                                                  "blacklist", "derank", "blrank"],
                         salon: Optional[discord.TextChannel] = None, *, titre: Optional[str] = None):
        if salon:
            set_setting(ctx.guild.id, f"logch:{type}", salon.id)
        if titre:
            set_setting(ctx.guild.id, f"logtitle:{type}", titre[:100])
        if not salon and not titre:
            db_run("DELETE FROM settings WHERE guild_id=? AND key IN (?,?)",
                   (ctx.guild.id, f"logch:{type}", f"logtitle:{type}"))
            return await ok(ctx, f"Logs `{type}` remis par défaut.")
        await ok(ctx, f"Logs `{type}` → {salon.mention if salon else 'salon inchangé'} / titre : "
                      f"**{titre or 'inchangé'}**")

    @commands.hybrid_command(name="customvc", description="Personnaliser le message de /vc (vide = reset)")
    @owner_only()
    async def customvc(self, ctx, *, message: Optional[str] = None):
        if not message:
            db_run("DELETE FROM settings WHERE guild_id=? AND key='vc_msg'", (ctx.guild.id,))
            return await ok(ctx, "Message /vc remis par défaut.")
        set_setting(ctx.guild.id, "vc_msg", message[:1500])
        await ok(ctx, "Message /vc personnalisé. Variables : `{serveur}` `{membres}` `{online}` `{vocal}` `{boosts}`")

    @commands.hybrid_group(name="profil", description="Profil global du bot", invoke_without_command=True)
    async def profil(self, ctx):
        self._bot_owner(ctx)
        u = self.bot.user
        e = emb(title="Profil du bot")
        e.set_thumbnail(url=u.display_avatar.url)
        e.add_field(name="Nom", value=u.name)
        e.add_field(name="Statut", value=get_setting(0, "status", "=help"))
        e.add_field(name="Serveurs", value=str(len(self.bot.guilds)))
        e.set_footer(text="profil nom / profil statut / profil avatar")
        await ctx.send(embed=e)

    @staticmethod
    def _bot_owner(ctx):
        if not OWNER_ID or ctx.author.id != OWNER_ID:
            raise commands.CheckFailure("Réservé au propriétaire du bot (variable OWNER_ID).")

    @profil.command(name="nom", description="Changer le nom du bot")
    async def profil_nom(self, ctx, *, nom: str):
        self._bot_owner(ctx)
        await self.bot.user.edit(username=nom[:32])
        await ok(ctx, f"Nom changé : **{nom[:32]}**")

    @profil.command(name="statut", description="Changer le statut du bot")
    async def profil_statut(self, ctx, *, texte: str):
        self._bot_owner(ctx)
        set_setting(0, "status", texte[:100])
        await self.bot.change_presence(activity=discord.Game(name=texte[:100]))
        await ok(ctx, f"Statut changé : **{texte[:100]}**")

    @profil.command(name="avatar", description="Changer l'avatar du bot")
    async def profil_avatar(self, ctx, image: discord.Attachment):
        self._bot_owner(ctx)
        await self.bot.user.edit(avatar=await image.read())
        await ok(ctx, "Avatar changé.")

    @commands.hybrid_command(name="prefix", description="Changer le préfixe des commandes")
    @owner_only()
    async def prefix(self, ctx, nouveau: str):
        if len(nouveau) > 3:
            return await ko(ctx, "3 caractères maximum.")
        set_setting(ctx.guild.id, "prefix", nouveau)
        await ok(ctx, f"Nouveau préfixe : `{nouveau}`")


# ════════════════════════════════════════════════════════════
#  COG : ABSENCES
# ════════════════════════════════════════════════════════════
class AbsBtn(discord.ui.DynamicItem[discord.ui.Button], template=r"abs:(?P<id>\d+):(?P<act>ok|no)"):
    def __init__(self, abs_id, act):
        super().__init__(discord.ui.Button(
            label="Valider" if act == "ok" else "Refuser",
            style=discord.ButtonStyle.success if act == "ok" else discord.ButtonStyle.danger,
            custom_id=f"abs:{abs_id}:{act}"))
        self.abs_id, self.act = abs_id, act

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["id"]), match["act"])

    async def callback(self, i):
        rid = get_setting(i.guild.id, "abs_role")
        if not (i.user.guild_permissions.administrator or (rid and any(r.id == int(rid) for r in i.user.roles))):
            return await i.response.send_message("Tu n'es pas valideur.", ephemeral=True)
        status = "accepted" if self.act == "ok" else "refused"
        db_run("UPDATE absences SET status=?, decided_by=? WHERE id=?", (status, i.user.id, self.abs_id))
        e = i.message.embeds[0]
        e.color = 0x57F287 if status == "accepted" else 0xED4245
        e.add_field(name="Décision", value=f"{'✅ Acceptée' if status == 'accepted' else '❌ Refusée'} par {i.user.mention}")
        await i.response.edit_message(embed=e, view=None)


def abs_view(abs_id):
    v = discord.ui.View(timeout=None)
    v.add_item(AbsBtn(abs_id, "ok"))
    v.add_item(AbsBtn(abs_id, "no"))
    return v


class Absences(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_group(name="absence", description="Gérer tes absences", invoke_without_command=True)
    async def absence(self, ctx):
        await ko(ctx, "Sous-commandes : `set`, `reset`.")

    @absence.command(name="set", description="Déposer une demande d'absence (ex: 3d)")
    async def absence_set(self, ctx, duree: str, *, raison: str = "Non précisée"):
        cid, rid = get_setting(ctx.guild.id, "abs_channel"), get_setting(ctx.guild.id, "abs_role")
        if not cid or not rid:
            return await ko(ctx, "Absences non configurées (`setup absence`).")
        d = parse_duration(duree)
        if not d:
            return await ko(ctx, "Durée invalide. Exemples : `12h`, `3d`.")
        ends = time.time() + d.total_seconds()
        aid = db_run("INSERT INTO absences(guild_id,user_id,reason,ends,created) VALUES(?,?,?,?,?)",
                     (ctx.guild.id, ctx.author.id, raison, ends, time.time()))
        e = emb(title=f"Demande d'absence #{aid}")
        e.add_field(name="Membre", value=ctx.author.mention)
        e.add_field(name="Jusqu'au", value=tsd(ends, "F"))
        e.add_field(name="Raison", value=raison, inline=False)
        ch = ctx.guild.get_channel(int(cid))
        await ch.send(content=f"<@&{rid}>", embed=e, view=abs_view(aid))
        await ok(ctx, f"Demande **#{aid}** envoyée dans {ch.mention}.")

    @absence.command(name="reset", description="Annuler ton absence en cours")
    async def absence_reset(self, ctx):
        db_run("UPDATE absences SET status='cancelled' WHERE guild_id=? AND user_id=? AND status IN ('pending','accepted')",
               (ctx.guild.id, ctx.author.id))
        await ok(ctx, "Absence annulée.")

    @commands.hybrid_command(name="absences", description="Consulter les demandes d'absence")
    async def absences(self, ctx):
        rows = db_all("SELECT * FROM absences WHERE guild_id=? ORDER BY id DESC LIMIT 15", (ctx.guild.id,))
        if not rows:
            return await ctx.send(embed=emb("Aucune demande d'absence."))
        icon = {"pending": "⏳", "accepted": "✅", "refused": "❌", "cancelled": "🚫"}
        txt = "\n".join(f"{icon.get(r['status'], '•')} **#{r['id']}** <@{r['user_id']}> → {tsd(r['ends'], 'd')} — {r['reason']}"
                        for r in rows)
        await ctx.send(embed=emb(txt[:4000], "Absences"))


# ════════════════════════════════════════════════════════════
#  COG : TRIBUNAL
# ════════════════════════════════════════════════════════════
class TribBtn(discord.ui.DynamicItem[discord.ui.Button], template=r"trib:(?P<id>\d+):(?P<act>ban|no)"):
    def __init__(self, tid, act):
        super().__init__(discord.ui.Button(
            label="Bannir" if act == "ban" else "Rejeter",
            style=discord.ButtonStyle.danger if act == "ban" else discord.ButtonStyle.secondary,
            custom_id=f"trib:{tid}:{act}"))
        self.tid, self.act = tid, act

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["id"]), match["act"])

    async def callback(self, i):
        g = i.guild
        rid = get_setting(g.id, "trib_role")
        is_judge = i.user.guild_permissions.administrator or (rid and any(r.id == int(rid) for r in i.user.roles))
        if not is_judge:
            return await i.response.send_message("Seuls les juges peuvent trancher.", ephemeral=True)
        r = db_one("SELECT * FROM tribunal WHERE id=?", (self.tid,))
        if not r or r["status"] != "pending":
            return await i.response.send_message("Demande déjà traitée.", ephemeral=True)
        e = i.message.embeds[0]
        if self.act == "ban":
            if db_one("SELECT 1 FROM protected WHERE guild_id=? AND user_id=?", (g.id, r["target_id"])):
                return await i.response.send_message("Cet utilisateur est protégé.", ephemeral=True)
            try:
                await g.ban(discord.Object(r["target_id"]), reason=f"Tribunal #{self.tid} : {r['reason']}")
            except discord.HTTPException:
                return await i.response.send_message("Impossible de bannir (hiérarchie ?).", ephemeral=True)
            db_run("INSERT INTO sanctions(guild_id,user_id,mod_id,type,reason,created) VALUES(?,?,?,?,?,?)",
                   (g.id, r["target_id"], i.user.id, "ban", f"Tribunal : {r['reason']}", time.time()))
            status, label, color = "banned", "🔨 Banni", 0xED4245
        else:
            status, label, color = "rejected", "✋ Rejetée", 0x95A5A6
        db_run("UPDATE tribunal SET status=?, decided_by=? WHERE id=?", (status, i.user.id, self.tid))
        e.color = color
        e.add_field(name="Verdict", value=f"{label} par {i.user.mention}", inline=False)
        await i.response.edit_message(embed=e, view=None)


def trib_view(tid):
    v = discord.ui.View(timeout=None)
    v.add_item(TribBtn(tid, "ban"))
    v.add_item(TribBtn(tid, "no"))
    return v


class Tribunal(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="tribunal", description="Soumettre une demande de bannissement au Tribunal")
    async def tribunal(self, ctx, user: discord.User, *, raison: str):
        cid = get_setting(ctx.guild.id, "trib_channel")
        if not cid:
            return await ko(ctx, "Tribunal non configuré (`setup tribunal`).")
        if db_one("SELECT 1 FROM protected WHERE guild_id=? AND user_id=?", (ctx.guild.id, user.id)):
            return await ko(ctx, "Cet utilisateur est protégé.")
        if user.id == ctx.author.id or user.bot:
            return await ko(ctx, "Cible invalide.")
        tid = db_run("INSERT INTO tribunal(guild_id,target_id,requester_id,reason,created) VALUES(?,?,?,?,?)",
                     (ctx.guild.id, user.id, ctx.author.id, raison, time.time()))
        e = emb(title=f"Demande de bannissement #{tid}", color=0xFEE75C)
        e.add_field(name="Accusé", value=f"{user.mention} (`{user.id}`)")
        e.add_field(name="Demandeur", value=ctx.author.mention)
        e.add_field(name="Motif", value=raison[:1000], inline=False)
        ch = ctx.guild.get_channel(int(cid))
        rid = get_setting(ctx.guild.id, "trib_role")
        await ch.send(content=f"<@&{rid}>" if rid else None, embed=e, view=trib_view(tid))
        await ok(ctx, f"Demande **#{tid}** envoyée au Tribunal.")


# ════════════════════════════════════════════════════════════
#  COG : ACCÈS (niveaux et permissions)
# ════════════════════════════════════════════════════════════
class Acces(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_group(name="acces", description="Niveaux d'accès et permissions", invoke_without_command=True)
    @owner_only()
    async def acces(self, ctx):
        await self.panneau(ctx)

    async def panneau(self, ctx):
        roles = db_all("SELECT * FROM access_roles WHERE guild_id=? ORDER BY level DESC", (ctx.guild.id,))
        cmds = db_all("SELECT * FROM access WHERE guild_id=? ORDER BY level DESC, command", (ctx.guild.id,))
        t1 = "\n".join(f"Niveau **{r['level']}** → <@&{r['role_id']}>" for r in roles) or "Aucun rôle associé."
        t2 = "\n".join(f"`{c['command']}` → {'🌍 public' if c['level'] < 0 else 'niveau ' + str(c['level'])}"
                       for c in cmds) or "Aucune règle (permissions Discord par défaut)."
        e = emb(title="Niveaux et permissions")
        e.add_field(name="Rôles", value=t1[:1000], inline=False)
        e.add_field(name="Commandes", value=t2[:1000], inline=False)
        e.set_footer(text="Niveau 0 = aucun accès spécial. Les owners passent partout.")
        await ctx.send(embed=e)

    @acces.command(name="panneau", description="Afficher les niveaux et permissions")
    @owner_only()
    async def acces_panneau(self, ctx):
        await self.panneau(ctx)

    @acces.command(name="role", description="Associer un rôle à un niveau (0 = retirer)")
    @owner_only()
    async def acces_role(self, ctx, role: discord.Role, niveau: commands.Range[int, 0, 10]):
        if niveau == 0:
            db_run("DELETE FROM access_roles WHERE guild_id=? AND role_id=?", (ctx.guild.id, role.id))
            return await ok(ctx, f"{role.mention} retiré des niveaux.")
        db_run("INSERT OR REPLACE INTO access_roles VALUES(?,?,?)", (ctx.guild.id, role.id, niveau))
        await ok(ctx, f"{role.mention} → niveau **{niveau}**")

    @acces.command(name="commande", description="Niveau requis pour une commande (0 = règle retirée)")
    @owner_only()
    async def acces_commande(self, ctx, niveau: commands.Range[int, 0, 10], *, commande: str):
        cmd = self.bot.get_command(commande.lower().lstrip("/="))
        if not cmd:
            return await ko(ctx, "Commande introuvable (ex : `bl add`, `ban`).")
        name = cmd.qualified_name
        if niveau == 0:
            db_run("DELETE FROM access WHERE guild_id=? AND command=?", (ctx.guild.id, name))
            return await ok(ctx, f"Règle retirée pour `{name}`.")
        db_run("INSERT OR REPLACE INTO access VALUES(?,?,?)", (ctx.guild.id, name, niveau))
        await ok(ctx, f"`{name}` demande maintenant le niveau **{niveau}**.")

    @acces.command(name="public", description="Autoriser/interdire une commande à tout le monde")
    @owner_only()
    async def acces_public(self, ctx, autoriser: bool, *, commande: str):
        cmd = self.bot.get_command(commande.lower().lstrip("/="))
        if not cmd:
            return await ko(ctx, "Commande introuvable.")
        name = cmd.qualified_name
        if autoriser:
            db_run("INSERT OR REPLACE INTO access VALUES(?,?,?)", (ctx.guild.id, name, -1))
            await ok(ctx, f"`{name}` est maintenant **publique**.")
        else:
            db_run("DELETE FROM access WHERE guild_id=? AND command=? AND level<0", (ctx.guild.id, name))
            await ok(ctx, f"`{name}` n'est plus publique.")


# ════════════════════════════════════════════════════════════
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("❌ Variable d'environnement TOKEN manquante.")
    SanctionsBot().run(TOKEN)
