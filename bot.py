import os
import re
import asyncio
from PIL import Image, ImageFilter, ImageDraw, ImageFont

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# -------- Fix event loop (Python جديد) --------
try:
    asyncio.get_running_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())
# ---------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "").strip()

TEMPLATE_PATH = "scout_ramdan.png"   # قالبك (300x500)
FONT_PATH = "HacenBeirut.ttf"

# ✅ صندوق الاسم كنِسَب (مضبوط قريب من الخط الأحمر فوق "يتمنى لكم")
# عدّل y قليلاً إذا تحب يطلع/يهبط: +/- 0.01
NAME_BOX_PCT = (0.08, 0.515, 0.92, 0.595)
NAME_PADDING = 14  # مهم على قالب صغير

MAX_FONT = 64
MIN_FONT = 14

WELCOME_TEXT = (
    "✨ أهلاً بك!\n\n"
    "📌 هذا البوت يجمع صوركم لعمل تصميم رمضاني خاص بالكشافة.\n"
    "✅ بعد إرسال *اسمك* و*صورتك* سيتم إنشاء الصورة ونشرها على مستوى صفحة:\n"
    "«صدى الكشافة» التابعة للقيادة العامة لزيادة التفاعل.\n\n"
    "✍️ اكتب اسمك ولقبك الآن (إجباري)."
)

ASK_NAME_TEXT = "✍️ اكتب اسمك ولقبك من فضلك (إجباري):"
ASK_PHOTO_TEXT = "📸 الآن ابعث الصورة تاعك (بالطول ولا بالعرض… ماشي مشكل)."
WAIT_TEXT = "⏳ وصلتني… راني نخدم عليها الآن، شوية برك!"
RECEIVED_TEXT = (
    "✅ تم استلام مشاركتك!\n"
    "📌 سيتم إنشاء الصورة ونشرها على مستوى صفحة «صدى الكشافة» التابعة للقيادة العامة."
)
NEED_NAME_FIRST = "📌 لازم تكتب اسمك أولاً. اكتب اسمك الآن ✍️"
BAD_NAME_TEXT = "❌ الاسم قصير بزاف. اكتب اسم واضح (على الأقل 3 حروف)."
ERROR_TEXT = "❌ صرا خطأ أثناء المعالجة. جرّب من جديد بعد شوية."
ADMIN_MISSING_TEXT = "⚠️ ADMIN_CHAT_ID ناقص. لازم تحطّو في Environment Variables."


# ---------------- Text helpers ----------------
def clean_name(name: str) -> str:
    name = (name or "").strip()
    # حذف علامات اتجاه مخفية
    name = (
        name.replace("\u200f", "")
            .replace("\u200e", "")
            .replace("\u202a", "")
            .replace("\u202b", "")
            .replace("\u202c", "")
    )
    name = re.sub(r"\s+", " ", name)
    return name


def has_arabic(s: str) -> bool:
    for ch in s:
        o = ord(ch)
        if (0x0600 <= o <= 0x06FF) or (0x0750 <= o <= 0x077F) or (0x08A0 <= o <= 0x08FF):
            return True
    return False


def shape_ar(text: str) -> str:
    """
    Arabic shaping + bidi.
    إذا صارت مشكلة تثبيت، fallback باش ما يبانش مقلوب بزاف.
    """
    text = (text or "").strip()
    if not text:
        return text
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display

        reshaped = arabic_reshaper.reshape(text)
        return get_display(reshaped)
    except Exception:
        return text[::-1] if has_arabic(text) else text


# ---------------- Box helper ----------------
def box_from_pct(w: int, h: int, pct_box):
    x1p, y1p, x2p, y2p = pct_box
    return (int(w * x1p), int(h * y1p), int(w * x2p), int(h * y2p))


# ---------------- Image helpers ----------------
def center_crop_to_aspect(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Smart center-crop to match aspect ratio then resize (no distortion)."""
    iw, ih = img.size
    target_ratio = target_w / target_h
    img_ratio = iw / ih

    if img_ratio > target_ratio:
        new_w = int(ih * target_ratio)
        left = (iw - new_w) // 2
        img = img.crop((left, 0, left + new_w, ih))
    else:
        new_h = int(iw / target_ratio)
        top = (ih - new_h) // 2
        img = img.crop((0, top, iw, top + new_h))

    return img.resize((target_w, target_h), Image.LANCZOS)


def build_black_region_mask(template_rgb: Image.Image, threshold: int = 25) -> Image.Image:
    """
    Mask للمنطقة السوداء (مكان الصورة): الأسود => 255 في الماسك.
    """
    rgb = template_rgb.convert("RGB")
    w, h = rgb.size
    src = rgb.getdata()

    out = []
    for (r, g, b) in src:
        out.append(255 if (r <= threshold and g <= threshold and b <= threshold) else 0)

    mask = Image.new("L", (w, h))
    mask.putdata(out)
    return mask.filter(ImageFilter.GaussianBlur(radius=1.0))


def punch_hole_in_template(template_rgba: Image.Image, hole_mask: Image.Image) -> Image.Image:
    """Make black region transparent (hole) so user photo appears behind it."""
    r, g, b, a = template_rgba.split()
    zero = Image.new("L", template_rgba.size, 0)
    new_alpha = Image.composite(zero, a, hole_mask)  # mask=255 => alpha=0
    return Image.merge("RGBA", (r, g, b, new_alpha))


def fit_font_one_line(draw: ImageDraw.ImageDraw, text: str, box_w: int, box_h: int) -> ImageFont.FreeTypeFont:
    usable_w = max(10, box_w - 2 * NAME_PADDING)
    usable_h = max(10, box_h - 2 * NAME_PADDING)

    for size in range(MAX_FONT, MIN_FONT - 1, -1):
        font = ImageFont.truetype(FONT_PATH, size)
        l, t, r, b = draw.textbbox((0, 0), text, font=font)
        tw = r - l
        th = b - t
        if tw <= usable_w and th <= usable_h:
            return font

    return ImageFont.truetype(FONT_PATH, MIN_FONT)


def draw_name_one_line(img_rgb: Image.Image, name: str) -> Image.Image:
    """
    - سطر واحد فقط
    - Auto shrink
    - Center صحيح حتى مع bbox السلبي
    - بدون stroke (ظل خفيف فقط)
    """
    img = img_rgb.convert("RGB")
    draw = ImageDraw.Draw(img)

    w, h = img.size
    x1, y1, x2, y2 = box_from_pct(w, h, NAME_BOX_PCT)
    box_w = x2 - x1
    box_h = y2 - y1

    name_text = shape_ar(name)
    font = fit_font_one_line(draw, name_text, box_w, box_h)

    l, t, r, b = draw.textbbox((0, 0), name_text, font=font)
    tw = r - l
    th = b - t

    usable_w = box_w - 2 * NAME_PADDING
    usable_h = box_h - 2 * NAME_PADDING

    cx = x1 + NAME_PADDING + (usable_w / 2)
    cy = y1 + NAME_PADDING + (usable_h / 2)

    x = int(cx - (tw / 2) - l)
    y = int(cy - (th / 2) - t)

    # ظل خفيف + أبيض (بدون stroke)
    draw.text((x + 2, y + 2), name_text, font=font, fill=(0, 0, 0))
    draw.text((x, y), name_text, font=font, fill=(255, 255, 255))

    return img


def compose_final(user_img_path: str, name: str) -> str:
    template = Image.open(TEMPLATE_PATH).convert("RGBA")
    tw, th = template.size  # متوقع 300x500

    user = Image.open(user_img_path).convert("RGB")
    user = center_crop_to_aspect(user, tw, th).convert("RGBA")

    hole_mask = build_black_region_mask(template, threshold=25)
    template_hole = punch_hole_in_template(template, hole_mask)

    final = Image.alpha_composite(user, template_hole).convert("RGB")
    final = draw_name_one_line(final, name)

    out_path = user_img_path.replace("_in.jpg", "_out.jpg")
    final.save(out_path, "JPEG", quality=92, optimize=True)
    return out_path


# ---------------- Bot handlers ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(WELCOME_TEXT)
    await update.message.reply_text(ASK_NAME_TEXT)


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🆔 Chat ID: {update.effective_chat.id}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text.startswith("/"):
        return

    name = clean_name(text)
    if len(name) < 3:
        await update.message.reply_text(BAD_NAME_TEXT)
        return

    context.user_data["name"] = name
    await update.message.reply_text(f"✅ تشرفنا يا {name}!\n{ASK_PHOTO_TEXT}")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = context.user_data.get("name")
    if not name:
        await update.message.reply_text(NEED_NAME_FIRST)
        await update.message.reply_text(ASK_NAME_TEXT)
        return

    await update.message.reply_text(WAIT_TEXT)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_PHOTO)

    try:
        os.makedirs("tmp", exist_ok=True)
        msg_id = update.message.message_id
        in_path = os.path.join("tmp", f"{msg_id}_in.jpg")

        photo = update.message.photo[-1]
        tg_file = await photo.get_file()
        await tg_file.download_to_drive(in_path)

        out_path = compose_final(in_path, name)

        # إرسال النتيجة للإدمن فقط
        if not ADMIN_CHAT_ID:
            await update.message.reply_text(ADMIN_MISSING_TEXT)
        else:
            username = update.effective_user.username
            user_line = f"@{username}" if username else f"user_id: {update.effective_user.id}"

            caption = (
                "🟢 مشاركة جديدة جاهزة للنشر\n\n"
                f"👤 الاسم: {name}\n"
                f"🔗 المستخدم: {user_line}"
            )

            await context.bot.send_chat_action(chat_id=int(ADMIN_CHAT_ID), action=ChatAction.UPLOAD_PHOTO)
            with open(out_path, "rb") as f:
                await context.bot.send_photo(chat_id=int(ADMIN_CHAT_ID), photo=f, caption=caption)

        # تأكيد للمستخدم بدون إرسال الصورة
        await update.message.reply_text(RECEIVED_TEXT)

        # تنظيف الملفات
        try:
            os.remove(in_path)
            os.remove(out_path)
        except Exception:
            pass

    except Exception:
        await update.message.reply_text(ERROR_TEXT)


async def handle_other(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📌 اكتب اسمك ثم ابعث صورة فقط ✅")


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN ناقص. ضيفه في Environment Variables.")
    if not os.path.exists(TEMPLATE_PATH):
        raise SystemExit(f"القالب ناقص: {TEMPLATE_PATH}")
    if not os.path.exists(FONT_PATH):
        raise SystemExit(f"الخط ناقص: {FONT_PATH}")

    # منع Conflict + إسقاط التحديثات القديمة
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(~filters.PHOTO & ~filters.TEXT, handle_other))

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
