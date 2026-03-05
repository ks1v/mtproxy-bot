"""
mtproxy-bot — MTProto proxy key management bot.
Only responds to OWNER_ID. All others are silently ignored.
"""

import os
import re
import json
import secrets
import socket
import logging
from datetime import datetime, timezone
from pathlib import Path

import tomlkit
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, MessageHandler, CommandHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN   = os.environ["BOT_TOKEN"]
OWNER_ID    = int(os.environ["OWNER_ID"])
PROXY_HOST   = os.environ.get("PROXY_HOST", "89.125.209.103")
PROXY_PORT   = os.environ.get("PROXY_PORT", "443")
PROXY_DOMAIN = os.environ.get("PROXY_DOMAIN", "")   # TLS fake-domain, e.g. "1c.ru"
TOML_PATH    = Path(os.environ.get("TOML_PATH", "/data/telemt.toml"))
STATS_PATH   = Path(os.environ.get("STATS_PATH", "/data/stats.json"))

PAGE_SIZE = 10

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)
# httpx logs every Telegram API call URL, which includes the bot token — suppress it
logging.getLogger("httpx").setLevel(logging.WARNING)

# Forwarding instruction stays in Russian — this is what gets sent to end users
INSTRUCTION = "Нажать на ссылку → Добавить → работает само 🚀"

# ── TOML helpers ──────────────────────────────────────────────────────────────

def load_toml() -> tomlkit.TOMLDocument:
    if not TOML_PATH.exists():
        raise FileNotFoundError(f"telemt.toml not found at {TOML_PATH}")
    return tomlkit.parse(TOML_PATH.read_text())

def save_toml(doc: tomlkit.TOMLDocument):
    TOML_PATH.write_text(tomlkit.dumps(doc))

def get_users(doc) -> dict:
    return dict(doc.get("access", {}).get("users", {}))

def set_user(doc, username: str, secret: str):
    if "access" not in doc:
        doc["access"] = tomlkit.table()
    if "users" not in doc["access"]:
        doc["access"]["users"] = tomlkit.table()
    doc["access"]["users"][username] = secret

def delete_user(doc, username: str):
    try:
        del doc["access"]["users"][username]
    except KeyError:
        pass

def gen_secret() -> str:
    return secrets.token_hex(16)

def proxy_link(secret: str) -> str:
    # Telemt runs in TLS-only mode; Telegram clients need the fake-TLS domain
    # embedded in the secret (hex-encoded) to know which SNI to present.
    # Without it the app gets stuck in "connecting..." and never opens TCP.
    domain_hex = PROXY_DOMAIN.encode("ascii").hex() if PROXY_DOMAIN else ""
    return f"tg://proxy?server={PROXY_HOST}&port={PROXY_PORT}&secret=ee{secret}{domain_hex}"

# ── Stats helpers ─────────────────────────────────────────────────────────────

def load_stats() -> dict:
    if STATS_PATH.exists():
        try:
            return json.loads(STATS_PATH.read_text())
        except Exception:
            pass
    return {}

# ── Auth guard ────────────────────────────────────────────────────────────────

def owner_only(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id if update.effective_user else None
        if uid != OWNER_ID:
            return
        return await func(update, ctx)
    return wrapper

# ── Username parsing ──────────────────────────────────────────────────────────

def clean_username(raw: str) -> str:
    return raw.strip().lstrip("@").lower()

def parse_usernames(text: str) -> list[str]:
    tokens = re.findall(r"@?([A-Za-z0-9_]{3,32})", text)
    return [clean_username(t) for t in tokens if t]

# ── Core: get or create user ──────────────────────────────────────────────────

async def send_user_card(chat_id, username: str, context, created: bool = None):
    doc = load_toml()
    users = get_users(doc)
    if username not in users:
        secret = gen_secret()
        set_user(doc, username, secret)
        save_toml(doc)
        created = True
    else:
        secret = users[username]
        if created is None:
            created = False

    link = proxy_link(secret)
    icon   = "✨" if created else "👤"
    status = "new key created" if created else "key found"

    text = (
        f"{icon} <b>{username}</b> — {status}\n\n"
        f"{link} \n\n"
        f"<i>{INSTRUCTION}</i>"
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True
    )

# ── Handlers ──────────────────────────────────────────────────────────────────

@owner_only
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Primary interface: plain text message = username lookup/create."""
    text = update.message.text.strip()
    if text.startswith("/"):
        return
    usernames = parse_usernames(text)
    if not usernames:
        await update.message.reply_text("No username found. Try @username or just username.")
        return
    for username in usernames:
        await send_user_card(update.effective_chat.id, username, ctx)


@owner_only
async def cmd_user(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/user @username"""
    args = " ".join(ctx.args) if ctx.args else ""
    usernames = parse_usernames(args)
    if not usernames:
        await update.message.reply_text("Usage: /user @username")
        return
    for username in usernames:
        await send_user_card(update.effective_chat.id, username, ctx)


@owner_only
async def cmd_batch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/batch @u1 @u2 @u3"""
    args = " ".join(ctx.args) if ctx.args else ""
    usernames = parse_usernames(args)
    if not usernames:
        await update.message.reply_text("Usage: /batch @u1 @u2 @u3 ...")
        return
    await update.message.reply_text(f"Processing {len(usernames)} users...")
    for username in usernames:
        await send_user_card(update.effective_chat.id, username, ctx)


@owner_only
async def cmd_revoke(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/revoke @username"""
    args = " ".join(ctx.args) if ctx.args else ""
    usernames = parse_usernames(args)
    if not usernames:
        await update.message.reply_text("Usage: /revoke @username")
        return
    doc = load_toml()
    results = []
    for username in usernames:
        if username in get_users(doc):
            delete_user(doc, username)
            results.append(f"🗑 <b>{username}</b> — revoked")
        else:
            results.append(f"❓ <b>{username}</b> — not found")
    save_toml(doc)
    await update.message.reply_text("\n".join(results), parse_mode="HTML")


@owner_only
async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/list [page]"""
    page = int(ctx.args[0]) if ctx.args else 0
    await send_list_page(update.effective_chat.id, page, ctx, message_id=None)


async def send_list_page(chat_id, page: int, ctx, message_id=None):
    doc = load_toml()
    usernames = sorted(get_users(doc).keys())
    total = len(usernames)

    if total == 0:
        await ctx.bot.send_message(chat_id=chat_id, text="No users yet.")
        return

    start = page * PAGE_SIZE
    end   = min(start + PAGE_SIZE, total)
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE

    lines = [f"👥 <b>Users</b> (page {page+1}/{total_pages}, total {total})\n"]
    for i, username in enumerate(usernames[start:end], start=start+1):
        lines.append(f"{i}. <code>{username}</code>")

    keyboard = []
    for username in usernames[start:end]:
        keyboard.append([
            InlineKeyboardButton(f"👤 {username}", callback_data=f"user|{username}|{page}"),
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"page|{page-1}"))
    if end < total:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"page|{page+1}"))
    if nav:
        keyboard.append(nav)

    markup = InlineKeyboardMarkup(keyboard)

    if message_id:
        await ctx.bot.edit_message_text(
            chat_id=chat_id, message_id=message_id,
            text="\n".join(lines), parse_mode="HTML", reply_markup=markup
        )
    else:
        await ctx.bot.send_message(
            chat_id=chat_id, text="\n".join(lines),
            parse_mode="HTML", reply_markup=markup
        )


async def send_user_detail(chat_id, username: str, page: int, ctx, message_id=None):
    """Show Send / Reset / Delete buttons for a single user, with a Back button."""
    text = f"👤 <b>{username}</b>"
    keyboard = [
        [
            InlineKeyboardButton("📤 Send",   callback_data=f"send|{username}"),
            InlineKeyboardButton("🔄 Reset",  callback_data=f"reset|{username}"),
            InlineKeyboardButton("🗑 Delete", callback_data=f"delete|{username}"),
        ],
        [InlineKeyboardButton("◀️ Back", callback_data=f"back|{page}")],
    ]
    markup = InlineKeyboardMarkup(keyboard)
    if message_id:
        await ctx.bot.edit_message_text(
            chat_id=chat_id, message_id=message_id,
            text=text, parse_mode="HTML", reply_markup=markup
        )
    else:
        await ctx.bot.send_message(
            chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=markup
        )


@owner_only
async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data       = query.data
    chat_id    = query.message.chat_id
    message_id = query.message.message_id

    if data.startswith("page|"):
        await send_list_page(chat_id, int(data.split("|")[1]), ctx, message_id=message_id)

    elif data.startswith("user|"):
        # user|{username}|{page}
        parts = data.split("|")
        username = parts[1]
        page = int(parts[2]) if len(parts) > 2 else 0
        await send_user_detail(chat_id, username, page, ctx, message_id=message_id)

    elif data.startswith("back|"):
        await send_list_page(chat_id, int(data.split("|")[1]), ctx, message_id=message_id)

    elif data.startswith("send|"):
        await send_user_card(chat_id, data.split("|")[1], ctx)

    elif data.startswith("reset|"):
        username = data.split("|")[1]
        doc = load_toml()
        new_secret = gen_secret()
        set_user(doc, username, new_secret)
        save_toml(doc)
        link = proxy_link(new_secret)
        await ctx.bot.send_message(
            chat_id=chat_id,
            text=(
                f"🔄 <b>{username}</b> — key reset\n\n"
                f"{link} \n\n"
                f"<i>{INSTRUCTION}</i>"
            ),
            parse_mode="HTML",
            disable_web_page_preview=True
        )

    elif data.startswith("delete|"):
        username = data.split("|")[1]
        doc = load_toml()
        delete_user(doc, username)
        save_toml(doc)
        await query.answer(f"Deleted: {username}", show_alert=True)
        await send_list_page(chat_id, 0, ctx, message_id=message_id)


# ── Stats ─────────────────────────────────────────────────────────────────────

@owner_only
async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/stats day|week|month"""
    period = ctx.args[0].lower() if ctx.args else "day"
    if period not in ("day", "week", "month"):
        await update.message.reply_text("Usage: /stats day|week|month")
        return

    stats = load_stats()
    if not stats:
        await update.message.reply_text(
            "No stats yet — wait up to 5 minutes for the first cron run."
        )
        return

    now = datetime.now(timezone.utc)
    hours_back, period_label = {
        "day":   (24,      "24 hours"),
        "week":  (24*7,    "7 days"),
        "month": (24*30,   "30 days"),
    }[period]

    user_totals = {}
    for username, data in stats.items():
        conn_total, err_total, warn_total, err_types = 0, 0, 0, {}
        peer_ips: dict[str, int] = {}
        for bucket_key, bucket in data.get("buckets", {}).items():
            try:
                bucket_dt = datetime.strptime(bucket_key, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if (now - bucket_dt).total_seconds() / 3600 > hours_back:
                continue
            conn_total  += bucket.get("conn", 0)
            err_total   += bucket.get("errors", 0)
            warn_total  += bucket.get("warnings", 0)
            for etype, count in bucket.get("error_types", {}).items():
                err_types[etype] = err_types.get(etype, 0) + count
            for ip, cnt in bucket.get("peer_ips", {}).items():
                peer_ips[ip] = peer_ips.get(ip, 0) + cnt
        if conn_total > 0 or err_total > 0 or warn_total > 0:
            user_totals[username] = {
                "conn": conn_total, "errors": err_total,
                "warnings": warn_total, "error_types": err_types,
                "peer_ips": peer_ips,
            }

    if not user_totals:
        await update.message.reply_text(f"No data for the last {period_label}.")
        return

    # "unknown" = errors that fired before telemt could identify the key
    # (TLS/transport layer failures: Telegram health-probes, scanners, early drops)
    unknown_data = user_totals.pop("unknown", None)
    sorted_known = sorted(user_totals.items(), key=lambda x: x[1]["conn"], reverse=True)
    active_known = len(sorted_known)

    known_conn   = sum(v["conn"]     for v in user_totals.values())
    known_errors = sum(v["errors"]   for v in user_totals.values())
    known_warns  = sum(v["warnings"] for v in user_totals.values())
    unk_conn     = unknown_data["conn"]   if unknown_data else 0
    total_conn   = known_conn + unk_conn

    user_word = "user" if active_known == 1 else "users"
    lines = [f"📊 <b>Stats for {period_label}</b> · {active_known} active {user_word}\n"]

    def _fmt_row(username: str, data: dict) -> str:
        conn     = data["conn"]
        errors   = data["errors"]
        warnings = data.get("warnings", 0)
        err_rate  = (errors   / conn * 100) if conn else 0
        warn_rate = (warnings / conn * 100) if conn else 0
        share    = (conn / total_conn * 100) if total_conn else 0
        unique_ips = len(data.get("peer_ips", {}))

        top_errors = sorted(data["error_types"].items(), key=lambda x: x[1], reverse=True)
        err_str = ""
        if top_errors:
            show = [top_errors[0]]
            if len(top_errors) > 1 and top_errors[1][1] >= top_errors[0][1] * 0.5:
                show.append(top_errors[1])
            err_str = " | " + ", ".join(_shorten_error(e) for e, _ in show)

        ew_parts = []
        if errors > 0:
            ew_parts.append(f"E {errors} ({err_rate:.1f}%)")
        if warnings > 0:
            ew_parts.append(f"W {warnings} ({warn_rate:.1f}%)")
        ew_str = " · " + ", ".join(ew_parts) if ew_parts else ""

        ip_str = f" · {unique_ips} IPs" if unique_ips > 0 else ""

        return (
            f"👤 <b>{username}</b>\n"
            f"   {conn:,} conn ({share:.0f}%){ew_str}{ip_str}{err_str}"
        )

    for username, data in sorted_known:
        lines.append(_fmt_row(username, data))

    # Separator + known subtotal + separator, then pre-auth "unknown" block
    if unknown_data is not None:
        sep = "─" * 22
        known_err_rate  = (known_errors / known_conn * 100) if known_conn else 0
        known_warn_rate = (known_warns  / known_conn * 100) if known_conn else 0
        known_ew = []
        if known_errors > 0:
            known_ew.append(f"E {known_errors} ({known_err_rate:.1f}%)")
        if known_warns > 0:
            known_ew.append(f"W {known_warns} ({known_warn_rate:.1f}%)")
        known_ew_str = " · " + ", ".join(known_ew) if known_ew else ""
        lines.append(f"\n{sep}")
        lines.append(f"<b>Known:</b> {known_conn:,} conn{known_ew_str}")
        lines.append(sep)
        lines.append(_fmt_row("unknown", unknown_data))
        # Show top source IPs for unknown — useful for spotting scanners / probes
        top_ips = sorted(unknown_data.get("peer_ips", {}).items(), key=lambda x: x[1], reverse=True)[:5]
        if top_ips:
            ip_parts = ", ".join(f"{ip} ({cnt})" for ip, cnt in top_ips)
            lines.append(f"   <i>Top IPs: {ip_parts}</i>")

    anomalies = []
    for username, data in sorted_known:
        conn   = data["conn"]
        errors = data["errors"]
        share  = (conn / total_conn * 100) if total_conn else 0
        rate   = (errors / conn * 100) if conn else 0
        if share > 40 and conn >= 100:
            anomalies.append(f"⚠️ <b>{username}</b> — {share:.0f}% of all traffic (possible key leak)")
        if rate > 15:
            anomalies.append(f"⚠️ <b>{username}</b> — high error rate {rate:.1f}%")

    if anomalies:
        lines.append("\n<b>Anomalies:</b>")
        lines.extend(anomalies)

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


def _shorten_error(error: str) -> str:
    mapping = {
        "Telegram handshake timeout":              "tg timeout",
        "IO error: expected 64 bytes, got 0":      "empty conn",
        "IO error: Operation timed out":           "tcp timeout",
        "IO error: early eof":                     "early eof",
        "IO error: Connection reset by peer":      "reset",
        "IO error: Host is unreachable":           "unreachable",
    }
    for k, v in mapping.items():
        if k in error:
            return v
    return error[:30]


# ── Start / status ────────────────────────────────────────────────────────────

def check_proxy() -> tuple[bool, str]:
    """TCP connect to the proxy port. Returns (ok, message)."""
    try:
        with socket.create_connection((PROXY_HOST, int(PROXY_PORT)), timeout=5):
            return True, f"✅ Proxy reachable at {PROXY_HOST}:{PROXY_PORT}"
    except OSError as e:
        return False, f"❌ Proxy unreachable — {e}"


@owner_only
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    doc = load_toml()
    user_count = len(get_users(doc))
    proxy_ok, proxy_status = check_proxy()

    await update.message.reply_text(
        "🛰 <b>mtproxy-bot</b>\n\n"
        f"{proxy_status}\n"
        f"👥 Users: {user_count}\n\n"
        "<b>Usage:</b>\n"
        "Send <code>@username</code> — get or create a key\n\n"
        "<b>Commands:</b>\n"
        "/list — all users (paginated)\n"
        "/revoke @username — delete key\n"
        "/stats day|week|month — usage report\n"
        "/batch @u1 @u2 ... — bulk add",
        parse_mode="HTML"
    )


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("user",   cmd_user))
    app.add_handler(CommandHandler("batch",  cmd_batch))
    app.add_handler(CommandHandler("revoke", cmd_revoke))
    app.add_handler(CommandHandler("list",   cmd_list))
    app.add_handler(CommandHandler("stats",  cmd_stats))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("mtproxy-bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
