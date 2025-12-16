import os
import re
import sqlite3
import asyncio
from typing import List, Tuple
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, PollAnswer
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    PollAnswerHandler,
    filters,
)

# -------------------- الإعدادات --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = 7358178408  # آيديك
DB_PATH = "super_mcq.db"

# -------------------- قاعدة البيانات --------------------
def init_db():
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""CREATE TABLE IF NOT EXISTS questions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, q TEXT, options TEXT, correct_idx INTEGER, explanation TEXT)""")
        con.execute("""CREATE TABLE IF NOT EXISTS targets(chat_id INTEGER PRIMARY KEY, title TEXT)""")
        con.execute("""CREATE TABLE IF NOT EXISTS active_polls(poll_id TEXT PRIMARY KEY, correct_idx INTEGER)""")
        con.execute("""CREATE TABLE IF NOT EXISTS user_scores(
            user_id INTEGER PRIMARY KEY, first_name TEXT, correct_count INTEGER DEFAULT 0, total_count INTEGER DEFAULT 0)""")
        con.commit()

def get_stats():
    with sqlite3.connect(DB_PATH) as con:
        q_count = con.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        t_count = con.execute("SELECT COUNT(*) FROM targets").fetchone()[0]
    return q_count, t_count

# -------------------- لوحة التحكم --------------------
def get_dashboard_markup(q_count, t_count):
    keyboard = [
        [
            InlineKeyboardButton(f"📦 الأسئلة: {q_count}", callback_data="ignore"),
            InlineKeyboardButton(f"📢 القنوات: {t_count}", callback_data="show_channels")
        ],
        [InlineKeyboardButton("📤 إرسال سؤال واحد", callback_data="send_one")],
        [InlineKeyboardButton("🚀 إرسال الكل (تلقائي)", callback_data="send_all")],
        [
            InlineKeyboardButton("🗑️ حذف الأسئلة", callback_data="clear_ask"),
            InlineKeyboardButton("🔄 تحديث اللوحة", callback_data="refresh")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

async def show_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    q, t = get_stats()
    await update.message.reply_text("🎛 **لوحة التحكم الرئيسية**", reply_markup=get_dashboard_markup(q, t), parse_mode="Markdown")

async def refresh_panel_inplace(query, context):
    q, t = get_stats()
    try: await query.edit_message_reply_markup(reply_markup=get_dashboard_markup(q, t))
    except: pass

# -------------------- معالجة الأزرار --------------------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return
    data = query.data

    if data == "show_channels":
        with sqlite3.connect(DB_PATH) as con:
            rows = con.execute("SELECT chat_id, title FROM targets").fetchall()
        if not rows:
            await query.answer("❌ لا توجد قنوات! استخدم /forceadd لإضافتها.", show_alert=True)
            return
        keyboard = []
        msg_text = "📺 **القنوات المرتبطة:**\n"
        for cid, title in rows:
            keyboard.append([InlineKeyboardButton(f"🗑️ حذف: {title}", callback_data=f"del_target_{cid}")])
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="refresh")])
        await query.edit_message_text(msg_text, reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("del_target_"):
        cid = int(data.split("_")[2])
        with sqlite3.connect(DB_PATH) as con: con.execute("DELETE FROM targets WHERE chat_id=?", (cid,))
        await query.answer("✅ تم الحذف.")
        q, t = get_stats()
        await query.edit_message_text("🎛 **لوحة التحكم**", reply_markup=get_dashboard_markup(q, t), parse_mode="Markdown")

    elif data == "refresh":
        await refresh_panel_inplace(query, context); await query.answer("تم التحديث")

    elif data == "send_one":
        await query.answer("⏳ جاري الإرسال...")
        # نمرر الآيدي لكي يرسل لك تقرير الخطأ إذا فشل
        if await process_send_next(context, update.effective_user.id): 
            await refresh_panel_inplace(query, context)
            await context.bot.answer_callback_query(query.id, text="✅ تم الإرسال!", show_alert=False)
        else:
            await context.bot.answer_callback_query(query.id, text="⚠️ راجع الخاص لمعرفة سبب الفشل.", show_alert=True)

    elif data == "send_all":
        await query.answer("🚀 بدأ النشر...")
        status_msg = await query.message.reply_text("⏳ **جاري النشر...**")
        count = await process_send_all(context, status_msg, update.effective_user.id)
        await status_msg.delete()
        await refresh_panel_inplace(query, context)
        await context.bot.answer_callback_query(query.id, text=f"🏁 تم نشر {count} سؤال.", show_alert=True)

    elif data == "clear_ask":
        key = [[InlineKeyboardButton("تأكيد الحذف 🗑️", callback_data="clear_confirm"), InlineKeyboardButton("تراجع", callback_data="refresh")]]
        await query.edit_message_reply_markup(InlineKeyboardMarkup(key))

    elif data == "clear_confirm":
        with sqlite3.connect(DB_PATH) as con:
            con.execute("DELETE FROM questions"); con.execute("DELETE FROM sqlite_sequence WHERE name='questions'")
        await refresh_panel_inplace(query, context); await query.answer("تم التنظيف")

# -------------------- الإرسال (مصحح للقنوات) --------------------
async def _send_poll_to_chat(app, chat_id, row, admin_id=None):
    qid, q, raw_opts, c_idx, exp = row
    opts = raw_opts.split("|||")
    try:
        msg = await app.bot.send_poll(
            chat_id=chat_id, 
            question=q, 
            options=opts, 
            type="quiz", 
            correct_option_id=c_idx, 
            explanation=exp[:200] if exp else None,
            is_anonymous=True  # ✅ هذا هو التعديل الضروري للقنوات
        )
        with sqlite3.connect(DB_PATH) as con: 
            con.execute("INSERT OR REPLACE INTO active_polls(poll_id, correct_idx) VALUES(?,?)", (msg.poll.id, c_idx))
        return True
    except Exception as e:
        # إرسال تقرير بالخطأ لك في الخاص
        if admin_id:
            try: await app.bot.send_message(admin_id, f"❌ فشل النشر في القناة {chat_id}:\nالسبب: {e}")
            except: pass
        return False

async def process_send_next(context, admin_id):
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute("SELECT * FROM questions ORDER BY id ASC LIMIT 1").fetchone()
        targets = [r[0] for r in con.execute("SELECT chat_id FROM targets").fetchall()]
    if not row or not targets: return False
    
    success = False
    for chat_id in targets: 
        if await _send_poll_to_chat(context.application, chat_id, row, admin_id):
            success = True
            
    if success: 
        with sqlite3.connect(DB_PATH) as con: con.execute("DELETE FROM questions WHERE id=?", (row[0],))
    return success

async def process_send_all(context, status_msg, admin_id):
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute("SELECT * FROM questions ORDER BY id ASC").fetchall()
        targets = [r[0] for r in con.execute("SELECT chat_id FROM targets").fetchall()]
    if not rows or not targets: return 0
    
    count = 0
    for i, row in enumerate(rows, 1):
        if i % 5 == 0: await status_msg.edit_text(f"🚀 جاري النشر... ({i}/{len(rows)})")
        
        sent_any = False
        for chat_id in targets: 
            if await _send_poll_to_chat(context.application, chat_id, row, admin_id):
                sent_any = True
        
        if sent_any:
            with sqlite3.connect(DB_PATH) as con: con.execute("DELETE FROM questions WHERE id=?", (row[0],))
            count += 1
        
        await asyncio.sleep(3) # فاصل زمني
    return count

async def handle_txt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    doc = update.message.document
    if not doc.file_name.lower().endswith(".txt"): return
    f = await doc.get_file()
    c = await f.download_as_bytearray()
    try: content = c.decode("utf-8")
    except: content = c.decode("cp1256", errors="ignore")
    
    parts = content.split("---")
    added = 0
    with sqlite3.connect(DB_PATH) as con:
        for block in parts:
            if not block.strip(): continue
            lines = [l.strip() for l in block.strip().splitlines() if l.strip()]
            q, opts, ans, exp = "", [], None, ""
            for l in lines:
                if not q and (l.startswith("س") or l.startswith("Q") or "?" in l): q = re.sub(r"^(س:|Q:|س-|Q-|\d+[\.-])\s*", "", l).strip()
                elif l.lower().startswith(("صح:", "ans:", "ج:")): 
                    nums = re.findall(r'\d+', l)
                    if nums: ans = int(nums[0])
                elif l.startswith(("شرح:", "exp:")): exp = l.split(":", 1)[1].strip()
                elif re.match(r'^[\w\d][\)\.\-]', l): opts.append(re.sub(r'^[\w\d][\)\.\-]\s*', "", l).strip())
            if q and ans and len(opts)>=2:
                con.execute("INSERT INTO questions(q, options, correct_idx, explanation) VALUES(?,?,?,?)", (q, "|||".join(opts), ans-1, exp))
                added += 1
    if added > 0: await update.message.reply_text(f"✅ تم إضافة {added} سؤال.")

async def force_add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    try:
        target_id = int(context.args[0])
        with sqlite3.connect(DB_PATH) as con:
            con.execute("INSERT OR REPLACE INTO targets(chat_id, title) VALUES(?,?)", (target_id, "قناة مضافة يدوياً"))
        await update.message.reply_text(f"✅ تم الربط: `{target_id}`", parse_mode="Markdown")
    except: await update.message.reply_text("❌ خطأ في الآيدي")

def main():
    if not BOT_TOKEN: print("TOKEN Error"); return
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler(["start", "admin"], show_panel))
    app.add_handler(CommandHandler("forceadd", force_add_channel))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_txt))
    
    async def handle_poll(update, context): pass 
    app.add_handler(PollAnswerHandler(handle_poll))
    
    print("Bot is Running...")
    app.run_polling()

if __name__ == "__main__":
    main()
