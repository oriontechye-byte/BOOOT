import os
import sqlite3
from typing import List, Tuple, Optional, Set
from telegram import Update, Document
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# ===================== CONFIG (ENV VARS) =====================
# Required:
#   BOT_TOKEN      : Telegram bot token from @BotFather
#   ADMIN_ID       : Single admin Telegram numeric ID (legacy)
# Optional (recommended):
#   ADMIN_IDS      : Comma/space separated admin IDs (overrides ADMIN_ID if set)
#   DB_PATH        : SQLite db file path (default: mcq.db)
#   QUIZ_ANON      : 0/1 (default 0) - send polls anonymous?
#   PARSE_SPLITTER : question separator line (default: ---)
#   MAX_PER_SEND   : limit questions in /sendall (0 = unlimited)
#   EXPLAIN_MAX    : max chars for explanation (Telegram limits; default 200)
#   QUESTION_MAX   : max chars for question (default 280)
#   OPT_MAX        : max chars per option (default 100)
#   ALLOW_EXT      : allowed uploads extensions, default: txt
#
# Notes:
# - Telegram requires admin IDs to be numbers (not @username).
# - Railway variables names must match exactly.

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

def _parse_admin_ids() -> Set[int]:
    raw = os.getenv("ADMIN_IDS", "").strip()
    if raw:
        parts = [p.strip() for p in raw.replace(";", ",").replace("\n", ",").split(",")]
        ids = set()
        for p in parts:
            if not p:
                continue
            # allow spaces too
            for sub in p.split():
                if sub.isdigit():
                    ids.add(int(sub))
        return ids
    # fallback legacy single admin
    legacy = os.getenv("ADMIN_ID", "0").strip() or "0"
    return {int(legacy)} if legacy.isdigit() and int(legacy) != 0 else set()

ADMIN_IDS = _parse_admin_ids()

DB_PATH = os.getenv("DB_PATH", "mcq.db").strip() or "mcq.db"
QUIZ_ANON = os.getenv("QUIZ_ANON", "0").strip() in ("1", "true", "True", "yes", "YES")
PARSE_SPLITTER = os.getenv("PARSE_SPLITTER", "---").strip() or "---"
MAX_PER_SEND = int(os.getenv("MAX_PER_SEND", "0").strip() or "0")          # 0 = unlimited
EXPLAIN_MAX = int(os.getenv("EXPLAIN_MAX", "200").strip() or "200")
QUESTION_MAX = int(os.getenv("QUESTION_MAX", "280").strip() or "280")
OPT_MAX = int(os.getenv("OPT_MAX", "100").strip() or "100")
ALLOW_EXT = os.getenv("ALLOW_EXT", "txt").strip().lower()                 # ex: "txt,pdf"

# ===================== SECURITY =====================
def is_admin(update: Update) -> bool:
    uid = update.effective_user.id if update.effective_user else None
    return uid in ADMIN_IDS

# ===================== DATABASE =====================
def get_con():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS questions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            q TEXT NOT NULL,
            options TEXT NOT NULL,          -- separated by |||
            correct_idx INTEGER NOT NULL,   -- 0-based
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

# ===================== PARSING ONE BLOCK =====================
def parse_one_block(block: str) -> Tuple[str, List[str], int, str]:
    lines = [l.strip() for l in block.strip().splitlines() if l.strip()]
    q = ""
    options: List[str] = []
    ans: Optional[int] = None
    exp = ""

    for l in lines:
        if l.startswith("س:"):
            q = l[2:].strip()
        elif l.startswith("صح:"):
            # allow "صح: 3" only
            ans = int(l[3:].strip())
        elif l.startswith("شرح:"):
            exp = l[4:].strip()
        elif len(l) >= 2 and l[:2] in ("A)", "B)", "C)", "D)", "E)", "F)", "G)", "H)", "I)", "J)", "K)", "L)"):
            options.append(l[2:].strip())

    if not q:
        raise ValueError("❌ خطأ: لم يتم كتابة (س:).")
    if len(q) > QUESTION_MAX:
        raise ValueError(f"❌ خطأ: السؤال طويل جدًا (>{QUESTION_MAX}).")
    if len(options) < 2:
        raise ValueError("❌ خطأ: عدد الاختيارات أقل من 2.")
    if any(len(o) > OPT_MAX for o in options):
        raise ValueError(f"❌ خطأ: خيار من الخيارات أطول من المسموح (>{OPT_MAX}).")
    if ans is None:
        raise ValueError("❌ خطأ: لم يتم كتابة (صح:).")

    correct_idx = ans - 1
    if correct_idx < 0 or correct_idx >= len(options):
        raise ValueError("❌ خطأ: رقم (صح:) خارج نطاق الاختيارات.")

    if exp and len(exp) > EXPLAIN_MAX:
        exp = exp[:EXPLAIN_MAX].rstrip()

    return q, options, correct_idx, exp

# ===================== PARSE FULL TXT =====================
def parse_txt_payload(payload: str) -> Tuple[List[Tuple[str, List[str], int, str]], List[str]]:
    # split questions by a separator line (default: ---)
    parts: List[str] = []
    current: List[str] = []
    for line in payload.splitlines():
        if line.strip() == PARSE_SPLITTER:
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

# ===================== HELP =====================
HELP_TEXT = (
    "أوامر البوت (للمشرف فقط):\n"
    "/settarget  (أرسلها داخل المجموعة/القناة لتسجيلها كهدف إرسال)\n"
    "/count      (عدد الأسئلة والأهداف)\n"
    "/sendnext   (يرسل أول سؤال في الطابور)\n"
    "/sendall    (يرسل كل الأسئلة)\n"
    "/whoami     (يعرض User ID و Chat ID)\n\n"
    "رفع ملف TXT:\n"
    "ارسل ملف .txt للبوت بصيغة:\n"
    "س: ...\nA) ...\nB) ...\nC) ...\nD) ...\nصح: 3\nشرح: ... (اختياري)\n"
    f"{PARSE_SPLITTER}\n"
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
    await update.message.reply_text(f"✅ تم تسجيل هذا الشات كهدف إرسال.\nChat ID: {chat_id}")

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
        is_anonymous=QUIZ_ANON,
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

    if MAX_PER_SEND > 0:
        rows = rows[:MAX_PER_SEND]

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

    await update.message.reply_text(f"📤 تم إرسال الأسئلة.\n✅ نجح: {sent_total}\n❌ فشل: {failed_total}\n🗑️ تم حذف {len(ids)} سؤال من الطابور.")

# ===================== TXT UPLOAD =====================
async def handle_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    doc: Document = update.message.document
    name = (doc.file_name or "").lower()

    allowed = {e.strip() for e in ALLOW_EXT.split(",") if e.strip()}
    if not any(name.endswith(f".{e}") for e in allowed):
        await update.message.reply_text(f"❌ ارسل ملف بالامتدادات التالية فقط: {', '.join(sorted(allowed))}")
        return

    if not name.endswith(".txt"):
        await update.message.reply_text("❌ حالياً هذا الإصدار يدعم TXT فقط. (PDF سيتم إضافته كميزة منفصلة)")
        return

    tg_file = await doc.get_file()
    content_bytes = await tg_file.download_as_bytearray()

    # UTF-8 + Windows Arabic
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
        msg += "\n⚠️ أخطاء في بعض الأسئلة (أول 10):\n" + "\n".join(errors[:10])
    await update.message.reply_text(msg)

def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN غير موجود.")
    if not ADMIN_IDS:
        raise SystemExit("ADMIN_ID/ADMIN_IDS غير مضبوط.")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("settarget", settarget))
    app.add_handler(CommandHandler("count", count))
    app.add_handler(CommandHandler("sendnext", sendnext))
    app.add_handler(CommandHandler("sendall", sendall))

    app.add_handler(MessageHandler(filters.Document.ALL, handle_upload))

    app.run_polling()

if __name__ == "__main__":
    main()
