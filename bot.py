import os
import asyncio
from PIL import Image, ImageFilter, ImageOps
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# --- Better event loop fix (works on newer Python) ---
try:
    asyncio.get_running_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())
# ----------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "").strip()

TEMPLATE_PATH = "ramdan_cadre.png"

WELCOME_TEXT = (
    "✨ أهلاً!\n\n"
    "📸 ابعث صورك (حتى لو أكثر من صورة)، والبوت يقصّهم تلقائي 16/9 بالعرض "
    "ويركّب القالب ويرجعهم لك واحد بواحد ✅"
)
WAIT_TEXT = "⏳ وصلتني… راني نخدم عليها!"
ERROR_TEXT = "❌ صرا خطأ أثناء المعالجة. جرّب من جديد."
ONLY_PHOTO_TEXT = "📌 ابعث صورة/صور فقط ✅"

# ✅ قفل لكل مستخدم: يخلي المعالجة واحد بواحد حتى مع ألبوم/عدة صور
USER_LOCKS: dict[int, asyncio.Lock] = {}

def get_user_lock(user_id: int) -> asyncio.Lock:
    lock = USER_LOCKS.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        USER_LOCKS[user_id] = lock
    return lock

# ---------- Image helpers ----------
def center_crop_to_ratio(img: Image.Image, target_ratio: float) -> Image.Image:
    """Center-crop to a target ratio (e.g., 16/9) without distortion."""
    iw, ih = img.size
    img_ratio = iw / ih

    if img_ratio > target_ratio:
        # image wider -> crop left/right
        new_w = int(ih * target_ratio)
        left = (iw - new_w) // 2
        img = img.crop((left, 0, left + new_w, ih))
    else:
        # image taller -> crop top/bottom
        new_h = int(iw / target_ratio)
        top = (ih - new_h) // 2
        img = img.crop((0, top, iw, top + new_h))

    return img

def prepare_user_image(path: str, out_w: int, out_h: int) -> Image.Image:
    """
    - Fix EXIF rotation
    - Force 16:9 landscape crop (target ratio)
    - Resize to template size
    - Safe downscale for huge images
    """
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)  # ✅ fixes phone rotations
    img = img.convert("RGB")

    # ✅ قص 16/9 بالعرض
    target_ratio = 16 / 9
    img = center_crop_to_ratio(img, target_ratio)

    # ✅ إذا كانت الصورة ضخمة بزاف، نقصها قبل resize لتفادي مشاكل الذاكرة
    # (اختياري لكن مفيد)
    img.thumbnail((5000, 5000), Image.LANCZOS)

    # ✅ Resize لمقاس القالب
    img = img.resize((out_w, out_h), Image.LANCZOS)
    return img.convert("RGBA")

def build_black_region_mask(template_rgba: Image.Image, threshold: int = 20) -> Image.Image:
    """Mask للمنطقة السوداء (مكان الصورة): الأسود => 255 في الماسك."""
    rgb = template_rgba.convert("RGB")
    w, h = rgb.size
    src = rgb.getdata()

    out = []
    for (r, g, b) in src:
        out.append(255 if (r <= threshold and g <= threshold and b <= threshold) else 0)

    mask = Image.new("L", (w, h))
    mask.putdata(out)
    mask = mask.filter(ImageFilter.GaussianBlur(radius=1.2))
    return mask

def punch_hole_in_template(template_rgba: Image.Image, hole_mask: Image.Image) -> Image.Image:
    """نحوّل المنطقة السوداء إلى شفافة في القالب."""
    r, g, b, a = template_rgba.split()
    zero = Image.new("L", template_rgba.size, 0)
    new_alpha = Image.composite(zero, a, hole_mask)  # mask=255 => alpha=0
    return Image.merge("RGBA", (r, g, b, new_alpha))

def compose_final(user_img_path: str) -> str:
    template = Image.open(TEMPLATE_PATH).convert("RGBA")
    tw, th = template.size

    user = prepare_user_image(user_img_path, tw, th)

    hole_mask = build_black_region_mask(template, threshold=20)
    template_hole = punch_hole_in_template(template, hole_mask)

    final = Image.alpha_composite(user, template_hole).convert("RGB")

    out_path = user_img_path.replace("_in.jpg", "_out.jpg")
    final.save(out_path, "JPEG", quality=92, optimize=True)
    return out_path

# ---------- Bot handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME_TEXT)

async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🆔 Chat ID: {update.effective_chat.id}")

async def process_one_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_PHOTO)

    os.makedirs("tmp", exist_ok=True)
    msg_id = update.message.message_id
    in_path = os.path.join("tmp", f"{update.effective_user.id}_{msg_id}_in.jpg")

    photo = update.message.photo[-1]
    tg_file = await photo.get_file()
    await tg_file.download_to_drive(in_path)

    out_path = compose_final(in_path)

    # ✅ إرسال النتيجة للمستخدم
    with open(out_path, "rb") as f:
        await update.message.reply_photo(photo=f, caption="✅ تفضل! (قصّ تلقائي 16/9) 🌙")

    # (اختياري) نسخة للأدمن
    if ADMIN_CHAT_ID:
        username = update.effective_user.username
        user_line = f"@{username}" if username else f"user_id: {update.effective_user.id}"
        caption = f"🟢 صورة جديدة\n🔗 المستخدم: {user_line}"

        await context.bot.send_chat_action(chat_id=int(ADMIN_CHAT_ID), action=ChatAction.UPLOAD_PHOTO)
        with open(out_path, "rb") as f:
            await context.bot.send_photo(chat_id=int(ADMIN_CHAT_ID), photo=f, caption=caption)

    # Cleanup
    try:
        os.remove(in_path)
        os.remove(out_path)
    except Exception:
        pass

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lock = get_user_lock(user_id)

    await update.message.reply_text(WAIT_TEXT)

    async with lock:
        try:
            await process_one_photo(update, context)
        except Exception:
            await update.message.reply_text(ERROR_TEXT)

async def handle_other(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(ONLY_PHOTO_TEXT)

def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN ناقص. ضيفه في Environment Variables.")

    if not os.path.exists(TEMPLATE_PATH):
        raise SystemExit(f"القالب ناقص: {TEMPLATE_PATH}")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("myid", myid))

    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(~filters.PHOTO, handle_other))

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
