import asyncio
import time
import os
import html

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    MessageHandler,
    MessageReactionHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")

GROUP_A_ID = -1003904086062
TOPIC_A_ID = 305

GROUP_B_ID = -1002415746359
TOPIC_B_ID = 3302852

COOLDOWN = 10
SEARCH_TIMEOUT = 20

active_searches = {}
user_cooldown = {}


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
    elif chat_id == GROUP_B_ID:
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


async def cleanup_search_later(key):
    await asyncio.sleep(SEARCH_TIMEOUT)
    search = active_searches.get(key)
    if search and not search["handled"] and time.time() > search["expire"]:
        del active_searches[key]


# ========= FIND =========
async def handle_find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.from_user:
        return

    user = msg.from_user
    now = time.time()

    if user.id in user_cooldown and now - user_cooldown[user.id] < COOLDOWN:
        return

    user_cooldown[user.id] = now

    if not msg.reply_to_message:
        await msg.reply_text("Use /find as a reply to the player name.")
        return

    source_msg = msg.reply_to_message
    player_name = (
        source_msg.text
        or source_msg.caption
        or ""
    ).strip()

    if not player_name:
        await msg.reply_text("The replied message has no text.")
        return

    route = get_route(update.effective_chat.id)
    if not route:
        return

    sent_msg = await context.bot.send_message(
        chat_id=route["target_group"],
        message_thread_id=route["target_topic"],
        text=f"{player_name}\n\nReply or react if you are here.\n⏱️ {SEARCH_TIMEOUT} sec"
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
    }

    asyncio.create_task(cleanup_search_later(key))


# ========= REPLY =========
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

    if not search:
        return

    if time.time() > search["expire"] or search["handled"]:
        return

    search["handled"] = True

    origin_mention = user_mention(search["origin_user_id"], search["origin_user_name"])
    responder_mention = user_mention(msg.from_user.id, msg.from_user.full_name)
    reply_text = html.escape(msg.text or msg.caption or "Reply received")

    await context.bot.send_message(
        chat_id=search["origin_group"],
        message_thread_id=search["origin_topic"],
        reply_to_message_id=search["find_message_id"],
        text=(
            f"✅ Found in {html.escape(search['label'])}\n"
            f"{origin_mention}\n"
            f"👤 Response from: {responder_mention}\n"
            f"💬 {reply_text}"
        ),
        parse_mode=ParseMode.HTML,
    )

    if key in active_searches:
        del active_searches[key]


# ========= REACTION =========
async def handle_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reaction_update = update.message_reaction

    if not reaction_update:
        return

    if not reaction_update.user:
        return

    if reaction_update.user.is_bot:
        return

    if not is_allowed_topic(reaction_update.chat.id, reaction_update.message_thread_id):
        return

    key = (reaction_update.chat.id, reaction_update.message_id)
    search = active_searches.get(key)

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
        if emoji:
            reaction_texts.append(emoji)
        else:
            reaction_texts.append("reaction")

    reactions_str = " ".join(reaction_texts)

    origin_mention = user_mention(search["origin_user_id"], search["origin_user_name"])
    reactor_mention = user_mention(reaction_update.user.id, reaction_update.user.full_name)

    await context.bot.send_message(
        chat_id=search["origin_group"],
        message_thread_id=search["origin_topic"],
        reply_to_message_id=search["find_message_id"],
        text=(
            f"✅ Found in {html.escape(search['label'])}\n"
            f"{origin_mention}\n"
            f"👤 Reaction from: {reactor_mention}\n"
            f"❤️ {html.escape(reactions_str)}"
        ),
        parse_mode=ParseMode.HTML,
    )

    if key in active_searches:
        del active_searches[key]


# ========= ROUTER =========
async def router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message

    if not msg:
        return

    if not msg.from_user or msg.from_user.is_bot:
        return

    chat_id = update.effective_chat.id
    topic_id = msg.message_thread_id

    if not is_allowed_topic(chat_id, topic_id):
        return

    text = (msg.text or "").strip().lower()

    if text.startswith("/find"):
        await handle_find(update, context)
    else:
        await handle_replies(update, context)


# ========= MAIN =========
def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(MessageHandler(filters.ALL, router))
    app.add_handler(MessageReactionHandler(handle_reaction))

    print("Bot Running...")

    app.run_polling(
        allowed_updates=["message", "message_reaction"]
    )


if __name__ == "__main__":
    main()
