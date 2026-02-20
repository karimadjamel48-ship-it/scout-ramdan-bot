# bot.py
import os
import logging
import asyncio
from pathlib import Path

from PIL import Image, ImageOps
from telegram import Update
from telegram.constants import ChatAction
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# إعدادات (ENV)
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# قالب الشريط السفلي (PNG شفافة) - حط ملفك هنا
# مثال: assets/ramadan_bar.png
OVERLAY_PATH = os.getenv("OVERLAY_PATH", "assets/ramadan_bar.png")

# مجلد العمل
WORKDIR = Path(os.getenv("WORKDIR", "work"))
WORKDIR.mkdir(parents=True, exist_ok=True)

# =========================
# LOGGING
# =========================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("ramdan-bot")


# =========================
# أدوات معالجة الصور
# =========================
MAX_SIDE = 4096
MIN_SIDE = 64


def normalize_for_telegram(in_path: str) -> str:
    """
    - يصلح EXIF rotation
    - يحول RGB
    - يمنع أبعاد صغيرة جدا / كبيرة جدا
    - يخرج JPEG جاهز لـ sendPhoto
    """
    img = Image.open(in_path)
    img = ImageOps.exif_transpose(img)

    if img.mode != "RGB":
        img = img.convert("RGB")

    w, h = img.size
    log.info("ORIGINAL SIZE: %sx%s", w, h)

    if w < MIN_SIDE or h < MIN_SIDE:
        scale = max(MIN_SIDE / w, MIN_SIDE / h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        w, h = img.size

    if max(w, h) > MAX_SIDE:
        img.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)

    safe_path = str(Path(in_path).with_suffix("")) + "_tg.jpg"
    img.save(safe_path, format="JPEG", quality=92, optimize=True)
    log.info("FINAL SIZE: %sx%s", img.size[0], img.size[1])

    return safe_path


def apply_bottom_overlay(photo_path: str, overlay_path: str) -> str:
    """
    يركّب شريط سفلي (PNG شفافة) على الصورة.
    - overlay يتعدل عرضه تلقائيا على عرض الصورة
    - يتحط تحت
    """
    base = Image.open(photo_path)
    base = ImageOps.exif_transpose(base)

    # نخليها RGBA باش نركب PNG alpha
    if base.mode != "RGBA":
        base = base.convert("RGBA")

    if not Path(overlay_path).exists():
        # إذا ماكانش overlay، نرجع نفس الصورة
        out_path = WORKDIR / f"out_{Path(photo_path).stem}.png"
        base.save(out_path, format="PNG")
        return str(out_path)

    overlay = Image.open(overlay_path)
    if overlay.mode != "RGBA":
        overlay = overlay.convert("RGBA")

    bw, bh = base.size

    # نعدل overlay على عرض الصورة
    ow, oh = overlay.size
    new_oh = max(1, int((bw / ow) * oh))
    overlay = overlay.resize((bw, new_oh), Image.LANCZOS)

    # نركب تحت
    y = bh - new_oh
    if y < 0:
        # إذا overlay أطول من الصورة، نكبر الصورة أو نقص overlay
        # هنا نختار نقص overlay لتناسب
        overlay = overlay.crop((0, 0, bw, bh))
        y = 0

    composed = base.copy()
    composed.alpha_composite(overlay, (0, y))

    out_path = WORKDIR / f"out_{Path(photo_path).stem}.png"
    composed.save(out_path, format="PNG")
    return str(out_path)


async def safe_send_photo(update: Update, image_path: str, caption: str = "✅ تفضل 🌙"):
    """
    يطبع + يرسل بصورة بعد normalize.
    إذا رفض Telegram sendPhoto -> يرسل Document تلقائيا.
    """
    try:
        safe_path = normalize_for_telegram(image_path)
        with open(safe_path, "rb") as f:
            await update.message.reply_photo(photo=f, caption=caption)
        return

    except BadRequest as e:
        log.exception("PHOTO FAILED, fallback to document: %s", e)

        # fallback: send as document (أقل تشدد)
        try:
            with open(image_path, "rb") as f:
                await update.message.reply_document(document=f, caption=caption)
        except Exception:
            log.exception("Document fallback also failed.")


# =========================
# Handlers
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 مرحبا!\n"
        "📸 ابعثلي صورة وأنا نركّبلها الشريط الرمضاني السفلي.\n"
        "✅ النتيجة ترجعلك مباشرة."
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.chat.send_action(action=ChatAction.UPLOAD_PHOTO)

        # ناخذ أعلى دقة
        photo = update.message.photo[-1]
        file = await photo.get_file()

        in_path = WORKDIR / f"in_{photo.file_unique_id}.jpg"
        await file.download_to_drive(custom_path=str(in_path))

        # ركّب overlay
        out_path = apply_bottom_overlay(str(in_path), OVERLAY_PATH)

        # إرسال آمن
        await safe_send_photo(update, out_path)

    except Exception:
        log.exception("PHOTO ERROR")
        await update.message.reply_text("صار خطأ أثناء معالجة الصورة 😅 جرب صورة أخرى.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📸 ابعثلي صورة برك، وأنا نخدمها.")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("GLOBAL ERROR:", exc_info=context.error)


# =========================
# Main
# =========================
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN ناقص. حطو في Environment Variables.")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(on_error)

    # مهم لتفادي مشاكل قديمة + يساعد مع بعض حالات conflict
    # ملاحظة: 409 الحقيقي يجي إذا كاين instance أخرى شغالة، هذا لازم توقفها من الاستضافة.
    log.info("Application started (polling).")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
