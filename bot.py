import os
import sqlite3
from typing import List, Tuple
from telegram import Update, Document
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip() or "0")
DB_PATH = os.getenv("DB_PATH", "mcq.db")

def is_admin(update: Update) -> bool:
    return update.effective_user is not None and update.effective_user.id == ADMIN_ID

# -------------------- قاعدة البيانات --------------------
def get_con():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS questions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            q TEXT NOT NULL,
            options TEXT NOT NULL,          -- مفصولة بـ |||
            correct_idx INTEGER NOT NULL,   -- يبدأ من 0
            explanation TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS targets(
            chat_id INTEGER PRIMARY KEY
        )
    """)
    con.commit()
    return con

# -------------------- تحليل سؤال واحد --------------------
def parse_one_block(block: str) -> Tuple[str, List[str], int, str]:
    lines = [l.strip() for l in block.strip().splitlines() if l.strip()]
    q = ""
    options: List[str] = []
    ans = None
    exp = ""

    for l in lines:
        if l.startswith("س:"):
            q = l[2:].strip()
        elif l.startswith("صح:"):
            ans = int(l[3:].strip())
        elif l.startswith("شرح:"):
            exp = l[4:].strip()
        elif l[:2] in ("A)", "B)", "C)", "D)", "E)", "F)"):
            options.append(l[2:].strip())

    if not q:
        raise ValueError("❌ خطأ: لم يتم كتابة (س:).")
    if len(options) < 2:
        raise ValueError("❌ خطأ: عدد الاختيارات أقل من 2.")
    if ans is None:
        raise ValueError("❌ خطأ: لم يتم كتابة (صح:).")
    correct_idx = ans - 1
    if correct_idx < 0 or correct_idx >= len(options):
        raise ValueError("❌ خطأ: رقم (صح:) خارج نطاق الاختيارات.")

    return q, options, correct_idx, exp

# -------------------- تحليل ملف TXT كامل --------------------
def parse_txt_payload(payload: str) -> Tuple[List[Tuple[str, List[str], int, str]], List[str]]:
    # يفصل الأسئلة بسطر يحتوي فقط على ---
    parts = []
    current = []
    for line in payload.splitlines():
        if line.strip() == "---":
            if current:
                parts.append("\n".join(current))
                current = []
        else:
            current.append(line)
    if current:
        parts.append("\n".join(current))

    parsed = []
    errors = []
    for i, block in enumerate(parts, start=1):
        if not block.strip():
            continue
        try:
            parsed.append(parse_one_block(block))
        except Exception as e:
            errors.append(f"سؤال #{i}: {e}")
    return parsed, errors

# -------------------- رسائل المساعدة --------------------
HELP_TEXT = (
    "أوامر البوت (للمشرف فقط):\n"
    "/settarget  (أرسلها داخل المجموعة/القناة لتسجيلها كهدف إرسال)\n"
    "/count      (عدد الأسئلة والأهداف)\n"
    "/sendnext   (يرسل أول سؤال في الطابور)\n"
    "/sendall    (يرسل كل الأسئلة)\n\n"
    "رفع ملف TXT:\n"
    "ارسل ملف .txt للبوت مباشرة بصيغة:\n"
    "س: ...\nA) ...\nB) ...\nC) ...\nD) ...\nصح: 3\nشرح: ... (اختياري)\n---\n"
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)

async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    cid = update.effective_chat.id if update.effective_chat else None
    await update.message.reply_text(f"User ID: {uid}\nChat ID: {cid}")

async def settarget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    chat_id = update.effective_chat.id
    con = get_con()
    con.execute("INSERT OR IGNORE INTO targets(chat_id) VALUES(?)", (chat_id,))
    con.commit()
    con.close()
    await update.message.reply_text(f"✅ تم تسجيل هذه المجموعة/القناة كهدف إرسال.\nChat ID: {chat_id}")

async def count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    con = get_con()
    q_count = con.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
    t_count = con.execute("SELECT COUNT(*) FROM targets").fetchone()[0]
    con.close()
    await update.message.reply_text(f"📊 الأسئلة في الطابور: {q_count}\n📌 الأهداف: {t_count}")

async def _send_quiz(app: Application, chat_id: int, q: str, opts: List[str], correct_idx: int, exp: str):
    await app.bot.send_poll(
        chat_id=chat_id,
        question=q,
        options=opts,
        type="quiz",
        correct_option_id=correct_idx,
        explanation=exp if exp else None,
        is_anonymous=False,
        allows_multiple_answers=False
    )

async def sendnext(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    con = get_con()
    row = con.execute(
        "SELECT id, q, options, correct_idx, explanation FROM questions ORDER BY id ASC LIMIT 1"
    ).fetchone()
    targets = [r[0] for r in con.execute("SELECT chat_id FROM targets").fetchall()]
    con.close()

    if not targets:
        await update.message.reply_text("❌ لا يوجد أهداف إرسال. استخدم /settarget داخل المجموعة/القناة.")
        return
    if not row:
        await update.message.reply_text("❌ لا توجد أسئلة في الطابور.")
        return

    qid, q, options, correct_idx, exp = row
    opts = options.split("|||")

    sent = 0
    failed = 0
    for chat_id in targets:
        try:
            await _send_quiz(context.application, chat_id, q, opts, correct_idx, exp)
            sent += 1
        except Exception:
            failed += 1

    con = get_con()
    con.execute("DELETE FROM questions WHERE id=?", (qid,))
    con.commit()
    con.close()

    await update.message.reply_text(f"📤 تم إرسال السؤال.\n✅ نجح: {sent}\n❌ فشل: {failed}\n🗑️ حُذف من الطابور (ID: {qid}).")

async def sendall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    con = get_con()
    rows = con.execute(
        "SELECT id, q, options, correct_idx, explanation FROM questions ORDER BY id ASC"
    ).fetchall()
    targets = [r[0] for r in con.execute("SELECT chat_id FROM targets").fetchall()]
    con.close()

    if not targets:
        await update.message.reply_text("❌ لا يوجد أهداف إرسال. استخدم /settarget داخل المجموعة/القناة.")
        return
    if not rows:
        await update.message.reply_text("❌ لا توجد أسئلة في الطابور.")
        return

    sent_total = 0
    failed_total = 0
    ids = []

    for (qid, q, options, correct_idx, exp) in rows:
        opts = options.split("|||")
        for chat_id in targets:
            try:
                await _send_quiz(context.application, chat_id, q, opts, correct_idx, exp)
                sent_total += 1
            except Exception:
                failed_total += 1
        ids.append(qid)

    con2 = get_con()
    con2.executemany("DELETE FROM questions WHERE id=?", [(i,) for i in ids])
    con2.commit()
    con2.close()

    await update.message.reply_text(f"📤 تم إرسال كل الأسئلة.\n✅ نجح: {sent_total}\n❌ فشل: {failed_total}\n🗑️ تم تفريغ الطابور.")

# -------------------- استقبال ملف TXT --------------------
async def handle_txt_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    doc: Document = update.message.document
    name = (doc.file_name or "").lower()

    if not name.endswith(".txt"):
        await update.message.reply_text("❌ ارسل ملف .txt فقط.")
        return

    tg_file = await doc.get_file()
    content_bytes = await tg_file.download_as_bytearray()

    # دعم UTF-8 و Windows Arabic
    try:
        payload = content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        try:
            payload = content_bytes.decode("cp1256")
        except Exception:
            await update.message.reply_text("❌ تعذر قراءة الملف. احفظه UTF-8 ثم أعد الإرسال.")
            return

    parsed, errors = parse_txt_payload(payload)

    if not parsed:
        await update.message.reply_text("❌ لم يتم استيراد أي سؤال.\n" + ("\n".join(errors[:10]) if errors else "تحقق من الصيغة."))
        return

    con = get_con()
    for (q, opts, correct_idx, exp) in parsed:
        con.execute(
            "INSERT INTO questions(q, options, correct_idx, explanation) VALUES(?,?,?,?)",
            (q, "|||".join(opts), correct_idx, exp)
        )
    con.commit()
    con.close()

    msg = f"✅ تم استيراد {len(parsed)} سؤال."
    if errors:
        msg += "\n⚠️ أخطاء في بعض الأسئلة:\n" + "\n".join(errors[:10])
    await update.message.reply_text(msg)

def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN غير موجود.")
    if ADMIN_ID == 0:
        raise SystemExit("ADMIN_ID غير مضبوط.")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("settarget", settarget))
    app.add_handler(CommandHandler("count", count))
    app.add_handler(CommandHandler("sendnext", sendnext))
    app.add_handler(CommandHandler("sendall", sendall))

    app.add_handler(MessageHandler(filters.Document.ALL, handle_txt_upload))

    app.run_polling()

if __name__ == "__main__":
    main()
