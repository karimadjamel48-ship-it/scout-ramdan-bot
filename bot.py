import os
import asyncio
import logging
import traceback
from PIL import Image, ImageFilter, ImageOps, ImageFile

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---- Fix truncated images ----
ImageFile.LOAD_TRUNCATED_IMAGES = True

# ---- Logging ----
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "").strip()

TEMPLATE_PATH = "ramdan_cadre.png"

# ✅ مهم: مسار خاص بـ Termux
TMP_DIR = os.path.join(os.path.expanduser("~"), "tmp")

WELCOME_TEXT = (
    "✨ أهلاً!\n\n"
    "📸 ابعث صورك، نقصّهم تلقائي 16/9 ونركّب القالب ونرجعهم لك واحد بواحد."
)

WAIT_TEXT = "⏳ راني نخدم عليها..."
ERROR_TEXT = "❌ صرا خطأ أثناء المعالجة."
ONLY_PHOTO_TEXT = "📌 ابعث صورة فقط."

USER_LOCKS: dict[int, asyncio.Lock] = {}

def get_user_lock(user_id: int) -> asyncio.Lock:
    lock = USER_LOCKS.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        USER_LOCKS[user_id] = lock
    return lock

# ---------------- Template cache ----------------
TEMPLATE_HOLE = None
TEMPLATE_SIZE = None

def build_black_region_mask(template_rgba: Image.Image, threshold: int = 20) -> Image.Image:
    rgb = template_rgba.convert("RGB")
    src = rgb.get_flattened_data()
    out = []
    for (r, g, b) in src:
        out.append(255 if (r <= threshold and g <= threshold and b <= threshold) else 0)

    mask = Image.new("L", rgb.size)
    mask.putdata(out)
    mask = mask.filter(ImageFilter.GaussianBlur(radius=1.2))
    return mask

def punch_hole_in_template(template_rgba: Image.Image, hole_mask: Image.Image) -> Image.Image:
    r, g, b, a = template_rgba.split()
    zero = Image.new("L", template_rgba.size, 0)
    new_alpha = Image.composite(zero, a, hole_mask)
    return Image.merge("RGBA", (r, g, b, new_alpha))

def load_template_once():
    global TEMPLATE_HOLE, TEMPLATE_SIZE
    if TEMPLATE_HOLE is not None:
        return
    template = Image.open(TEMPLATE_PATH).convert("RGBA")
    TEMPLATE_SIZE = template.size
    hole_mask = build_black_region_mask(template, threshold=20)
    TEMPLATE_HOLE = punch_hole_in_template(template, hole_mask)

# ---------------- Image processing ----------------
def center_crop_to_ratio(img: Image.Image, target_ratio: float) -> Image.Image:
    iw, ih = img.size
    img_ratio = iw / ih
    if img_ratio > target_ratio:
        new_w = int(ih * target_ratio)
        left = (iw - new_w) // 2
        return img.crop((left, 0, left + new_w, ih))
    else:
        new_h = int(iw / target_ratio)
        top = (ih - new_h) // 2
        return img.crop((0, top, iw, top + new_h))

def prepare_user_image(path: str, out_w: int, out_h: int) -> Image.Image:
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")

    # تقليل الحجم لتفادي مشاكل الذاكرة
    img.thumbnail((2500, 2500), Image.LANCZOS)

    img = center_crop_to_ratio(img, 16 / 9)
    img = img.resize((out_w, out_h), Image.LANCZOS)
    return img.convert("RGBA")

def compose_final(user_img_path: str) -> str:
    load_template_once()
    tw, th = TEMPLATE_SIZE
    user = prepare_user_image(user_img_path, tw, th)
    final = Image.alpha_composite(user, TEMPLATE_HOLE).convert("RGB")

    out_path = user_img_path.replace("_in.jpg", "_out.jpg")
    final.save(out_path, "JPEG", quality=90, optimize=True)
    return out_path

# ---------------- Bot handlers ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME_TEXT)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lock = get_user_lock(user_id)

    await update.message.reply_text(WAIT_TEXT)

    async with lock:
        try:
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id,
                action=ChatAction.UPLOAD_PHOTO,
            )

            os.makedirs(TMP_DIR, exist_ok=True)

            msg_id = update.message.message_id
            in_path = os.path.join(TMP_DIR, f"{user_id}_{msg_id}_in.jpg")

            photo = update.message.photo[-1]
            tg_file = await photo.get_file()
            await tg_file.download_to_drive(in_path)

            out_path = compose_final(in_path)

            with open(out_path, "rb") as f:
                await update.message.reply_photo(photo=f, caption="✅ تفضل 🌙")

            try:
                os.remove(in_path)
                os.remove(out_path)
            except Exception:
                pass

        except Exception as e:
            logger.error("PHOTO ERROR: %s", e, exc_info=True)
            await update.message.reply_text(ERROR_TEXT)

async def handle_other(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(ONLY_PHOTO_TEXT)

def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN ناقص.")

    if not os.path.exists(TEMPLATE_PATH):
        raise SystemExit("ramdan_cadre.png غير موجود.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(~filters.PHOTO, handle_other))

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
