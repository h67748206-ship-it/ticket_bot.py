"""
Xenity Shop - Bot de gestion de shop Discord v2 (un seul fichier)

UNE SEULE commande à faire : /setup
  -> crée les rôles, catégories, salons, poste le panneau + le catalogue, ajoute tes produits.

Automatismes :
- Panneau de tickets et catalogue mis à jour tout seuls quand les produits/stocks changent
- Le menu de tickets est généré à partir de tes produits (rupture de stock = ticket refusé)
- Auto-claim : le 1er message d'un staff dans un ticket le prend en charge
- Bouton "Livré" : rôle client + stock -1 + DM + demande de vouch
- Ticket livré fermé tout seul après 30 min, ticket inactif fermé après 48h (transcripts dans les logs)
- Vouchs : /vouch, bouton après livraison, top vendeurs, stats, anti-spam
- PAIEMENT LTC AUTO : à l'ouverture d'un ticket le bot envoie l'adresse LTC + le montant exact
  (prix en € converti en LTC, avec un petit suffixe unique par ticket). Il surveille la blockchain,
  détecte le paiement, confirme, donne le rôle client, ping le staff et affiche le bouton
  "Laisser un vouch" -> le vouch est posté dans le salon des vouchs.
  Config : /set_ltc <adresse>   (ou variable d'env LTC_ADDRESS)
  Optionnel : variable d'env LTC_API (par défaut https://litecoinspace.org/api, format Esplora)

Variables d'environnement : TOKEN (obligatoire), PORT (Render le met tout seul)
Requirements : discord.py>=2.3  aiohttp
"""
import io
import os
import random
import re
import sqlite3
import time

import aiohttp
import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands, tasks

TOKEN = os.getenv("TOKEN")
DB = "shop.db"
COLOR = 0x8B5CF6
SHOP_NAME = "Xenity Shop"
VOUCH_COOLDOWN = 12 * 3600      # 1 vouch / 12h / membre
DELIVERED_CLOSE_AFTER = 30 * 60  # fermeture auto 30 min après livraison
INACTIVE_CLOSE_AFTER = 48 * 3600  # fermeture auto après 48h sans message
LTC_API = os.getenv("LTC_API", "https://litecoinspace.org/api").rstrip("/")  # API au format Esplora
COINGECKO = "https://api.coingecko.com/api/v3/simple/price?ids=litecoin&vs_currencies=eur"
PAY_WINDOW = 3 * 3600            # un montant LTC reste surveillé 3h après sa génération
DEFAULT_PRODUCTS = [
    ("Discord Nitro", "Sur demande", "Nitro livré rapidement."),
    ("Numéro virtuel", "Sur demande", "Numéros virtuels pour vérifications."),
    ("Mullvad VPN", "Sur demande", "Clés Mullvad VPN."),
]


# ───────────────────────── Base de données ─────────────────────────
def init_db():
    with sqlite3.connect(DB) as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS config(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS products(
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE,
                price TEXT, stock INTEGER DEFAULT -1, description TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS vouches(
                id INTEGER PRIMARY KEY AUTOINCREMENT, author_id INTEGER, target_id INTEGER,
                stars INTEGER, text TEXT, product TEXT, ts INTEGER);
            CREATE TABLE IF NOT EXISTS orders(
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, channel_id INTEGER,
                product TEXT, status TEXT DEFAULT 'open', staff_id INTEGER, ts INTEGER,
                delivered_ts INTEGER, ltc_amount INTEGER, ltc_eur REAL, ltc_ts INTEGER,
                pay_state INTEGER DEFAULT 0, paid_txid TEXT);
            CREATE TABLE IF NOT EXISTS blacklist(user_id INTEGER PRIMARY KEY, reason TEXT);
            """
        )
        for col in ("delivered_ts INTEGER", "ltc_amount INTEGER", "ltc_eur REAL", "ltc_ts INTEGER",
                    "pay_state INTEGER DEFAULT 0", "paid_txid TEXT"):
            try:  # migration si tu avais une ancienne version
                c.execute(f"ALTER TABLE orders ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass


def q(sql, args=(), one=False):
    with sqlite3.connect(DB) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(sql, args).fetchall()
    if one:
        return rows[0] if rows else None
    return rows


def ex(sql, args=()):
    with sqlite3.connect(DB) as c:
        return c.execute(sql, args).lastrowid


def cfg_get(key, default=None):
    r = q("SELECT value FROM config WHERE key=?", (key,), one=True)
    return r["value"] if r else default


def cfg_set(key, value):
    ex("INSERT OR REPLACE INTO config(key,value) VALUES(?,?)", (key, str(value)))


def cfg_int(key):
    v = cfg_get(key)
    return int(v) if v else None


def cfg_channel(guild, key):
    cid = cfg_int(key)
    return guild.get_channel(cid) if cid else None


def is_staff(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    rid = cfg_int("staff_role")
    return bool(rid) and any(r.id == rid for r in member.roles)


def is_blacklisted(user_id: int) -> bool:
    return q("SELECT 1 FROM blacklist WHERE user_id=?", (user_id,), one=True) is not None


def order_of(channel_id: int):
    return q("SELECT * FROM orders WHERE channel_id=?", (channel_id,), one=True)


def stars_str(n: int) -> str:
    return "⭐" * n + "☆" * (5 - n)


def categories():
    names = [r["name"] for r in q("SELECT name FROM products ORDER BY name")][:24]
    return names + ["Autre / Question"]


# ───────────────────────── Paiement LTC automatique ─────────────────────────
_rate = {"v": None, "t": 0.0}


def ltc_address():
    return cfg_get("ltc_address") or os.getenv("LTC_ADDRESS")


def parse_eur(text):
    """Extrait le prix en € d'un texte comme '5€' ou '12,50 €'. None si pas de nombre."""
    m = re.search(r"(\d+(?:[.,]\d+)?)", text or "")
    return float(m.group(1).replace(",", ".")) if m else None


async def http_json(url):
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.get(url, headers={"User-Agent": "XenityShopBot"}) as r:
            r.raise_for_status()
            return await r.json()


async def ltc_rate():
    """Prix de 1 LTC en €, mis en cache 60s (réutilise l'ancien cours si l'API plante)."""
    if _rate["v"] and time.time() - _rate["t"] < 60:
        return _rate["v"]
    try:
        data = await http_json(COINGECKO)
        _rate["v"] = float(data["litecoin"]["eur"])
        _rate["t"] = time.time()
    except Exception:
        if not _rate["v"]:
            raise
    return _rate["v"]


async def make_payment(order_id, eur):
    """Calcule le montant LTC exact (avec suffixe unique) et l'enregistre sur la commande."""
    rate = await ltc_rate()
    base = int(round(eur / rate * 1e8))
    taken = {r["ltc_amount"] for r in q(
        "SELECT ltc_amount FROM orders WHERE ltc_amount IS NOT NULL AND pay_state<2 AND status IN ('open','claimed')")}
    amount = base + random.randint(1, 9999)
    for _ in range(500):
        if amount not in taken:
            break
        amount = base + random.randint(1, 9999)
    ex("UPDATE orders SET ltc_amount=?, ltc_eur=?, ltc_ts=?, pay_state=0 WHERE id=?",
       (amount, eur, int(time.time()), order_id))
    return amount


def payment_embed(o):
    emb = discord.Embed(
        title="💳 Paiement Litecoin (LTC)",
        description=(
            "Envoie **exactement** le montant ci-dessous à l'adresse ci-dessous "
            "(les frais réseau se paient en plus, ne les déduis pas du montant).\n"
            "Le paiement est **détecté automatiquement** ✅ pas besoin de nous prévenir."
        ),
        color=0x345D9D,
    )
    emb.add_field(name="Adresse LTC", value=f"```{ltc_address()}```", inline=False)
    emb.add_field(name="Montant EXACT", value=f"```{o['ltc_amount'] / 1e8:.8f}```", inline=False)
    emb.add_field(name="Prix", value=f"{o['ltc_eur']:.2f} €")
    emb.add_field(name="Valable", value="3 heures")
    emb.set_footer(text=SHOP_NAME)
    return emb


async def send_auto_payment(ch, order_id, prod):
    """Appelé à l'ouverture du ticket : envoie direct l'adresse LTC + le montant."""
    if not ltc_address():
        return
    eur = parse_eur(prod["price"]) if prod else None
    if not eur:
        return await ch.send("💳 Un vendeur va te donner le montant à payer en LTC dans ce ticket.")
    try:
        await make_payment(order_id, eur)
    except Exception as e:
        print("paiement:", e)
        return await ch.send("⚠️ Impossible de calculer le montant LTC pour l'instant, un vendeur s'en occupe.")
    await ch.send(embed=payment_embed(q("SELECT * FROM orders WHERE id=?", (order_id,), one=True)))


async def on_paid(ch, o, txid):
    """Paiement confirmé : rôle client, ping staff, bouton vouch (le vouch part dans le salon des vouchs)."""
    g = ch.guild
    rid = cfg_int("client_role")
    try:
        buyer = await g.fetch_member(o["user_id"])
        if rid and g.get_role(rid):
            await buyer.add_roles(g.get_role(rid), reason="Paiement LTC reçu")
    except discord.HTTPException:
        pass
    srid = cfg_int("staff_role")
    emb = discord.Embed(
        title="✅ Paiement reçu !",
        description=(
            f"**{o['ltc_amount'] / 1e8:.8f} LTC** confirmés sur la blockchain.\n"
            "Un vendeur va te livrer ta commande.\n\n"
            "⭐ Laisse ton vouch avec le bouton ci-dessous, il sera posté dans le salon des vouchs."
        ),
        color=0x22C55E,
    )
    emb.add_field(name="Transaction", value=f"[Voir sur la blockchain](https://blockchair.com/litecoin/transaction/{txid})")
    await ch.send(
        content=f"<@{o['user_id']}>" + (f" <@&{srid}>" if srid else ""),
        embed=emb, view=VouchView(),
        allowed_mentions=discord.AllowedMentions(users=True, roles=True),
    )
    await send_log(g, embed=discord.Embed(
        title="💰 Paiement LTC reçu",
        description=f"Ticket {ch.mention} • <@{o['user_id']}> • {o['product']}\n{o['ltc_amount'] / 1e8:.8f} LTC ({o['ltc_eur']:.2f} €)\n`{txid}`",
        color=0x22C55E))


# ───────────────────────── Embeds auto-rafraîchis ─────────────────────────
def catalogue_embed():
    rows = q("SELECT * FROM products ORDER BY name")
    emb = discord.Embed(title=f"🛍️ Catalogue {SHOP_NAME}", color=COLOR, timestamp=discord.utils.utcnow())
    if not rows:
        emb.description = "Le catalogue est vide pour l'instant."
    for p in rows:
        stock = "♾️ Disponible" if p["stock"] < 0 else ("❌ Rupture" if p["stock"] == 0 else f"✅ {p['stock']} en stock")
        emb.add_field(name=f"{p['name']} — {p['price']}", value=f"{p['description'] or '—'}\n{stock}", inline=False)
    emb.set_footer(text="Pour commander : ouvre un ticket dans le salon d'achat • Mis à jour")
    return emb


def panel_embed():
    emb = discord.Embed(
        title=f"🛒 {SHOP_NAME}",
        description="Choisis un produit dans le menu ci-dessous pour ouvrir un ticket d'achat.\nUn vendeur te répond rapidement.",
        color=COLOR,
    )
    emb.set_footer(text=SHOP_NAME)
    return emb


async def _upsert(guild, chan_key, msg_key, **kwargs):
    ch = cfg_channel(guild, chan_key)
    if not ch:
        return
    try:
        msg = await ch.fetch_message(cfg_int(msg_key))
        await msg.edit(**kwargs)
    except Exception:
        msg = await ch.send(**kwargs)
        cfg_set(msg_key, msg.id)


async def refresh_shop(guild):
    """Remet à jour le catalogue et le panneau de tickets."""
    await _upsert(guild, "catalogue_channel", "catalogue_msg", embed=catalogue_embed())
    await _upsert(guild, "panel_channel", "panel_msg", embed=panel_embed(), view=PanelView())


# ───────────────────────── Vouch ─────────────────────────
def check_vouch(member: discord.Member):
    if is_blacklisted(member.id):
        return "Tu es blacklist, tu ne peux pas vouch."
    if cfg_get("vouch_client_only") == "1":
        rid = cfg_int("client_role")
        if rid and not any(r.id == rid for r in member.roles):
            return "Il faut avoir acheté au shop (rôle client) pour laisser un vouch."
    last = q("SELECT ts FROM vouches WHERE author_id=? ORDER BY ts DESC LIMIT 1", (member.id,), one=True)
    if last and time.time() - last["ts"] < VOUCH_COOLDOWN:
        reste = int((VOUCH_COOLDOWN - (time.time() - last["ts"])) // 3600) + 1
        return f"Tu as déjà vouch récemment, réessaie dans ~{reste}h."
    return None


async def post_vouch(guild, author, target_id, stars, text, product=None):
    vid = ex(
        "INSERT INTO vouches(author_id,target_id,stars,text,product,ts) VALUES(?,?,?,?,?,?)",
        (author.id, target_id or 0, stars, text, product, int(time.time())),
    )
    emb = discord.Embed(
        title=f"✅ Nouveau vouch #{vid}",
        description=f"{stars_str(stars)}\n\n> {text}",
        color=COLOR,
        timestamp=discord.utils.utcnow(),
    )
    emb.add_field(name="De", value=author.mention)
    emb.add_field(name="Vendeur", value=f"<@{target_id}>" if target_id else SHOP_NAME)
    if product:
        emb.add_field(name="Produit", value=product)
    emb.set_thumbnail(url=author.display_avatar.url)
    emb.set_footer(text=SHOP_NAME)
    ch = cfg_channel(guild, "vouch_channel")
    if ch:
        await ch.send(embed=emb)
    return vid


class VouchModal(discord.ui.Modal, title="Laisse ton vouch"):
    note = discord.ui.TextInput(label="Note (1 à 5)", max_length=1, placeholder="5")
    avis = discord.ui.TextInput(
        label="Ton avis", style=discord.TextStyle.paragraph, max_length=500,
        placeholder="Rapide, sérieux, merci !",
    )

    def __init__(self, target_id=0, product=None):
        super().__init__()
        self.target_id = target_id
        self.product = product

    async def on_submit(self, i: discord.Interaction):
        if not self.note.value.isdigit() or not 1 <= int(self.note.value) <= 5:
            return await i.response.send_message("Note invalide, mets un chiffre de 1 à 5.", ephemeral=True)
        err = check_vouch(i.user)
        if err:
            return await i.response.send_message(err, ephemeral=True)
        vid = await post_vouch(i.guild, i.user, self.target_id, int(self.note.value), self.avis.value, self.product)
        await i.response.send_message(f"Merci pour ton vouch #{vid} ! ❤️", ephemeral=True)


class VouchView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Laisser un vouch", emoji="⭐", style=discord.ButtonStyle.success, custom_id="shop:vouchbtn")
    async def vouch(self, i: discord.Interaction, b: discord.ui.Button):
        o = order_of(i.channel.id)
        if o and o["user_id"] != i.user.id and not is_staff(i.user):
            return await i.response.send_message("Seul l'acheteur peut vouch ici.", ephemeral=True)
        await i.response.send_modal(VouchModal(o["staff_id"] if o and o["staff_id"] else 0, o["product"] if o else None))


# ───────────────────────── Tickets ─────────────────────────
async def transcript_file(channel: discord.TextChannel):
    lines = []
    async for m in channel.history(limit=None, oldest_first=True):
        extra = "".join(f" [{a.url}]" for a in m.attachments)
        lines.append(f"[{m.created_at:%Y-%m-%d %H:%M}] {m.author}: {m.content}{extra}")
    return discord.File(io.BytesIO("\n".join(lines).encode()), filename=f"{channel.name}.txt")


async def send_log(guild, content=None, embed=None, file=None):
    ch = cfg_channel(guild, "logs_channel")
    if ch:
        await ch.send(content=content, embed=embed, file=file)


async def close_channel(channel: discord.TextChannel, reason: str):
    o = order_of(channel.id)
    try:
        f = await transcript_file(channel)
        await send_log(channel.guild, content=f"📁 Transcript de `{channel.name}` ({reason})", file=f)
    except discord.HTTPException:
        pass
    if o:
        if o["status"] == "delivered":
            ex("UPDATE orders SET channel_id=0 WHERE id=?", (o["id"],))
        else:
            ex("UPDATE orders SET status='closed', channel_id=0 WHERE id=?", (o["id"],))
    try:
        await channel.delete(reason=reason)
    except discord.HTTPException:
        pass


async def create_ticket(i: discord.Interaction, category: str):
    g = i.guild
    if is_blacklisted(i.user.id):
        return await i.response.send_message("Tu es blacklist du shop.", ephemeral=True)
    prod = q("SELECT * FROM products WHERE name=?", (category,), one=True)
    if prod and prod["stock"] == 0:
        return await i.response.send_message(f"❌ **{category}** est en rupture de stock pour le moment.", ephemeral=True)
    for r in q("SELECT channel_id FROM orders WHERE user_id=? AND status IN ('open','claimed')", (i.user.id,)):
        if r["channel_id"] and g.get_channel(r["channel_id"]):
            return await i.response.send_message(f"Tu as déjà un ticket ouvert : <#{r['channel_id']}>", ephemeral=True)

    await i.response.defer(ephemeral=True)
    ow = {
        g.default_role: discord.PermissionOverwrite(view_channel=False),
        i.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, read_message_history=True),
        g.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
    }
    srid = cfg_int("staff_role")
    if srid and g.get_role(srid):
        ow[g.get_role(srid)] = discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, read_message_history=True)
    cat = g.get_channel(cfg_int("ticket_category") or 0)
    ch = await g.create_text_channel(f"ticket-{i.user.name}"[:90], category=cat, overwrites=ow, topic=f"Ticket de {i.user} ({i.user.id})")
    oid = ex("INSERT INTO orders(user_id,channel_id,product,ts) VALUES(?,?,?,?)", (i.user.id, ch.id, category, int(time.time())))

    emb = discord.Embed(
        title=f"🎫 Ticket #{oid} ouvert par {i.user.display_name}",
        description=(
            f"**Produit :** {category}" + (f" — {prod['price']}" if prod else "") + "\n\n"
            "Explique ce que tu veux, un vendeur arrive.\n"
            "Clique sur **Paiement** pour voir les moyens de paiement."
        ),
        color=COLOR,
        timestamp=discord.utils.utcnow(),
    )
    emb.set_footer(text=SHOP_NAME)
    await ch.send(content=i.user.mention, embed=emb, view=TicketControls(),
                  allowed_mentions=discord.AllowedMentions(users=[i.user], roles=False, everyone=False))
    await send_auto_payment(ch, oid, prod)
    await send_log(g, embed=discord.Embed(title="Ticket ouvert", description=f"{i.user.mention} → {ch.mention} ({category})", color=COLOR))
    await i.followup.send(f"Ticket créé : {ch.mention}", ephemeral=True)


class PanelSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Choisis un produit…", custom_id="shop:panel",
            options=[discord.SelectOption(label=n[:100], value=n[:100], emoji="❓" if n.startswith("Autre") else "🛒")
                     for n in categories()],
        )

    async def callback(self, i: discord.Interaction):
        await create_ticket(i, self.values[0])
        try:  # reset du menu pour pouvoir re-choisir la même option
            await i.message.edit(view=PanelView())
        except discord.HTTPException:
            pass


class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(PanelSelect())


class TicketControls(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim", emoji="🙋", style=discord.ButtonStyle.primary, custom_id="shop:claim")
    async def claim(self, i: discord.Interaction, b: discord.ui.Button):
        if not is_staff(i.user):
            return await i.response.send_message("Réservé au staff.", ephemeral=True)
        ex("UPDATE orders SET status='claimed', staff_id=? WHERE channel_id=?", (i.user.id, i.channel.id))
        await i.response.send_message(embed=discord.Embed(description=f"🙋 Ticket pris en charge par {i.user.mention}", color=COLOR))

    @discord.ui.button(label="Paiement", emoji="💳", style=discord.ButtonStyle.secondary, custom_id="shop:pay")
    async def pay(self, i: discord.Interaction, b: discord.ui.Button):
        o = order_of(i.channel.id)
        if o and o["pay_state"] == 2:
            return await i.response.send_message("✅ Cette commande est déjà payée.", ephemeral=True)
        if o and o["ltc_amount"] and ltc_address():
            return await i.response.send_message(embed=payment_embed(o))
        txt = cfg_get("paiement", "Crypto (BTC / LTC / USDT) et PayPal acceptés.\nDemande l'adresse au vendeur.")
        await i.response.send_message(embed=discord.Embed(title="💳 Moyens de paiement", description=txt, color=COLOR))

    @discord.ui.button(label="Livré", emoji="✅", style=discord.ButtonStyle.success, custom_id="shop:deliver")
    async def deliver(self, i: discord.Interaction, b: discord.ui.Button):
        if not is_staff(i.user):
            return await i.response.send_message("Réservé au staff.", ephemeral=True)
        o = order_of(i.channel.id)
        if not o:
            return await i.response.send_message("Commande introuvable.", ephemeral=True)
        if o["status"] == "delivered":
            return await i.response.send_message("Déjà marqué comme livré.", ephemeral=True)
        ex("UPDATE orders SET status='delivered', delivered_ts=?, staff_id=COALESCE(staff_id, ?) WHERE channel_id=?",
           (int(time.time()), i.user.id, i.channel.id))
        ex("UPDATE products SET stock = stock - 1 WHERE name=? AND stock > 0", (o["product"],))
        buyer = None
        rid = cfg_int("client_role")
        try:
            buyer = await i.guild.fetch_member(o["user_id"])
            if rid and i.guild.get_role(rid):
                await buyer.add_roles(i.guild.get_role(rid), reason="Commande livrée")
        except discord.HTTPException:
            pass
        emb = discord.Embed(
            title="✅ Commande livrée",
            description="Merci pour ton achat !\nLaisse un vouch avec le bouton ci-dessous (ou `/vouch`).\n"
                        "_Ce ticket sera fermé automatiquement dans 30 minutes._",
            color=0x22C55E,
        )
        await i.response.send_message(content=f"<@{o['user_id']}>", embed=emb, view=VouchView(),
                                      allowed_mentions=discord.AllowedMentions(users=True))
        if buyer:
            try:
                await buyer.send(f"✅ Ta commande **{o['product']}** chez {SHOP_NAME} est livrée ! Pense à laisser un vouch 🙏")
            except discord.HTTPException:
                pass
        await send_log(i.guild, embed=discord.Embed(
            title="Commande livrée", description=f"#{o['id']} • {o['product']} • <@{o['user_id']}> • par {i.user.mention}", color=0x22C55E))
        await refresh_shop(i.guild)

    @discord.ui.button(label="Fermer", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="shop:close")
    async def close(self, i: discord.Interaction, b: discord.ui.Button):
        o = order_of(i.channel.id)
        if not (is_staff(i.user) or (o and o["user_id"] == i.user.id)):
            return await i.response.send_message("Tu ne peux pas fermer ce ticket.", ephemeral=True)
        await i.response.send_message("🔒 Fermeture du ticket…")
        await close_channel(i.channel, f"fermé par {i.user}")


# ───────────────────────── Bot ─────────────────────────
class ShopBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=".", intents=discord.Intents.default())

    async def setup_hook(self):
        init_db()
        self.add_view(PanelView())
        self.add_view(TicketControls())
        self.add_view(VouchView())
        await self.tree.sync()
        janitor.start()
        payment_watcher.start()
        app = web.Application()
        app.router.add_get("/", lambda r: web.Response(text=f"{SHOP_NAME} bot OK"))
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 8080))).start()

    async def on_ready(self):
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name=f"{SHOP_NAME} 🛒"))
        print(f"Connecté en tant que {self.user}")


bot = ShopBot()
tree = bot.tree


@bot.event
async def on_message(m: discord.Message):
    """Auto-claim : le premier staff qui écrit dans un ticket le prend en charge."""
    if m.author.bot or not m.guild or not isinstance(m.author, discord.Member):
        return
    o = order_of(m.channel.id)
    if o and o["status"] == "open" and m.author.id != o["user_id"] and is_staff(m.author):
        ex("UPDATE orders SET status='claimed', staff_id=? WHERE id=?", (m.author.id, o["id"]))
        await m.channel.send(embed=discord.Embed(description=f"🙋 Ticket pris en charge par {m.author.mention}", color=COLOR))


@tasks.loop(minutes=5)
async def janitor():
    """Ferme tout seul les tickets livrés (30 min) et les tickets inactifs (48h)."""
    now = time.time()
    try:
        for r in q("SELECT * FROM orders WHERE status='delivered' AND channel_id!=0 AND delivered_ts IS NOT NULL AND delivered_ts < ?",
                   (now - DELIVERED_CLOSE_AFTER,)):
            ch = bot.get_channel(r["channel_id"])
            if ch:
                await close_channel(ch, "auto : commande livrée")
            else:
                ex("UPDATE orders SET channel_id=0 WHERE id=?", (r["id"],))
        for r in q("SELECT * FROM orders WHERE status IN ('open','claimed') AND channel_id!=0"):
            ch = bot.get_channel(r["channel_id"])
            if not ch:
                ex("UPDATE orders SET status='closed', channel_id=0 WHERE id=?", (r["id"],))
                continue
            last = discord.utils.snowflake_time(ch.last_message_id).timestamp() if ch.last_message_id else ch.created_at.timestamp()
            if now - last > INACTIVE_CLOSE_AFTER:
                await close_channel(ch, "auto : inactif 48h")
    except Exception as e:  # le janitor ne doit jamais s'arrêter
        print("janitor:", e)


@janitor.before_loop
async def _wait():
    await bot.wait_until_ready()


@tasks.loop(seconds=30)
async def payment_watcher():
    """Cherche sur la blockchain les paiements LTC des tickets en attente."""
    try:
        addr = ltc_address()
        if not addr:
            return
        now = int(time.time())
        pend = q("SELECT * FROM orders WHERE status IN ('open','claimed') AND channel_id!=0 "
                 "AND ltc_amount IS NOT NULL AND pay_state<2 AND ltc_ts > ?", (now - PAY_WINDOW,))
        if not pend:
            return
        txs = await http_json(f"{LTC_API}/address/{addr}/txs")
        for o in pend:
            for tx in txs:
                paid_here = any(v.get("scriptpubkey_address") == addr and v.get("value") == o["ltc_amount"]
                                for v in tx.get("vout", []))
                if not paid_here:
                    continue
                if q("SELECT 1 FROM orders WHERE paid_txid=? AND id!=?", (tx["txid"], o["id"]), one=True):
                    continue  # cette transaction a déjà servi pour un autre ticket
                ch = bot.get_channel(o["channel_id"])
                if not ch:
                    break
                if tx.get("status", {}).get("confirmed"):
                    ex("UPDATE orders SET pay_state=2, paid_txid=? WHERE id=?", (tx["txid"], o["id"]))
                    await on_paid(ch, o, tx["txid"])
                elif o["pay_state"] == 0:
                    ex("UPDATE orders SET pay_state=1, paid_txid=? WHERE id=?", (tx["txid"], o["id"]))
                    await ch.send(embed=discord.Embed(
                        title="⏳ Paiement détecté",
                        description="On a vu ta transaction, on attend la confirmation du réseau (quelques minutes).",
                        color=0xF59E0B))
                break
    except Exception as e:  # le watcher ne doit jamais s'arrêter
        print("watcher:", e)


@payment_watcher.before_loop
async def _wait_pay():
    await bot.wait_until_ready()


def staff_only():
    async def pred(i: discord.Interaction):
        if isinstance(i.user, discord.Member) and is_staff(i.user):
            return True
        raise app_commands.CheckFailure()
    return app_commands.check(pred)


@tree.error
async def on_tree_error(i: discord.Interaction, error: app_commands.AppCommandError):
    msg = "Tu n'as pas la permission pour ça." if isinstance(error, app_commands.CheckFailure) else f"Erreur : {error}"
    if i.response.is_done():
        await i.followup.send(msg, ephemeral=True)
    else:
        await i.response.send_message(msg, ephemeral=True)


async def product_autocomplete(i: discord.Interaction, current: str):
    rows = q("SELECT name FROM products WHERE name LIKE ? ORDER BY name LIMIT 25", (f"%{current}%",))
    return [app_commands.Choice(name=r["name"], value=r["name"]) for r in rows]


# ───────────────────────── /setup : tout en un ─────────────────────────
admin = app_commands.default_permissions(administrator=True)


@tree.command(name="setup", description="Installe le shop (rôles, salons d'achat, panneau, catalogue, vouchs, logs)")
@admin
async def setup(i: discord.Interaction):
    await do_setup(i, full=False)


@tree.command(name="creer_shop", description="Crée TOUT le shop : rôles, salons, règlement, bienvenue, panneau, catalogue, vouchs, logs")
@admin
async def creer_shop(i: discord.Interaction):
    await do_setup(i, full=True)


async def do_setup(i: discord.Interaction, full: bool = False):
    await i.response.defer(ephemeral=True)
    g = i.guild

    async def ensure_role(key, name, color, mentionable=False):
        r = g.get_role(cfg_int(key) or 0) or discord.utils.get(g.roles, name=name)
        if not r:
            r = await g.create_role(name=name, color=discord.Color(color), mentionable=mentionable, reason="Setup shop")
        cfg_set(key, r.id)
        return r

    staff = await ensure_role("staff_role", "Staff Shop", 0x8B5CF6)
    await ensure_role("client_role", "Client", 0x22C55E)
    await ensure_role("ping_role", "Restock", 0xF59E0B, mentionable=True)
    try:  # celui qui lance /setup devient staff
        await i.user.add_roles(staff)
    except discord.HTTPException:
        pass

    ro = {
        g.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False, add_reactions=False),
        staff: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        g.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True, manage_messages=True),
    }
    private = {
        g.default_role: discord.PermissionOverwrite(view_channel=False),
        staff: discord.PermissionOverwrite(view_channel=True, send_messages=False),
        g.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True),
    }

    async def ensure_category(key, name, overwrites):
        c = g.get_channel(cfg_int(key) or 0)
        if not c:
            c = await g.create_category(name, overwrites=overwrites, reason="Setup shop")
            cfg_set(key, c.id)
        return c

    async def ensure_channel(key, name, category, overwrites):
        c = cfg_channel(g, key)
        if not c:
            c = await g.create_text_channel(name, category=category, overwrites=overwrites, reason="Setup shop")
            cfg_set(key, c.id)
        return c

    shop_cat = await ensure_category("shop_category", "🛒・SHOP", {})
    await ensure_category("ticket_category", "🎫・TICKETS", {g.default_role: discord.PermissionOverwrite(view_channel=False),
                                                          staff: discord.PermissionOverwrite(view_channel=True, send_messages=True),
                                                          g.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)})
    await ensure_channel("panel_channel", "🎫・acheter", shop_cat, ro)
    await ensure_channel("catalogue_channel", "📦・catalogue", shop_cat, ro)
    await ensure_channel("vouch_channel", "✅・vouchs", shop_cat, ro)
    await ensure_channel("annonces_channel", "📢・restock", shop_cat, ro)
    admin_cat = await ensure_category("admin_category", "🔒・STAFF", private)
    await ensure_channel("logs_channel", "📁・logs-shop", admin_cat, private)

    if full:
        staff_chat = {
            g.default_role: discord.PermissionOverwrite(view_channel=False),
            staff: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True),
            g.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        await ensure_channel("staffchat_channel", "💼・staff-chat", admin_cat, staff_chat)
        info_cat = await ensure_category("info_category", "📌・INFOS", {})
        await ensure_channel("welcome_channel", "👋・bienvenue", info_cat, ro)
        await ensure_channel("rules_channel", "📜・règlement", info_cat, ro)
        await ensure_channel("news_channel", "📣・annonces", info_cat, ro)
        com_cat = await ensure_category("com_category", "💬・COMMUNAUTÉ", {})
        await ensure_channel("general_channel", "💬・général", com_cat, {})
        await ensure_channel("media_channel", "🖼️・médias", com_cat, {})

    if not q("SELECT 1 FROM products LIMIT 1", one=True):
        for n, p, d in DEFAULT_PRODUCTS:
            ex("INSERT OR IGNORE INTO products(name,price,stock,description) VALUES(?,?,-1,?)", (n, p, d))
    if not cfg_get("paiement"):
        cfg_set("paiement", "Crypto (BTC / LTC / USDT) et PayPal acceptés.\nLe vendeur te donne l'adresse dans le ticket.")

    await refresh_shop(g)
    if full:
        buy, vch = cfg_int("panel_channel"), cfg_int("vouch_channel")
        await _upsert(g, "welcome_channel", "welcome_msg", embed=discord.Embed(
            title=f"👋 Bienvenue sur {SHOP_NAME} !",
            description=(
                f"Ici tu trouves nos produits digitaux.\n\n"
                f"🛒 Pour acheter : ouvre un ticket dans <#{buy}>\n"
                f"✅ Les avis de nos clients : <#{vch}>\n"
                f"📜 Lis le règlement avant de commander."
            ),
            color=COLOR))
        await _upsert(g, "rules_channel", "rules_msg", embed=discord.Embed(
            title="📜 Règlement",
            description=(
                "**1.** Respecte les membres et le staff.\n"
                "**2.** Pas de spam, pas de pub, pas d'insultes.\n"
                "**3.** Les achats se font **uniquement via un ticket**. Ne fais confiance à aucun message privé, "
                "seul le staff dans ton ticket peut te vendre quelque chose.\n"
                "**4.** Paie uniquement avec les moyens donnés par le bot dans ton ticket, en envoyant le montant exact.\n"
                "**5.** Garde tes preuves (captures, txid) en cas de problème.\n"
                "**6.** Après ta commande, laisse un vouch, ça aide tout le monde.\n\n"
                "_Le staff peut sanctionner en cas de non-respect._"
            ),
            color=COLOR))
    await i.followup.send(
        "✅ **Shop installé !**\n"
        "• Salons, rôles (Staff Shop / Client / Restock), panneau et catalogue créés\n"
        "• Tu as reçu le rôle **Staff Shop**, donne-le à tes vendeurs avec `/staff_ajouter`\n"
        "• Change tes produits avec `/produit_ajouter` et `/produit_modifier`, le catalogue et le menu se mettent à jour tout seuls\n"
        "• Mets ton adresse Litecoin avec `/set_ltc` : elle sera envoyée toute seule à chaque ticket\n"
        "• Les prix doivent contenir un nombre en € (ex : `5€`) pour que le montant LTC se calcule tout seul"
        + ("\n• Mode complet : bienvenue, règlement, annonces, général, médias et staff-chat créés aussi" if full else ""),
        ephemeral=True,
    )


class PaiementModal(discord.ui.Modal, title="Moyens de paiement"):
    texte = discord.ui.TextInput(label="Texte affiché", style=discord.TextStyle.paragraph, max_length=1500,
                                 placeholder="BTC : ...\nLTC : ...\nPayPal : ...")

    async def on_submit(self, i: discord.Interaction):
        cfg_set("paiement", self.texte.value)
        await i.response.send_message("✅ Moyens de paiement mis à jour.", ephemeral=True)


@tree.command(name="set_paiement", description="Définir le texte des moyens de paiement")
@admin
async def set_paiement(i: discord.Interaction):
    await i.response.send_modal(PaiementModal())


@tree.command(name="vouch_obligatoire", description="Exiger le rôle client pour vouch")
@admin
async def vouch_obligatoire(i: discord.Interaction, actif: bool):
    cfg_set("vouch_client_only", "1" if actif else "0")
    await i.response.send_message(f"✅ Vouch réservé aux clients : **{'oui' if actif else 'non'}**", ephemeral=True)


@tree.command(name="staff_ajouter", description="Donner le rôle staff à un membre")
@admin
async def staff_ajouter(i: discord.Interaction, membre: discord.Member):
    r = i.guild.get_role(cfg_int("staff_role") or 0)
    if not r:
        return await i.response.send_message("Fais d'abord `/setup`.", ephemeral=True)
    await membre.add_roles(r)
    await i.response.send_message(f"✅ {membre.mention} est staff.", ephemeral=True,
                                  allowed_mentions=discord.AllowedMentions.none())


# ───────────────────────── Vouch (commandes) ─────────────────────────
@tree.command(name="set_ltc", description="Définir ton adresse LTC de réception")
@admin
async def set_ltc(i: discord.Interaction, adresse: str):
    adresse = adresse.strip()
    if len(adresse) < 26 or not adresse.startswith(("ltc1", "L", "M", "3")):
        return await i.response.send_message("Cette adresse LTC a l'air invalide (elle commence par ltc1, L ou M).", ephemeral=True)
    cfg_set("ltc_address", adresse)
    await i.response.send_message(
        f"✅ Adresse LTC enregistrée : `{adresse}`\nFais un test avec un tout petit paiement avant de t'en servir pour de vrai.",
        ephemeral=True)


@tree.command(name="ltc_montant", description="Générer le paiement LTC de ce ticket à partir d'un prix en € (staff)")
@staff_only()
async def ltc_montant(i: discord.Interaction, euros: app_commands.Range[float, 0.5, 100000]):
    o = order_of(i.channel.id)
    if not o:
        return await i.response.send_message("À utiliser dans un ticket.", ephemeral=True)
    if not ltc_address():
        return await i.response.send_message("Mets d'abord ton adresse avec `/set_ltc`.", ephemeral=True)
    if o["pay_state"] == 2:
        return await i.response.send_message("Cette commande est déjà payée.", ephemeral=True)
    await i.response.defer()
    try:
        await make_payment(o["id"], euros)
    except Exception as e:
        return await i.followup.send(f"Impossible de calculer le montant LTC : {e}", ephemeral=True)
    await i.followup.send(embed=payment_embed(q("SELECT * FROM orders WHERE id=?", (o["id"],), one=True)))


@tree.command(name="vouch", description="Laisse un vouch pour un vendeur")
async def vouch(i: discord.Interaction, vendeur: discord.Member, note: app_commands.Range[int, 1, 5],
                avis: app_commands.Range[str, 3, 500], produit: str = None):
    if vendeur.id == i.user.id:
        return await i.response.send_message("Tu ne peux pas te vouch toi-même 😅", ephemeral=True)
    err = check_vouch(i.user)
    if err:
        return await i.response.send_message(err, ephemeral=True)
    vid = await post_vouch(i.guild, i.user, vendeur.id, note, avis, produit)
    await i.response.send_message(f"✅ Vouch #{vid} envoyé, merci !", ephemeral=True)


@tree.command(name="vouches", description="Voir les vouchs d'un membre (ou du shop)")
async def vouches(i: discord.Interaction, membre: discord.Member = None):
    if membre:
        rows = q("SELECT * FROM vouches WHERE target_id=? ORDER BY ts DESC", (membre.id,))
        titre = f"Vouchs de {membre.display_name}"
    else:
        rows = q("SELECT * FROM vouches ORDER BY ts DESC")
        titre = f"Vouchs de {SHOP_NAME}"
    if not rows:
        return await i.response.send_message("Aucun vouch pour l'instant.", ephemeral=True)
    moy = sum(r["stars"] for r in rows) / len(rows)
    emb = discord.Embed(title=titre, color=COLOR,
                        description=f"**{len(rows)}** vouchs • moyenne **{moy:.2f}/5** {stars_str(round(moy))}")
    for r in rows[:5]:
        emb.add_field(name=f"#{r['id']} • {stars_str(r['stars'])}", value=f"<@{r['author_id']}> : {r['text'][:200]}", inline=False)
    await i.response.send_message(embed=emb, allowed_mentions=discord.AllowedMentions.none())


@tree.command(name="top_vouch", description="Classement des vendeurs par vouchs")
async def top_vouch(i: discord.Interaction):
    rows = q("SELECT target_id, COUNT(*) n, AVG(stars) m FROM vouches WHERE target_id!=0 GROUP BY target_id ORDER BY n DESC LIMIT 10")
    if not rows:
        return await i.response.send_message("Pas encore de classement.", ephemeral=True)
    medals = ["🥇", "🥈", "🥉"]
    desc = "\n".join(f"{medals[n] if n < 3 else f'`{n+1}.`'} <@{r['target_id']}> — **{r['n']}** vouchs • {r['m']:.2f}/5"
                     for n, r in enumerate(rows))
    await i.response.send_message(embed=discord.Embed(title="🏆 Top vendeurs", description=desc, color=COLOR),
                                  allowed_mentions=discord.AllowedMentions.none())


@tree.command(name="vouch_supprimer", description="Supprimer un vouch (staff)")
@staff_only()
async def vouch_supprimer(i: discord.Interaction, id: int):
    if not q("SELECT 1 FROM vouches WHERE id=?", (id,), one=True):
        return await i.response.send_message("Vouch introuvable.", ephemeral=True)
    ex("DELETE FROM vouches WHERE id=?", (id,))
    await i.response.send_message(f"🗑️ Vouch #{id} supprimé (le message dans le salon reste à supprimer à la main).", ephemeral=True)


# ───────────────────────── Produits / stock ─────────────────────────
@tree.command(name="catalogue", description="Voir les produits du shop")
async def catalogue(i: discord.Interaction):
    await i.response.send_message(embed=catalogue_embed())


@tree.command(name="produit_ajouter", description="Ajouter un produit (staff)")
@staff_only()
async def produit_ajouter(i: discord.Interaction, nom: str, prix: str, stock: int = -1, description: str = ""):
    try:
        ex("INSERT INTO products(name,price,stock,description) VALUES(?,?,?,?)", (nom, prix, stock, description))
    except sqlite3.IntegrityError:
        return await i.response.send_message("Ce produit existe déjà (utilise /produit_modifier).", ephemeral=True)
    await i.response.send_message(f"✅ **{nom}** ajouté ({prix}). Catalogue et menu mis à jour.", ephemeral=True)
    await refresh_shop(i.guild)


@tree.command(name="produit_modifier", description="Modifier prix / stock / description d'un produit (staff)")
@staff_only()
@app_commands.autocomplete(nom=product_autocomplete)
async def produit_modifier(i: discord.Interaction, nom: str, prix: str = None, stock: int = None, description: str = None):
    p = q("SELECT * FROM products WHERE name=?", (nom,), one=True)
    if not p:
        return await i.response.send_message("Produit introuvable.", ephemeral=True)
    ex("UPDATE products SET price=?, stock=?, description=? WHERE name=?",
       (prix if prix is not None else p["price"], stock if stock is not None else p["stock"],
        description if description is not None else p["description"], nom))
    await i.response.send_message(f"✅ **{nom}** modifié. Catalogue mis à jour.", ephemeral=True)
    await refresh_shop(i.guild)


@tree.command(name="produit_supprimer", description="Supprimer un produit (staff)")
@staff_only()
@app_commands.autocomplete(nom=product_autocomplete)
async def produit_supprimer(i: discord.Interaction, nom: str):
    ex("DELETE FROM products WHERE name=?", (nom,))
    await i.response.send_message(f"🗑️ **{nom}** supprimé. Catalogue et menu mis à jour.", ephemeral=True)
    await refresh_shop(i.guild)


@tree.command(name="restock", description="Ajouter du stock et annoncer le restock (staff)")
@staff_only()
@app_commands.autocomplete(nom=product_autocomplete)
async def restock(i: discord.Interaction, nom: str, quantite: app_commands.Range[int, 1, 100000]):
    p = q("SELECT * FROM products WHERE name=?", (nom,), one=True)
    if not p:
        return await i.response.send_message("Produit introuvable.", ephemeral=True)
    new = quantite if p["stock"] < 0 else p["stock"] + quantite
    ex("UPDATE products SET stock=? WHERE name=?", (new, nom))
    ch = cfg_channel(i.guild, "annonces_channel")
    if ch:
        rid = cfg_int("ping_role")
        emb = discord.Embed(title="📦 RESTOCK !", description=f"**{nom}** — {p['price']}\nStock : **{new}**\nOuvre un ticket pour commander !", color=0x22C55E)
        await ch.send(content=f"<@&{rid}>" if rid else None, embed=emb,
                      allowed_mentions=discord.AllowedMentions(roles=True))
    await i.response.send_message(f"✅ Stock de **{nom}** : {new}", ephemeral=True)
    await refresh_shop(i.guild)


# ───────────────────────── Stats / blacklist ─────────────────────────
@tree.command(name="stats", description="Stats du shop (staff)")
@staff_only()
async def stats(i: discord.Interaction):
    done = q("SELECT COUNT(*) n FROM orders WHERE status='delivered'", one=True)["n"]
    opened = q("SELECT COUNT(*) n FROM orders WHERE status IN ('open','claimed')", one=True)["n"]
    v = q("SELECT COUNT(*) n, AVG(stars) m FROM vouches", one=True)
    top = q("SELECT product, COUNT(*) n FROM orders WHERE status='delivered' GROUP BY product ORDER BY n DESC LIMIT 3")
    emb = discord.Embed(title=f"📊 Stats {SHOP_NAME}", color=COLOR)
    emb.add_field(name="Commandes livrées", value=str(done))
    emb.add_field(name="Tickets ouverts", value=str(opened))
    emb.add_field(name="Vouchs", value=f"{v['n']} (moy. {v['m'] or 0:.2f}/5)")
    if top:
        emb.add_field(name="Top produits", value="\n".join(f"{t['product']} : {t['n']}" for t in top), inline=False)
    await i.response.send_message(embed=emb, ephemeral=True)


@tree.command(name="blacklist_ajouter", description="Blacklist un membre du shop (staff)")
@staff_only()
async def bl_add(i: discord.Interaction, membre: discord.Member, raison: str = "Aucune raison"):
    ex("INSERT OR REPLACE INTO blacklist(user_id,reason) VALUES(?,?)", (membre.id, raison))
    await i.response.send_message(f"⛔ {membre.mention} blacklist : {raison}", ephemeral=True)
    await send_log(i.guild, embed=discord.Embed(title="Blacklist", description=f"{membre.mention} par {i.user.mention}\n{raison}", color=0xEF4444))


@tree.command(name="blacklist_retirer", description="Retirer un membre de la blacklist (staff)")
@staff_only()
async def bl_remove(i: discord.Interaction, membre: discord.Member):
    ex("DELETE FROM blacklist WHERE user_id=?", (membre.id,))
    await i.response.send_message(f"✅ {membre.mention} retiré de la blacklist.", ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Il manque la variable d'environnement TOKEN")
    bot.run(TOKEN)
