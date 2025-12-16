import os
import re
import asyncio
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from PyPDF2 import PdfReader

# ================== ENV VARIABLES ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))

if not BOT_TOKEN or not ADMIN_ID:
    raise RuntimeError("BOT_TOKEN or ADMIN_ID is missing")

# ================== STORAGE ==================
QUESTIONS = []          # list of dicts
CURRENT_INDEX = {}      # user_id -> question index

# ================== HELPERS ==================
QUESTION_PATTERN = re.compile(
    r"س:\s*(.*?)\n"
    r"A\)\s*(.*?)\n"
    r"B\)\s*(.*?)\n"
    r"C\)\s*(.*?)\n"
    r"D\)\s*(.*?)\n"
    r"صح:\s*(\d)\n"
    r"شرح:\s*(.*?)(?=\nس:|\Z)",
    re.S
)

def parse_questions(text: str):
    parsed = []
    for match in QUESTION_PATTERN.finditer(text):
        parsed.append({
            "question": match.group(1).strip(),
            "options": [
                match.group(2).strip(),
                match.group(3).strip(),
                match.group(4).strip(),
                match.group(5).strip(),
            ],
            "correct": int(match.group(6)) - 1,
            "explain": match.group(7).strip(),
        })
    return parsed

def admin_only(user_id: int):
    return user_id == ADMIN_ID

# ================== COMMANDS ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update.effective_user.id):
        await update.message.reply_text("❌ هذا البوت مخصص للإدارة فقط.")
        return

    keyboard = [
        [InlineKeyboardButton("📥 رفع ملف أسئلة", callback_data="upload")],
        [InlineKeyboardButton("📊 عدد الأسئلة", callback_data="count")],
        [InlineKeyboardButton("▶️ إرسال سؤال", callback_data="send_one")],
        [InlineKeyboardButton("📢 إرسال الكل", callback_data="send_all")],
        [InlineKeyboardButton("🗑️ مسح الأسئلة", callback_data="clear")],
    ]
    await update.message.reply_text(
        "لوحة تحكم الأدمن",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ================== CALLBACKS ==================
async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    if not admin_only(uid):
        await query.message.reply_text("❌ غير مصرح.")
        return

    if query.data == "count":
        await query.message.reply_text(f"📊 عدد الأسئلة: {len(QUESTIONS)}")

    elif query.data == "clear":
        QUESTIONS.clear()
        CURRENT_INDEX.clear()
        await query.message.reply_text("🗑️ تم مسح جميع الأسئلة.")

    elif query.data == "send_one":
        idx = CURRENT_INDEX.get(uid, 0)
        if idx >= len(QUESTIONS):
            await query.message.reply_text("❌ لا توجد أسئلة متبقية.")
            return
        await send_question(uid, idx, context)
        CURRENT_INDEX[uid] = idx + 1

    elif query.data == "send_all":
        if not QUESTIONS:
            await query.message.reply_text("❌ لا توجد أسئلة.")
            return
        for i in range(len(QUESTIONS)):
            await send_question(uid, i, context)
            await asyncio.sleep(0.7)

# ================== FILE HANDLING ==================
async def file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not admin_only(uid):
        return

    file = await update.message.document.get_file()
    content = ""

    if update.message.document.file_name.endswith(".txt"):
        content = (await file.download_as_bytearray()).decode("utf-8")

    elif update.message.document.file_name.endswith(".pdf"):
        data = await file.download_as_bytearray()
        reader = PdfReader(data)
        for page in reader.pages:
            content += page.extract_text() + "\n"

    else:
        await update.message.reply_text("❌ الصيغة غير مدعومة.")
        return

    parsed = parse_questions(content)
    if not parsed:
        await update.message.reply_text("❌ لم يتم العثور على أسئلة بصيغة صحيحة.")
        return

    QUESTIONS.extend(parsed)
    await update.message.reply_text(f"✅ تم استيراد {len(parsed)} سؤال بنجاح.")

# ================== SEND QUESTION ==================
async def send_question(chat_id: int, index: int, context: ContextTypes.DEFAULT_TYPE):
    q = QUESTIONS[index]
    keyboard = []
    for i, opt in enumerate(q["options"]):
        keyboard.append([
            InlineKeyboardButton(opt, callback_data=f"ans_{index}_{i}")
        ])

    text = f"📝 *سؤال {index+1}:*\n{q['question']}"
    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

# ================== ANSWER HANDLER ==================
async def answer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, q_idx, choice = query.data.split("_")
    q_idx, choice = int(q_idx), int(choice)

    q = QUESTIONS[q_idx]
    if choice == q["correct"]:
        msg = "✅ *إجابة صحيحة*\n" + q["explain"]
    else:
        msg = f"❌ *إجابة خاطئة*\n✅ الصحيح: {q['options'][q['correct']]}\n\n{q['explain']}"

    await query.message.reply_text(msg, parse_mode="Markdown")

# ================== MAIN ==================
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(buttons, pattern="^(count|clear|send_one|send_all)$"))
    app.add_handler(CallbackQueryHandler(answer_handler, pattern="^ans_"))
    app.add_handler(MessageHandler(filters.Document.ALL, file_handler))

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
