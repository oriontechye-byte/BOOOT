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
# ضع القيم الخاصة بك هنا مباشرة إذا لم تستخدم ملف .env
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
# استبدل الرقم 0 بآيديك الحقيقي (تأكد أنه رقم وليس نص)
ADMIN_ID = "7358178408"
DB_PATH = "super_mcq.db"

# -------------------- قاعدة البيانات --------------------
def init_db():
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""CREATE TABLE IF NOT EXISTS questions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, q TEXT, options TEXT, correct_idx INTEGER, explanation TEXT)""")
        con.execute("""CREATE TABLE IF NOT EXISTS targets(chat_id INTEGER PRIMARY KEY)""")
        con.execute("""CREATE TABLE IF NOT EXISTS active_polls(poll_id TEXT PRIMARY KEY, correct_idx INTEGER)""")
        con.execute("""CREATE TABLE IF NOT EXISTS user_scores(
            user_id INTEGER PRIMARY KEY, first_name TEXT, correct_count INTEGER DEFAULT 0, total_count INTEGER DEFAULT 0)""")
        con.commit()

def get_stats():
    """جلب الإحصائيات الحالية للأزرار"""
    with sqlite3.connect(DB_PATH) as con:
        q_count = con.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        t_count = con.execute("SELECT COUNT(*) FROM targets").fetchone()[0]
    return q_count, t_count

# -------------------- لوحة التحكم الثابتة (Smart Dashboard) --------------------
def get_dashboard_markup(q_count, t_count):
    """تصميم الأزرار مع الأرقام المحدثة"""
    keyboard = [
        [
            InlineKeyboardButton(f"📦 الأسئلة: {q_count}", callback_data="ignore"),
            InlineKeyboardButton(f"📢 القنوات: {t_count}", callback_data="ignore")
        ],
        [
            InlineKeyboardButton("📤 إرسال سؤال واحد", callback_data="send_one"),
        ],
        [
            InlineKeyboardButton("🚀 إرسال الكل (تلقائي)", callback_data="send_all"),
        ],
        [
            InlineKeyboardButton("🗑️ حذف الأسئلة", callback_data="clear_ask"),
            InlineKeyboardButton("🔄 تحديث اللوحة", callback_data="refresh")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

async def show_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عرض اللوحة لأول مرة"""
    if update.effective_user.id != ADMIN_ID: return
    q, t = get_stats()
    text = (
        "🎛 **لوحة التحكم الرئيسية**\n"
        "هذه اللوحة ستظل ثابتة. يمكنك التحكم بالبوت من هنا."
    )
    await update.message.reply_text(text, reply_markup=get_dashboard_markup(q, t), parse_mode="Markdown")

async def refresh_panel_inplace(query, context):
    """تحديث الأرقام في اللوحة دون إرسال رسالة جديدة"""
    q, t = get_stats()
    try:
        await query.edit_message_reply_markup(reply_markup=get_dashboard_markup(q, t))
    except:
        pass # لم يتغير شيء لتحديثه

# -------------------- معالجة الأزرار --------------------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    
    # 1. زر تحديث أو تجاهل
    if data == "ignore":
        await query.answer("هذه مجرد إحصائية 📊")
        return
    if data == "refresh":
        await refresh_panel_inplace(query, context)
        await query.answer("تم تحديث البيانات 🔄")
        return

    # 2. زر إرسال سؤال واحد
    if data == "send_one":
        # إشعار سريع
        await query.answer("⏳ جاري الإرسال...") 
        success = await process_send_next(context)
        
        if success:
            # تحديث الأرقام في نفس اللوحة
            await refresh_panel_inplace(query, context)
            # إشعار منبثق
            await context.bot.answer_callback_query(query.id, text="✅ تم الإرسال بنجاح!", show_alert=False)
        else:
            await context.bot.answer_callback_query(query.id, text="⚠️ القائمة فارغة أو لا يوجد قنوات!", show_alert=True)

    # 3. زر إرسال الكل
    if data == "send_all":
        await query.answer("🚀 بدأ الإرسال الجماعي...")
        # نرسل رسالة منفصلة للتقدم ثم نحذفها
        status_msg = await query.message.reply_text("⏳ **جاري تحضير الإرسال...**")
        
        count = await process_send_all(context, status_msg)
        
        await status_msg.delete() # حذف رسالة التقدم
        await refresh_panel_inplace(query, context) # تحديث اللوحة الأصلية
        await context.bot.answer_callback_query(query.id, text=f"🏁 انتهى! تم نشر {count} سؤال.", show_alert=True)

    # 4. الحذف
    if data == "clear_ask":
        key = [[InlineKeyboardButton("نعم، احذف 🗑️", callback_data="clear_confirm"), InlineKeyboardButton("تراجع 🔙", callback_data="refresh")]]
        await query.edit_message_reply_markup(InlineKeyboardMarkup(key))
    
    if data == "clear_confirm":
        with sqlite3.connect(DB_PATH) as con:
            con.execute("DELETE FROM questions")
            con.execute("DELETE FROM sqlite_sequence WHERE name='questions'")
        await refresh_panel_inplace(query, context)
        await query.answer("تم تنظيف القائمة 🗑️")

# -------------------- منطق الإرسال (Back-end) --------------------
async def _send_poll_to_chat(app, chat_id, row):
    qid, q, raw_opts, c_idx, exp = row
    opts = raw_opts.split("|||")
    try:
        msg = await app.bot.send_poll(
            chat_id=chat_id, question=q, options=opts, type="quiz",
            correct_option_id=c_idx, explanation=exp[:200] if exp else None,
            is_anonymous=False
        )
        with sqlite3.connect(DB_PATH) as con:
            con.execute("INSERT OR REPLACE INTO active_polls(poll_id, correct_idx) VALUES(?,?)", (msg.poll.id, c_idx))
        return True
    except Exception as e:
        print(f"Fail {chat_id}: {e}")
        return False

async def process_send_next(context):
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute("SELECT * FROM questions ORDER BY id ASC LIMIT 1").fetchone()
        targets = [r[0] for r in con.execute("SELECT chat_id FROM targets").fetchall()]
    
    if not row or not targets: return False
    
    for chat_id in targets:
        await _send_poll_to_chat(context.application, chat_id, row)
    
    with sqlite3.connect(DB_PATH) as con:
        con.execute("DELETE FROM questions WHERE id=?", (row[0],))
    return True

async def process_send_all(context, status_msg):
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute("SELECT * FROM questions ORDER BY id ASC").fetchall()
        targets = [r[0] for r in con.execute("SELECT chat_id FROM targets").fetchall()]
    
    if not rows or not targets: return 0
    
    total = len(rows)
    sent_count = 0
    
    for i, row in enumerate(rows, 1):
        # تحديث شريط التقدم كل 3 أسئلة
        if i % 3 == 0 or i == 1:
            try: await status_msg.edit_text(f"🚀 **جاري النشر...**\nالسؤال: {i} من {total}")
            except: pass
            
        for chat_id in targets:
            await _send_poll_to_chat(context.application, chat_id, row)
        
        with sqlite3.connect(DB_PATH) as con:
            con.execute("DELETE FROM questions WHERE id=?", (row[0],))
        
        sent_count += 1
        await asyncio.sleep(2.5) # حماية من الحظر
    
    return sent_count

# -------------------- معالج النقاط والملفات --------------------
async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer = update.poll_answer
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute("SELECT correct_idx FROM active_polls WHERE poll_id=?", (answer.poll_id,)).fetchone()
        if row:
            is_correct = (answer.option_ids[0] == row[0])
            con.execute("INSERT OR IGNORE INTO user_scores(user_id, first_name, correct_count, total_count) VALUES(?,?,0,0)", 
                        (answer.user.id, answer.user.first_name))
            if is_correct:
                con.execute("UPDATE user_scores SET correct_count=correct_count+1, total_count=total_count+1 WHERE user_id=?", (answer.user.id,))
            else:
                con.execute("UPDATE user_scores SET total_count=total_count+1 WHERE user_id=?", (answer.user.id,))
            con.commit()

async def handle_txt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    
    doc = update.message.document
    if not doc.file_name.lower().endswith(".txt"): 
        await update.message.reply_text("❌ أرسل ملف .txt فقط")
        return

    f = await doc.get_file()
    content_bytes = await f.download_as_bytearray()
    try: content = content_bytes.decode("utf-8")
    except: content = content_bytes.decode("cp1256", errors="ignore")
    
    # Simple Parser
    parts = content.split("---")
    added = 0
    with sqlite3.connect(DB_PATH) as con:
        for block in parts:
            if not block.strip(): continue
            try:
                lines = [l.strip() for l in block.strip().splitlines() if l.strip()]
                q, opts, ans, exp = "", [], None, ""
                for l in lines:
                    if l.startswith(("س:", "Q:", "السؤال:")): q = l.split(":",1)[1].strip()
                    elif l.startswith(("صح:", "Ans:", "الجواب:")): ans = int(re.search(r'\d+', l).group())
                    elif l.startswith(("شرح:", "Exp:")): exp = l.split(":",1)[1].strip()
                    elif l[0].isalnum() and l[1] in [")", "."]: opts.append(l[2:].strip())
                
                if q and ans and len(opts)>=2:
                    con.execute("INSERT INTO questions(q, options, correct_idx, explanation) VALUES(?,?,?,?)", 
                                (q, "|||".join(opts), ans-1, exp))
                    added += 1
            except: pass
            
    await update.message.reply_text(f"✅ تم إضافة {added} سؤال.\nاضغط /admin لفتح اللوحة.")

async def myscore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with sqlite3.connect(DB_PATH) as con:
        r = con.execute("SELECT correct_count, total_count FROM user_scores WHERE user_id=?", (update.effective_user.id,)).fetchone()
    if r: await update.message.reply_text(f"📊 نقاطك: {r[0]} صح من أصل {r[1]}")
    else: await update.message.reply_text("لم تشارك بعد!")

async def settarget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # مسموح لأي مشرف في القناة بالتفعيل
    with sqlite3.connect(DB_PATH) as con:
        con.execute("INSERT OR IGNORE INTO targets(chat_id) VALUES(?)", (update.effective_chat.id,))
    await update.message.reply_text("✅ تم تفعيل القناة.")

# -------------------- التشغيل --------------------
def main():
    if not BOT_TOKEN: print("Error: TOKEN missing"); return
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler(["start", "admin"], show_panel))
    app.add_handler(CommandHandler("settarget", settarget))
    app.add_handler(CommandHandler("myscore", myscore))
    
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(PollAnswerHandler(handle_poll_answer))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_txt))
    
    print("Bot is Live...")
    app.run_polling()

if __name__ == "__main__":
    main()
