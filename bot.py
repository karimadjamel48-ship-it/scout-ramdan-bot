import os
import logging
from pathlib import Path
from PIL import Image, ImageOps
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.error import BadRequest
from telegram.constants import ChatAction

# =======================
# إعدادات
# =======================
BOT_TOKEN = os.getenv("BOT_TOKEN")
WORKDIR = Path("work")
WORKDIR.mkdir(exist_ok=True)

OVERLAY_PATH = "ramadan_bar.png"  # لازم يكون في نفس فولدر bot.py

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ramadan-bot")

# =======================
# قص مباشر إلى 16:9 (بدون تدوير)
# =======================
def crop_to_16x9_paysage(img: Image.Image) -> Image.Image:
    img = ImageOps.exif_transpose(img)
    w, h = img.size

    target_ratio = 16 / 9
    current_ratio = w / h

    if current_ratio > target_ratio:
        # عريضة بزاف -> نقص الجوانب
        new_w = int(h * target_ratio)
        left = (w - new_w) // 2
        return img.crop((left, 0, left + new_w, h))
    else:
        # طولية أو مربعة -> نقص من فوق وتحت
        new_h = int(w / target_ratio)
        top = (h - new_h) // 2
        return img.crop((0, top, w, top + new_h))

# =======================
# تركيب القالب
# =======================
def apply_overlay(photo_path: str) -> str:
    overlay = Image.open(OVERLAY_PATH).convert("RGBA")
    target_w, target_h = overlay.size

    base = Image.open(photo_path)
    base = ImageOps.exif_transpose(base).convert("RGBA")

    # قص 16:9
    base = crop_to_16x9_paysage(base)

    # Resize فقط
    base = base.resize((target_w, target_h), Image.LANCZOS)

    # تركيب مباشر
    result = Image.alpha_composite(base, overlay)

    out_path = WORKDIR / f"out_{Path(photo_path).stem}.png"
    result.save(out_path, format="PNG")

    return str(out_path)
        
# =======================
# تجهيز للإرسال
# =======================
def normalize_for_telegram(path: str) -> str:
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)

    if img.mode != "RGB":
        img = img.convert("RGB")

    if max(img.size) > 4096:
        img.thumbnail((4096, 4096), Image.LANCZOS)

    final_path = str(Path(path).with_suffix("")) + "_tg.jpg"
    img.save(final_path, format="JPEG", quality=92, optimize=True)
    return final_path

async def safe_send(update: Update, image_path: str):
    try:
        final = normalize_for_telegram(image_path)
        with open(final, "rb") as f:
            await update.message.reply_photo(photo=f, caption="✅ تفضل 🌙")
    except BadRequest:
        with open(image_path, "rb") as f:
            await update.message.reply_document(document=f, caption="✅ تفضل 🌙")

# =======================
# Handlers
# =======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📸 ابعث صورة، نعملها 16:9 paysage ونركب قالب رمضان 🌙")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.chat.send_action(action=ChatAction.UPLOAD_PHOTO)

    photo = update.message.photo[-1]
    file = await photo.get_file()

    in_path = WORKDIR / f"in_{photo.file_unique_id}.jpg"
    await file.download_to_drive(str(in_path))

    out_path = apply_overlay(str(in_path))
    await safe_send(update, out_path)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("ERROR:", exc_info=context.error)

# =======================
# Main
# =======================
def main():
    if not BOT_TOKEN:
        raise RuntimeError("حط BOT_TOKEN في Environment Variables")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_error_handler(error_handler)

    print("Bot started...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()


