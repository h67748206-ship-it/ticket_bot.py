"""
Xenity Shop - Bot de gestion de shop Discord v3 (un seul fichier)

Requirements : discord.py>=2.6  aiohttp
Variables d'environnement : TOKEN (obligatoire), PORT (Render), LTC_ADDRESS (optionnel),
                            LTC_API (optionnel), DB_PATH (optionnel, chemin de la base)

NOUVEAUTÉS v3 (+ TOUT se contrôle depuis /config : emplacements, produits, textes, vouchs, tickets, stats, blacklist, sauvegarde)
- Panneau de tickets "grand format" (bannière + titre + texte + menu de catégories), style Components V2
- /config : GRAND PANNEAU de configuration (rôles, ping des tickets, salons, apparence du panneau,
  catégories du menu, paiement LTC + test, options) -> plus besoin de taper des commandes
- /help : menu d'aide avec liste déroulante (adapté à ton niveau : membre / staff / admin)
- Vouchs fiabilisés : salon détecté automatiquement si besoin, erreurs affichées clairement,
  lien direct vers le vouch posté
- Tickets : boutons Claim / Paiement / Livré / Fermer + Ajouter / Retirer / Renommer,
  ping des rôles de ton choix (global + par catégorie), transcript envoyé en DM à la fermeture
- Optimisé : une seule connexion SQLite (WAL), config en cache mémoire, session HTTP partagée

Automatismes conservés : /creer_shop, paiement LTC automatique, auto-claim, fermeture auto, restock, stats.
"""
import io
import json
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
DB_PATH = os.getenv("DB_PATH", "shop.db")
COLOR = 0x8B5CF6
SHOP_NAME = "Xenity Shop"
HAS_V2 = hasattr(discord.ui, "LayoutView")  # Components V2 (discord.py >= 2.6)

VOUCH_COOLDOWN = 12 * 3600
DELIVERED_CLOSE_AFTER = 30 * 60
INACTIVE_CLOSE_AFTER = 48 * 3600
LTC_API = os.getenv("LTC_API", "https://litecoinspace.org/api").rstrip("/")  # format Esplora
COINGECKO = "https://api.coingecko.com/api/v3/simple/price?ids=litecoin&vs_currencies=eur"
PAY_WINDOW = 3 * 3600

DEFAULT_PRODUCTS = [
    ("Discord Nitro", "Sur demande", "Nitro livré rapidement."),
    ("Numéro virtuel", "Sur demande", "Numéros virtuels pour vérifications."),
    ("Mullvad VPN", "Sur demande", "Clés Mullvad VPN."),
]
# (label, emoji, description, produit lié)
DEFAULT_CATEGORIES = [
    ("Nitro / Deco", "💎", "Nitro et déco de profil", "Discord Nitro"),
    ("Numéro virtuel", "📱", "Numéros virtuels", "Numéro virtuel"),
    ("Mullvad VPN", "🔒", "Clés Mullvad VPN", "Mullvad VPN"),
    ("Problème / Report", "🚨", "Un souci ? Signale-le ici", None),
    ("Rôle Legit", "✅", "Demande ton rôle legit", None),
    ("Créer ton shop", "🛒", "Ouvre ton propre mini shop", None),
]
DEFAULT_PANEL_DESC = (
    "🕐 On te répond dès que possible — notre équipe est là pour t'aider.\n\n"
    "📩 Sélectionne une catégorie dans le menu ci-dessous pour ouvrir un ticket."
)
DEFAULT_TICKET_WELCOME = "Explique ce que tu veux, un vendeur arrive très vite.\nUtilise les boutons ci-dessous."
DEFAULT_DELIVERED = "Merci pour ton achat !\nLaisse un vouch avec le bouton ci-dessous (ou `/vouch`)."
DEFAULT_PAID = "Un vendeur va te livrer ta commande.\n\n⭐ Laisse ton vouch avec le bouton ci-dessous, il sera posté dans le salon des vouchs."
DEFAULT_WELCOME = ("Ici tu trouves nos produits digitaux.\n\n🛒 Pour acheter : ouvre un ticket dans {achat}\n"
                   "✅ Les avis de nos clients : {vouchs}\n📜 Lis le règlement avant de commander.")
DEFAULT_RULES = (
    "**1.** Respecte les membres et le staff.\n"
    "**2.** Pas de spam, pas de pub, pas d'insultes.\n"
    "**3.** Les achats se font **uniquement via un ticket**. Ne fais confiance à aucun message privé, "
    "seul le staff dans ton ticket peut te vendre quelque chose.\n"
    "**4.** Paie uniquement avec les moyens donnés par le bot dans ton ticket, en envoyant le montant exact.\n"
    "**5.** Garde tes preuves (captures, txid) en cas de problème.\n"
    "**6.** Après ta commande, laisse un vouch, ça aide tout le monde.\n\n"
    "_Le staff peut sanctionner en cas de non-respect._"
)


# ═════════════════════════ Base de données (1 connexion, WAL, cache config) ═════════════════════════
_db = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_db.row_factory = sqlite3.Row
_db.execute("PRAGMA journal_mode=WAL")
_db.execute("PRAGMA synchronous=NORMAL")
_cfg = {}


def q(sql, args=(), one=False):
    rows = _db.execute(sql, args).fetchall()
    if one:
        return rows[0] if rows else None
    return rows


def ex(sql, args=()):
    return _db.execute(sql, args).lastrowid


def cfg_get(key, default=None):
    return _cfg.get(key, default)


def cfg_set(key, value):
    _cfg[key] = str(value)
    ex("INSERT OR REPLACE INTO config(key,value) VALUES(?,?)", (key, str(value)))


def cfg_del(key):
    _cfg.pop(key, None)
    ex("DELETE FROM config WHERE key=?", (key,))


def cfg_int(key):
    v = _cfg.get(key)
    return int(v) if v and v.isdigit() else None


def id_list(key):
    return [int(x) for x in (cfg_get(key) or "").split(",") if x.strip().isdigit()]


def cfg_channel(guild, key):
    cid = cfg_int(key)
    return guild.get_channel(cid) if cid else None


def init_db():
    _db.executescript(
        """
        CREATE TABLE IF NOT EXISTS config(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS products(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE,
            price TEXT, stock INTEGER DEFAULT -1, description TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS categories(
            id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT, emoji TEXT,
            description TEXT DEFAULT '', product TEXT, ping_role INTEGER, pos INTEGER DEFAULT 0);
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
        try:  # migration depuis une ancienne version
            _db.execute(f"ALTER TABLE orders ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    _db.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_orders_channel ON orders(channel_id);
        CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id, status);
        CREATE INDEX IF NOT EXISTS idx_vouch_target ON vouches(target_id);
        """
    )
    _cfg.clear()
    for r in q("SELECT key,value FROM config"):
        _cfg[r["key"]] = r["value"]
    if cfg_get("seeded") != "1":  # produits et catégories par défaut, une seule fois
        for n, p, d in DEFAULT_PRODUCTS:
            ex("INSERT OR IGNORE INTO products(name,price,stock,description) VALUES(?,?,-1,?)", (n, p, d))
        if not q("SELECT 1 FROM categories LIMIT 1", one=True):
            for pos, (label, emoji, desc, prod) in enumerate(DEFAULT_CATEGORIES):
                ex("INSERT INTO categories(label,emoji,description,product,pos) VALUES(?,?,?,?,?)",
                   (label, emoji, desc, prod, pos))
        cfg_set("seeded", "1")


init_db()


# ═════════════════════════ Helpers ═════════════════════════
def staff_role_ids():
    ids = set(id_list("staff_roles"))
    if cfg_int("staff_role"):
        ids.add(cfg_int("staff_role"))
    return ids


def staff_roles(guild):
    return [r for r in (guild.get_role(x) for x in staff_role_ids()) if r]


def is_staff(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    ids = staff_role_ids()
    return bool(ids) and any(r.id in ids for r in member.roles)


def cfg_num(key, default):
    try:
        return float(cfg_get(key, default))
    except (TypeError, ValueError):
        return float(default)


def render(text, **kw):
    """Remplace {variables} dans un texte personnalisé (sans planter sur les autres accolades)."""
    for k, v in kw.items():
        text = text.replace("{" + k + "}", str(v))
    return text


def is_blacklisted(user_id: int) -> bool:
    return q("SELECT 1 FROM blacklist WHERE user_id=?", (user_id,), one=True) is not None


def order_of(channel_id: int):
    return q("SELECT * FROM orders WHERE channel_id=?", (channel_id,), one=True)


def stars_str(n: int) -> str:
    return "⭐" * n + "☆" * (5 - n)


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "ticket"


def clean_emoji(text: str):
    """Garde un emoji unicode ou un emoji custom <:nom:id>, sinon None (évite les erreurs Discord)."""
    t = (text or "").strip()
    if not t:
        return None
    if re.fullmatch(r"<a?:\w+:\d+>", t):
        return t
    if len(t) <= 8 and not re.search(r"[A-Za-z0-9]", t):
        return t
    return None


def accent() -> int:
    try:
        return int((cfg_get("panel_color") or "").lstrip("#"), 16)
    except ValueError:
        return COLOR


_session = None


def session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), headers={"User-Agent": "XenityShopBot"})
    return _session


async def http_json(url):
    async with session().get(url) as r:
        r.raise_for_status()
        return await r.json(content_type=None)


async def send_log(guild, content=None, embed=None, file=None):
    ch = cfg_channel(guild, "logs_channel")
    if ch:
        try:
            await ch.send(content=content, embed=embed, file=file)
        except discord.HTTPException:
            pass


# ═════════════════════════ Paiement LTC automatique ═════════════════════════
_rate = {"v": None, "t": 0.0}


def ltc_address():
    return cfg_get("ltc_address") or os.getenv("LTC_ADDRESS")


def parse_eur(text):
    m = re.search(r"(\d+(?:[.,]\d+)?)", text or "")
    return float(m.group(1).replace(",", ".")) if m else None


async def ltc_rate():
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
    if not ltc_address() or not prod:
        return
    eur = parse_eur(prod["price"])
    if not eur:
        return await ch.send("💳 Un vendeur va te donner le montant à payer en LTC dans ce ticket.")
    try:
        await make_payment(order_id, eur)
    except Exception as e:
        print("paiement:", e)
        return await ch.send("⚠️ Impossible de calculer le montant LTC pour l'instant, un vendeur s'en occupe.")
    await ch.send(embed=payment_embed(q("SELECT * FROM orders WHERE id=?", (order_id,), one=True)))


async def ltc_selftest():
    """Teste l'adresse, le cours LTC et l'API blockchain. Retourne un texte lisible."""
    out = []
    addr = ltc_address()
    out.append(f"Adresse LTC : {'✅ ' + addr if addr else '❌ pas configurée'}")
    try:
        out.append(f"Cours LTC (CoinGecko) : ✅ {await ltc_rate():.2f} €")
    except Exception as e:
        out.append(f"Cours LTC (CoinGecko) : ❌ {type(e).__name__}: {e}")
    if addr:
        try:
            txs = await http_json(f"{LTC_API}/address/{addr}/txs")
            out.append(f"API blockchain : ✅ {len(txs)} transaction(s) vue(s) sur l'adresse")
        except Exception as e:
            out.append(f"API blockchain : ❌ {type(e).__name__}: {e}\n_(change `LTC_API` sur Render si ça persiste)_")
    out.append(f"\nAPI utilisée : `{LTC_API}`")
    return "\n".join(out)


# ═════════════════════════ Apparence : panneau + catalogue ═════════════════════════
def catalogue_embed():
    rows = q("SELECT * FROM products ORDER BY name")
    emb = discord.Embed(title=f"🛍️ Catalogue {SHOP_NAME}", color=accent(), timestamp=discord.utils.utcnow())
    if not rows:
        emb.description = "Le catalogue est vide pour l'instant."
    for p in rows:
        stock = "♾️ Disponible" if p["stock"] < 0 else ("❌ Rupture" if p["stock"] == 0 else f"✅ {p['stock']} en stock")
        emb.add_field(name=f"{p['name']} — {p['price']}", value=f"{p['description'] or '—'}\n{stock}", inline=False)
    emb.set_footer(text="Pour commander : ouvre un ticket dans le salon d'achat • Mis à jour")
    return emb


class PanelSelect(discord.ui.Select):
    def __init__(self):
        opts = []
        for c in q("SELECT * FROM categories ORDER BY pos, id LIMIT 25"):
            opts.append(discord.SelectOption(
                label=c["label"][:100], value=str(c["id"]),
                description=(c["description"] or "")[:100] or None, emoji=clean_emoji(c["emoji"])))
        if not opts:
            opts = [discord.SelectOption(label="Aucune catégorie (voir /config)", value="0")]
        super().__init__(placeholder="Sélectionne une catégorie…", custom_id="shop:panel", options=opts)

    async def callback(self, i: discord.Interaction):
        await create_ticket(i, int(self.values[0]))
        try:  # remet le menu à zéro pour pouvoir re-choisir la même catégorie
            await i.message.edit(view=panel_view())
        except discord.HTTPException:
            pass


class PanelView(discord.ui.View):  # version embed classique (si discord.py < 2.6)
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(PanelSelect())


if HAS_V2:
    class PanelLayout(discord.ui.LayoutView):
        """Grand panneau : bannière, titre, texte, menu de catégories (comme ta capture)."""

        def __init__(self):
            super().__init__(timeout=None)
            c = discord.ui.Container(accent_colour=accent())
            img = cfg_get("panel_image")
            if img:
                c.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(img)))
            c.add_item(discord.ui.TextDisplay(f"## {cfg_get('panel_title') or SHOP_NAME}"))
            c.add_item(discord.ui.TextDisplay(cfg_get("panel_desc") or DEFAULT_PANEL_DESC))
            c.add_item(discord.ui.Separator())
            row = discord.ui.ActionRow()
            row.add_item(PanelSelect())
            c.add_item(row)
            if cfg_get("panel_footer"):
                c.add_item(discord.ui.TextDisplay(f"-# {cfg_get('panel_footer')}"))
            self.add_item(c)


def panel_view():
    return PanelLayout() if HAS_V2 else PanelView()


def panel_kwargs():
    if HAS_V2:
        return {"view": PanelLayout()}
    emb = discord.Embed(title=cfg_get("panel_title") or SHOP_NAME,
                        description=cfg_get("panel_desc") or DEFAULT_PANEL_DESC, color=accent())
    if cfg_get("panel_image"):
        emb.set_image(url=cfg_get("panel_image"))
    if cfg_get("panel_footer"):
        emb.set_footer(text=cfg_get("panel_footer"))
    return {"embed": emb, "view": PanelView()}


async def _upsert(guild, chan_key, msg_key, **kwargs):
    """Modifie le message enregistré, ou le recrée s'il a disparu / ne peut plus être édité."""
    ch = cfg_channel(guild, chan_key)
    if not ch:
        return None
    mid, msg = cfg_int(msg_key), None
    if mid:
        try:
            msg = await ch.fetch_message(mid)
            await msg.edit(**kwargs)
            return msg
        except discord.NotFound:
            pass
        except discord.HTTPException:
            try:
                await msg.delete()
            except Exception:
                pass
    try:
        msg = await ch.send(**kwargs)
        cfg_set(msg_key, msg.id)
        return msg
    except discord.HTTPException as e:
        print(f"_upsert {chan_key}:", e)
        return None


async def refresh_shop(guild):
    """Remet à jour le catalogue et le panneau de tickets."""
    await _upsert(guild, "catalogue_channel", "catalogue_msg", embed=catalogue_embed())
    await _upsert(guild, "panel_channel", "panel_msg", **panel_kwargs())


async def post_panel(channel):
    cfg_set("panel_channel", channel.id)
    cfg_del("panel_msg")
    msg = await channel.send(**panel_kwargs())
    cfg_set("panel_msg", msg.id)
    return msg


# ═════════════════════════ Vouch ═════════════════════════
def vouch_channel(guild):
    """Salon des vouchs configuré, sinon détecté par son nom (contient 'vouch') et enregistré."""
    ch = cfg_channel(guild, "vouch_channel")
    if ch:
        return ch
    ch = discord.utils.find(lambda c: "vouch" in c.name.lower(), guild.text_channels)
    if ch:
        cfg_set("vouch_channel", ch.id)
    return ch


def check_vouch(member: discord.Member):
    if is_blacklisted(member.id):
        return "Tu es blacklist, tu ne peux pas vouch."
    if cfg_get("vouch_client_only", "0") == "1":
        rid = cfg_int("client_role")
        if rid and not any(r.id == rid for r in member.roles):
            return "Il faut avoir acheté au shop (rôle client) pour laisser un vouch."
    last = q("SELECT ts FROM vouches WHERE author_id=? ORDER BY ts DESC LIMIT 1", (member.id,), one=True)
    cd = cfg_num("vouch_cooldown_h", 12) * 3600
    if cd and last and time.time() - last["ts"] < cd:
        reste = int((cd - (time.time() - last["ts"])) // 3600) + 1
        return f"Tu as déjà vouch récemment, réessaie dans ~{reste}h."
    return None


async def post_vouch(guild, author, target_id, stars, text, product=None):
    """Enregistre le vouch puis le poste. Retourne (id, message_posté ou None, problème ou None)."""
    vid = ex("INSERT INTO vouches(author_id,target_id,stars,text,product,ts) VALUES(?,?,?,?,?,?)",
             (author.id, target_id or 0, stars, text, product, int(time.time())))
    total = q("SELECT COUNT(*) n FROM vouches WHERE target_id=?", (target_id or 0,), one=True)["n"]
    emb = discord.Embed(title=f"✅ Nouveau vouch #{vid}", description=f"{stars_str(stars)}\n\n> {text}",
                        color=accent(), timestamp=discord.utils.utcnow())
    emb.add_field(name="De", value=author.mention)
    emb.add_field(name="Vendeur", value=f"<@{target_id}>" if target_id else SHOP_NAME)
    if product:
        emb.add_field(name="Produit", value=product)
    emb.set_thumbnail(url=author.display_avatar.url)
    emb.set_footer(text=f"{SHOP_NAME} • {total} vouch(s)")
    ch = vouch_channel(guild)
    if not ch:
        return vid, None, "aucun salon des vouchs configuré (un admin doit faire `/config` → Emplacements)"
    try:
        return vid, await ch.send(embed=emb), None
    except discord.HTTPException as e:
        return vid, None, f"le bot ne peut pas écrire dans {ch.mention} ({e.text or e.status}) — vérifie ses permissions"


def vouch_feedback(vid, sent, problem):
    if sent:
        return f"✅ Vouch #{vid} posté dans {sent.channel.mention}, [voir le message]({sent.jump_url}). Merci ! ❤️"
    return f"⚠️ Vouch #{vid} enregistré, mais pas posté : {problem}."


class VouchModal(discord.ui.Modal, title="Laisse ton vouch"):
    note = discord.ui.TextInput(label="Note (1 à 5)", max_length=1, placeholder="5")
    avis = discord.ui.TextInput(label="Ton avis", style=discord.TextStyle.paragraph, max_length=500,
                                placeholder="Rapide, sérieux, merci !")

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
        await i.response.defer(ephemeral=True)
        vid, sent, problem = await post_vouch(i.guild, i.user, self.target_id, int(self.note.value),
                                              self.avis.value, self.product)
        await i.followup.send(vouch_feedback(vid, sent, problem), ephemeral=True)


class VouchView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Laisser un vouch", emoji="⭐", style=discord.ButtonStyle.success, custom_id="shop:vouchbtn")
    async def vouch(self, i: discord.Interaction, b: discord.ui.Button):
        o = order_of(i.channel.id)
        if o and o["user_id"] != i.user.id and not is_staff(i.user):
            return await i.response.send_message("Seul l'acheteur peut vouch ici.", ephemeral=True)
        await i.response.send_modal(VouchModal(o["staff_id"] if o and o["staff_id"] else 0, o["product"] if o else None))


# ═════════════════════════ Tickets ═════════════════════════
async def transcript_bytes(channel: discord.TextChannel) -> bytes:
    lines = []
    async for m in channel.history(limit=None, oldest_first=True):
        extra = "".join(f" [{a.url}]" for a in m.attachments)
        lines.append(f"[{m.created_at:%Y-%m-%d %H:%M}] {m.author}: {m.content}{extra}")
    return "\n".join(lines).encode()


async def close_channel(channel: discord.TextChannel, reason: str):
    o = order_of(channel.id)
    try:
        data = await transcript_bytes(channel)
        name = f"{channel.name}.txt"
        await send_log(channel.guild, content=f"📁 Transcript de `{channel.name}` ({reason})",
                       file=discord.File(io.BytesIO(data), filename=name))
        if o and cfg_get("dm_transcript", "1") == "1":
            try:
                user = await bot.fetch_user(o["user_id"])
                await user.send(f"📁 Voici le transcript de ton ticket chez {SHOP_NAME}.",
                                file=discord.File(io.BytesIO(data), filename=name))
            except discord.HTTPException:
                pass
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


async def create_ticket(i: discord.Interaction, cat_id: int):
    g = i.guild
    cat = q("SELECT * FROM categories WHERE id=?", (cat_id,), one=True)
    if not cat:
        return await i.response.send_message("Cette catégorie n'existe plus.", ephemeral=True)
    if is_blacklisted(i.user.id):
        return await i.response.send_message("Tu es blacklist du shop.", ephemeral=True)
    prod = q("SELECT * FROM products WHERE name=?", (cat["product"],), one=True) if cat["product"] else None
    if prod and prod["stock"] == 0:
        return await i.response.send_message(f"❌ **{prod['name']}** est en rupture de stock pour le moment.", ephemeral=True)
    mine = [r["channel_id"] for r in q("SELECT channel_id FROM orders WHERE user_id=? AND status IN ('open','claimed') AND channel_id!=0",
                                       (i.user.id,)) if g.get_channel(r["channel_id"])]
    limit = int(cfg_num("max_tickets", 1))
    if limit and len(mine) >= limit:
        return await i.response.send_message("Tu as déjà un ticket ouvert : " + " ".join(f"<#{c}>" for c in mine), ephemeral=True)

    await i.response.defer(ephemeral=True)
    ow = {
        g.default_role: discord.PermissionOverwrite(view_channel=False),
        i.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, read_message_history=True),
        g.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
    }
    for srole in staff_roles(g):
        ow[srole] = discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, read_message_history=True)
    parent = g.get_channel(cfg_int("ticket_category") or 0)
    try:
        ch = await g.create_text_channel(f"{slug(cat['label'])}-{slug(i.user.name)}"[:90], category=parent,
                                         overwrites=ow, topic=f"Ticket de {i.user} ({i.user.id}) • {cat['label']}")
    except discord.HTTPException as e:
        return await i.followup.send(f"❌ Impossible de créer le ticket : {e.text or e}", ephemeral=True)
    oid = ex("INSERT INTO orders(user_id,channel_id,product,ts) VALUES(?,?,?,?)",
             (i.user.id, ch.id, prod["name"] if prod else cat["label"], int(time.time())))

    emb = discord.Embed(
        title=f"🎫 Ticket #{oid} — {cat['label']}",
        description=render(cfg_get("ticket_welcome") or DEFAULT_TICKET_WELCOME, user=i.user.mention, category=cat["label"],
                           product=prod["name"] if prod else cat["label"], shop=SHOP_NAME),
        color=accent(), timestamp=discord.utils.utcnow(),
    )
    emb.add_field(name="Ouvert par", value=i.user.mention)
    if prod:
        emb.add_field(name="Produit", value=f"{prod['name']} — {prod['price']}")
    emb.set_footer(text=SHOP_NAME)
    roles = []
    if cfg_get("ping_on_open", "1") == "1":
        ids = set(id_list("ticket_ping_roles"))
        if cat["ping_role"]:
            ids.add(cat["ping_role"])
        roles = [g.get_role(x) for x in ids if g.get_role(x)]
    content = " ".join([i.user.mention] + [r.mention for r in roles])
    await ch.send(content=content, embed=emb, view=TicketControls(),
                  allowed_mentions=discord.AllowedMentions(users=[i.user], roles=roles, everyone=False))
    await send_auto_payment(ch, oid, prod)
    await send_log(g, embed=discord.Embed(title="Ticket ouvert", description=f"{i.user.mention} → {ch.mention} ({cat['label']})", color=COLOR))
    await i.followup.send(f"Ticket créé : {ch.mention}", ephemeral=True)


class MemberPicker(discord.ui.View):
    def __init__(self, add: bool):
        super().__init__(timeout=120)
        self.add = add

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Choisis le membre…", min_values=1, max_values=1)
    async def pick(self, i: discord.Interaction, select: discord.ui.UserSelect):
        m = select.values[0]
        o = order_of(i.channel.id)
        if not self.add and o and m.id == o["user_id"]:
            return await i.response.edit_message(content="Tu ne peux pas retirer le client du ticket.", view=None)
        try:
            if self.add:
                await i.channel.set_permissions(m, view_channel=True, send_messages=True, read_message_history=True, attach_files=True)
            else:
                await i.channel.set_permissions(m, overwrite=None)
        except Exception as e:
            return await i.response.edit_message(content=f"❌ Impossible : {e}", view=None)
        await i.response.edit_message(content=f"✅ {m.mention} {'ajouté au' if self.add else 'retiré du'} ticket.", view=None)
        if self.add:
            await i.channel.send(f"➕ {m.mention} a été ajouté au ticket par {i.user.mention}.",
                                 allowed_mentions=discord.AllowedMentions(users=[m]))


class RenameModal(discord.ui.Modal, title="Renommer le ticket"):
    nom = discord.ui.TextInput(label="Nouveau nom", max_length=80)

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        try:
            await i.channel.edit(name=slug(self.nom.value)[:90])
            await i.followup.send("✅ Ticket renommé.", ephemeral=True)
        except discord.HTTPException as e:
            await i.followup.send(f"❌ {e.text or e}", ephemeral=True)


class TicketControls(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim", emoji="🙋", style=discord.ButtonStyle.primary, custom_id="shop:claim", row=0)
    async def claim(self, i: discord.Interaction, b: discord.ui.Button):
        if not is_staff(i.user):
            return await i.response.send_message("Réservé au staff.", ephemeral=True)
        ex("UPDATE orders SET status='claimed', staff_id=? WHERE channel_id=?", (i.user.id, i.channel.id))
        await i.response.send_message(embed=discord.Embed(description=f"🙋 Ticket pris en charge par {i.user.mention}", color=accent()))

    @discord.ui.button(label="Paiement", emoji="💳", style=discord.ButtonStyle.secondary, custom_id="shop:pay", row=0)
    async def pay(self, i: discord.Interaction, b: discord.ui.Button):
        o = order_of(i.channel.id)
        if o and o["pay_state"] == 2:
            return await i.response.send_message("✅ Cette commande est déjà payée.", ephemeral=True)
        if o and o["ltc_amount"] and ltc_address():
            return await i.response.send_message(embed=payment_embed(o))
        txt = cfg_get("paiement", "Crypto (BTC / LTC / USDT) et PayPal acceptés.\nDemande l'adresse au vendeur.")
        await i.response.send_message(embed=discord.Embed(title="💳 Moyens de paiement", description=txt, color=accent()))

    @discord.ui.button(label="Livré", emoji="✅", style=discord.ButtonStyle.success, custom_id="shop:deliver", row=0)
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
        buyer, rid = None, cfg_int("client_role")
        try:
            buyer = await i.guild.fetch_member(o["user_id"])
            if rid and i.guild.get_role(rid):
                await buyer.add_roles(i.guild.get_role(rid), reason="Commande livrée")
        except discord.HTTPException:
            pass
        auto = cfg_get("auto_close_delivered", "1") == "1"
        emb = discord.Embed(
            title="✅ Commande livrée",
            description=render(cfg_get("delivered_text") or DEFAULT_DELIVERED, user=f"<@{o['user_id']}>", product=o["product"], shop=SHOP_NAME)
                        + (f"\n_Ce ticket sera fermé automatiquement dans {int(cfg_num('delivered_close_min', 30))} minutes._" if auto else ""),
            color=0x22C55E)
        await i.response.send_message(content=f"<@{o['user_id']}>", embed=emb, view=VouchView(),
                                      allowed_mentions=discord.AllowedMentions(users=True))
        if buyer:
            try:
                await buyer.send(f"✅ Ta commande **{o['product']}** chez {SHOP_NAME} est livrée ! Pense à laisser un vouch 🙏")
            except discord.HTTPException:
                pass
        await send_log(i.guild, embed=discord.Embed(
            title="Commande livrée", description=f"#{o['id']} • {o['product']} • <@{o['user_id']}> • par {i.user.mention}", color=0x22C55E))
        sales = cfg_channel(i.guild, "sales_channel")
        if sales:
            try:
                await sales.send(embed=discord.Embed(
                    title="🛒 Nouvelle vente", description=f"**{o['product']}** livré à <@{o['user_id']}> par {i.user.mention}",
                    color=0x22C55E, timestamp=discord.utils.utcnow()), allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass
        await refresh_shop(i.guild)

    @discord.ui.button(label="Fermer", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="shop:close", row=0)
    async def close(self, i: discord.Interaction, b: discord.ui.Button):
        o = order_of(i.channel.id)
        if not (is_staff(i.user) or (o and o["user_id"] == i.user.id)):
            return await i.response.send_message("Tu ne peux pas fermer ce ticket.", ephemeral=True)
        await i.response.send_message("🔒 Fermeture du ticket…")
        await close_channel(i.channel, f"fermé par {i.user}")

    @discord.ui.button(label="Ajouter", emoji="➕", style=discord.ButtonStyle.secondary, custom_id="shop:add", row=1)
    async def add_member(self, i: discord.Interaction, b: discord.ui.Button):
        if not is_staff(i.user):
            return await i.response.send_message("Réservé au staff.", ephemeral=True)
        await i.response.send_message("Qui veux-tu ajouter ?", view=MemberPicker(True), ephemeral=True)

    @discord.ui.button(label="Retirer", emoji="➖", style=discord.ButtonStyle.secondary, custom_id="shop:remove", row=1)
    async def remove_member(self, i: discord.Interaction, b: discord.ui.Button):
        if not is_staff(i.user):
            return await i.response.send_message("Réservé au staff.", ephemeral=True)
        await i.response.send_message("Qui veux-tu retirer ?", view=MemberPicker(False), ephemeral=True)

    @discord.ui.button(label="Renommer", emoji="✏️", style=discord.ButtonStyle.secondary, custom_id="shop:rename", row=1)
    async def rename(self, i: discord.Interaction, b: discord.ui.Button):
        if not is_staff(i.user):
            return await i.response.send_message("Réservé au staff.", ephemeral=True)
        await i.response.send_modal(RenameModal())


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
    sroles = staff_roles(g)
    emb = discord.Embed(
        title="✅ Paiement reçu !",
        description=f"**{o['ltc_amount'] / 1e8:.8f} LTC** confirmés sur la blockchain.\n"
                    + render(cfg_get("paid_text") or DEFAULT_PAID, user=f"<@{o['user_id']}>", product=o["product"], shop=SHOP_NAME),
        color=0x22C55E)
    emb.add_field(name="Transaction", value=f"[Voir sur la blockchain](https://blockchair.com/litecoin/transaction/{txid})")
    await ch.send(content=" ".join([f"<@{o['user_id']}>"] + [r.mention for r in sroles]), embed=emb, view=VouchView(),
                  allowed_mentions=discord.AllowedMentions(users=True, roles=sroles))
    await send_log(g, embed=discord.Embed(
        title="💰 Paiement LTC reçu",
        description=f"Ticket {ch.mention} • <@{o['user_id']}> • {o['product']}\n{o['ltc_amount'] / 1e8:.8f} LTC ({o['ltc_eur']:.2f} €)\n`{txid}`",
        color=0x22C55E))


# ═════════════════════════ GRAND PANNEAU DE CONFIG (/config) ═════════════════════════
def fmt_role(g, rid):
    r = g.get_role(int(rid)) if rid else None
    return r.mention if r else "❌ non défini"


def fmt_roles(g, key):
    rs = [g.get_role(x) for x in id_list(key)]
    return " ".join(r.mention for r in rs if r) or "❌ aucun"


def fmt_chan(g, key):
    c = cfg_channel(g, key)
    return c.mention if c else "❌ non défini"


async def go(i: discord.Interaction, view):
    await i.response.edit_message(embed=view.embed(i.guild), view=view)


class ConfigView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=900)

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if i.user.guild_permissions.administrator:
            return True
        await i.response.send_message("Réservé aux admins.", ephemeral=True)
        return False

    def embed(self, g):
        raise NotImplementedError

    async def refresh(self, i: discord.Interaction):
        await i.response.edit_message(embed=self.embed(i.guild), view=self)

    def add_toggles(self, opts, row=0):
        """Ajoute des boutons on/off. opts = [(clé, texte, valeur par défaut "0"/"1")]. La page doit pouvoir se recréer sans argument."""
        for n, (key, label, default) in enumerate(opts):
            on = cfg_get(key, default) == "1"
            b = discord.ui.Button(label=f"{'✅' if on else '❌'} {label}"[:80], row=row + n // 2,
                                  style=discord.ButtonStyle.success if on else discord.ButtonStyle.secondary)
            b.callback = self._toggle_cb(key, default)
            self.add_item(b)

    def _toggle_cb(self, key, default):
        async def cb(i: discord.Interaction):
            cfg_set(key, "0" if cfg_get(key, default) == "1" else "1")
            await go(i, type(self)())
        return cb


class Page(ConfigView):
    def back_view(self):
        return MainView()

    @discord.ui.button(label="Retour", emoji="↩️", style=discord.ButtonStyle.secondary, row=4)
    async def back(self, i: discord.Interaction, b: discord.ui.Button):
        await go(i, self.back_view())


def fmt_staff(g):
    return " ".join(r.mention for r in staff_roles(g)) or "❌ aucun"


class MainView(ConfigView):
    def embed(self, g):
        nc = q("SELECT COUNT(*) n FROM categories", one=True)["n"]
        npd = q("SELECT COUNT(*) n FROM products", one=True)["n"]
        emb = discord.Embed(
            title=f"⚙️ Configuration de {SHOP_NAME}", color=accent(),
            description="Tout se règle ici, les changements s'appliquent tout de suite. Choisis une section avec les boutons.")
        emb.add_field(name="🎭 Rôles", value=(f"Staff : {fmt_staff(g)}\nPing tickets : {fmt_roles(g, 'ticket_ping_roles')}\n"
                                              f"Client : {fmt_role(g, cfg_get('client_role'))}\nRestock : {fmt_role(g, cfg_get('ping_role'))}"))
        emb.add_field(name="📍 Emplacements", value="\n".join(
            [f"{nm} : {fmt_chan(g, ck)}" for nm, ck, _ in POSTS.values()]
            + [f"{nm.split(' (')[0]} : {fmt_chan(g, ck)}" for nm, ck in FEEDS.values()]))
        emb.add_field(name="🛍️ Boutique", value=f"{npd} produit(s) • {nc} catégorie(s)\nLTC : {'✅ configuré' if ltc_address() else '❌ non configuré'}",
                      inline=False)
        return emb

    @discord.ui.button(label="Rôles", emoji="🎭", style=discord.ButtonStyle.primary, row=0)
    async def roles(self, i, b):
        await go(i, RolesPage())

    @discord.ui.button(label="Emplacements", emoji="📍", style=discord.ButtonStyle.primary, row=0)
    async def places(self, i, b):
        await go(i, PlacesPage())

    @discord.ui.button(label="Apparence", emoji="🎨", style=discord.ButtonStyle.primary, row=0)
    async def look(self, i, b):
        await go(i, LookPage())

    @discord.ui.button(label="Textes", emoji="💬", style=discord.ButtonStyle.primary, row=0)
    async def texts(self, i, b):
        await go(i, TextsPage())

    @discord.ui.button(label="Catégories", emoji="🗂️", style=discord.ButtonStyle.primary, row=0)
    async def cats(self, i, b):
        await go(i, CategoriesPage())

    @discord.ui.button(label="Produits", emoji="🛍️", style=discord.ButtonStyle.primary, row=1)
    async def products(self, i, b):
        await go(i, ProductsPage())

    @discord.ui.button(label="Paiement", emoji="💳", style=discord.ButtonStyle.primary, row=1)
    async def pay(self, i, b):
        await go(i, PaymentPage())

    @discord.ui.button(label="Vouchs", emoji="⭐", style=discord.ButtonStyle.primary, row=1)
    async def vouchs(self, i, b):
        await go(i, VouchPage())

    @discord.ui.button(label="Tickets", emoji="🎫", style=discord.ButtonStyle.primary, row=1)
    async def tickets(self, i, b):
        await go(i, TicketsPage())

    @discord.ui.button(label="Stats", emoji="📊", style=discord.ButtonStyle.primary, row=1)
    async def stats(self, i, b):
        await go(i, StatsPage())

    @discord.ui.button(label="Blacklist", emoji="⛔", style=discord.ButtonStyle.secondary, row=2)
    async def blacklist(self, i, b):
        await go(i, BlacklistPage())

    @discord.ui.button(label="Sauvegarde", emoji="💾", style=discord.ButtonStyle.secondary, row=2)
    async def backup(self, i, b):
        await go(i, BackupPage())

    @discord.ui.button(label="Créer tout le shop", emoji="🏗️", style=discord.ButtonStyle.success, row=2)
    async def build(self, i: discord.Interaction, b):
        await do_setup(i, full=True)


class RolesPage(Page):
    def embed(self, g):
        emb = discord.Embed(title="🎭 Rôles", color=accent(),
                            description="Sélectionne les rôles dans les menus ci-dessous.")
        emb.add_field(name="👮 Staff (voit et gère les tickets)", value=fmt_staff(g), inline=False)
        emb.add_field(name="🔔 Rôles pingés à l'ouverture d'un ticket", value=fmt_roles(g, "ticket_ping_roles"), inline=False)
        emb.add_field(name="🧾 Rôle client (donné au paiement / à la livraison)", value=fmt_role(g, cfg_get("client_role")), inline=False)
        emb.add_field(name="📦 Rôle ping restock", value=fmt_role(g, cfg_get("ping_role")), inline=False)
        return emb

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="👮 Rôles staff (jusqu'à 5)", min_values=1, max_values=5, row=0)
    async def staff(self, i, sel):
        cfg_set("staff_roles", ",".join(str(r.id) for r in sel.values))
        cfg_del("staff_role")
        await self.refresh(i)

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="🔔 Rôles à ping à l'ouverture d'un ticket", min_values=0, max_values=5, row=1)
    async def ping(self, i, sel):
        cfg_set("ticket_ping_roles", ",".join(str(r.id) for r in sel.values))
        await self.refresh(i)

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="🧾 Rôle client", row=2)
    async def client(self, i, sel):
        cfg_set("client_role", sel.values[0].id)
        await self.refresh(i)

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="📦 Rôle ping restock", row=3)
    async def restock(self, i, sel):
        cfg_set("ping_role", sel.values[0].id)
        await self.refresh(i)


POSTS = {  # élément -> (nom, clé du salon, clé du message)
    "panel": ("🎫 Panneau de tickets", "panel_channel", "panel_msg"),
    "catalogue": ("📦 Catalogue", "catalogue_channel", "catalogue_msg"),
    "welcome": ("👋 Message de bienvenue", "welcome_channel", "welcome_msg"),
    "rules": ("📜 Règlement", "rules_channel", "rules_msg"),
}
FEEDS = {  # élément -> (nom, clé du salon)
    "vouch": ("✅ Vouchs", "vouch_channel"),
    "sales": ("💰 Ventes (annonce à chaque livraison)", "sales_channel"),
    "logs": ("📁 Logs (transcripts, paiements)", "logs_channel"),
    "annonces": ("📢 Annonces restock", "annonces_channel"),
    "tickets": ("🎫 Catégorie des tickets", "ticket_category"),
}


def rules_embed():
    return discord.Embed(title="📜 Règlement", description=cfg_get("rules_text") or DEFAULT_RULES, color=accent())


def welcome_embed(g):
    achat = f"<#{cfg_int('panel_channel')}>" if cfg_int("panel_channel") else "le salon d'achat"
    vouchs = f"<#{cfg_int('vouch_channel')}>" if cfg_int("vouch_channel") else "le salon des vouchs"
    return discord.Embed(title=f"👋 Bienvenue sur {SHOP_NAME} !", color=accent(),
                         description=render(cfg_get("welcome_text") or DEFAULT_WELCOME, shop=SHOP_NAME, achat=achat, vouchs=vouchs))


async def publish(g, item):
    """Poste ou met à jour le message d'un élément dans son salon."""
    if item == "panel":
        await _upsert(g, "panel_channel", "panel_msg", **panel_kwargs())
    elif item == "catalogue":
        await _upsert(g, "catalogue_channel", "catalogue_msg", embed=catalogue_embed())
    elif item == "welcome":
        await _upsert(g, "welcome_channel", "welcome_msg", embed=welcome_embed(g))
    elif item == "rules":
        await _upsert(g, "rules_channel", "rules_msg", embed=rules_embed())


async def move_post(g, item, ch):
    """Déplace un message du bot dans un autre salon (supprime l'ancien et republie)."""
    _, ckey, mkey = POSTS[item]
    old, mid = cfg_channel(g, ckey), cfg_int(mkey)
    if old and mid and old.id != ch.id:
        try:
            await (await old.fetch_message(mid)).delete()
        except discord.HTTPException:
            pass
    if old is None or old.id != ch.id:
        cfg_del(mkey)
    cfg_set(ckey, ch.id)
    await publish(g, item)


class PlacesPage(Page):
    def __init__(self):
        super().__init__()
        self.item = None
        opts = [discord.SelectOption(label=v[0][:100], value=f"post:{k}", description="Message posté dans le salon choisi")
                for k, v in POSTS.items()]
        opts += [discord.SelectOption(label=v[0][:100], value=f"feed:{k}", description="Salon utilisé par le bot")
                 for k, v in FEEDS.items()]
        sel = discord.ui.Select(placeholder="1️⃣ Que veux-tu placer ?", options=opts, row=0)

        async def pick(i: discord.Interaction):
            self.item = i.data["values"][0]
            await self.refresh(i)
        sel.callback = pick
        self.add_item(sel)

    def embed(self, g):
        emb = discord.Embed(
            title="📍 Emplacements", color=accent(),
            description="Choisis **ce que tu veux placer** (menu 1), puis **le salon** (menu 2). "
                        "Les messages du bot sont déplacés et republiés tout de suite.")
        emb.add_field(name="Messages du bot", value="\n".join(f"{nm} → {fmt_chan(g, ck)}" for nm, ck, _ in POSTS.values()), inline=False)
        emb.add_field(name="Salons utilisés", value="\n".join(f"{nm} → {fmt_chan(g, ck)}" for nm, ck in FEEDS.values()), inline=False)
        if self.item:
            kind, key = self.item.split(":")
            emb.add_field(name="Sélection", value=f"{(POSTS if kind == 'post' else FEEDS)[key][0]}\nChoisis maintenant le salon (menu 2).", inline=False)
        return emb

    @discord.ui.select(cls=discord.ui.ChannelSelect, placeholder="2️⃣ Où ? Choisis le salon",
                       channel_types=[discord.ChannelType.text, discord.ChannelType.news, discord.ChannelType.category], row=1)
    async def where(self, i: discord.Interaction, sel):
        if not self.item:
            return await i.response.send_message("Choisis d'abord ce que tu veux placer (menu 1).", ephemeral=True)
        kind, key = self.item.split(":")
        g = i.guild
        ch = g.get_channel(sel.values[0].id)
        if ch is None:
            return await i.response.send_message("Je ne vois pas ce salon (permissions du bot ?).", ephemeral=True)
        is_cat = ch.type == discord.ChannelType.category
        if key == "tickets" and not is_cat:
            return await i.response.send_message("Pour les tickets, choisis une **catégorie** (un dossier), pas un salon.", ephemeral=True)
        if key != "tickets" and is_cat:
            return await i.response.send_message("Choisis un **salon texte**, pas une catégorie.", ephemeral=True)
        await i.response.defer()
        try:
            if kind == "post":
                await move_post(g, key, ch)
            else:
                cfg_set(FEEDS[key][1], ch.id)
        except discord.HTTPException as e:
            await i.followup.send(f"❌ Impossible de poster dans {ch.mention} : {e.text or e}", ephemeral=True)
        await i.edit_original_response(embed=self.embed(g), view=self)

    @discord.ui.button(label="Tout republier", emoji="🔄", style=discord.ButtonStyle.primary, row=2)
    async def repub(self, i: discord.Interaction, b):
        await i.response.defer(ephemeral=True)
        for item, (_, ck, _) in POSTS.items():
            if cfg_channel(i.guild, ck):
                await publish(i.guild, item)
        await i.followup.send("✅ Messages mis à jour.", ephemeral=True)


class PanelLookModal(discord.ui.Modal, title="Apparence du panneau"):
    titre = discord.ui.TextInput(label="Titre", max_length=100, required=False)
    desc = discord.ui.TextInput(label="Texte", style=discord.TextStyle.paragraph, max_length=1500, required=False)
    image = discord.ui.TextInput(label="Bannière : lien d'une image (https://…)", max_length=500, required=False)
    couleur = discord.ui.TextInput(label="Couleur (hex, ex : 8B5CF6)", max_length=7, required=False)
    footer = discord.ui.TextInput(label="Petit texte en bas", max_length=100, required=False)

    def __init__(self):
        super().__init__()
        self.titre.default = cfg_get("panel_title") or SHOP_NAME
        self.desc.default = cfg_get("panel_desc") or DEFAULT_PANEL_DESC
        self.image.default = cfg_get("panel_image") or None
        self.couleur.default = cfg_get("panel_color") or None
        self.footer.default = cfg_get("panel_footer") or None

    async def on_submit(self, i: discord.Interaction):
        notes = []
        cfg_set("panel_title", self.titre.value.strip())
        cfg_set("panel_desc", self.desc.value.strip())
        img = self.image.value.strip()
        if img and not img.lower().startswith(("http://", "https://")):
            notes.append("⚠️ Le lien de la bannière doit commencer par https:// (ignoré).")
            img = ""
        cfg_set("panel_image", img)
        col = self.couleur.value.strip().lstrip("#")
        if col and not re.fullmatch(r"[0-9A-Fa-f]{6}", col):
            notes.append("⚠️ Couleur invalide (6 caractères hex), ignorée.")
            col = ""
        cfg_set("panel_color", col)
        cfg_set("panel_footer", self.footer.value.strip())
        await i.response.defer(ephemeral=True)
        await refresh_shop(i.guild)
        await i.followup.send("✅ Panneau mis à jour." + ("\n" + "\n".join(notes) if notes else ""), ephemeral=True)


class TicketTextModal(discord.ui.Modal, title="Message dans les tickets"):
    texte = discord.ui.TextInput(label="Texte d'accueil", style=discord.TextStyle.paragraph, max_length=1500, required=False)

    def __init__(self):
        super().__init__()
        self.texte.default = cfg_get("ticket_welcome") or DEFAULT_TICKET_WELCOME

    async def on_submit(self, i: discord.Interaction):
        cfg_set("ticket_welcome", self.texte.value.strip())
        await i.response.send_message("✅ Message d'accueil des tickets mis à jour.", ephemeral=True)


class LookPage(Page):
    def embed(self, g):
        emb = discord.Embed(title="🎨 Apparence", color=accent(), description="Change le look du panneau de tickets et des messages.")
        emb.add_field(name="Titre", value=cfg_get("panel_title") or SHOP_NAME, inline=False)
        emb.add_field(name="Texte", value=(cfg_get("panel_desc") or DEFAULT_PANEL_DESC)[:300], inline=False)
        emb.add_field(name="Bannière", value=cfg_get("panel_image") or "aucune", inline=False)
        emb.add_field(name="Couleur", value=f"#{cfg_get('panel_color') or f'{COLOR:06X}'}")
        emb.add_field(name="Style", value="Components V2 ✅" if HAS_V2 else "Embed classique")
        return emb

    @discord.ui.button(label="Panneau (titre, texte, bannière…)", emoji="🖼️", style=discord.ButtonStyle.primary, row=0)
    async def panel(self, i, b):
        await i.response.send_modal(PanelLookModal())

    @discord.ui.button(label="Message dans les tickets", emoji="💬", style=discord.ButtonStyle.primary, row=1)
    async def ticket(self, i, b):
        await i.response.send_modal(TicketTextModal())


class CategoryModal(discord.ui.Modal):
    label_in = discord.ui.TextInput(label="Nom de la catégorie", max_length=80)
    emoji_in = discord.ui.TextInput(label="Emoji", max_length=40, required=False, placeholder="💎")
    desc_in = discord.ui.TextInput(label="Description (sous le nom dans le menu)", max_length=100, required=False)
    prod_in = discord.ui.TextInput(label="Produit lié pour le paiement (optionnel)", max_length=100, required=False,
                                   placeholder="Nom exact d'un produit du catalogue")

    def __init__(self, cid=None):
        super().__init__(title="Modifier la catégorie" if cid else "Nouvelle catégorie")
        self.cid = cid
        if cid:
            c = q("SELECT * FROM categories WHERE id=?", (cid,), one=True)
            self.label_in.default = c["label"]
            self.emoji_in.default = c["emoji"] or None
            self.desc_in.default = c["description"] or None
            self.prod_in.default = c["product"] or None

    async def on_submit(self, i: discord.Interaction):
        prod = None
        if self.prod_in.value.strip():
            p = q("SELECT name FROM products WHERE lower(name)=lower(?)", (self.prod_in.value.strip(),), one=True)
            if not p:
                names = ", ".join(r["name"] for r in q("SELECT name FROM products ORDER BY name")) or "aucun produit"
                return await i.response.send_message(f"❌ Produit introuvable. Produits existants : {names}", ephemeral=True)
            prod = p["name"]
        emoji = clean_emoji(self.emoji_in.value) or ""
        if self.cid:
            ex("UPDATE categories SET label=?, emoji=?, description=?, product=? WHERE id=?",
               (self.label_in.value.strip(), emoji, self.desc_in.value.strip(), prod, self.cid))
            page = CategoryEditPage(self.cid)
        else:
            if q("SELECT COUNT(*) n FROM categories", one=True)["n"] >= 25:
                return await i.response.send_message("❌ Maximum 25 catégories.", ephemeral=True)
            pos = (q("SELECT COALESCE(MAX(pos),0)+1 p FROM categories", one=True)["p"])
            ex("INSERT INTO categories(label,emoji,description,product,pos) VALUES(?,?,?,?,?)",
               (self.label_in.value.strip(), emoji, self.desc_in.value.strip(), prod, pos))
            page = CategoriesPage()
        await i.response.edit_message(embed=page.embed(i.guild), view=page)
        await refresh_shop(i.guild)


class CategoriesPage(Page):
    def __init__(self):
        super().__init__()
        rows = q("SELECT * FROM categories ORDER BY pos, id LIMIT 25")
        if rows:
            sel = discord.ui.Select(
                placeholder="✏️ Choisis une catégorie à modifier…", row=0,
                options=[discord.SelectOption(label=c["label"][:100], value=str(c["id"]), emoji=clean_emoji(c["emoji"])) for c in rows])

            async def pick(i: discord.Interaction):
                await go(i, CategoryEditPage(int(i.data["values"][0])))
            sel.callback = pick
            self.add_item(sel)

    def embed(self, g):
        rows = q("SELECT * FROM categories ORDER BY pos, id")
        lines = [f"{clean_emoji(c['emoji']) or '▫️'} **{c['label']}**" + (f" → 💳 {c['product']}" if c["product"] else "")
                 + (f" • 🔔 <@&{c['ping_role']}>" if c["ping_role"] else "") for c in rows]
        return discord.Embed(title="🗂️ Catégories du menu de tickets", color=accent(),
                             description="\n".join(lines) or "Aucune catégorie. Ajoutes-en une avec le bouton ci-dessous !")

    @discord.ui.button(label="Ajouter une catégorie", emoji="➕", style=discord.ButtonStyle.success, row=1)
    async def add(self, i, b):
        await i.response.send_modal(CategoryModal())


class CategoryEditPage(Page):
    def __init__(self, cid):
        super().__init__()
        self.cid = cid

    def back_view(self):
        return CategoriesPage()

    def embed(self, g):
        c = q("SELECT * FROM categories WHERE id=?", (self.cid,), one=True)
        emb = discord.Embed(title=f"✏️ {c['label']}", color=accent())
        emb.add_field(name="Emoji", value=clean_emoji(c["emoji"]) or "aucun")
        emb.add_field(name="Description", value=c["description"] or "aucune")
        emb.add_field(name="Produit lié (paiement auto)", value=c["product"] or "aucun (ticket sans paiement)", inline=False)
        emb.add_field(name="Rôle pingé en plus", value=fmt_role(g, c["ping_role"]), inline=False)
        return emb

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="🔔 Rôle à ping pour cette catégorie", min_values=0, max_values=1, row=0)
    async def ping(self, i, sel):
        ex("UPDATE categories SET ping_role=? WHERE id=?", (sel.values[0].id if sel.values else None, self.cid))
        await self.refresh(i)

    @discord.ui.button(label="Modifier", emoji="✏️", style=discord.ButtonStyle.primary, row=1)
    async def edit(self, i, b):
        await i.response.send_modal(CategoryModal(self.cid))

    @discord.ui.button(label="Supprimer", emoji="🗑️", style=discord.ButtonStyle.danger, row=1)
    async def delete(self, i: discord.Interaction, b):
        ex("DELETE FROM categories WHERE id=?", (self.cid,))
        page = CategoriesPage()
        await i.response.edit_message(embed=page.embed(i.guild), view=page)
        await refresh_shop(i.guild)


class PaymentModal(discord.ui.Modal, title="Paiement"):
    ltc = discord.ui.TextInput(label="Adresse Litecoin (LTC)", max_length=120, required=False, placeholder="ltc1…")
    texte = discord.ui.TextInput(label="Autres moyens de paiement (texte libre)", style=discord.TextStyle.paragraph,
                                 max_length=1500, required=False, placeholder="PayPal : …")

    def __init__(self):
        super().__init__()
        self.ltc.default = cfg_get("ltc_address") or None
        self.texte.default = cfg_get("paiement") or None

    async def on_submit(self, i: discord.Interaction):
        addr = self.ltc.value.strip()
        if addr and (len(addr) < 26 or not addr.startswith(("ltc1", "L", "M", "3"))):
            return await i.response.send_message("❌ Adresse LTC invalide (elle commence par ltc1, L ou M).", ephemeral=True)
        cfg_set("ltc_address", addr)
        cfg_set("paiement", self.texte.value.strip())
        await i.response.send_message("✅ Paiement mis à jour. Clique sur **Tester le LTC** pour vérifier.", ephemeral=True)


class PaymentPage(Page):
    def embed(self, g):
        emb = discord.Embed(title="💳 Paiement", color=accent(),
                            description="L'adresse LTC est envoyée toute seule dans chaque ticket d'achat, avec le montant exact.")
        emb.add_field(name="Adresse LTC", value=f"`{ltc_address()}`" if ltc_address() else "❌ non configurée", inline=False)
        emb.add_field(name="Autres moyens", value=(cfg_get("paiement") or "aucun")[:500], inline=False)
        return emb

    @discord.ui.button(label="Modifier", emoji="✏️", style=discord.ButtonStyle.primary, row=0)
    async def edit(self, i, b):
        await i.response.send_modal(PaymentModal())

    @discord.ui.button(label="Tester le LTC", emoji="🔎", style=discord.ButtonStyle.success, row=0)
    async def test(self, i: discord.Interaction, b):
        await i.response.defer(ephemeral=True)
        await i.followup.send(await ltc_selftest(), ephemeral=True)


class NumbersModal(discord.ui.Modal):
    """Modale générique pour régler des nombres. fields = [(clé, texte, défaut)], back = classe de la page de retour."""

    def __init__(self, title, fields, back):
        super().__init__(title=title[:45])
        self.back = back
        self.items = []
        for key, label, default in fields:
            ti = discord.ui.TextInput(label=label[:45], default=f"{cfg_num(key, default):g}", max_length=8)
            self.add_item(ti)
            self.items.append((key, ti))

    async def on_submit(self, i: discord.Interaction):
        vals = {}
        for key, ti in self.items:
            try:
                v = float(ti.value.strip().replace(",", "."))
                assert v >= 0
            except (ValueError, AssertionError):
                return await i.response.send_message(f"❌ « {ti.label} » doit être un nombre positif.", ephemeral=True)
            vals[key] = v
        for k, v in vals.items():
            cfg_set(k, f"{v:g}")
        page = self.back()
        await i.response.edit_message(embed=page.embed(i.guild), view=page)


class TicketsPage(Page):
    TOGGLES = [
        ("ping_on_open", "Pinger les rôles à l'ouverture", "1"),
        ("auto_claim", "Claim auto au 1er message du staff", "1"),
        ("auto_close_delivered", "Fermer le ticket après livraison", "1"),
        ("dm_transcript", "Transcript en DM à la fermeture", "1"),
    ]

    def __init__(self):
        super().__init__()
        self.add_toggles(self.TOGGLES, row=0)

    def embed(self, g):
        emb = discord.Embed(title="🎫 Tickets", color=accent(), description="Clique sur un bouton pour activer / désactiver.")
        emb.add_field(name="Fermeture après livraison", value=f"{cfg_num('delivered_close_min', 30):g} min")
        emb.add_field(name="Fermeture si inactif", value=("jamais" if not cfg_num("inactive_close_h", 48) else f"{cfg_num('inactive_close_h', 48):g} h"))
        emb.add_field(name="Tickets max par membre", value=("illimité" if not cfg_num("max_tickets", 1) else f"{cfg_num('max_tickets', 1):g}"))
        return emb

    @discord.ui.button(label="Délais et limites", emoji="⏱️", style=discord.ButtonStyle.primary, row=2)
    async def nums(self, i, b):
        await i.response.send_modal(NumbersModal("Délais et limites", [
            ("delivered_close_min", "Fermeture après livraison (minutes)", "30"),
            ("inactive_close_h", "Fermeture si inactif (heures, 0 = jamais)", "48"),
            ("max_tickets", "Tickets max par membre (0 = illimité)", "1"),
        ], TicketsPage))


async def announce_restock(g, name, price, new):
    ch = cfg_channel(g, "annonces_channel")
    if not ch:
        return
    rid = cfg_int("ping_role")
    emb = discord.Embed(title="📦 RESTOCK !", description=f"**{name}** — {price}\nStock : **{new}**\nOuvre un ticket pour commander !", color=0x22C55E)
    try:
        await ch.send(content=f"<@&{rid}>" if rid else None, embed=emb, allowed_mentions=discord.AllowedMentions(roles=True))
    except discord.HTTPException:
        pass


def stock_text(stock):
    return "♾️ illimité" if stock < 0 else ("❌ rupture" if stock == 0 else f"{stock} en stock")


class ProductModal(discord.ui.Modal):
    nom = discord.ui.TextInput(label="Nom du produit", max_length=100)
    prix = discord.ui.TextInput(label="Prix (avec un nombre en €, ex : 5€)", max_length=50, placeholder="5€")
    stock = discord.ui.TextInput(label="Stock (-1 = illimité)", max_length=7)
    desc = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph, max_length=300, required=False)

    def __init__(self, pid=None):
        super().__init__(title="Modifier le produit" if pid else "Nouveau produit")
        self.pid = pid
        p = q("SELECT * FROM products WHERE id=?", (pid,), one=True) if pid else None
        self.nom.default = p["name"] if p else None
        self.prix.default = p["price"] if p else None
        self.stock.default = str(p["stock"]) if p else "-1"
        self.desc.default = (p["description"] or None) if p else None

    async def on_submit(self, i: discord.Interaction):
        try:
            stock = int(self.stock.value.strip())
        except ValueError:
            return await i.response.send_message("❌ Le stock doit être un nombre (-1 = illimité).", ephemeral=True)
        name = self.nom.value.strip()
        try:
            if self.pid:
                old = q("SELECT name FROM products WHERE id=?", (self.pid,), one=True)["name"]
                ex("UPDATE products SET name=?, price=?, stock=?, description=? WHERE id=?",
                   (name, self.prix.value.strip(), stock, self.desc.value.strip(), self.pid))
                if old != name:
                    ex("UPDATE categories SET product=? WHERE product=?", (name, old))
                page = ProductEditPage(self.pid)
            else:
                ex("INSERT INTO products(name,price,stock,description) VALUES(?,?,?,?)",
                   (name, self.prix.value.strip(), stock, self.desc.value.strip()))
                page = ProductsPage()
        except sqlite3.IntegrityError:
            return await i.response.send_message("❌ Un produit porte déjà ce nom.", ephemeral=True)
        await i.response.edit_message(embed=page.embed(i.guild), view=page)
        await refresh_shop(i.guild)


class RestockModal(discord.ui.Modal, title="Restock"):
    qte = discord.ui.TextInput(label="Quantité à ajouter", max_length=6, placeholder="10")

    def __init__(self, pid):
        super().__init__()
        self.pid = pid

    async def on_submit(self, i: discord.Interaction):
        try:
            qte = int(self.qte.value.strip())
            assert qte > 0
        except (ValueError, AssertionError):
            return await i.response.send_message("❌ Mets un nombre supérieur à 0.", ephemeral=True)
        p = q("SELECT * FROM products WHERE id=?", (self.pid,), one=True)
        new = qte if p["stock"] < 0 else p["stock"] + qte
        ex("UPDATE products SET stock=? WHERE id=?", (new, self.pid))
        page = ProductEditPage(self.pid)
        await i.response.edit_message(embed=page.embed(i.guild), view=page)
        await announce_restock(i.guild, p["name"], p["price"], new)
        await refresh_shop(i.guild)


class ProductsPage(Page):
    def __init__(self):
        super().__init__()
        rows = q("SELECT * FROM products ORDER BY name LIMIT 25")
        if rows:
            sel = discord.ui.Select(
                placeholder="✏️ Choisis un produit à modifier…", row=0,
                options=[discord.SelectOption(label=p["name"][:100], value=str(p["id"]),
                                              description=f"{p['price']} • {stock_text(p['stock'])}"[:100]) for p in rows])

            async def pick(i: discord.Interaction):
                await go(i, ProductEditPage(int(i.data["values"][0])))
            sel.callback = pick
            self.add_item(sel)

    def embed(self, g):
        rows = q("SELECT * FROM products ORDER BY name")
        lines = [f"**{p['name']}** — {p['price']} • {stock_text(p['stock'])}" for p in rows]
        return discord.Embed(title="🛍️ Produits", color=accent(),
                             description="\n".join(lines) or "Aucun produit. Ajoutes-en un avec le bouton ci-dessous !")

    @discord.ui.button(label="Ajouter un produit", emoji="➕", style=discord.ButtonStyle.success, row=1)
    async def add(self, i, b):
        await i.response.send_modal(ProductModal())


class ProductEditPage(Page):
    def __init__(self, pid):
        super().__init__()
        self.pid = pid

    def back_view(self):
        return ProductsPage()

    def embed(self, g):
        p = q("SELECT * FROM products WHERE id=?", (self.pid,), one=True)
        if not p:
            return discord.Embed(title="Produit introuvable", color=accent())
        cats = ", ".join(c["label"] for c in q("SELECT label FROM categories WHERE product=?", (p["name"],)))
        emb = discord.Embed(title=f"🛍️ {p['name']}", color=accent())
        emb.add_field(name="Prix", value=p["price"])
        emb.add_field(name="Stock", value=stock_text(p["stock"]))
        emb.add_field(name="Description", value=p["description"] or "aucune", inline=False)
        emb.add_field(name="Catégories liées (paiement auto)", value=cats or "aucune : ajoute une catégorie liée dans 🗂️ Catégories pour l'afficher dans le menu", inline=False)
        return emb

    @discord.ui.button(label="Modifier", emoji="✏️", style=discord.ButtonStyle.primary, row=0)
    async def edit(self, i, b):
        await i.response.send_modal(ProductModal(self.pid))

    @discord.ui.button(label="Restock", emoji="📦", style=discord.ButtonStyle.success, row=0)
    async def restock(self, i, b):
        await i.response.send_modal(RestockModal(self.pid))

    @discord.ui.button(label="Supprimer", emoji="🗑️", style=discord.ButtonStyle.danger, row=1)
    async def delete(self, i: discord.Interaction, b):
        p = q("SELECT name FROM products WHERE id=?", (self.pid,), one=True)
        if p:
            ex("UPDATE categories SET product=NULL WHERE product=?", (p["name"],))
            ex("DELETE FROM products WHERE id=?", (self.pid,))
        page = ProductsPage()
        await i.response.edit_message(embed=page.embed(i.guild), view=page)
        await refresh_shop(i.guild)


class DeleteVouchModal(discord.ui.Modal, title="Supprimer un vouch"):
    vid = discord.ui.TextInput(label="Numéro du vouch (#)", max_length=10, placeholder="12")

    async def on_submit(self, i: discord.Interaction):
        v = self.vid.value.strip().lstrip("#")
        if not v.isdigit() or not q("SELECT 1 FROM vouches WHERE id=?", (int(v),), one=True):
            return await i.response.send_message("❌ Vouch introuvable.", ephemeral=True)
        ex("DELETE FROM vouches WHERE id=?", (int(v),))
        page = VouchPage()
        await i.response.edit_message(embed=page.embed(i.guild), view=page)


class VouchPage(Page):
    TOGGLES = [
        ("vouch_client_only", "Réservé aux clients (rôle client)", "0"),
        ("vouch_anywhere", "Autoriser /vouch hors d'un ticket", "1"),
    ]

    def __init__(self):
        super().__init__()
        self.add_toggles(self.TOGGLES, row=0)

    def embed(self, g):
        st = q("SELECT COUNT(*) n, AVG(stars) m FROM vouches", one=True)
        emb = discord.Embed(title="⭐ Vouchs", color=accent(), description="Les vouchs sont postés dans le salon choisi dans 📍 Emplacements.")
        emb.add_field(name="Salon", value=fmt_chan(g, "vouch_channel"))
        emb.add_field(name="Délai entre 2 vouchs", value=("aucun" if not cfg_num("vouch_cooldown_h", 12) else f"{cfg_num('vouch_cooldown_h', 12):g} h"))
        emb.add_field(name="Total", value=f"{st['n']} (moy. {st['m'] or 0:.2f}/5)")
        last = q("SELECT * FROM vouches ORDER BY ts DESC LIMIT 6")
        if last:
            emb.add_field(name="Derniers vouchs", value="\n".join(
                f"`#{v['id']}` {'⭐' * v['stars']} <@{v['author_id']}> : {v['text'][:40]}" for v in last), inline=False)
        return emb

    @discord.ui.button(label="Délai entre vouchs", emoji="⏱️", style=discord.ButtonStyle.primary, row=1)
    async def delay(self, i, b):
        await i.response.send_modal(NumbersModal("Délai entre vouchs", [
            ("vouch_cooldown_h", "Heures entre 2 vouchs (0 = aucun)", "12")], VouchPage))

    @discord.ui.button(label="Supprimer un vouch", emoji="🗑️", style=discord.ButtonStyle.danger, row=1)
    async def delete(self, i, b):
        await i.response.send_modal(DeleteVouchModal())


TEXTS = {  # clé -> (nom, texte par défaut, variables disponibles, élément à republier)
    "ticket_welcome": ("Accueil dans les tickets", DEFAULT_TICKET_WELCOME, "{user} {category} {product} {shop}", None),
    "delivered_text": ("Message de livraison", DEFAULT_DELIVERED, "{user} {product} {shop}", None),
    "paid_text": ("Message après paiement LTC", DEFAULT_PAID, "{user} {product} {shop}", None),
    "welcome_text": ("Message de bienvenue", DEFAULT_WELCOME, "{achat} {vouchs} {shop}", "welcome"),
    "rules_text": ("Règlement", DEFAULT_RULES, "", "rules"),
}


class TextModal(discord.ui.Modal):
    def __init__(self, key):
        label, default, vars_, item = TEXTS[key]
        super().__init__(title=label[:45])
        self.key, self.item = key, item
        self.texte = discord.ui.TextInput(
            label="Texte (vide = par défaut)", style=discord.TextStyle.paragraph, max_length=3500, required=False,
            default=cfg_get(key) or default, placeholder=(f"Variables : {vars_}" if vars_ else None))
        self.add_item(self.texte)

    async def on_submit(self, i: discord.Interaction):
        cfg_set(self.key, self.texte.value.strip())
        await i.response.defer(ephemeral=True)
        if self.item:
            await publish(i.guild, self.item)
        await i.followup.send(f"✅ « {TEXTS[self.key][0]} » mis à jour.", ephemeral=True)


class TextsPage(Page):
    def __init__(self):
        super().__init__()
        sel = discord.ui.Select(placeholder="✏️ Quel message modifier ?", row=0,
                                options=[discord.SelectOption(label=v[0][:100], value=k) for k, v in TEXTS.items()])

        async def pick(i: discord.Interaction):
            await i.response.send_modal(TextModal(i.data["values"][0]))
        sel.callback = pick
        self.add_item(sel)

    def embed(self, g):
        emb = discord.Embed(title="💬 Textes", color=accent(),
                            description="Choisis un message pour le modifier. Laisse le texte vide pour remettre celui par défaut.")
        for k, (label, default, vars_, _) in TEXTS.items():
            cur = cfg_get(k) or default
            emb.add_field(name=label, value=cur[:150] + ("…" if len(cur) > 150 else "") + (f"\n`{vars_}`" if vars_ else ""), inline=False)
        return emb


class StatsPage(Page):
    def embed(self, g):
        now = time.time()
        one = lambda sql, *a: q(sql, a, one=True)["n"]
        emb = discord.Embed(title=f"📊 Stats {SHOP_NAME}", color=accent())
        emb.add_field(name="Livrées (total)", value=str(one("SELECT COUNT(*) n FROM orders WHERE status='delivered'")))
        emb.add_field(name="Livrées 7 jours", value=str(one("SELECT COUNT(*) n FROM orders WHERE status='delivered' AND delivered_ts > ?", now - 7 * 86400)))
        emb.add_field(name="Livrées 24 h", value=str(one("SELECT COUNT(*) n FROM orders WHERE status='delivered' AND delivered_ts > ?", now - 86400)))
        emb.add_field(name="Tickets ouverts", value=str(one("SELECT COUNT(*) n FROM orders WHERE status IN ('open','claimed') AND channel_id!=0")))
        v = q("SELECT COUNT(*) n, AVG(stars) m FROM vouches", one=True)
        emb.add_field(name="Vouchs", value=f"{v['n']} (moy. {v['m'] or 0:.2f}/5)")
        emb.add_field(name="Encaissé en LTC", value=f"{q('SELECT COALESCE(SUM(ltc_eur),0) s FROM orders WHERE pay_state=2', one=True)['s']:.2f} €")
        top = q("SELECT product, COUNT(*) n FROM orders WHERE status='delivered' GROUP BY product ORDER BY n DESC LIMIT 5")
        if top:
            emb.add_field(name="Top produits", value="\n".join(f"{t['product']} : {t['n']}" for t in top), inline=False)
        return emb

    @discord.ui.button(label="Rafraîchir", emoji="🔄", style=discord.ButtonStyle.primary, row=0)
    async def reload(self, i, b):
        await go(i, StatsPage())


class BlacklistPage(Page):
    def __init__(self):
        super().__init__()
        rows = q("SELECT * FROM blacklist LIMIT 25")
        if rows:
            sel = discord.ui.Select(
                placeholder="✅ Retirer de la blacklist…", row=1,
                options=[discord.SelectOption(label=str(r["user_id"]), value=str(r["user_id"]),
                                              description=(r["reason"] or "")[:100] or None) for r in rows])

            async def rm(i: discord.Interaction):
                ex("DELETE FROM blacklist WHERE user_id=?", (int(i.data["values"][0]),))
                await go(i, BlacklistPage())
            sel.callback = rm
            self.add_item(sel)

    def embed(self, g):
        rows = q("SELECT * FROM blacklist LIMIT 15")
        return discord.Embed(title="⛔ Blacklist", color=accent(),
                             description="\n".join(f"<@{r['user_id']}> — {r['reason'] or '—'}" for r in rows)
                             or "Personne n'est blacklist. Les membres blacklist ne peuvent ni ouvrir de ticket ni vouch.")

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="⛔ Ajouter un membre à la blacklist", row=0)
    async def add(self, i, sel):
        for u in sel.values:
            ex("INSERT OR REPLACE INTO blacklist(user_id,reason) VALUES(?,?)", (u.id, "Via /config"))
        await go(i, BlacklistPage())


BACKUP_TABLES = ["config", "products", "categories", "vouches", "blacklist"]


def export_data():
    return {"version": 3, "shop": SHOP_NAME, "exported": int(time.time()),
            "tables": {t: [dict(r) for r in q(f"SELECT * FROM {t}")] for t in BACKUP_TABLES}}


def restore_data(data):
    tables = data["tables"]
    _db.execute("BEGIN")
    try:
        for t in BACKUP_TABLES:
            if t not in tables:
                continue
            cols = [r["name"] for r in q(f"PRAGMA table_info({t})")]
            _db.execute(f"DELETE FROM {t}")
            for row in tables[t]:
                keys = [k for k in row if k in cols]
                _db.execute(f"INSERT INTO {t}({','.join(keys)}) VALUES({','.join('?' * len(keys))})", [row[k] for k in keys])
        _db.execute("COMMIT")
    except Exception:
        _db.execute("ROLLBACK")
        raise
    _cfg.clear()
    for r in q("SELECT key,value FROM config"):
        _cfg[r["key"]] = r["value"]


class BackupPage(Page):
    def embed(self, g):
        return discord.Embed(
            title="💾 Sauvegarde", color=accent(),
            description=("Exporte toute ta config (rôles, salons, produits, catégories, textes, vouchs, blacklist) dans un fichier.\n\n"
                         "**Pense à en faire une avant chaque redéploiement** : sur Render gratuit, le disque est effacé.\n"
                         "Pour restaurer : `/restaurer` et joins le fichier."))

    @discord.ui.button(label="Exporter la sauvegarde", emoji="💾", style=discord.ButtonStyle.success, row=0)
    async def export(self, i: discord.Interaction, b):
        data = json.dumps(export_data(), ensure_ascii=False, indent=1).encode()
        await i.response.send_message("💾 Voici ta sauvegarde. Garde-la précieusement.", ephemeral=True,
                                      file=discord.File(io.BytesIO(data), filename=f"sauvegarde-shop-{time.strftime('%Y%m%d-%H%M')}.json"))


# ═════════════════════════ /help ═════════════════════════
def help_pages(member):
    pages = {
        "🛒 Acheter": ("🛒 Acheter",
                       "1. Va dans le salon d'achat et choisis une catégorie dans le menu.\n"
                       "2. Un ticket privé s'ouvre, l'adresse **LTC** et le **montant exact** arrivent automatiquement.\n"
                       "3. Une fois le paiement détecté, un vendeur te livre.\n"
                       "4. Laisse un vouch avec le bouton ⭐\n\n"
                       "`/catalogue` voir les produits"),
        "⭐ Vouchs": ("⭐ Vouchs",
                      "`/vouch` laisser un vouch (note 1-5, avis, vendeur)\n"
                      "`/vouches` voir les vouchs d'un membre ou du shop\n"
                      "`/top_vouch` classement des vendeurs"),
    }
    if is_staff(member):
        pages["🛠️ Staff"] = ("🛠️ Commandes staff",
                             "**Dans un ticket** : boutons Claim, Paiement, Livré, Fermer, Ajouter, Retirer, Renommer\n"
                             "`/ltc_montant` générer le paiement LTC d'un ticket\n"
                             "`/produit_ajouter` `/produit_modifier` `/produit_supprimer`\n"
                             "`/restock` ajouter du stock + annonce\n"
                             "`/stats` statistiques du shop\n"
                             "`/blacklist_ajouter` `/blacklist_retirer`\n"
                             "`/vouch_supprimer` supprimer un vouch")
    if member.guild_permissions.administrator:
        pages["⚙️ Admin"] = ("⚙️ Commandes admin",
                             "`/config` **grand panneau** : tout se règle ici (rôles, emplacements, produits, catégories, textes, vouchs, tickets, paiement, stats, blacklist, sauvegarde)\n"
                             "`/creer_shop` crée tout le serveur (rôles, salons, règlement…)\n"
                             "`/setup` installe seulement la partie shop\n"
                             "`/panel_tickets` poste le panneau de tickets dans ce salon\n"
                             "`/set_ltc` définir l'adresse LTC\n"
                             "`/staff_ajouter` donner le rôle staff\n"
                             "`/restaurer` restaurer une sauvegarde (fichier JSON)")
    return pages


class HelpView(discord.ui.View):
    def __init__(self, member):
        super().__init__(timeout=300)
        self.pages = help_pages(member)
        sel = discord.ui.Select(placeholder="Choisis une catégorie d'aide…",
                                options=[discord.SelectOption(label=k) for k in self.pages])
        sel.callback = self.on_pick
        self.add_item(sel)

    async def on_pick(self, i: discord.Interaction):
        title, text = self.pages[i.data["values"][0]]
        await i.response.edit_message(embed=discord.Embed(title=title, description=text, color=accent()), view=self)


# ═════════════════════════ Bot ═════════════════════════
class ShopBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=".", intents=discord.Intents.default(), max_messages=None,
                         chunk_guilds_at_startup=False)

    async def setup_hook(self):
        self.add_view(panel_view())
        self.add_view(TicketControls())
        self.add_view(VouchView())
        await self.tree.sync()
        janitor.start()
        payment_watcher.start()
        app = web.Application()
        app.router.add_get("/", lambda r: web.Response(text=f"{SHOP_NAME} bot OK"))
        app.router.add_get("/health", lambda r: web.Response(text="ok"))
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 8080))).start()

    async def on_ready(self):
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name=f"{SHOP_NAME} 🛒"))
        print(f"Connecté en tant que {self.user}")

    async def close(self):
        if _session and not _session.closed:
            await _session.close()
        await super().close()


bot = ShopBot()
tree = bot.tree


@bot.event
async def on_message(m: discord.Message):
    """Auto-claim : le premier staff qui écrit dans un ticket le prend en charge."""
    if m.author.bot or not m.guild or not isinstance(m.author, discord.Member):
        return
    o = order_of(m.channel.id)
    if (o and cfg_get("auto_claim", "1") == "1" and o["status"] == "open"
            and m.author.id != o["user_id"] and is_staff(m.author)):
        ex("UPDATE orders SET status='claimed', staff_id=? WHERE id=?", (m.author.id, o["id"]))
        await m.channel.send(embed=discord.Embed(description=f"🙋 Ticket pris en charge par {m.author.mention}", color=accent()))


@tasks.loop(minutes=5)
async def janitor():
    now = time.time()
    try:
        if cfg_get("auto_close_delivered", "1") == "1":
            for r in q("SELECT * FROM orders WHERE status='delivered' AND channel_id!=0 AND delivered_ts IS NOT NULL AND delivered_ts < ?",
                       (now - cfg_num("delivered_close_min", 30) * 60,)):
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
            if cfg_num("inactive_close_h", 48) and now - last > cfg_num("inactive_close_h", 48) * 3600:
                await close_channel(ch, "auto : inactif 48h")
    except Exception as e:
        print("janitor:", e)


@janitor.before_loop
async def _wait():
    await bot.wait_until_ready()


@tasks.loop(seconds=30)
async def payment_watcher():
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
                if not any(v.get("scriptpubkey_address") == addr and v.get("value") == o["ltc_amount"] for v in tx.get("vout", [])):
                    continue
                if q("SELECT 1 FROM orders WHERE paid_txid=? AND id!=?", (tx["txid"], o["id"]), one=True):
                    continue
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
    except Exception as e:
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
    try:
        if i.response.is_done():
            await i.followup.send(msg, ephemeral=True)
        else:
            await i.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


async def product_autocomplete(i: discord.Interaction, current: str):
    rows = q("SELECT name FROM products WHERE name LIKE ? ORDER BY name LIMIT 25", (f"%{current}%",))
    return [app_commands.Choice(name=r["name"], value=r["name"]) for r in rows]


# ═════════════════════════ Installation du serveur ═════════════════════════
admin = app_commands.default_permissions(administrator=True)


async def do_setup(i: discord.Interaction, full: bool = False):
    await i.response.defer(ephemeral=True)
    g = i.guild

    async def ensure_role(key, name, color, mentionable=False):
        r = g.get_role(cfg_int(key) or 0) or discord.utils.get(g.roles, name=name)
        if not r:
            r = await g.create_role(name=name, color=discord.Color(color), mentionable=mentionable, reason="Setup shop")
        cfg_set(key, r.id)
        return r

    if not staff_roles(g):
        await ensure_role("staff_role", "Staff Shop", 0x8B5CF6)
    staff = staff_roles(g)[0]
    await ensure_role("client_role", "Client", 0x22C55E)
    await ensure_role("ping_role", "Restock", 0xF59E0B, mentionable=True)
    if cfg_get("ticket_ping_roles") is None:
        cfg_set("ticket_ping_roles", staff.id)
    try:
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
        if not c and key == "vouch_channel":  # réutilise un salon de vouchs déjà existant
            c = vouch_channel(g)
        if not c:
            c = await g.create_text_channel(name, category=category, overwrites=overwrites, reason="Setup shop")
            cfg_set(key, c.id)
        return c

    shop_cat = await ensure_category("shop_category", "🛒・SHOP", {})
    await ensure_category("ticket_category", "🎫・TICKETS", {
        g.default_role: discord.PermissionOverwrite(view_channel=False),
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

    if not cfg_get("paiement"):
        cfg_set("paiement", "Crypto (BTC / LTC / USDT) et PayPal acceptés.\nLe vendeur te donne l'adresse dans le ticket.")

    await refresh_shop(g)
    if full:
        await publish(g, "welcome")
        await publish(g, "rules")
    await i.followup.send(
        "✅ **Shop installé !**\n"
        "• Salons, rôles (Staff Shop / Client / Restock), panneau et catalogue créés\n"
        "• Tu as reçu le rôle **Staff Shop**\n"
        "• Tape **`/config`** pour régler les rôles à ping, les salons, l'apparence du panneau, les catégories et le paiement LTC"
        + ("\n• Mode complet : bienvenue, règlement, annonces, général, médias et staff-chat créés aussi" if full else ""),
        ephemeral=True)


@tree.command(name="config", description="Grand panneau de configuration du shop")
@admin
async def config_cmd(i: discord.Interaction):
    v = MainView()
    await i.response.send_message(embed=v.embed(i.guild), view=v, ephemeral=True)


@tree.command(name="restaurer", description="Restaurer une sauvegarde exportée depuis /config (fichier JSON)")
@admin
async def restaurer(i: discord.Interaction, fichier: discord.Attachment):
    if fichier.size > 8_000_000:
        return await i.response.send_message("❌ Fichier trop gros.", ephemeral=True)
    await i.response.defer(ephemeral=True)
    try:
        data = json.loads(await fichier.read())
        if "tables" not in data:
            raise ValueError("ce n'est pas une sauvegarde du bot")
        restore_data(data)
    except Exception as e:
        return await i.followup.send(f"❌ Sauvegarde invalide : {e}", ephemeral=True)
    await refresh_shop(i.guild)
    await i.followup.send("✅ Sauvegarde restaurée (config, produits, catégories, vouchs, blacklist).", ephemeral=True)


@tree.command(name="help", description="Aide et liste des commandes")
async def help_cmd(i: discord.Interaction):
    v = HelpView(i.user)
    emb = discord.Embed(
        title=f"❓ Aide {SHOP_NAME}", color=accent(),
        description="Choisis une catégorie dans le menu ci-dessous.\n\n🛒 **Pour acheter** : ouvre un ticket dans le salon d'achat.")
    await i.response.send_message(embed=emb, view=v, ephemeral=True)


@tree.command(name="setup", description="Installe le shop (rôles, salons d'achat, panneau, catalogue, vouchs, logs)")
@admin
async def setup(i: discord.Interaction):
    await do_setup(i, full=False)


@tree.command(name="creer_shop", description="Crée TOUT le shop : rôles, salons, règlement, bienvenue, panneau, catalogue, vouchs, logs")
@admin
async def creer_shop(i: discord.Interaction):
    await do_setup(i, full=True)


@tree.command(name="panel_tickets", description="Poster le panneau de tickets dans ce salon")
@admin
async def panel_tickets(i: discord.Interaction):
    await i.response.defer(ephemeral=True)
    try:
        await post_panel(i.channel)
        await i.followup.send("✅ Panneau posté.", ephemeral=True)
    except discord.HTTPException as e:
        await i.followup.send(f"❌ Impossible de poster ici : {e.text or e}", ephemeral=True)


@tree.command(name="set_ltc", description="Définir ton adresse LTC de réception")
@admin
async def set_ltc(i: discord.Interaction, adresse: str):
    adresse = adresse.strip()
    if len(adresse) < 26 or not adresse.startswith(("ltc1", "L", "M", "3")):
        return await i.response.send_message("Cette adresse LTC a l'air invalide (elle commence par ltc1, L ou M).", ephemeral=True)
    cfg_set("ltc_address", adresse)
    await i.response.send_message(f"✅ Adresse LTC enregistrée : `{adresse}`\nTeste avec `/config` → Paiement → Tester le LTC.", ephemeral=True)


@tree.command(name="staff_ajouter", description="Donner le rôle staff à un membre")
@admin
async def staff_ajouter(i: discord.Interaction, membre: discord.Member):
    r = (staff_roles(i.guild) or [None])[0]
    if not r:
        return await i.response.send_message("Fais d'abord `/setup` ou choisis le rôle staff dans `/config`.", ephemeral=True)
    await membre.add_roles(r)
    await i.response.send_message(f"✅ {membre.mention} est staff.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@tree.command(name="ltc_montant", description="Générer le paiement LTC de ce ticket à partir d'un prix en € (staff)")
@staff_only()
async def ltc_montant(i: discord.Interaction, euros: app_commands.Range[float, 0.5, 100000.0]):
    o = order_of(i.channel.id)
    if not o:
        return await i.response.send_message("À utiliser dans un ticket.", ephemeral=True)
    if not ltc_address():
        return await i.response.send_message("Mets d'abord ton adresse dans `/config` → Paiement.", ephemeral=True)
    if o["pay_state"] == 2:
        return await i.response.send_message("Cette commande est déjà payée.", ephemeral=True)
    await i.response.defer()
    try:
        await make_payment(o["id"], euros)
    except Exception as e:
        return await i.followup.send(f"Impossible de calculer le montant LTC : {e}", ephemeral=True)
    await i.followup.send(embed=payment_embed(q("SELECT * FROM orders WHERE id=?", (o["id"],), one=True)))


# ═════════════════════════ Vouch (commandes) ═════════════════════════
@tree.command(name="vouch", description="Laisse un vouch pour le shop ou un vendeur")
async def vouch(i: discord.Interaction, note: app_commands.Range[int, 1, 5], avis: app_commands.Range[str, 3, 500],
                vendeur: discord.Member = None, produit: str = None):
    if vendeur and vendeur.id == i.user.id:
        return await i.response.send_message("Tu ne peux pas te vouch toi-même 😅", ephemeral=True)
    if cfg_get("vouch_anywhere", "1") == "0" and not order_of(i.channel.id):
        return await i.response.send_message("Les vouchs se font depuis ton ticket (bouton ⭐ après la livraison).", ephemeral=True)
    err = check_vouch(i.user)
    if err:
        return await i.response.send_message(err, ephemeral=True)
    await i.response.defer(ephemeral=True)
    vid, sent, problem = await post_vouch(i.guild, i.user, vendeur.id if vendeur else 0, note, avis, produit)
    await i.followup.send(vouch_feedback(vid, sent, problem), ephemeral=True)


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
    emb = discord.Embed(title=titre, color=accent(), description=f"**{len(rows)}** vouchs • moyenne **{moy:.2f}/5** {stars_str(round(moy))}")
    for r in rows[:5]:
        emb.add_field(name=f"#{r['id']} • {stars_str(r['stars'])}", value=f"<@{r['author_id']}> : {r['text'][:200]}", inline=False)
    await i.response.send_message(embed=emb, allowed_mentions=discord.AllowedMentions.none())


@tree.command(name="top_vouch", description="Classement des vendeurs par vouchs")
async def top_vouch(i: discord.Interaction):
    rows = q("SELECT target_id, COUNT(*) n, AVG(stars) m FROM vouches WHERE target_id!=0 GROUP BY target_id ORDER BY n DESC LIMIT 10")
    if not rows:
        return await i.response.send_message("Pas encore de classement.", ephemeral=True)
    medals = ["🥇", "🥈", "🥉"]
    desc = "\n".join(f"{medals[n] if n < 3 else f'`{n+1}.`'} <@{r['target_id']}> — **{r['n']}** vouchs • {r['m']:.2f}/5" for n, r in enumerate(rows))
    await i.response.send_message(embed=discord.Embed(title="🏆 Top vendeurs", description=desc, color=accent()),
                                  allowed_mentions=discord.AllowedMentions.none())


@tree.command(name="vouch_supprimer", description="Supprimer un vouch (staff)")
@staff_only()
async def vouch_supprimer(i: discord.Interaction, id: int):
    if not q("SELECT 1 FROM vouches WHERE id=?", (id,), one=True):
        return await i.response.send_message("Vouch introuvable.", ephemeral=True)
    ex("DELETE FROM vouches WHERE id=?", (id,))
    await i.response.send_message(f"🗑️ Vouch #{id} supprimé (le message dans le salon reste à supprimer à la main).", ephemeral=True)


# ═════════════════════════ Produits / stock ═════════════════════════
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
    await i.response.send_message(f"✅ **{nom}** ajouté ({prix}). Pour qu'il apparaisse dans le menu de tickets, "
                                  "ajoute une catégorie liée dans `/config` → Catégories.", ephemeral=True)
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
    await i.response.send_message(f"🗑️ **{nom}** supprimé. Catalogue mis à jour.", ephemeral=True)
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
        try:
            await ch.send(content=f"<@&{rid}>" if rid else None, embed=emb, allowed_mentions=discord.AllowedMentions(roles=True))
        except discord.HTTPException:
            pass
    await i.response.send_message(f"✅ Stock de **{nom}** : {new}", ephemeral=True)
    await refresh_shop(i.guild)


# ═════════════════════════ Stats / blacklist ═════════════════════════
@tree.command(name="stats", description="Stats du shop (staff)")
@staff_only()
async def stats(i: discord.Interaction):
    done = q("SELECT COUNT(*) n FROM orders WHERE status='delivered'", one=True)["n"]
    opened = q("SELECT COUNT(*) n FROM orders WHERE status IN ('open','claimed') AND channel_id!=0", one=True)["n"]
    v = q("SELECT COUNT(*) n, AVG(stars) m FROM vouches", one=True)
    top = q("SELECT product, COUNT(*) n FROM orders WHERE status='delivered' GROUP BY product ORDER BY n DESC LIMIT 3")
    emb = discord.Embed(title=f"📊 Stats {SHOP_NAME}", color=accent())
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
