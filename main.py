import os
import asyncio
import discord
from discord.ext import commands
from discord import app_commands
from flask import Flask
from threading import Thread

# ==========================================
# 🌐 Render ポート監視（Health Check）対策
# ==========================================
app = Flask('')

@app.route('/')
def home():
    return "Train Bot is alive!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

Thread(target=run_flask).start()

# ==========================================
# 🤖 Discord Bot 基本設定
# ==========================================
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# 運行設定を保持するセッションクラス
class TrainSession:
    def __init__(self, title: str, user_mention: str, event_link: str = "なし", image_url: str = None):
        self.title = title
        self.user_mention = user_mention
        self.event_link = event_link
        self.image_url = image_url
        self.railway = "未設定"
        self.section = "未設定"
        self.start_time = "23:00"
        self.end_time = "0:00"
        self.remarks = "未設定"

    def make_embed(self):
        embed = discord.Embed(
            title=f"🚉 {self.title} のダイヤを作成中",
            color=discord.Color.blue()
        )
        if self.image_url:
            embed.set_image(url=self.image_url)

        embed.description = (
            f"現在の設定:\n"
            f"・運行先鉄道: {self.railway}\n"
            f"・イベントリンク: {self.event_link}\n"
            f"・走行区間: {self.section}\n"
            f"・開始時間: {self.start_time}\n"
            f"・終了時間: {self.end_time}\n"
            f"・備 考: {self.remarks}\n"
            f"───────────────────"
        )
        embed.add_field(name="主催者", value=self.user_mention, inline=False)
        return embed

# ==========================================
# 🚉 ダイヤ作成スラッシュコマンド
# ==========================================
@bot.tree.command(name="create", description="路線ダイヤの作成ウィザードを開始します")
@app_commands.describe(チャンネル="ダイヤパネルを送信するチャンネル", イベントリンク="イベントのリンク(任意)")
async def create(interaction: discord.Interaction, チャンネル: discord.TextChannel, イベントリンク: str = "なし"):
    await interaction.response.send_message("🚀 ダイヤ作成パネルを送信しました。スレッドに移動してください。", ephemeral=True)

    session = TrainSession(title="ダイヤ作成", user_mention=interaction.user.mention, event_link=イベントリンク)
    
    panel_msg = await チャンネル.send(embed=session.make_embed())
    
    thread = await チャンネル.create_thread(
        name=f"ダイヤ作成-{interaction.user.display_name}",
        message=panel_msg,
        type=discord.ChannelType.public_thread
    )

    questions = [
        ("運行先鉄道", "運行先の鉄道名を入力してください（例: 尾羽旧電鉄）"),
        ("走行区間", "走行区間を入力してください（例: 尾羽急本線）"),
        ("開始時間", "開始時間を入力してください（例: 23:00）"),
        ("終了時間", "終了時間を入力してください（例: 0:00）"),
        ("備 考", "備考を入力してください（例: 終電運行 / なしなら「なし」）"),
        ("画像", "最後に、Embedの1番上に載せる画像を送信してください（画像がない場合は「なし」と送信してください）")
    ]

    def check(m):
        return m.author == interaction.user and m.channel == thread

    try:
        for attr, q_text in questions:
            q_msg = await thread.send(f"{interaction.user.mention} {q_text}")
            msg = await bot.wait_for('message', timeout=1800.0, check=check)
            
            try:
                await msg.delete()
                await q_msg.delete()
            except Exception:
                pass

            if attr == "運行先鉄道":
                session.railway = msg.content
            elif attr == "走行区間":
                session.section = msg.content
            elif attr == "開始時間":
                session.start_time = msg.content
            elif attr == "終了時間":
                session.end_time = msg.content
            elif attr == "備 考":
                session.remarks = msg.content
            elif attr == "画像" and msg.attachments:
                session.image_url = msg.attachments[0].url

            await panel_msg.edit(embed=session.make_embed())

        final_embed = discord.Embed(title="ダイヤ運行予定", color=discord.Color.green())
        if session.image_url:
            final_embed.set_image(url=session.image_url)

        final_embed.add_field(name="主催者", value=session.user_mention, inline=False)
        final_embed.add_field(name="運行先鉄道", value=session.railway, inline=False)
        final_embed.add_field(name="イベントリンク", value=session.event_link, inline=False)
        final_embed.add_field(name="走行区間", value=session.section, inline=False)
        final_embed.add_field(name="開始時刻", value=session.start_time, inline=True)
        final_embed.add_field(name="終了時刻", value=session.end_time, inline=True)
        if session.remarks != "未設定" and session.remarks != "なし":
            final_embed.add_field(name="備 考", value=session.remarks, inline=False)

        await panel_msg.edit(content=f"✅ ダイヤ運行予定が正式に投稿されました！ (メッセージID: `{panel_msg.id}`)", embed=final_embed)
        await thread.send("✅ すべての設定が完了しました！このスレッドを閉じます。")
        await asyncio.sleep(3)
        await thread.edit(archived=True, locked=True)

    except asyncio.TimeoutError:
        await thread.send("⏰ 30分間応答がなかったため、ダイヤ作成をキャンセルしました。")
        await asyncio.sleep(3)
        try:
            await panel_msg.delete()
            await thread.edit(archived=True, locked=True)
        except Exception:
            pass

# ==========================================
# イベントキャンセル機能 (!event cancel)
# ==========================================
@bot.group(name="event", invoke_without_command=True)
async def event_group(ctx):
    await ctx.send("使用方法: `!event cancel [キャンセル理由] [messageID]`")

@event_group.command(name="cancel")
async def event_cancel(ctx, reason: str = None, message_id: int = None):
    if not reason or not message_id:
        await ctx.send("❌ 使い方: `!event cancel [キャンセル理由] [messageID]`")
        return

    try:
        await ctx.message.delete()
    except Exception:
        pass

    target_message = None
    for channel in ctx.guild.text_channels:
        try:
            target_message = await channel.fetch_message(message_id)
            break
        except (discord.NotFound, discord.Forbidden):
            continue

    if not target_message:
        await ctx.send("❌ 指定されたIDのメッセージが見つかりませんでした。", delete_after=10)
        return

    try:
        if target_message.embeds:
            embed = target_message.embeds[0]
            embed.color = discord.Color.red()
            embed.title = "🚫 【ダイヤ運行中止】"
            embed.add_field(name="キャンセル理由", value=reason, inline=False)
            await target_message.edit(content="⚠️ **このダイヤ運行は中止されました。**", embed=embed)
            await ctx.send(f"✅ メッセージID `{message_id}` のイベントをキャンセルしました。", delete_after=10)
        else:
            await ctx.send("❌ 指定されたメッセージにはEmbedが含まれていません。", delete_after=10)
    except Exception as e:
        await ctx.send(f"❌ キャンセル処理に失敗しました: {e}", delete_after=10)

# ==========================================
# ボット起動時イベント
# ==========================================
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Failed to sync commands: {e}")

bot.run(TOKEN)
