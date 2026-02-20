# bot.py
import os
import asyncio
import logging
from PIL import Image, ImageFilter, ImageOps, ImageFile

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.request import HTTPXRequest
from telegram.error import TimedOut

ImageFile.LOAD_TRUNCATED_IMAGES = True

# ---------- Logging ----------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("ramdan-bot")

# ---------- ENV ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

# ✅ القالب الجديد
TEMPLATE_PATH = "ramdan_cadre.png"

TMP_DIR = "tmp"
WELCOME_TEXT = "✨ ابعث صورتك/صورك… نركّب القالب ونرجعهم لك مباشرة 🌙"
WAIT_TEXT = "⏳ راني نخدم عليها…"
ERROR_TEXT = "❌ صرا خطأ أثناء المعالجة. جرّب من جديد."
ONLY_PHOTO_TEXT = "📌 ابعث صورة فقط ✅"

# ---------- Per-user sequential processing (avoid mixing multiple photos) ----------
USER_LOCKS: dict[int, asyncio.Lock] = {}

def get_user_lock(user_id: int) -> asyncio.Lock:
    lock = USER_LOCKS.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        USER_LOCKS[user_id] = lock
    return lock

# ---------- Template cache ----------
TEMPLATE_HOLE = None
TEMPLATE_SIZE = None

def build_black_region_mask(template_rgba: Image.Image, threshold: int = 25) -> Image.Image:
    """
    Mask للمنطقة السوداء (مكان الصورة): الأسود => 255 في الماسك.
    """
    rgb = template_rgba.convert("RGB")
    src = rgb.getdata()

    out = []
    for (r, g, b) in src:
        out.append(255 if (r <= threshold and g <= threshold and b <= threshold) else 0)

    mask = Image.new("L", rgb.size)
    mask.putdata(out)
    mask = mask.filter(ImageFilter.GaussianBlur(radius=1.2))
    return mask

def punch_hole_in_template(template_rgba: Image.Image, hole_mask: Image.Image) -> Image.Image:
    """
    نحول المنطقة السوداء إلى شفافة (Hole) في القالب.
    """
    r, g, b, a = template_rgba.split()
    zero = Image.new("L", template_rgba.size, 0)
    new_alpha = Image.composite(zero, a, hole_mask)  # mask=255 => alpha=0
    return Image.merge("RGBA", (r, g, b, new_alpha))

def load_template_once():
    global TEMPLATE_HOLE, TEMPLATE_SIZE
    if TEMPLATE_HOLE is not None:
        return
    template = Image.open(TEMPLATE_PATH).convert("RGBA")
    TEMPLATE_SIZE = template.size
    hole_mask = build_black_region_mask(template, threshold=25)
    TEMPLATE_HOLE = punch_hole_in_template(template, hole_mask)

# ---------- Image helpers ----------
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
    img = ImageOps.exif_transpose(img)   # يصلح دوران صور الهاتف
    img = img.convert("RGB")

    # تصغير أولي لتقليل الوقت والحجم
    img.thumbnail((2200, 2200), Image.LANCZOS)

    # ✅ قص تلقائي 16/9 (بالعرض)
    img = center_crop_to_ratio(img, 16 / 9)

    # ✅ ثم resize لمقاس القالب بالضبط
    img = img.resize((out_w, out_h), Image.LANCZOS)
    return img.convert("RGBA")

def compose_final(user_img_path: str) -> str:
    load_template_once()
    tw, th = TEMPLATE_SIZE

    user = prepare_user_image(user_img_path, tw, th)
    final = Image.alpha_composite(user, TEMPLATE_HOLE).convert("RGB")

    out_path = user_img_path.replace("_in.jpg", "_out.jpg")
    # جودة أخف شوية لتفادي timeouts في الإرسال
    final.save(out_path, "JPEG", quality=82, optimize=True, progressive=True)
    return out_path

# ---------- Bot handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME_TEXT)

async def safe_send_photo(update: Update, out_path: str):
    # Retry على الإرسال إذا صار timeout
    for attempt in (1, 2, 3):
        try:
            with open(out_path, "rb") as f:
                await update.message.reply_photo(photo=f, caption="✅ تفضل 🌙")
            return
        except TimedOut:
            if attempt == 3:
                raise
            await asyncio.sleep(2 * attempt)

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
            await safe_send_photo(update, out_path)

            # cleanup
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
        raise SystemExit("BOT_TOKEN ناقص. ضيفه في Railway Variables.")

    if not os.path.exists(TEMPLATE_PATH):
        raise SystemExit(f"القالب ناقص: {TEMPLATE_PATH}")

    # ✅ timeouts كبار (Railway/شبكة)
    request = HTTPXRequest(
        connect_timeout=60,
        read_timeout=300,
        write_timeout=300,
        pool_timeout=60,
    )

    app = ApplicationBuilder().token(BOT_TOKEN).request(request).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(~filters.PHOTO, handle_other))

    async def error_handler(update, context):
        logger.error("GLOBAL ERROR:", exc_info=context.error)

    app.add_error_handler(error_handler)

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
