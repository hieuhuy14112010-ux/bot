"""
FULL TOOL Bot – button menu, conversation flow.
Token & admin có thể override bằng env BOT_TOKEN / ADMIN_IDS.
"""
import asyncio
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, Update, InputFile,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, MessageHandler,
    ContextTypes, filters,
)

from telethon import TelegramClient, errors as tele_errors

from config import BOT_TOKEN, ADMIN_IDS, API_ID, API_HASH, SESSIONS_DIR
from modules.session_mgr import (
    list_sessions, make_client, delete_session, session_path,
)
from modules import check as mod_check
from modules import auto as mod_auto
from modules import ref as mod_ref
from modules import sex as mod_spam

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

SEP = "━━━━━━━━━━━━━━━━━━"

# ─── per-user runtime state ─────────────────────────────
@dataclass
class UserState:
    # conversation step e.g. "check.link", "auto.delay", "addacc.code"
    step: Optional[str] = None
    data: dict = field(default_factory=dict)
    # login flow
    login_client: Optional[TelegramClient] = None
    login_phone: Optional[str] = None
    # active task
    task: Optional[asyncio.Task] = None
    stop_event: Optional[asyncio.Event] = None
    log_buf: deque = field(default_factory=lambda: deque(maxlen=200))
    log_msg_id: Optional[int] = None
    log_chat_id: Optional[int] = None
    last_log_flush: float = 0.0
    status: str = "IDLE"  # IDLE / RUNNING / STOPPED / ERROR / FLOODWAIT


STATES: dict[int, UserState] = {}


def st(uid: int) -> UserState:
    if uid not in STATES:
        STATES[uid] = UserState()
    return STATES[uid]


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


# ─── keyboards ──────────────────────────────────────────
def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛡 CHECK JOIN", callback_data="m:check"),
         InlineKeyboardButton("🤖 AUTO",      callback_data="m:auto")],
        [InlineKeyboardButton("🎯 REF",        callback_data="m:ref"),
         InlineKeyboardButton("💬 SEX SPAM",   callback_data="m:spam")],
        [InlineKeyboardButton("👤 QUẢN LÝ ACC", callback_data="m:acc")],
        [InlineKeyboardButton("📜 LOGS",   callback_data="m:logs"),
         InlineKeyboardButton("📊 STATUS", callback_data="m:status")],
        [InlineKeyboardButton("⛔ STOP TASK", callback_data="m:stop")],
    ])


def kb_acc():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ THÊM ACC",     callback_data="acc:add"),
         InlineKeyboardButton("📋 DANH SÁCH",    callback_data="acc:list")],
        [InlineKeyboardButton("🗑 XOÁ ACC",      callback_data="acc:del"),
         InlineKeyboardButton("💓 CHECK LIVE",   callback_data="acc:live")],
        [InlineKeyboardButton("📥 IMPORT SESSION", callback_data="acc:import"),
         InlineKeyboardButton("📤 EXPORT SESSION", callback_data="acc:export")],
        [InlineKeyboardButton("⬅️ BACK", callback_data="m:home")],
    ])


def kb_cancel():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ HUỶ",  callback_data="ctrl:cancel"),
         InlineKeyboardButton("🏠 HOME", callback_data="m:home")],
    ])


def kb_confirm():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ XÁC NHẬN", callback_data="ctrl:yes"),
         InlineKeyboardButton("❌ HUỶ",      callback_data="ctrl:cancel")],
    ])


def kb_back_home():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ BACK", callback_data="m:home")],
    ])


# ─── helpers ────────────────────────────────────────────
async def send(update: Update, text: str, kb=None):
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text, reply_markup=kb, parse_mode=ParseMode.HTML)
            return
        except Exception:
            pass
    await update.effective_chat.send_message(
        text, reply_markup=kb, parse_mode=ParseMode.HTML)


def banner(title: str, body: str) -> str:
    return f"{SEP}\n<b>{title}</b>\n{SEP}\n\n{body}"


async def show_home(update: Update):
    text = banner(
        "🧰 FULL TOOL — MENU",
        "Chọn chức năng bên dưới.\n"
        "Mỗi bước sẽ được hỏi tuần tự giống tool Termux.\n\n"
        f"👮 Admin: <code>{', '.join(map(str, ADMIN_IDS))}</code>"
    )
    await send(update, text, kb_main())


# ─── log streaming ──────────────────────────────────────
def _format_log(s: UserState) -> str:
    lines = list(s.log_buf)[-30:]
    body = "\n".join(lines) if lines else "(chưa có log)"
    return banner(f"📜 LIVE LOG — {s.status}", f"<pre>{_html_escape(body)}</pre>")


def _html_escape(t: str) -> str:
    return (t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


async def make_logger(app: Application, uid: int, chat_id: int):
    s = st(uid)
    s.log_buf.clear()
    s.last_log_flush = 0
    msg = await app.bot.send_message(
        chat_id, _format_log(s),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⛔ STOP", callback_data="m:stop"),
             InlineKeyboardButton("🏠 HOME", callback_data="m:home")]
        ]),
        parse_mode=ParseMode.HTML,
    )
    s.log_msg_id = msg.message_id
    s.log_chat_id = chat_id

    async def _log(text: str):
        line = f"{time.strftime('%H:%M:%S')} {text}"
        s.log_buf.append(line)
        log.info(text)
        now = time.time()
        if now - s.last_log_flush >= 1.2:
            s.last_log_flush = now
            try:
                await app.bot.edit_message_text(
                    _format_log(s), chat_id=chat_id,
                    message_id=s.log_msg_id, parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⛔ STOP", callback_data="m:stop"),
                         InlineKeyboardButton("🏠 HOME", callback_data="m:home")]
                    ]),
                )
            except Exception:
                pass
    return _log


async def finalize_log(app: Application, uid: int):
    s = st(uid)
    if s.log_msg_id and s.log_chat_id:
        try:
            await app.bot.edit_message_text(
                _format_log(s), chat_id=s.log_chat_id,
                message_id=s.log_msg_id, parse_mode=ParseMode.HTML,
                reply_markup=kb_back_home(),
            )
        except Exception:
            pass


# ─── /start ─────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text(
            f"❌ Bạn không có quyền.\nUser ID: <code>{uid}</code>",
            parse_mode=ParseMode.HTML)
        return
    st(uid)  # init
    await show_home(update)


# ─── menu callback ──────────────────────────────────────
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_admin(uid):
        await q.answer("❌ No permission", show_alert=True)
        return
    await q.answer()
    data = q.data
    s = st(uid)

    # control
    if data == "ctrl:cancel":
        s.step = None
        s.data.clear()
        await show_home(update)
        return
    if data == "m:home":
        s.step = None
        s.data.clear()
        await show_home(update)
        return
    if data == "m:stop":
        await handle_stop(update, ctx)
        return
    if data == "m:status":
        await send(update, banner("📊 STATUS",
            f"Trạng thái: <b>{s.status}</b>\n"
            f"Step hiện tại: <code>{s.step or '-'}</code>\n"
            f"Sessions: <b>{len(list_sessions())}</b>"), kb_back_home())
        return
    if data == "m:logs":
        await send(update, _format_log(s), kb_back_home())
        return

    # main menu entries
    if data == "m:check":   await flow_check_start(update, ctx); return
    if data == "m:auto":    await flow_auto_start(update, ctx); return
    if data == "m:ref":     await flow_ref_start(update, ctx); return
    if data == "m:spam":    await flow_spam_start(update, ctx); return
    if data == "m:acc":
        await send(update, banner("👤 QUẢN LÝ ACC",
            f"Sessions hiện có: <b>{len(list_sessions())}</b>"), kb_acc())
        return

    # account ops
    if data == "acc:add":    await acc_add_start(update, ctx); return
    if data == "acc:list":   await acc_list(update, ctx); return
    if data == "acc:del":    await acc_del_start(update, ctx); return
    if data == "acc:live":   await acc_live(update, ctx); return
    if data == "acc:import":
        s.step = "import.wait"
        await send(update, banner("📥 IMPORT SESSION",
            "Gửi file <code>.session</code> vào chat này."), kb_cancel())
        return
    if data == "acc:export": await acc_export(update, ctx); return

    # confirms for flows
    if data == "ctrl:yes":
        if s.step == "check.confirm":
            await launch_check(update, ctx); return
        if s.step == "auto.confirm":
            await launch_auto(update, ctx); return
        if s.step == "ref.confirm":
            await launch_ref(update, ctx); return
        if s.step == "spam.confirm":
            await launch_spam(update, ctx); return
        if s.step and s.step.startswith("acc.del.confirm:"):
            name = s.step.split(":", 1)[1]
            delete_session(name)
            s.step = None
            await send(update, banner("🗑 XOÁ ACC", f"Đã xoá <code>{name}</code>"), kb_acc())
            return


# ─── stop handler ───────────────────────────────────────
async def handle_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = st(update.effective_user.id)
    if s.task and not s.task.done():
        if s.stop_event:
            s.stop_event.set()
        s.task.cancel()
        s.status = "STOPPED"
        await send(update, banner("⛔ STOP", "Đã yêu cầu dừng task."), kb_back_home())
    else:
        await send(update, banner("⛔ STOP", "Không có task nào đang chạy."), kb_back_home())


# ─── flow: CHECK JOIN ───────────────────────────────────
async def flow_check_start(update, ctx):
    s = st(update.effective_user.id)
    if not list_sessions():
        await send(update, banner("⚠ CHECK JOIN",
            "Chưa có acc nào. Vào QUẢN LÝ ACC để thêm."), kb_back_home()); return
    s.step = "check.link"
    s.data = {}
    await send(update, banner("⚙️ CHECK JOIN",
        "📌 Vui lòng nhập <b>link nhóm</b>\n\n"
        "Ví dụ:\n<code>https://t.me/xxxx</code>\n\n➤ Nhập link:"),
        kb_cancel())


async def step_check(update: Update, ctx, text: str):
    s = st(update.effective_user.id)
    if s.step == "check.link":
        s.data["link"] = text.strip()
        s.step = "check.delay"
        await update.message.reply_text(banner("⏱ CÀI ĐẶT DELAY",
            "Delay = thời gian nghỉ giữa mỗi lượt check\n\n"
            "Ví dụ:\n"
            "<code>1</code> = nhanh\n"
            "<code>3</code> = trung bình\n"
            "<code>5</code> = an toàn\n\n➤ Nhập delay (giây):"),
            reply_markup=kb_cancel(), parse_mode=ParseMode.HTML)
    elif s.step == "check.delay":
        try: s.data["delay"] = float(text)
        except: await update.message.reply_text("❌ Sai số. Nhập lại:"); return
        s.step = "check.times"
        await update.message.reply_text(banner("🔁 SỐ LẦN",
            "Số vòng check muốn thực hiện.\n\n➤ Nhập số lần:"),
            reply_markup=kb_cancel(), parse_mode=ParseMode.HTML)
    elif s.step == "check.times":
        try: s.data["times"] = int(text)
        except: await update.message.reply_text("❌ Sai. Nhập lại số nguyên:"); return
        s.step = "check.confirm"
        d = s.data
        await update.message.reply_text(banner("✅ XÁC NHẬN CHECK JOIN",
            f"🔗 Link  : <code>{_html_escape(d['link'])}</code>\n"
            f"⏱ Delay : <b>{d['delay']}s</b>\n"
            f"🔁 Số lần: <b>{d['times']}</b>\n"
            f"👥 Acc   : <b>{len(list_sessions())}</b>\n\n"
            "Bấm <b>XÁC NHẬN</b> để bắt đầu."),
            reply_markup=kb_confirm(), parse_mode=ParseMode.HTML)


async def launch_check(update, ctx):
    s = st(update.effective_user.id)
    s.step = None
    s.status = "RUNNING"
    s.stop_event = asyncio.Event()
    logger = await make_logger(ctx.application, update.effective_user.id,
                               update.effective_chat.id)
    sessions = list_sessions()
    d = s.data

    async def runner():
        try:
            await mod_check.run_check(sessions, d["link"], d["delay"], d["times"], logger)
            s.status = "DONE"
        except asyncio.CancelledError:
            s.status = "STOPPED"
        except Exception as e:
            s.status = "ERROR"
            await logger(f"❌ {e}")
        finally:
            await finalize_log(ctx.application, update.effective_user.id)

    s.task = asyncio.create_task(runner())


# ─── flow: AUTO ─────────────────────────────────────────
async def flow_auto_start(update, ctx):
    s = st(update.effective_user.id)
    if not list_sessions():
        await send(update, banner("⚠ AUTO",
            "Chưa có acc. Hãy thêm acc trước."), kb_back_home()); return
    s.step = "auto.numacc"; s.data = {}
    await send(update, banner("🤖 AUTO",
        f"📌 Số acc muốn dùng (tối đa <b>{len(list_sessions())}</b>)\n\n"
        "➤ Nhập số acc:"), kb_cancel())


async def step_auto(update, ctx, text: str):
    s = st(update.effective_user.id); d = s.data
    if s.step == "auto.numacc":
        try: d["numacc"] = max(1, min(int(text), len(list_sessions())))
        except: await update.message.reply_text("❌ Sai. Nhập lại:"); return
        s.step = "auto.link"
        await update.message.reply_text(banner("🔗 LINK / USERNAME",
            "Nhập link nhóm hoặc @username hoặc số điện thoại\n\n"
            "Ví dụ: <code>https://t.me/xxxx</code>\n\n➤ Nhập:"),
            reply_markup=kb_cancel(), parse_mode=ParseMode.HTML)
    elif s.step == "auto.link":
        d["link"] = text.strip(); s.step = "auto.delay"
        await update.message.reply_text(banner("⏱ DELAY GỬI",
            "Thời gian nghỉ giữa mỗi tin (giây)\n\n"
            "Ví dụ:\n<code>1</code> nhanh\n<code>3</code> trung bình\n"
            "<code>5</code> an toàn\n\n➤ Nhập delay:"),
            reply_markup=kb_cancel(), parse_mode=ParseMode.HTML)
    elif s.step == "auto.delay":
        try: d["delay"] = float(text)
        except: await update.message.reply_text("❌ Sai. Nhập lại:"); return
        s.step = "auto.deldelay"
        await update.message.reply_text(banner("🗑 DELAY XOÁ",
            "Sau bao nhiêu giây sẽ xoá tin vừa gửi\n\n➤ Nhập:"),
            reply_markup=kb_cancel(), parse_mode=ParseMode.HTML)
    elif s.step == "auto.deldelay":
        try: d["deldelay"] = float(text)
        except: await update.message.reply_text("❌ Sai. Nhập lại:"); return
        s.step = "auto.total"
        await update.message.reply_text(banner("🔁 SỐ LẦN",
            "Số tin tối đa mỗi acc sẽ gửi\n\n➤ Nhập:"),
            reply_markup=kb_cancel(), parse_mode=ParseMode.HTML)
    elif s.step == "auto.total":
        try: d["total"] = int(text)
        except: await update.message.reply_text("❌ Sai. Nhập lại:"); return
        s.step = "auto.confirm"
        await update.message.reply_text(banner("✅ XÁC NHẬN AUTO",
            f"👥 Acc      : <b>{d['numacc']}</b>\n"
            f"🔗 Target   : <code>{_html_escape(d['link'])}</code>\n"
            f"⏱ Delay gửi: <b>{d['delay']}s</b>\n"
            f"🗑 Delay xoá: <b>{d['deldelay']}s</b>\n"
            f"🔁 Số tin  : <b>{d['total']}</b>"),
            reply_markup=kb_confirm(), parse_mode=ParseMode.HTML)


async def launch_auto(update, ctx):
    s = st(update.effective_user.id); s.step = None
    s.status = "RUNNING"; s.stop_event = asyncio.Event()
    logger = await make_logger(ctx.application, update.effective_user.id,
                               update.effective_chat.id)
    d = s.data
    sessions = list_sessions()[:d["numacc"]]

    async def runner():
        try:
            await mod_auto.run_auto(sessions, d["link"], d["delay"],
                                    d["deldelay"], d["total"], s.stop_event, logger)
            s.status = "DONE"
        except asyncio.CancelledError: s.status = "STOPPED"
        except Exception as e:
            s.status = "ERROR"; await logger(f"❌ {e}")
        finally:
            await finalize_log(ctx.application, update.effective_user.id)
    s.task = asyncio.create_task(runner())


# ─── flow: REF ──────────────────────────────────────────
async def flow_ref_start(update, ctx):
    s = st(update.effective_user.id)
    if not list_sessions():
        await send(update, banner("⚠ REF", "Chưa có acc."), kb_back_home()); return
    s.step = "ref.link"; s.data = {}
    await send(update, banner("🎯 REF",
        "📌 Dán <b>link ref bot</b>\n\n"
        "Ví dụ: <code>https://t.me/botname?start=xxxxxx</code>\n\n"
        "➤ Nhập link:"), kb_cancel())


async def step_ref(update, ctx, text: str):
    s = st(update.effective_user.id); d = s.data
    if s.step == "ref.link":
        d["link"] = text.strip(); s.step = "ref.times"
        await update.message.reply_text(banner("🔁 SỐ REF",
            "Mỗi acc sẽ chạy bao nhiêu lượt ref\n\n➤ Nhập số:"),
            reply_markup=kb_cancel(), parse_mode=ParseMode.HTML)
    elif s.step == "ref.times":
        try: d["times"] = int(text)
        except: await update.message.reply_text("❌ Sai."); return
        s.step = "ref.confirm"
        await update.message.reply_text(banner("✅ XÁC NHẬN REF",
            f"🔗 Link : <code>{_html_escape(d['link'])}</code>\n"
            f"🔁 Lần  : <b>{d['times']}</b>\n"
            f"👥 Acc  : <b>{len(list_sessions())}</b>"),
            reply_markup=kb_confirm(), parse_mode=ParseMode.HTML)


async def launch_ref(update, ctx):
    s = st(update.effective_user.id); s.step = None
    s.status = "RUNNING"; s.stop_event = asyncio.Event()
    logger = await make_logger(ctx.application, update.effective_user.id,
                               update.effective_chat.id)
    d = s.data; sessions = list_sessions()

    async def runner():
        try:
            await mod_ref.run_ref(sessions, d["link"], d["times"], logger)
            s.status = "DONE"
        except asyncio.CancelledError: s.status = "STOPPED"
        except Exception as e:
            s.status = "ERROR"; await logger(f"❌ {e}")
        finally:
            await finalize_log(ctx.application, update.effective_user.id)
    s.task = asyncio.create_task(runner())


# ─── flow: SEX SPAM ─────────────────────────────────────
async def flow_spam_start(update, ctx):
    s = st(update.effective_user.id)
    if not list_sessions():
        await send(update, banner("⚠ SEX SPAM", "Chưa có acc."), kb_back_home()); return
    s.step = "spam.target"; s.data = {}
    await send(update, banner("💬 SEX SPAM",
        "📌 Username/SĐT người nhận, hoặc link nhóm\n\n"
        "Ví dụ:\n<code>@username</code>\n<code>https://t.me/groupx</code>\n\n"
        "➤ Nhập target:"), kb_cancel())


async def step_spam(update, ctx, text: str):
    s = st(update.effective_user.id); d = s.data
    if s.step == "spam.target":
        d["target"] = text.strip()
        d["mode"] = "group" if ("t.me/" in d["target"] and "+" not in d["target"]) else "private"
        if d["target"].startswith("https://t.me/+"):
            d["mode"] = "group"
        s.step = "spam.msg"
        await update.message.reply_text(banner("✉️ NỘI DUNG",
            "Nhập nội dung tin nhắn muốn gửi\n\n➤ Nhập:"),
            reply_markup=kb_cancel(), parse_mode=ParseMode.HTML)
    elif s.step == "spam.msg":
        d["msg"] = text; s.step = "spam.total"
        await update.message.reply_text(banner("🔁 SỐ LẦN",
            "Số tin mỗi acc sẽ gửi\n\n➤ Nhập:"),
            reply_markup=kb_cancel(), parse_mode=ParseMode.HTML)
    elif s.step == "spam.total":
        try: d["total"] = int(text)
        except: await update.message.reply_text("❌ Sai."); return
        s.step = "spam.delay"
        await update.message.reply_text(banner("⏱ DELAY",
            "Giây giữa mỗi tin (>=0.3)\n\n➤ Nhập:"),
            reply_markup=kb_cancel(), parse_mode=ParseMode.HTML)
    elif s.step == "spam.delay":
        try:
            v = float(text)
            if v < 0.3: raise ValueError
            d["delay"] = v
        except: await update.message.reply_text("❌ >=0.3, nhập lại:"); return
        s.step = "spam.deldelay"
        await update.message.reply_text(banner("🗑 DELAY XOÁ",
            "Sau bao nhiêu giây sẽ xoá (0 = không xoá)\n\n➤ Nhập:"),
            reply_markup=kb_cancel(), parse_mode=ParseMode.HTML)
    elif s.step == "spam.deldelay":
        try: d["deldelay"] = int(text)
        except: await update.message.reply_text("❌ Sai."); return
        s.step = "spam.confirm"
        await update.message.reply_text(banner("✅ XÁC NHẬN SPAM",
            f"🎯 Target : <code>{_html_escape(d['target'])}</code>\n"
            f"📦 Mode   : <b>{d['mode']}</b>\n"
            f"✉️ Msg    : <code>{_html_escape(d['msg'][:60])}</code>\n"
            f"🔁 Số tin : <b>{d['total']}</b>\n"
            f"⏱ Delay  : <b>{d['delay']}s</b>\n"
            f"🗑 Xoá   : <b>{d['deldelay']}s</b>"),
            reply_markup=kb_confirm(), parse_mode=ParseMode.HTML)


async def launch_spam(update, ctx):
    s = st(update.effective_user.id); s.step = None
    s.status = "RUNNING"; s.stop_event = asyncio.Event()
    logger = await make_logger(ctx.application, update.effective_user.id,
                               update.effective_chat.id)
    d = s.data; sessions = list_sessions()

    async def runner():
        try:
            await mod_spam.run_spam(sessions, d["mode"], d["target"], d["msg"],
                                    d["total"], d["delay"], d["deldelay"],
                                    s.stop_event, logger)
            s.status = "DONE"
        except asyncio.CancelledError: s.status = "STOPPED"
        except Exception as e:
            s.status = "ERROR"; await logger(f"❌ {e}")
        finally:
            await finalize_log(ctx.application, update.effective_user.id)
    s.task = asyncio.create_task(runner())


# ─── ACCOUNT: ADD (login Telethon) ──────────────────────
async def acc_add_start(update, ctx):
    s = st(update.effective_user.id)
    s.step = "addacc.phone"; s.data = {}
    await send(update, banner("📱 THÊM TÀI KHOẢN",
        "➤ Nhập số điện thoại (kèm mã quốc gia, ví dụ <code>+84…</code>):"),
        kb_cancel())


async def step_addacc(update, ctx, text: str):
    uid = update.effective_user.id; s = st(uid)
    if s.step == "addacc.phone":
        phone = text.strip()
        if phone.startswith("0"): phone = "+84" + phone[1:]
        elif not phone.startswith("+"): phone = "+" + phone
        s.login_phone = phone
        s.login_client = make_client(phone)
        try:
            await s.login_client.connect()
            await s.login_client.send_code_request(phone)
        except Exception as e:
            await update.message.reply_text(f"❌ Lỗi gửi OTP: {e}",
                                            reply_markup=kb_cancel())
            try: await s.login_client.disconnect()
            except: pass
            s.login_client = None; s.step = None
            return
        s.step = "addacc.code"
        await update.message.reply_text(banner("📩 OTP",
            f"Đã gửi OTP tới <b>{_html_escape(phone)}</b>\n\n"
            "➤ Nhập mã OTP (có thể chèn dấu cách giữa các số):"),
            reply_markup=kb_cancel(), parse_mode=ParseMode.HTML)
    elif s.step == "addacc.code":
        code = "".join(c for c in text if c.isdigit())
        try:
            await s.login_client.sign_in(s.login_phone, code)
        except tele_errors.SessionPasswordNeededError:
            s.step = "addacc.2fa"
            await update.message.reply_text(banner("🔐 2FA",
                "Tài khoản có bật 2FA.\n➤ Nhập mật khẩu 2FA:"),
                reply_markup=kb_cancel(), parse_mode=ParseMode.HTML)
            return
        except Exception as e:
            await update.message.reply_text(f"❌ Sai OTP: {e}\nNhập lại:",
                                            reply_markup=kb_cancel())
            return
        await _finish_login(update, ctx)
    elif s.step == "addacc.2fa":
        try:
            await s.login_client.sign_in(password=text.strip())
        except Exception as e:
            await update.message.reply_text(f"❌ 2FA sai: {e}\nNhập lại:",
                                            reply_markup=kb_cancel())
            return
        await _finish_login(update, ctx)


async def _finish_login(update, ctx):
    uid = update.effective_user.id; s = st(uid)
    try:
        me = await s.login_client.get_me()
        info = f"✅ Đăng nhập thành công\n\n👤 {me.first_name or ''} {me.last_name or ''}\n📱 {me.phone}"
    except Exception as e:
        info = f"⚠ Lỗi lấy thông tin: {e}"
    try: await s.login_client.disconnect()
    except: pass
    s.login_client = None; s.login_phone = None; s.step = None
    await update.message.reply_text(banner("📱 THÊM TÀI KHOẢN", info),
                                    reply_markup=kb_acc(), parse_mode=ParseMode.HTML)


# ─── ACCOUNT: LIST / DELETE / LIVE / EXPORT ─────────────
async def acc_list(update, ctx):
    sessions = list_sessions()
    if not sessions:
        await send(update, banner("📋 DANH SÁCH ACC", "Chưa có acc nào."), kb_acc())
        return
    rows = []
    for i, name in enumerate(sessions, 1):
        rows.append(f"<b>{i}.</b> <code>{_html_escape(name)}</code>")
    await send(update, banner("📋 DANH SÁCH ACC",
        f"Tổng: <b>{len(sessions)}</b>\n\n" + "\n".join(rows)),
        kb_acc())


async def acc_del_start(update, ctx):
    sessions = list_sessions()
    if not sessions:
        await send(update, banner("🗑 XOÁ ACC", "Không có acc."), kb_acc()); return
    rows = [[InlineKeyboardButton(f"🗑 {n}", callback_data=f"acc:delpick:{n}")]
            for n in sessions]
    rows.append([InlineKeyboardButton("⬅️ BACK", callback_data="m:acc")])
    await send(update, banner("🗑 XOÁ ACC", "Chọn acc cần xoá:"),
               InlineKeyboardMarkup(rows))


async def acc_live(update, ctx):
    sessions = list_sessions()
    if not sessions:
        await send(update, banner("💓 CHECK LIVE", "Không có acc."), kb_acc()); return
    await send(update, banner("💓 CHECK LIVE", "Đang kiểm tra..."), kb_acc())
    results = []
    for name in sessions:
        c = make_client(name); alive = False
        try:
            await c.connect()
            alive = await c.is_user_authorized()
        except Exception:
            alive = False
        finally:
            try: await c.disconnect()
            except: pass
        results.append(f"{'✅' if alive else '❌'} <code>{_html_escape(name)}</code>")
    await update.effective_chat.send_message(
        banner("💓 CHECK LIVE — KẾT QUẢ", "\n".join(results)),
        reply_markup=kb_acc(), parse_mode=ParseMode.HTML)


async def acc_export(update, ctx):
    sessions = list_sessions()
    if not sessions:
        await send(update, banner("📤 EXPORT", "Không có acc."), kb_acc()); return
    for name in sessions:
        path = session_path(name) + ".session"
        if os.path.exists(path):
            try:
                with open(path, "rb") as f:
                    await update.effective_chat.send_document(
                        InputFile(f, filename=f"{name}.session"))
            except Exception as e:
                await update.effective_chat.send_message(f"❌ {name}: {e}")
    await update.effective_chat.send_message(
        banner("📤 EXPORT", "Hoàn tất."),
        reply_markup=kb_acc(), parse_mode=ParseMode.HTML)


# ─── delete pick callback ───────────────────────────────
async def on_del_pick(update: Update, ctx):
    q = update.callback_query; uid = q.from_user.id
    if not is_admin(uid):
        await q.answer("❌", show_alert=True); return
    await q.answer()
    name = q.data.split(":", 2)[2]
    s = st(uid); s.step = f"acc.del.confirm:{name}"
    await send(update, banner("🗑 XÁC NHẬN XOÁ",
        f"Xoá <code>{_html_escape(name)}</code>?"), kb_confirm())


# ─── document handler (IMPORT SESSION) ──────────────────
async def on_document(update: Update, ctx):
    uid = update.effective_user.id
    if not is_admin(uid): return
    s = st(uid)
    if s.step != "import.wait": return
    doc = update.message.document
    if not doc.file_name.endswith(".session"):
        await update.message.reply_text("❌ Phải là file .session"); return
    f = await doc.get_file()
    dest = os.path.join(SESSIONS_DIR, doc.file_name)
    await f.download_to_drive(dest)
    s.step = None
    await update.message.reply_text(
        banner("📥 IMPORT SESSION", f"✅ Đã thêm <code>{doc.file_name}</code>"),
        reply_markup=kb_acc(), parse_mode=ParseMode.HTML)


# ─── text router ────────────────────────────────────────
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    s = st(uid)
    if not s.step:
        await update.message.reply_text("Dùng menu bên dưới 👇",
                                        reply_markup=kb_main())
        return
    txt = update.message.text or ""
    try:
        if s.step.startswith("check."):   await step_check(update, ctx, txt)
        elif s.step.startswith("auto."):  await step_auto(update, ctx, txt)
        elif s.step.startswith("ref."):   await step_ref(update, ctx, txt)
        elif s.step.startswith("spam."):  await step_spam(update, ctx, txt)
        elif s.step.startswith("addacc."): await step_addacc(update, ctx, txt)
    except Exception as e:
        log.exception("step error")
        await update.message.reply_text(f"❌ Lỗi: {e}", reply_markup=kb_main())
        s.step = None


# ─── main ───────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_del_pick, pattern=r"^acc:delpick:"))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Bot starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
