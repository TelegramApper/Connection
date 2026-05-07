import asyncio
import time
import os
import re
import html
import json
import logging

from telegram import Update
from telegram.constants import ParseMode, ChatType
from telegram.ext import (
    Application,
    MessageHandler,
    MessageReactionHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
BLACKLIST_FILE = os.getenv("BLACKLIST_FILE", "blacklist.json")

GROUP_A_ID = -1003904086062
TOPIC_A_ID = 305

GROUP_B_ID = -1002415746359
TOPIC_B_ID = 3302852

COOLDOWN = 10
SEARCH_TIMEOUT = 20
CONFIRM_DELETE_DELAY = 15

active_searches = {}
user_cooldown = {}
BLACKLIST = set()

# ===== ترجمة الرسائل الإيطالية والإنجليزية =====
MESSAGES = {
    "en": {
        "header": "Reply or react if you are here.\n⏱️ {SEARCH_TIMEOUT} sec",
        "blacklisted": "{player_name} is blacklisted, please leave this match.",
        "searching_for": "Searching for {player_name}",
        "found_in_arabic": "✅ Found in Arabic group\n@{user_name}\n💬 {msg_text}",
        "no_text": "The replied message has no text.",
    },
    "it": {
        "header": "Rispondi a questo messaggio (in Inglese) se sei qua\n⏱️ {SEARCH_TIMEOUT} sec",
        "blacklisted": "È in blacklist, abbandona la partita",
        "searching_for": "Sto cercando",
        "found_in_arabic": "Trovato nel gruppo arabo\n@{user_name}\n💬 {msg_text}",
        "no_text": "Per favore scrivi il nome prima del comando",
    }
}

def normalize_name(name: str) -> str:
    return (name or "").strip().casefold()

def load_blacklist():
    global BLACKLIST
    try:
        with open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            BLACKLIST = {normalize_name(name) for name in data if str(name).strip()}
        logger.info("Blacklist loaded: %s items", len(BLACKLIST))
    except FileNotFoundError:
        BLACKLIST = set()
        logger.info("Blacklist file not found, starting with empty blacklist")
    except Exception as e:
        BLACKLIST = set()
        logger.exception("Failed to load blacklist: %s", e)

def save_blacklist():
    with open(BLACKLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(BLACKLIST), f, ensure_ascii=False, indent=2)
    logger.info("Blacklist saved: %s items", len(BLACKLIST))

def user_mention(user_id: int, full_name: str) -> str:
    safe_name = html.escape(full_name or "User")
    return f"<a href='tg://user?id={user_id}'>{safe_name}</a>"

def get_route(chat_id: int):
    if chat_id == GROUP_A_ID:
        return {
            "target_group": GROUP_B_ID,
            "target_topic": TOPIC_B_ID,
            "origin_group": GROUP_A_ID,
            "origin_topic": TOPIC_A_ID,
            "label": "Italian group",
        }
    if chat_id == GROUP_B_ID:
        return {
            "target_group": GROUP_A_ID,
            "target_topic": TOPIC_A_ID,
            "origin_group": GROUP_B_ID,
            "origin_topic": TOPIC_B_ID,
            "label": "Arabic group",
        }
    return None

def is_allowed_topic(chat_id: int, topic_id: int | None) -> bool:
    if chat_id == GROUP_A_ID:
        return topic_id == TOPIC_A_ID
    if chat_id == GROUP_B_ID:
        return topic_id == TOPIC_B_ID
    return False

def is_owner(update: Update) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    return bool(
        user
        and chat
        and user.id == OWNER_ID
        and chat.type == ChatType.PRIVATE
    )

async def delete_message_later(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int,
    delay: int = CONFIRM_DELETE_DELAY,
):
    await asyncio.sleep(delay)
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass

async def cleanup_search_later(key):
    await asyncio.sleep(SEARCH_TIMEOUT)
    search = active_searches.get(key)
    if search and not search["handled"] and time.time() > search["expire"]:
        del active_searches[key]
        logger.info("Search expired and removed: %s", key)

async def create_search(update: Update, context: ContextTypes.DEFAULT_TYPE, player_name: str):
    msg = update.message
    if not msg or not msg.from_user:
        return

    user = msg.from_user
    now = time.time()

    if user.id in user_cooldown and now - user_cooldown[user.id] < COOLDOWN:
        logger.info("Cooldown hit for user %s", user.id)
        return

    user_cooldown[user.id] = now

    player_name = player_name.strip()
    if not player_name:
        await msg.reply_text("Write the player name first.")
        return

    normalized_name = normalize_name(player_name)
    if normalized_name in BLACKLIST:
        await msg.reply_text(f"{player_name} is blacklisted, please leave this match.")
        return

    route = get_route(update.effective_chat.id)
    if not route:
        logger.warning("No route found for chat_id=%s", update.effective_chat.id)
        return

    # حدد اللغة: إذا الجروب في إيطاليا فـ "it"، غيرها "en"
    if route["target_group"] == GROUP_B_ID:
        lang = "it"
    else:
        lang = "en"

    # رسائل الجروب الإيطالي تستخدم النص الإيطالي
    header = MESSAGES[lang]["header"].format(SEARCH_TIMEOUT=SEARCH_TIMEOUT)
    logger.info("Sending header in lang=%s: %s", lang, header)

    sent_msg = await context.bot.send_message(
        chat_id=route["target_group"],
        message_thread_id=route["target_topic"],
        text=header
    )

    key = (route["target_group"], sent_msg.message_id)

    active_searches[key] = {
        "origin_group": route["origin_group"],
        "origin_topic": route["origin_topic"],
        "origin_user_id": user.id,
        "origin_user_name": user.full_name,
        "find_message_id": msg.message_id,
        "player_name": player_name,
        "expire": time.time() + SEARCH_TIMEOUT,
        "handled": False,
        "label": route["label"],
        "lang": lang,
    }

    logger.info("Search stored with key=%s, lang=%s", key, lang)
    asyncio.create_task(cleanup_search_later(key))

async def handle_find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    if not msg.reply_to_message:
        await msg.reply_text("Use /find or /f as a reply to the player name, or send: Name /f")
        return

    source_msg = msg.reply_to_message
    player_name = (source_msg.text or source_msg.caption or "").strip()

    if not player_name:
        await msg.reply_text("The replied message has no text.")
        return

    await create_search(update, context, player_name)

async def handle_inline_find(update: Update, context: ContextTypes.DEFAULT_TYPE, player_name: str):
    logger.info("handle_inline_find triggered with player_name=%s", player_name)
    await create_search(update, context, player_name)

async def handle_replies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message

    if not msg or not msg.from_user or msg.from_user.is_bot:
        return

    if not msg.reply_to_message:
        return

    if not is_allowed_topic(msg.chat.id, msg.message_thread_id):
        logger.info("Reply ignored due to topic mismatch: chat=%s topic=%s", msg.chat.id, msg.message_thread_id)
        return

    key = (msg.chat.id, msg.reply_to_message.message_id)
    search = active_searches.get(key)

    if not search:
        return

    if time.time() > search["expire"] or search["handled"]:
        logger.info("Reply ignored because expired or handled: key=%s", key)
        return

    # لغة الرسالة (الإنجليزي أو الإيطالي) تأتي من الحالة
    lang = search.get("lang", "en")

    search["handled"] = True

    origin_mention = user_mention(search["origin_user_id"], search["origin_user_name"])
    responder_mention = user_mention(msg.from_user.id, msg.from_user.full_name)
    reply_text = html.escape(msg.text or msg.caption or "Reply received")

    # في الجروب العربي: الرسالة بالإنجليزي، في الجروب الإيطالي: الإيطالي.
    if search["origin_group"] == GROUP_A_ID:
        lang_reply = "en"
    else:
        lang_reply = "it"

    text = MESSAGES[lang_reply]["found_in_arabic"].format(
        user_name=origin_mention,
        msg_text=reply_text
    )

    await context.bot.send_message(
        chat_id=search["origin_group"],
        message_thread_id=search["origin_topic"],
        reply_to_message_id=search["find_message_id"],
        text=text,
        parse_mode=ParseMode.HTML,
    )

    if key in active_searches:
        del active_searches[key]
        logger.info("Reply handled and search removed: %s", key)

async def handle_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reaction_update = update.message_reaction

    if not reaction_update:
        return

    if not reaction_update.user or reaction_update.user.is_bot:
        return

    if not is_allowed_topic(reaction_update.chat.id, reaction_update.message_thread_id):
        logger.info(
            "Reaction ignored due to topic mismatch: chat=%s topic=%s",
            reaction_update.chat.id,
            reaction_update.message_thread_id,
        )
        return

    key = (reaction_update.chat.id, reaction_update.message_id)
    search = active_searches.get(key)

    if not search:
        return

    if time.time() > search["expire"] or search["handled"]:
        logger.info("Reaction ignored because expired or handled: key=%s", key)
        return

    search["handled"] = True

    new_reactions = reaction_update.new_reaction or []
    if not new_reactions:
        return

    reaction_texts = []
    for r in new_reactions:
        emoji = getattr(r, "emoji", None)
        if emoji:
            reaction_texts.append(emoji)
        else:
            reaction_texts.append("reaction")

    reactions_str = " ".join(reaction_texts)

    origin_mention = user_mention(search["origin_user_id"], search["origin_user_name"])
    reactor_mention = user_mention(reaction_update.user.id, reaction_update.user.full_name)

    # في الجروب العربي: الإنجليزي، في الجروب الإيطالي: الإيطالي
    if search["origin_group"] == GROUP_A_ID:
        lang_reply = "en"
    else:
        lang_reply = "it"

    text = MESSAGES[lang_reply]["found_in_arabic"].format(
        user_name=origin_mention,
        msg_text=f"Reaction {reactions_str}"
    )

    await context.bot.send_message(
        chat_id=search["origin_group"],
        message_thread_id=search["origin_topic"],
        reply_to_message_id=search["find_message_id"],
        text=text,
        parse_mode=ParseMode.HTML,
    )

    if key in active_searches:
        del active_searches[key]
        logger.info("Reaction handled and search removed: %s", key)

async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not update.effective_user:
        return
    await msg.reply_text(f"Your user ID is: {update.effective_user.id}")

async def blacklist_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    if not is_owner(update):
        await msg.reply_text("This command is available only in private chat for the bot owner.")
        return

    if not context.args:
        await msg.reply_text("Usage: /blacklist_add name")
        return

    player_name = " ".join(context.args).strip()
    normalized_name = normalize_name(player_name)

    if not normalized_name:
        await msg.reply_text("Usage: /blacklist_add name")
        return

    if normalized_name in BLACKLIST:
        await msg.reply_text(f"{player_name} is already blacklisted.")
        return

    BLACKLIST.add(normalized_name)
    save_blacklist()
    await msg.reply_text(f"{player_name} added to blacklist.")

async def blacklist_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    if not is_owner(update):
        await msg.reply_text("This command is available only in private chat for the bot owner.")
        return

    if not context.args:
        await msg.reply_text("Usage: /blacklist_remove name")
        return

    player_name = " ".join(context.args).strip()
    normalized_name = normalize_name(player_name)

    if normalized_name not in BLACKLIST:
        await msg.reply_text(f"{player_name} is not in blacklist.")
        return

    BLACKLIST.remove(normalized_name)
    save_blacklist()
    await msg.reply_text(f"{player_name} removed from blacklist.")

async def blacklist_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    if not is_owner(update):
        await msg.reply_text("This command is available only in private chat for the bot owner.")
        return

    if not BLACKLIST:
        await msg.reply_text("Blacklist is empty.")
        return

    text = "Blacklisted names:\n" + "\n".join(f"- {name}" for name in sorted(BLACKLIST))
    await msg.reply_text(text)

async def router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message

    if not msg:
        return

    if not msg.from_user or msg.from_user.is_bot:
        return

    chat = update.effective_chat
    chat_id = chat.id if chat else None
    topic_id = msg.message_thread_id
    text = (msg.text or "").strip()

    logger.info(
        "ROUTER HIT | chat_id=%s | chat_type=%s | topic_id=%s | text=%r",
        chat_id,
        chat.type if chat else None,
        topic_id,
        text,
    )

    if chat and chat.type == ChatType.PRIVATE:
        logger.info("Router ignored private chat message")
        return

    if not is_allowed_topic(chat_id, topic_id):
        logger.warning(
            "TOPIC BLOCKED | chat_id=%s | received_topic=%s | expected_A=%s/%s | expected_B=%s/%s",
            chat_id,
            topic_id,
            GROUP_A_ID,
            TOPIC_A_ID,
            GROUP_B_ID,
            TOPIC_B_ID,
        )
        return

    if text.lower().startswith("/find") or text.lower().startswith("/f"):
        logger.info("Command-like text detected: %s", text)
        if msg.reply_to_message:
            await handle_find(update, context)
        else:
            logger.info("Command ignored because it is not a reply")
        return

    match = re.match(r"^(.*?)\s*/f$", text, re.IGNORECASE)
    if match:
        player_name = match.group(1).strip()
        if player_name:
            await handle_inline_find(update, context, player_name)
            return

    await handle_replies(update, context)

def main():
    load_blacklist()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("blacklist_add", blacklist_add))
    app.add_handler(CommandHandler("blacklist_remove", blacklist_remove))
    app.add_handler(CommandHandler("blacklist_list", blacklist_list))

    app.add_handler(MessageReactionHandler(handle_reaction))
    app.add_handler(MessageHandler(filters.ALL, router))

    logger.info("Bot starting...")
    app.run_polling(allowed_updates=["message", "message_reaction"])

if __name__ == "__main__":
    main()
