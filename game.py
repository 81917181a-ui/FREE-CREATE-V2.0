import asyncio
import discord
from discord.ext import commands

# 1. インテントの設定（メッセージ内容インテントが必須）
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    # スラッシュコマンドをDiscordに同期する
    await bot.tree.sync()
    print(f"Logged in as {bot.user} (Ready!)")
