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
# OWNER_ID ما يُستخدم كـ fallback بس لو حابب
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

# ADMINS: أكثر من مستخدم واحد يقدر يتحكم في الـ blacklist
ADMINS_RAW = os.getenv("ADMINS", "")
ADMINS = set()
if ADMINS_RAW:
    try:
        ADMINS = {int(id_str.strip()) for id_str in ADMINS_RAW.split(",") if id_str.strip()}
    except Exception as e:
        logger.exception("Failed to parse ADMINS: %s", e)
logger.info("ADMINS loaded: %s", ADMINS)

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

TEXTS = {
    "en": {
        "header": "Reply to this message if you are here.\\n⏱️ {timeout} sec",
        "blacklisted": "{player} is blacklisted, please leave this match.",
        "searching": "Searching for {player}",
        "found_ar": "✅ Found in Italian group\\n{origin}\\n👤 Response from: {actor}\\n💬 {content}",
        "found_it": "✅ Found in Arabic group\\n{origin}\\n👤 Response from: {actor}\\n💬 {content}",
        "react_ar": "✅ Found in Italian group\\n{origin}\\n👤 Reaction from: {actor}\\n❤️ {content}",
        "react_it": "✅ Found in Arabic group\\n{origin}\\n👤 Reaction from: {actor}\\n❤️ {content}",
        "no_text": "The replied message has no text.",
        "usage": "Use /find or /f as a reply to the player name, or send: Name /f",
        "write_name": "Write the player name first.",
    },
    "it": {
        "header": "Rispondi a questo messaggio (in Inglese) se sei qua\\n⏱️ {timeout} sec",
        "blacklisted": "È in blacklist, abbandona la partita",
        "searching": "Sto cercando {player}",
        "found_ar": "✅ Trovato nel gruppo arabo\\n{origin}\\n👤 Risposta da: {actor}\\n💬 {content}",
        "found_it": "✅ Trovato nel gruppo italiano\\n{origin}\\n👤 Risposta da: {actor}\\n💬 {content}",
        "react_ar": "✅ Trovato nel gruppo arabo\\n{origin}\\n👤 Reazione da: {actor}\\n❤️ {content}",
        "react_it": "✅ Trovato nel gruppo italiano\\n{origin}\\n👤 Reazione da: {actor}\\n❤️ {content}",
        "no_text": "Per favore scrivi il nome prima del comando",
        "usage": "Usa /f rispondendo al nome del player, oppure scrivi: Nome /f",
        "write_name": "Per favore scrivi il nome prima del comando",
    },
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

def is_group_a(chat_id: int) -> bool:
    return chat_id == GROUP_A_ID

def is_group_b(chat_id: int) -> bool:
    return chat_id == GROUP_B_ID

def chat_lang(chat_id: int) -> str:
    return "it" if is_group_b(chat_id) else "en"

def get_route(chat_id: int):
    if chat_id == GROUP_A_ID:
        return {
            "target_group": GROUP_B_ID,
            "target_topic": TOPIC_B_ID,
            "origin_group": GROUP_A_ID,
            "origin_topic": TOPIC_A_ID,
            "target_label": "Italian group",
        }
    if chat_id == GROUP_B_ID:
        return {
            "target_group": GROUP_A_ID,
            "target_topic": TOPIC_A_ID,
            "origin_group": GROUP_B_ID,
            "origin_topic": TOPIC_B_ID,
            "target_label": "Arabic group",
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
    if not user or not chat or chat.type != ChatType.PRIVATE:
        return False
    return user.id in ADMINS  # أي واحد في ADMINS يقدر يستخدم الأوامر

async def delete_message_later(context, chat_id: int, message_id: int, delay: int = CONFIRM_DELETE_DELAY):
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
    chat = update.effective_chat
    if not msg or not msg.from_user or not chat:
        return

    source_chat_id = chat.id
    source_lang = chat_lang(source_chat_id)
    target_route = get_route(source_chat_id)

    if not target_route:
        logger.warning("No route found for chat_id=%s", source_chat_id)
        return

    user = msg.from_user
    now = time.time()
    if user.id in user_cooldown and now - user_cooldown[user.id] < COOLDOWN:
        logger.info("Cooldown hit for user %s", user.id)
        return
    user_cooldown[user.id] = now

    player_name = (player_name or "").strip()
    if not player_name:
        await msg.reply_text(TEXTS[source_lang]["write_name"])
        return

    normalized_name = normalize_name(player_name)
    if normalized_name in BLACKLIST:
        text = TEXTS["it"]["blacklisted"] if source_lang == "it" else TEXTS["en"]["blacklisted"].format(player=player_name)
        await msg.reply_text(text)
        return

    header_lang = "it" if source_chat_id == GROUP_A_ID else "en"
    header_text = f"{player_name}\\n\\n" + TEXTS[header_lang]["header"].format(timeout=SEARCH_TIMEOUT)

    logger.info(
        "Creating search: player=%s from_chat=%s to_chat=%s thread=%s",
        player_name,
        source_chat_id,
        target_route["target_group"],
        target_route["target_topic"],
    )

    sent_msg = await context.bot.send_message(
        chat_id=target_route["target_group"],
        message_thread_id=target_route["target_topic"],
        text=header_text,
    )

    key = (target_route["target_group"], sent_msg.message_id)
    active_searches[key] = {
        "origin_group": target_route["origin_group"],
        "origin_topic": target_route["origin_topic"],
        "origin_user_id": user.id,
        "origin_user_name": user.full_name,
        "find_message_id": msg.message_id,
        "player_name": player_name,
        "expire": time.time() + SEARCH_TIMEOUT,
        "handled": False,
    }
    logger.info("Search stored with key=%s", key)
    asyncio.create_task(cleanup_search_later(key))

async def handle_find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = update.effective_chat
    if not msg or not chat:
        return

    lang = "it" if is_group_b(chat.id) else "en"

    if not msg.reply_to_message:
        await msg.reply_text(TEXTS[lang]["usage"])
        return

    source_msg = msg.reply_to_message
    player_name = (source_msg.text or source_msg.caption or "").strip()
    if not player_name:
        await msg.reply_text(TEXTS[lang]["no_text"])
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
        return

    key = (msg.chat.id, msg.reply_to_message.message_id)
    search = active_searches.get(key)
    logger.info("Reply received: key=%s found=%s", key, bool(search))
    if not search:
        return
    if time.time() > search["expire"] or search["handled"]:
        return

    search["handled"] = True
    origin_mention = user_mention(search["origin_user_id"], search["origin_user_name"])
    responder_mention = user_mention(msg.from_user.id, msg.from_user.full_name)
    reply_text = html.escape(msg.text or msg.caption or "Reply received")

    origin_lang = "it" if is_group_b(search["origin_group"]) else "en"
    if search["origin_group"] == GROUP_A_ID:
        text = TEXTS[origin_lang]["found_it"].format(origin=origin_mention, actor=responder_mention, content=reply_text)
    else:
        text = TEXTS[origin_lang]["found_ar"].format(origin=origin_mention, actor=responder_mention, content=reply_text)

    await context.bot.send_message(
        chat_id=search["origin_group"],
        message_thread_id=search["origin_topic"],
        reply_to_message_id=search["find_message_id"],
        text=text,
        parse_mode=ParseMode.HTML,
    )

    active_searches.pop(key, None)
    logger.info("Reply handled and search removed: %s", key)

async def handle_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reaction_update = update.message_reaction
    if not reaction_update:
        return
    if not reaction_update.user or reaction_update.user.is_bot:
        return
    if not is_allowed_topic(reaction_update.chat.id, reaction_update.message_thread_id):
        return

    key = (reaction_update.chat.id, reaction_update.message_id)
    search = active_searches.get(key)
    logger.info("Reaction received: key=%s found=%s", key, bool(search))
    if not search:
        return
    if time.time() > search["expire"] or search["handled"]:
        return

    new_reactions = reaction_update.new_reaction or []
    if not new_reactions:
        return

    search["handled"] = True
    reaction_texts = []
    for r in new_reactions:
        emoji = getattr(r, "emoji", None)
        reaction_texts.append(emoji if emoji else "reaction")
    reactions_str = " ".join(reaction_texts)

    origin_mention = user_mention(search["origin_user_id"], search["origin_user_name"])
    reactor_mention = user_mention(reaction_update.user.id, reaction_update.user.full_name)

    origin_lang = "it" if is_group_b(search["origin_group"]) else "en"
    if search["origin_group"] == GROUP_A_ID:
        text = TEXTS[origin_lang]["react_it"].format(origin=origin_mention, actor=reactor_mention, content=html.escape(reactions_str))
    else:
        text = TEXTS[origin_lang]["react_ar"].format(origin=origin_mention, actor=reactor_mention, content=html.escape(reactions_str))

    await context.bot.send_message(
        chat_id=search["origin_group"],
        message_thread_id=search["origin_topic"],
        reply_to_message_id=search["find_message_id"],
        text=text,
        parse_mode=ParseMode.HTML,
    )

    active_searches.pop(key, None)
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
        await msg.reply_text("This command is available only in private chat for the bot owner(s).")
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
        await msg.reply_text("This command is available only in private chat for the bot owner(s).")
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
        await msg.reply_text("This command is available only in private chat for the bot owner(s).")
        return
    if not BLACKLIST:
        await msg.reply_text("Blacklist is empty.")
        return

    text = "Blacklisted names:\\n" + "\\n".join(f"- {name}" for name in sorted(BLACKLIST))
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

    logger.info("ROUTER HIT | chat_id=%s | chat_type=%s | topic_id=%s | text=%r", chat_id, chat.type if chat else None, topic_id, text)

    if chat and chat.type == ChatType.PRIVATE:
        return
    if not is_allowed_topic(chat_id, topic_id):
        logger.warning("TOPIC BLOCKED | chat_id=%s | received_topic=%s", chat_id, topic_id)
        return

    if text.lower().startswith("/find") or text.lower().startswith("/f"):
        if msg.reply_to_message:
            await handle_find(update, context)
        else:
            lang = "it" if is_group_b(chat_id) else "en"
            await msg.reply_text(TEXTS[lang]["usage"])
        return

    match = re.match(r"^(.*?)\\s*/f$", text, re.IGNORECASE)
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

   
