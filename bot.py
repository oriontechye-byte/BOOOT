import os
import re
import sqlite3
import asyncio
from typing import List, Tuple
from telegram import Update, Document, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

# -------------------- الإعدادات --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip() or "0")
DB_PATH = os.getenv("DB_PATH", "mcq.db")

# التحقق من المشرف
def is_admin(update: Update) -> bool:
    return update.effective_user is not None and update.effective_user.id == ADMIN_ID

# -------------------- قاعدة البيانات --------------------
def init_db():
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS questions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                q TEXT NOT NULL,
                options TEXT NOT NULL,
                correct_idx INTEGER NOT NULL,
                explanation TEXT
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS targets(
                chat_id INTEGER PRIMARY KEY
            )
        """)
        con.commit()

# -------------------- تحليل النصوص (TXT) --------------------

def parse_one_block(block: str) -> Tuple[str, List[str], int, str]:
    """تحليل سؤال واحد"""
    lines = [l.strip() for l in block.strip().splitlines() if l.strip()]
    q = ""
    options: List[str] = []
    ans = None
    exp = ""

    # أنماط مرنة (Regex)
    q_pattern = re.compile(r"^(?:س|Q|S|السؤال)\s*[:\-\/]\s*(.*)", re.IGNORECASE)
    ans_pattern = re.compile(r"^(?:صح|الجواب|Ans|Answer|ج)\s*[:\-\/]\s*(\d+)", re.IGNORECASE)
    exp_pattern = re.compile(r"^(?:شرح|توضيح|Exp|السبب)\s*[:\-\/]\s*(.*)", re.IGNORECASE)
    opt_pattern = re.compile(r"^[A-F0-9][\)\.\-]\s*(.*)", re.IGNORECASE)

    for l in lines:
        if m_q := q_pattern.match(l):
            q = m_q.group(1).strip()
        elif m_ans := ans_pattern.match(l):
            ans = int(m_ans.group(1))
        elif m_exp := exp_pattern.match(l):
            exp = m_exp.group(1).strip()
        elif opt_pattern.match(l):
            clean_opt = re.sub(r"^[A-F0-9][\)\.\-]\s*", "", l)
            options.append(clean_opt)

    if not q: raise ValueError("لا يوجد نص للسؤال (س:)")
    if len(options) < 2: raise ValueError("الخيارات أقل من 2")
    if ans is None: raise ValueError("لم يتم تحديد الإجابة (صح:)")
    
    correct_idx = ans - 1
    if correct_idx < 0 or correct_idx >= len(options):
        raise ValueError(f"رقم الإجابة {ans} غير صحيح")

    return q, options, correct_idx, exp

def parse_payload(payload: str) -> Tuple[List[Tuple], List[str]]:
    """تقسيم الملف الكامل"""
    parts = []
    current = []
    for line in payload.splitlines():
        if line.strip() == "---":
            if current: parts.append("\n".join(current))
            current = []
        else:
            current.append(line)
    if current: parts.append("\n".join(current))

    parsed = []
    errors = []
    for i, block in enumerate(parts, start=1):
        if not block.strip(): continue
        try:
            parsed.append(parse_one_block(block))
        except Exception as e:
            errors.append(f"سؤال #{i}: {e}")
    return parsed, errors

# -------------------- لوحة التحكم (الأزرار) --------------------

async def show_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return

    with sqlite3.connect(DB_PATH) as con:
        q_count = con.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        t_count = con.execute("SELECT COUNT(*) FROM targets").fetchone()[0]

    keyboard = [
        [
            InlineKeyboardButton(f"📚 الأسئلة: {q_count}", callback_data="noop"),
            InlineKeyboardButton(f"📢 القنوات: {t_count}", callback_data="noop")
        ],
        [
            InlineKeyboardButton("📤 إرسال سؤال واحد", callback_data="send_one"),
            InlineKeyboardButton("🚀 إرسال الكل", callback_data="send_all")
        ],
        [
            InlineKeyboardButton("🗑️ حذف الكل", callback_data="clear_confirm"),
            InlineKeyboardButton("🔄 تحديث", callback_data="refresh")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = "👋 **لوحة تحكم البوت**"

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")

async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "refresh":
        await show_admin_panel(update, context)

    elif data == "noop":
        await query.answer("مجرد عداد للعرض 📊")

    elif data == "send_one":
        await send_next_question(update, context)
        await show_admin_panel(update, context)

    elif data == "send_all":
        await query.message.reply_text("🚀 بدأ الإرسال الجماعي...")
        await send_all_questions(update, context)
        await show_admin_panel(update, context)

    elif data == "clear_confirm":
        keyboard = [
            [InlineKeyboardButton("نعم، احذف ⚠️", callback_data="clear_final")],
            [InlineKeyboardButton("تراجع 🔙", callback_data="refresh")]
        ]
        await query.edit_message_text("⁉️ **هل أنت متأكد؟** سيتم تصفير الطابور.", 
                                      reply_markup=InlineKeyboardMarkup(keyboard), 
                                      parse_mode="Markdown")

    elif data == "clear_final":
        with sqlite3.connect(DB_PATH) as con:
            con.execute("DELETE FROM questions")
            con.execute("DELETE FROM sqlite_sequence WHERE name='questions'")
        await query.answer("تم التنظيف 🗑️")
        await show_admin_panel(update, context)

# -------------------- الإرسال --------------------

async def _send_quiz(app, chat_id, q, opts, correct_idx, exp):
    await app.bot.send_poll(
        chat_id=chat_id,
        question=q,
        options=opts,
        type="quiz",
        correct_option_id=correct_idx,
        explanation=exp[:200] if exp else None,
        is_anonymous=False
    )

async def send_next_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute("SELECT id, q, options, correct_idx, explanation FROM questions ORDER BY id ASC LIMIT 1").fetchone()
        targets = [r[0] for r in con.execute("SELECT chat_id FROM targets").fetchall()]

    if not targets:
        await context.bot.send_message(update.effective_chat.id, "❌ لا توجد أهداف (/settarget).")
        return
    if not row:
        await context.bot.send_message(update.effective_chat.id, "❌ لا توجد أسئلة.")
        return

    qid, q, raw_opts, c_idx, exp = row
    opts = raw_opts.split("|||")

    for chat_id in targets:
        try:
            await _send_quiz(context.application, chat_id, q, opts, c_idx, exp)
        except Exception: pass

    with sqlite3.connect(DB_PATH) as con:
        con.execute("DELETE FROM questions WHERE id=?", (qid,))

async def send_all_questions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute("SELECT id, q, options, correct_idx, explanation FROM questions ORDER BY id ASC").fetchall()
        targets = [r[0] for r in con.execute("SELECT chat_id FROM targets").fetchall()]

    if not rows or not targets: return

    msg = await context.bot.send_message(update.effective_chat.id, f"⏳ إرسال {len(rows)} سؤال...")
    ids_to_delete = []
    
    for i, (qid, q, raw_opts, c_idx, exp) in enumerate(rows):
        opts = raw_opts.split("|||")
        for chat_id in targets:
            try:
                await _send_quiz(context.application, chat_id, q, opts, c_idx, exp)
            except Exception: pass
        
        ids_to_delete.append(qid)
        # تأخير لتجنب الحظر
        await asyncio.sleep(2)
        if i % 10 == 0: await asyncio.sleep(3)

    with sqlite3.connect(DB_PATH) as con:
        for x in ids_to_delete:
            con.execute("DELETE FROM questions WHERE id=?", (x,))
    
    await msg.edit_text("✅ انتهى الإرسال.")

# -------------------- معالجة TXT فقط --------------------

async def handle_txt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return

    doc = update.message.document
    if not doc.file_name.lower().endswith(".txt"):
        await update.message.reply_text("❌ أرسل ملف .txt فقط.")
        return

    msg = await update.message.reply_text("⏳ جاري التحليل...")
    
    f = await doc.get_file()
    content_bytes = await f.download_as_bytearray()

    try:
        content = content_bytes.decode("utf-8")
    except:
        try: content = content_bytes.decode("cp1256")
        except: 
            await msg.edit_text("❌ ترميز الملف غير مدعوم.")
            return

    parsed, errors = parse_payload(content)

    if parsed:
        with sqlite3.connect(DB_PATH) as con:
            for (q, opts, c_idx, exp) in parsed:
                con.execute(
                    "INSERT INTO questions(q, options, correct_idx, explanation) VALUES(?,?,?,?)",
                    (q, "|||".join(opts), c_idx, exp)
                )
        await msg.edit_text(f"✅ تم حفظ {len(parsed)} سؤال.\nافتح اللوحة: /admin")
    else:
        err_msg = "\n".join(errors[:5])
        await msg.edit_text(f"⚠️ أخطاء في الملف:\n{err_msg}")

async def settarget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return
    chat_id = update.effective_chat.id
    with sqlite3.connect(DB_PATH) as con:
        con.execute("INSERT OR IGNORE INTO targets(chat_id) VALUES(?)", (chat_id,))
    await update.message.reply_text(f"✅ تم تفعيل الهدف: {chat_id}")

# -------------------- التشغيل --------------------
def main():
    if not BOT_TOKEN:
        print("BOT_TOKEN is missing!")
        return
    
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler(["start", "admin"], show_admin_panel))
    app.add_handler(CommandHandler("settarget", settarget))
    app.add_handler(CallbackQueryHandler(button_click))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_txt))

    print("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
