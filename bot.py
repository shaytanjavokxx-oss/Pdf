import os
import io
import logging
import sqlite3
import datetime

from PIL import Image
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# PDF birlashtirish uchun (pure-python, Termux'da o'rnatiladi: pip install pypdf)
try:
    from pypdf import PdfReader, PdfWriter
    HAS_PYPDF = True
except Exception:
    HAS_PYPDF = False

# PDF -> rasm uchun (ixtiyoriy: pip install pymupdf)
try:
    import fitz  # PyMuPDF
    HAS_FITZ = True
except Exception:
    HAS_FITZ = False

# ==================== SOZLAMALAR ====================
BOT_TOKEN = "8888539745:AAGx9eQZrb5kF1wqVg0r4ZP4RcQoAdeWokA"  # <-- o'z tokeningiz
ADMIN_ID = 7132963801        # Jony — admin
FREE_DAILY_LIMIT = 5         # bepul kunlik PDF limiti
VIP_PRICE = "20,000 so'm"    # umrbod VIP narxi
CARD_NUMBER = "9860 1760 1848 4958"  # to'lov kartasi
REQUIRED_CHANNEL = ""        # majburiy obuna kanali, masalan "@mychannel". Bo'sh = o'chiq
MAX_IMAGES = 50              # bitta PDF ga maksimal rasm
MAX_MERGE = 20              # birlashtirishga maksimal PDF
MAX_SPLIT_PAGES = 30        # PDF'dan ajratishda maksimal sahifa
DB_PATH = "bot_data.db"
# ====================================================

VIP_AD = (
    "\n\n💎 VIP — umrbod cheksiz foydalanish!\n"
    f"Narxi bor-yo'g'i {VIP_PRICE} (bir martalik).\n"
    "👉 /vip buyrug'ini bosing"
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ==================== BAZA (SQLite) ====================
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT,
            first_name  TEXT,
            pdf_count   INTEGER DEFAULT 0,
            is_pro      INTEGER DEFAULT 0,
            daily_count INTEGER DEFAULT 0,
            last_date   TEXT,
            joined      TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def _today():
    return datetime.date.today().isoformat()


def ensure_user(user):
    conn = db()
    row = conn.execute("SELECT user_id FROM users WHERE user_id=?", (user.id,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO users (user_id, username, first_name, last_date, joined) VALUES (?,?,?,?,?)",
            (user.id, user.username or "", user.first_name or "", _today(), _today()),
        )
    else:
        conn.execute(
            "UPDATE users SET username=?, first_name=? WHERE user_id=?",
            (user.username or "", user.first_name or "", user.id),
        )
    conn.commit()
    conn.close()


def _reset_if_needed(conn, user_id):
    row = conn.execute("SELECT last_date FROM users WHERE user_id=?", (user_id,)).fetchone()
    if row and row["last_date"] != _today():
        conn.execute(
            "UPDATE users SET daily_count=0, last_date=? WHERE user_id=?",
            (_today(), user_id),
        )


def check_limit(user_id):
    """(ruxsat: bool, qolgan: int|None). PRO uchun None (cheksiz)."""
    conn = db()
    _reset_if_needed(conn, user_id)
    conn.commit()
    row = conn.execute(
        "SELECT is_pro, daily_count FROM users WHERE user_id=?", (user_id,)
    ).fetchone()
    conn.close()
    if row is None:
        return True, FREE_DAILY_LIMIT
    if row["is_pro"]:
        return True, None
    remaining = FREE_DAILY_LIMIT - row["daily_count"]
    return remaining > 0, max(remaining, 0)


def record_make(user_id):
    conn = db()
    _reset_if_needed(conn, user_id)
    conn.execute(
        "UPDATE users SET pdf_count=pdf_count+1, daily_count=daily_count+1 WHERE user_id=?",
        (user_id,),
    )
    conn.commit()
    conn.close()


def set_pro(user_id, value):
    conn = db()
    conn.execute("UPDATE users SET is_pro=? WHERE user_id=?", (1 if value else 0, user_id))
    conn.commit()
    n = conn.total_changes
    conn.close()
    return n > 0


def get_user_stats(user_id):
    conn = db()
    _reset_if_needed(conn, user_id)
    conn.commit()
    row = conn.execute(
        "SELECT pdf_count, is_pro, daily_count FROM users WHERE user_id=?", (user_id,)
    ).fetchone()
    conn.close()
    return row


def top_users(n=10):
    conn = db()
    rows = conn.execute(
        "SELECT first_name, username, pdf_count FROM users ORDER BY pdf_count DESC LIMIT ?",
        (n,),
    ).fetchall()
    conn.close()
    return rows


def global_stats():
    conn = db()
    total = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    pdfs = conn.execute("SELECT COALESCE(SUM(pdf_count),0) s FROM users").fetchone()["s"]
    pros = conn.execute("SELECT COUNT(*) c FROM users WHERE is_pro=1").fetchone()["c"]
    conn.close()
    return total, pdfs, pros


# ==================== YORDAMCHI: PDF QURISH ====================
def _resize_max(img, max_side):
    w, h = img.size
    if max(w, h) <= max_side:
        return img
    if w >= h:
        nw, nh = max_side, int(h * max_side / w)
    else:
        nh, nw = max_side, int(w * max_side / h)
    return img.resize((nw, nh), Image.LANCZOS)


def _fit_a4(img):
    # A4 @ ~150 DPI
    A4 = (1240, 1754)
    margin = 40
    canvas = Image.new("RGB", A4, "white")
    avail = (A4[0] - 2 * margin, A4[1] - 2 * margin)
    im = img.copy()
    im.thumbnail(avail, Image.LANCZOS)
    x = (A4[0] - im.size[0]) // 2
    y = (A4[1] - im.size[1]) // 2
    canvas.paste(im, (x, y))
    return canvas


def build_pdf(images, a4=False, compress=False):
    pages = []
    for im in images:
        img = im.convert("RGB")
        if compress and not a4:
            img = _resize_max(img, 1600)
        if a4:
            img = _fit_a4(img)
        pages.append(img)
    buf = io.BytesIO()
    pages[0].save(buf, format="PDF", save_all=True, append_images=pages[1:])
    buf.seek(0)
    return buf


# ==================== KEYBOARDLAR ====================
def main_kb():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📄 PDF yasash", callback_data="make_pdf")],
            [
                InlineKeyboardButton("↩️ Oxirgisini", callback_data="del_last"),
                InlineKeyboardButton("🧹 Tozalash", callback_data="clear_imgs"),
            ],
            [InlineKeyboardButton("⚙️ Sozlamalar", callback_data="settings")],
        ]
    )


def name_kb():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✍️ Nom berish", callback_data="name_pdf"),
                InlineKeyboardButton("⚡ Standart", callback_data="default_name"),
            ],
            [InlineKeyboardButton("⬅️ Orqaga", callback_data="back_main")],
        ]
    )


def settings_kb(context):
    s = get_settings(context)
    a4 = "✅" if s["a4"] else "❌"
    cp = "✅" if s["compress"] else "❌"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"A4 sahifa: {a4}", callback_data="toggle_a4")],
            [InlineKeyboardButton(f"Siqish (kichik hajm): {cp}", callback_data="toggle_compress")],
            [InlineKeyboardButton("⬅️ Orqaga", callback_data="back_main")],
        ]
    )


def sub_kb():
    btns = []
    if REQUIRED_CHANNEL:
        ch = REQUIRED_CHANNEL.lstrip("@")
        btns.append([InlineKeyboardButton("📢 Kanalga obuna", url=f"https://t.me/{ch}")])
    btns.append([InlineKeyboardButton("✅ Tekshirish", callback_data="check_sub")])
    return InlineKeyboardMarkup(btns)


def get_settings(context):
    return context.user_data.setdefault("settings", {"a4": False, "compress": False})


def get_images(context):
    return context.user_data.setdefault("images", [])


# ==================== OBUNA TEKSHIRISH ====================
async def is_subscribed(user_id, context):
    if not REQUIRED_CHANNEL:
        return True
    try:
        m = await context.bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return m.status not in ("left", "kicked")
    except Exception as e:
        logger.warning(f"obuna tekshirib bo'lmadi: {e}")
        return True  # xatoda bloklamaymiz


async def ensure_access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user)
    if not await is_subscribed(user.id, context):
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="🔒 Botdan foydalanish uchun kanalga obuna bo'ling, keyin ✅ Tekshirish bosing.",
            reply_markup=sub_kb(),
        )
        return False
    return True


# ==================== BUYRUQLAR ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    get_images(context).clear()
    context.user_data["mode"] = "image"
    context.user_data["vip_mode"] = False
    await update.message.reply_text(
        "👋 Salom!\n\n"
        "Menga rasm yuboring — PDF qilib beraman. Bir nechta rasm yuborsangiz, "
        "bitta PDF ga jamlayman.\n\n"
        "Buyruqlar:\n"
        "📄 /pdf — PDF yasash\n"
        "🔗 /birlashtir — bir nechta PDF'ni birlashtirish\n"
        "💎 /vip — umrbod cheksiz (VIP)\n"
        "📊 /stat — statistikangiz\n"
        "🏆 /top — reyting\n"
        "🧹 /tozalash — rasmlarni o'chirish\n"
        "ℹ️ /yordam — qo'llanma"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ Qo'llanma:\n\n"
        "🖼 RASM → PDF: rasm(lar) yuboring, so'ng /pdf yoki tugma.\n"
        "📄 PDF → RASM: PDF yuboring, «Rasmlarga ajratish» tugmasini bosing.\n"
        "🔗 PDF birlashtirish: /birlashtir, so'ng PDF'larni yuboring, «Birlashtirish» bosing.\n\n"
        "⚙️ Sozlamalar:\n"
        "• A4 sahifa — barcha rasmlarni bir xil A4 varaqqa joylaydi.\n"
        "• Siqish — hajmni kichraytiradi.\n\n"
        f"💎 VIP: kuniga {FREE_DAILY_LIMIT} ta limitsiz, umrbod cheksiz — /vip\n\n"
        "💡 Sifat muhim bo'lsa, rasmni «Fayl» ko'rinishida yuboring."
    )


async def vip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    row = get_user_stats(update.effective_user.id)
    if row and row["is_pro"]:
        await update.message.reply_text("⭐ Sizda allaqachon VIP bor! Cheksiz foydalanavering 😊")
        return
    context.user_data["vip_mode"] = True
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Bekor qilish", callback_data="vip_cancel")]]
    )
    await update.message.reply_text(
        "💎 VIP — UMRBOD CHEKSIZ\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"Narxi: {VIP_PRICE} (bir martalik, umrbod!)\n\n"
        "✅ Kunlik limit yo'q\n"
        "✅ Cheksiz PDF yasash\n"
        "✅ Cheksiz birlashtirish\n\n"
        "To'lov uchun karta:\n"
        f"💳 {CARD_NUMBER}\n\n"
        "To'lovni amalga oshirgach, chek (screenshot)ni "
        "SHU YERGA rasm qilib yuboring.\n"
        "Admin tasdiqlagach, VIP avtomatik yoqiladi.",
        reply_markup=kb,
    )


async def pdf_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_access(update, context):
        return
    images = get_images(context)
    if not images:
        await update.message.reply_text("❌ Avval kamida bitta rasm yuboring.")
        return
    await update.message.reply_text(
        f"📄 {len(images)} ta rasm tayyor. Faylga nom berasizmi?",
        reply_markup=name_kb(),
    )


async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    get_images(context).clear()
    await update.message.reply_text("🧹 Barcha rasmlar tozalandi.")


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mode"] = "image"
    context.user_data["awaiting_name"] = False
    context.user_data["vip_mode"] = False
    context.user_data.pop("merge_pdfs", None)
    await update.message.reply_text("❎ Bekor qilindi. Asosiy holatga qaytdik.")


async def merge_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_access(update, context):
        return
    if not HAS_PYPDF:
        await update.message.reply_text(
            "❌ Birlashtirish uchun kutubxona yo'q. Termux'da: pip install pypdf"
        )
        return
    context.user_data["mode"] = "merge"
    context.user_data["merge_pdfs"] = []
    await update.message.reply_text(
        "🔗 Birlashtirish rejimi yoqildi.\n"
        "Endi PDF fayllarni ketma-ket yuboring (yuborilgan tartibda birlashadi).\n"
        "Tugagach «Birlashtirish» tugmasini bosing. Bekor: /otkaz"
    )


async def stat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    row = get_user_stats(update.effective_user.id)
    if not row:
        await update.message.reply_text("Ma'lumot topilmadi.")
        return
    if row["is_pro"]:
        limit_txt = "♾ Cheksiz (VIP)"
    else:
        limit_txt = f"{row['daily_count']}/{FREE_DAILY_LIMIT} (bugun)"
    text = (
        "📊 Sizning statistikangiz:\n\n"
        f"• Yasalgan PDF: {row['pdf_count']} ta\n"
        f"• Status: {'💎 VIP' if row['is_pro'] else 'Oddiy'}\n"
        f"• Bugungi limit: {limit_txt}"
    )
    if not row["is_pro"]:
        text += VIP_AD
    await update.message.reply_text(text)


async def top_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = top_users(10)
    if not rows:
        await update.message.reply_text("Hozircha reyting bo'sh.")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 TOP 10 foydalanuvchi:\n"]
    for i, r in enumerate(rows):
        name = r["first_name"] or (("@" + r["username"]) if r["username"] else "Foydalanuvchi")
        prefix = medals[i] if i < 3 else f"{i+1}."
        lines.append(f"{prefix} {name} — {r['pdf_count']} ta PDF")
    await update.message.reply_text("\n".join(lines))


# ---- Admin ----
async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    total, pdfs, pros = global_stats()
    await update.message.reply_text(
        "🛠 Admin statistika:\n\n"
        f"• Foydalanuvchilar: {total}\n"
        f"• Jami PDF: {pdfs}\n"
        f"• VIP foydalanuvchilar: {pros}"
    )


async def pro_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Foydalanish: /pro <user_id>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID raqam bo'lishi kerak.")
        return
    ok = set_pro(uid, True)
    await update.message.reply_text("✅ VIP berildi." if ok else "Foydalanuvchi topilmadi.")


async def unpro_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Foydalanish: /unpro <user_id>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID raqam bo'lishi kerak.")
        return
    ok = set_pro(uid, False)
    await update.message.reply_text("✅ VIP olib tashlandi." if ok else "Foydalanuvchi topilmadi.")


# ==================== RASM QABUL QILISH ====================
async def _add_image(update, context, image):
    images = get_images(context)
    if len(images) >= MAX_IMAGES:
        await update.message.reply_text(
            f"⚠️ Limit: {MAX_IMAGES} ta rasm. Avval /pdf bosing yoki /tozalash."
        )
        return
    images.append(image)
    await update.message.reply_text(
        f"🖼 Qabul qilindi ({len(images)} ta). Yana yuboring yoki PDF yasang:",
        reply_markup=main_kb(),
    )


async def handle_payment_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """VIP rejimida yuborilgan rasm — to'lov cheki sifatida adminga yuboriladi."""
    user = update.effective_user
    context.user_data["vip_mode"] = False
    photo = update.message.photo[-1]
    uname = f"@{user.username}" if user.username else "username yo'q"
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Tasdiqlash", callback_data=f"vipok_{user.id}"),
                InlineKeyboardButton("❌ Rad etish", callback_data=f"vipno_{user.id}"),
            ]
        ]
    )
    try:
        await context.bot.send_photo(
            chat_id=ADMIN_ID,
            photo=photo.file_id,
            caption=(
                "💎 YANGI VIP TO'LOV!\n\n"
                f"👤 {user.first_name} ({uname})\n"
                f"🆔 {user.id}\n\n"
                "Chekni tekshirib, tasdiqlang:"
            ),
            reply_markup=kb,
        )
        await update.message.reply_text(
            "✅ Chek adminga yuborildi!\n"
            "Tasdiqlangach sizga xabar keladi va VIP avtomatik yoqiladi. "
            "Odatda bu bir necha daqiqa oladi 😊"
        )
    except Exception as e:
        logger.error(f"payment forward: {e}")
        await update.message.reply_text(
            "❌ Chekni yuborishda xatolik. Birozdan keyin qayta urinib ko'ring."
        )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_access(update, context):
        return
    # VIP to'lov cheki rejimi
    if context.user_data.get("vip_mode"):
        await handle_payment_photo(update, context)
        return
    try:
        photo = update.message.photo[-1]
        f = await photo.get_file()
        data = await f.download_as_bytearray()
        img = Image.open(io.BytesIO(data)).convert("RGB")
        await _add_image(update, context, img)
    except Exception as e:
        logger.error(f"handle_photo: {e}")
        await update.message.reply_text("❌ Rasmni o'qishda xatolik. Qayta yuboring.")


async def handle_document_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_access(update, context):
        return
    doc = update.message.document
    try:
        f = await doc.get_file()
        data = await f.download_as_bytearray()
        img = Image.open(io.BytesIO(data)).convert("RGB")
        await _add_image(update, context, img)
    except Exception as e:
        logger.error(f"handle_document_image: {e}")
        await update.message.reply_text("❌ Faylni o'qishda xatolik. Qayta yuboring.")


async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_access(update, context):
        return
    doc = update.message.document

    # Birlashtirish rejimida bo'lsa -> ro'yxatga qo'shamiz
    if context.user_data.get("mode") == "merge":
        merge_list = context.user_data.setdefault("merge_pdfs", [])
        if len(merge_list) >= MAX_MERGE:
            await update.message.reply_text(f"⚠️ Limit: {MAX_MERGE} ta PDF.")
            return
        f = await doc.get_file()
        data = bytes(await f.download_as_bytearray())
        merge_list.append((doc.file_name or "fayl.pdf", data))
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔗 Birlashtirish", callback_data="merge_now")],
                [InlineKeyboardButton("🧹 Tozalash", callback_data="clear_merge")],
            ]
        )
        await update.message.reply_text(
            f"📎 Qo'shildi ({len(merge_list)} ta PDF). Yana yuboring yoki birlashtiring:",
            reply_markup=kb,
        )
        return

    # Oddiy rejim -> rasmga ajratishni taklif qilamiz
    if not HAS_FITZ:
        await update.message.reply_text(
            "ℹ️ PDF'ni rasmga ajratish hozircha mavjud emas.\n"
            "PDF'larni birlashtirish uchun /birlashtir buyrug'idan foydalaning."
        )
        return
    f = await doc.get_file()
    data = bytes(await f.download_as_bytearray())
    context.user_data["last_pdf"] = data
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🖼 Rasmlarga ajratish", callback_data="split_pdf")]]
    )
    await update.message.reply_text("📄 PDF qabul qilindi. Nima qilamiz?", reply_markup=kb)


# ==================== MATN (nom kutilganda) ====================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting_name"):
        name = update.message.text.strip()
        # xavfsiz fayl nomi
        name = "".join(c for c in name if c.isalnum() or c in (" ", "-", "_")).strip()
        if not name:
            name = "natija"
        context.user_data["awaiting_name"] = False
        await produce_pdf(context, update.effective_chat.id, update.effective_user, name)
        return
    await update.message.reply_text("🤔 Menga rasm yuboring yoki /yordam bosing.")


# ==================== PDF ISHLAB CHIQARISH ====================
async def produce_pdf(context, chat_id, user, filename):
    images = get_images(context)
    if not images:
        await context.bot.send_message(chat_id, "❌ Rasm qolmadi.")
        return

    allowed, remaining = check_limit(user.id)
    if not allowed:
        await context.bot.send_message(
            chat_id,
            f"⛔ Bugungi bepul limit ({FREE_DAILY_LIMIT} ta PDF) tugadi."
            + VIP_AD,
        )
        return

    await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
    s = get_settings(context)
    try:
        buf = build_pdf(images, a4=s["a4"], compress=s["compress"])
    except Exception as e:
        logger.error(f"build_pdf: {e}")
        await context.bot.send_message(chat_id, "❌ PDF yasashda xatolik.")
        return

    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"

    rem_txt = "♾" if remaining is None else f"{remaining - 1} ta qoldi"
    await context.bot.send_document(
        chat_id=chat_id,
        document=buf,
        filename=filename,
        caption=f"✅ Tayyor! {len(images)} ta rasm → PDF.\nBugun: {rem_txt}",
    )
    record_make(user.id)
    images.clear()


# ==================== TUGMALAR (CALLBACK) ====================
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data
    await q.answer()
    chat_id = q.message.chat_id
    user = q.from_user

    if data == "make_pdf":
        if not get_images(context):
            await q.edit_message_text("❌ Avval rasm yuboring.")
            return
        await q.edit_message_text(
            f"📄 {len(get_images(context))} ta rasm. Faylga nom berasizmi?",
            reply_markup=name_kb(),
        )

    elif data == "name_pdf":
        context.user_data["awaiting_name"] = True
        await q.edit_message_text("✍️ Fayl nomini yuboring (masalan: Hujjat):")

    elif data == "default_name":
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        await q.edit_message_text("⏳ PDF tayyorlanmoqda...")
        await produce_pdf(context, chat_id, user, f"PDF_{ts}")

    elif data == "del_last":
        imgs = get_images(context)
        if imgs:
            imgs.pop()
        await q.edit_message_text(
            f"↩️ Oxirgi rasm o'chirildi. Hozir: {len(imgs)} ta.",
            reply_markup=main_kb() if imgs else None,
        )

    elif data == "clear_imgs":
        get_images(context).clear()
        await q.edit_message_text("🧹 Rasmlar tozalandi.")

    elif data == "settings":
        await q.edit_message_text("⚙️ Sozlamalar:", reply_markup=settings_kb(context))

    elif data == "toggle_a4":
        s = get_settings(context)
        s["a4"] = not s["a4"]
        await q.edit_message_reply_markup(reply_markup=settings_kb(context))

    elif data == "toggle_compress":
        s = get_settings(context)
        s["compress"] = not s["compress"]
        await q.edit_message_reply_markup(reply_markup=settings_kb(context))

    elif data == "back_main":
        imgs = get_images(context)
        await q.edit_message_text(
            f"📋 Hozir {len(imgs)} ta rasm bor." if imgs else "Rasm yuboring.",
            reply_markup=main_kb() if imgs else None,
        )

    elif data == "split_pdf":
        await handle_split(q, context)

    elif data == "merge_now":
        await handle_merge_now(q, context)

    elif data == "clear_merge":
        context.user_data["merge_pdfs"] = []
        await q.edit_message_text("🧹 Birlashtirish ro'yxati tozalandi.")

    elif data == "vip_cancel":
        context.user_data["vip_mode"] = False
        await q.edit_message_text("❎ VIP xarid bekor qilindi.")

    elif data.startswith("vipok_"):
        if user.id != ADMIN_ID:
            await q.answer("Bu tugma faqat admin uchun!", show_alert=True)
            return
        uid = int(data.replace("vipok_", ""))
        ok = set_pro(uid, True)
        if ok:
            try:
                await context.bot.send_message(
                    uid,
                    "🎉 TABRIKLAYMIZ!\n\n"
                    "💎 To'lovingiz tasdiqlandi — sizga UMRBOD VIP berildi!\n"
                    "Endi kunlik limit yo'q, cheksiz foydalanavering 🚀",
                )
            except Exception:
                pass
            try:
                await q.edit_message_caption(caption=q.message.caption + "\n\n✅ TASDIQLANDI")
            except Exception:
                pass
        else:
            await q.answer("Foydalanuvchi bazada topilmadi!", show_alert=True)

    elif data.startswith("vipno_"):
        if user.id != ADMIN_ID:
            await q.answer("Bu tugma faqat admin uchun!", show_alert=True)
            return
        uid = int(data.replace("vipno_", ""))
        try:
            await context.bot.send_message(
                uid,
                "❌ Afsuski, to'lovingiz tasdiqlanmadi.\n"
                "Agar bu xato deb hisoblasangiz, chekni qayta yuboring: /vip",
            )
        except Exception:
            pass
        try:
            await q.edit_message_caption(caption=q.message.caption + "\n\n❌ RAD ETILDI")
        except Exception:
            pass

    elif data == "check_sub":
        if await is_subscribed(user.id, context):
            await q.edit_message_text("✅ Obuna tasdiqlandi! Endi rasm yuboring.")
        else:
            await q.answer("Hali obuna bo'lmagansiz.", show_alert=True)


async def handle_split(q, context):
    data = context.user_data.get("last_pdf")
    if not data:
        await q.edit_message_text("❌ PDF topilmadi. Qaytadan yuboring.")
        return
    if not HAS_FITZ:
        await q.edit_message_text("❌ Bu funksiya hozircha mavjud emas.")
        return

    chat_id = q.message.chat_id
    user = q.from_user

    allowed, _ = check_limit(user.id)
    if not allowed:
        await q.edit_message_text(
            f"⛔ Bugungi limit tugadi ({FREE_DAILY_LIMIT})." + VIP_AD
        )
        return

    await q.edit_message_text("⏳ Ajratilmoqda...")
    await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_PHOTO)

    try:
        doc = fitz.open(stream=data, filetype="pdf")
        total = len(doc)
        n = min(total, MAX_SPLIT_PAGES)
        media = []
        for i in range(n):
            page = doc.load_page(i)
            pix = page.get_pixmap(dpi=150)
            png = pix.tobytes("png")
            media.append(InputMediaPhoto(media=io.BytesIO(png)))
        doc.close()

        # 10 talab guruhlab yuboramiz (media group limiti)
        for i in range(0, len(media), 10):
            await context.bot.send_media_group(chat_id, media[i : i + 10])

        note = ""
        if total > MAX_SPLIT_PAGES:
            note = f"\n⚠️ PDF'da {total} sahifa bor, faqat birinchi {MAX_SPLIT_PAGES} tasi yuborildi."
        await context.bot.send_message(
            chat_id, f"✅ {n} ta sahifa rasm sifatida yuborildi.{note}"
        )
        record_make(user.id)
    except Exception as e:
        logger.error(f"split: {e}")
        await context.bot.send_message(chat_id, "❌ Ajratishda xatolik.")
    finally:
        context.user_data.pop("last_pdf", None)


async def handle_merge_now(q, context):
    merge_list = context.user_data.get("merge_pdfs", [])
    if len(merge_list) < 2:
        await q.answer("Kamida 2 ta PDF kerak.", show_alert=True)
        return
    if not HAS_PYPDF:
        await q.edit_message_text("❌ Kutubxona yo'q: pip install pypdf")
        return

    chat_id = q.message.chat_id
    user = q.from_user

    allowed, remaining = check_limit(user.id)
    if not allowed:
        await q.edit_message_text(
            f"⛔ Bugungi limit tugadi ({FREE_DAILY_LIMIT})." + VIP_AD
        )
        return

    await q.edit_message_text("⏳ Birlashtirilmoqda...")
    await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)

    try:
        writer = PdfWriter()
        for _, raw in merge_list:
            reader = PdfReader(io.BytesIO(raw))
            for page in reader.pages:
                writer.add_page(page)
        out = io.BytesIO()
        writer.write(out)
        out.seek(0)

        await context.bot.send_document(
            chat_id=chat_id,
            document=out,
            filename="birlashgan.pdf",
            caption=f"✅ {len(merge_list)} ta PDF birlashtirildi.",
        )
        record_make(user.id)
    except Exception as e:
        logger.error(f"merge: {e}")
        await context.bot.send_message(chat_id, "❌ Birlashtirishda xatolik.")
    finally:
        context.user_data["mode"] = "image"
        context.user_data["merge_pdfs"] = []


# ==================== XATO HANDLER ====================
async def error_handler(update, context):
    logger.error(f"Xatolik: {context.error}")


# ==================== MAIN ====================
def main():
    init_db()
    app = (ApplicationBuilder()
           .token(BOT_TOKEN)
           .connect_timeout(30).read_timeout(30).write_timeout(30)
           .build())

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("yordam", help_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("pdf", pdf_cmd))
    app.add_handler(CommandHandler("vip", vip_cmd))
    app.add_handler(CommandHandler("tozalash", clear_cmd))
    app.add_handler(CommandHandler("otkaz", cancel_cmd))
    app.add_handler(CommandHandler("birlashtir", merge_cmd))
    app.add_handler(CommandHandler("merge", merge_cmd))
    app.add_handler(CommandHandler("stat", stat_cmd))
    app.add_handler(CommandHandler("top", top_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("pro", pro_cmd))
    app.add_handler(CommandHandler("unpro", unpro_cmd))

    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE, handle_document_image))
    app.add_handler(MessageHandler(filters.Document.MimeType("application/pdf"), handle_pdf))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(error_handler)

    print("✅ Bot ishga tushdi...")
    app.run_polling()


if __name__ == "__main__":
    main()
