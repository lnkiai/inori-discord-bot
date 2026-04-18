import os
import threading
import asyncio
import aiohttp
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv
import discord
from discord.ext import commands


load_dotenv()


DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
API_SECRET = os.environ["API_SECRET"]
API_BASE_URL = os.environ.get("API_BASE_URL", "https://dify-docs-search.lnkiai.workers.dev")
PORT = int(os.environ.get("PORT", 10000))


MAX_MESSAGE_LENGTH = 2000
API_TIMEOUT_SECONDS = 30
THREAD_TITLE_MAX = 20  # スレッドタイトルの最大文字数



# ── /health サーバー ──────────────────────────────────────────────


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()


    def log_message(self, format, *args):
        pass  # アクセスログを黙らせる



def run_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    server.serve_forever()



# ── Discord Bot ───────────────────────────────────────────────────


intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)



def split_message(text: str) -> list[str]:
    """2000文字超えを分割する"""
    chunks = []
    while len(text) > MAX_MESSAGE_LENGTH:
        split_at = text.rfind("\n", 0, MAX_MESSAGE_LENGTH)
        if split_at == -1:
            split_at = MAX_MESSAGE_LENGTH
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


def make_thread_title(question: str) -> str:
    """質問文の先頭20文字をスレッドタイトルにする"""
    title = " ".join(question.split())  # 改行・連続スペースを整理
    if len(title) > THREAD_TITLE_MAX:
        title = title[:THREAD_TITLE_MAX] + "…"
    return title or "いのりへの質問"


async def call_ask_api(query: str, conversation_id: str | None) -> str:
    """検索・回答APIを呼ぶ"""
    headers = {
        "Content-Type": "application/json",
        "x-api-key": API_SECRET,
    }
    body = {
        "query": query,
        "fields": ["answer"],
    }
    if conversation_id:
        body["conversationId"] = conversation_id


    timeout = aiohttp.ClientTimeout(total=API_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(f"{API_BASE_URL}/ask", headers=headers, json=body) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data.get("answer", "（回答が取得できませんでした）")



def get_question_text(message: discord.Message) -> str:
    """メンション部分（ユーザー・ロール両方）を除いたテキストを返す"""
    content = message.content
    # ユーザーメンションを除去
    for mention in message.mentions:
        content = content.replace(f"<@{mention.id}>", "")
        content = content.replace(f"<@!{mention.id}>", "")
    # ロールメンションを除去
    for role in message.role_mentions:
        content = content.replace(f"<@&{role.id}>", "")
    return content.strip()


def is_bot_mentioned(message: discord.Message) -> bool:
    """ユーザーメンション or ボットが持つロールへのメンションを検知する"""
    # パターン1: @ボット（ユーザーメンション）
    if bot.user in message.mentions:
        return True
    # パターン2: ボットが持つロールへのメンション
    if message.guild and message.role_mentions:
        bot_member = message.guild.get_member(bot.user.id)
        if bot_member:
            bot_role_ids = {role.id for role in bot_member.roles}
            mentioned_role_ids = {role.id for role in message.role_mentions}
            if bot_role_ids & mentioned_role_ids:
                return True
    return False


async def reply_in_chunks(channel, text: str):
    """2000文字超えは分割して送信"""
    for chunk in split_message(text):
        await channel.send(chunk)



# ── イベントハンドラ ──────────────────────────────────────────────


@bot.event
async def on_ready():
    print(f"[いのり] ログイン完了: {bot.user} (ID: {bot.user.id})")



@bot.event
async def on_message(message: discord.Message):
    # 自分自身のメッセージは無視
    if message.author.bot:
        return


    is_mention = is_bot_mentioned(message)  # ← ユーザー＋ロールメンション両対応
    is_in_thread = isinstance(message.channel, discord.Thread)


    # ── パターン1：通常チャンネルへのメンション ──
    if is_mention and not is_in_thread:
        question = get_question_text(message)
        if not question:
            await message.reply("質問を入力してください。")
            return


        # スレッドタイトルを質問文から生成
        title = make_thread_title(question)
        thread = await message.create_thread(name=title)


        async with thread.typing():
            try:
                # conversationId = スレッドID（文字列）
                answer = await call_ask_api(question, str(thread.id))
            except asyncio.TimeoutError:
                await thread.send("時間がかかりすぎました。もう一度試してください。")
                return
            except Exception as e:
                print(f"[エラー] API呼び出し失敗: {e}")
                await thread.send("いのりには今ちょっと接続できません。しばらく待ってから、また聞いてください。")
                return


        await reply_in_chunks(thread, answer)
        return


    # ── パターン2：スレッド内のメッセージ（メンションなしでも反応） ──
    if is_in_thread:
        # ボットが作ったスレッドかどうかをowner_idで判定
        thread = message.channel
        if thread.owner_id != bot.user.id:
            return


        question = message.content.strip()
        if not question:
            return


        async with thread.typing():
            try:
                answer = await call_ask_api(question, str(thread.id))
            except asyncio.TimeoutError:
                await thread.send("時間がかかりすぎました。もう一度試してください。")
                return
            except Exception as e:
                print(f"[エラー] API呼び出し失敗: {e}")
                await thread.send("いのりには今ちょっと接続できません。しばらく待ってから、また聞いてください。")
                return


        await reply_in_chunks(thread, answer)
        return


    await bot.process_commands(message)



# ── 起動 ──────────────────────────────────────────────────────────


if __name__ == "__main__":
    # /health サーバーをバックグラウンドスレッドで起動
    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()
    print(f"[いのり] /health サーバー起動: port {PORT}")


    # Discordボット起動（メインスレッド）
    bot.run(DISCORD_TOKEN)