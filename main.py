import asyncio
import time
import os
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

BOT_TOKEN = os.getenv("BOT_TOKEN")

GROUP_A_ID = -1003904086062
TOPIC_A_ID = 305

GROUP_B_ID = -1002415746359
TOPIC_B_ID = 3302852

COOLDOWN = 10

active_searches = {}
user_cooldown = {}

# ========= FIND =========
async def handle_find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = msg.from_user
    now = time.time()

    # Anti-spam
    if user.id in user_cooldown and now - user_cooldown[user.id] < COOLDOWN:
        return

    user_cooldown[user.id] = now

    # لازم reply على اسم اللاعب
    if not msg.reply_to_message:
        return

    player_name = msg.reply_to_message.text.strip()

    # تحديد الاتجاه
    if update.effective_chat.id == GROUP_A_ID:
        target_group = GROUP_B_ID
        target_topic = TOPIC_B_ID
        origin_group = GROUP_A_ID
        origin_topic = TOPIC_A_ID
        label = "Italian group"
    else:
        target_group = GROUP_A_ID
        target_topic = TOPIC_A_ID
        origin_group = GROUP_B_ID
        origin_topic = TOPIC_B_ID
        label = "Arabic group"

    # إرسال للجروب التاني
    sent_msg = await context.bot.send_message(
        chat_id=target_group,
        message_thread_id=target_topic,
        text=f"{player_name}\nReply if you are here\n⏱️ 20 sec"
    )

    # تخزين الحالة
    active_searches[sent_msg.message_id] = {
        "origin_group": origin_group,
        "origin_topic": origin_topic,
        "origin_user": user,
        "expire": time.time() + 20,
        "handled": False,
        "label": label
    }

    # استنى 30 ثانية وبعدين امسح لو مفيش رد
    await asyncio.sleep(20)

    if sent_msg.message_id in active_searches:
        del active_searches[sent_msg.message_id]


# ========= REPLIES =========
async def handle_replies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message

    if not msg or msg.from_user.is_bot:
        return

    if not msg.reply_to_message:
        return

    search = active_searches.get(msg.reply_to_message.message_id)

    if not search:
        return

    # لو الوقت خلص أو already handled
    if time.time() > search["expire"] or search["handled"]:
        return

    search["handled"] = True

    user = search["origin_user"]

    await context.bot.send_message(
        chat_id=search["origin_group"],
        message_thread_id=search["origin_topic"],
        text=f"✅ Found in {search['label']}\n@{user.username}\n💬 {msg.text}"
    )

    del active_searches[msg.reply_to_message.message_id]


# ========= ROUTER =========
async def router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message

    if not msg:
        return

    # منع loop
    if msg.from_user.is_bot:
        return

    chat_id = update.effective_chat.id
    topic_id = msg.message_thread_id

    # فلترة التوبيك
    if chat_id == GROUP_A_ID and topic_id != TOPIC_A_ID:
        return

    if chat_id == GROUP_B_ID and topic_id != TOPIC_B_ID:
        return

    # /find
    if msg.text and msg.text.lower().startswith("/find"):
        await handle_find(update, context)
    else:
        await handle_replies(update, context)


# ========= MAIN =========
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.ALL, router))

    print("Bot Running...")
    app.run_polling()


if __name__ == "__main__":
    main()
