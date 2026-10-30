import asyncio
import io
import json
import random  # おみくじ等で使用（元コードでは import が抜けていたため追加）
import secrets
from datetime import datetime, timedelta
import os
import time
from threading import Thread
import psutil
import requests  # セルフ・ピン / Gemini REST 呼び出し用

import uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

import discord
from discord import app_commands
from discord.ext import commands

# ==========================================
# 🔑 環境変数（Render に登録するのはこの2つだけ）
#   DISCORD_BOT_TOKEN / GEMINI_API_KEY
#   ※ PORT・RENDER_EXTERNAL_URL は Render が自動設定する
# ==========================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-flash-latest"  # コード側で固定（環境変数を増やさない）
BASE_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://free-create-v2-0-ubpy.onrender.com").rstrip("/")
PORT = int(os.environ.get("PORT", 8080))


def gemini_generate(prompt: str, system: str = "", json_mode: bool = False, timeout: int = 25) -> str:
  """Gemini REST を同期で呼ぶ（asyncio.to_thread 経由で使う）。"""
  if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY が未設定です")
  url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
  body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
  if system:
    body["systemInstruction"] = {"parts": [{"text": system}]}
  if json_mode:
    body["generationConfig"] = {"responseMimeType": "application/json", "temperature": 0}
  r = requests.post(url, json=body, headers={"x-goog-api-key": GEMINI_API_KEY}, timeout=timeout)
  r.raise_for_status()
  data = r.json()
  return data["candidates"][0]["content"]["parts"][0]["text"]

# --- 👇 ここを追加・確認する ---
intents = discord.Intents.default()
intents.members = (
    True  # サーバーメンバーの取得に必須
)
intents.message_content = True  # メッセージ内容の取得に必須
intents.dm_messages = True  # DMの送受信に必須

# 進行中の人狼ゲームを管理する辞書
active_games = {}  # {channel_id: WolfGameSession}

# ボットの初期化（intents=intents を必ず渡してください）
bot = commands.Bot(command_prefix="!", intents=intents)
# ------------------------------


# game.pyを読み込む処理（まだ書いていなければ追加）
@bot.event
async def on_ready():
  await bot.load_extension("game")  # game.pyを読み込む
  await bot.tree.sync()
  print(f"Logged in as {bot.user}")


# ==========================================
# 🌐 Render ポート監視（Health Check）＆ セルフ・ピン対策
# ==========================================
#  Flask → FastAPI に置き換え（Bot と同じイベントループで動かし、Bot を直接参照できる）
api = FastAPI(docs_url=None, redoc_url=None)


@api.get('/', response_class=PlainTextResponse)
def home():
  return 'Bot is alive!'


# 自身を叩く用のヘルスチェックエンドポイント
@api.get('/health', response_class=PlainTextResponse)
def health():
  return 'OK'


# 10分ごとに自分自身を叩いてスリープを防ぐ関数
def self_ping():
  url = f'{BASE_URL}/health'

  while True:
    try:
      # 10分（600秒）待機してからアクセス
      time.sleep(600)
      response = requests.get(url)
      print(f'Self-ping sent! Status: {response.status_code}')
    except Exception as e:
      print(f'Self-ping failed: {e}')


# セルフ・ピンはバックグラウンドスレッドで起動（Webサーバーは末尾で Bot と同時起動）
Thread(target=self_ping, daemon=True).start()

# bot.run("あなたのトークン") は一番下に記述
# ==========================================
# 🤖 Discord Bot 基本設定
# ==========================================
TOKEN = os.getenv("DISCORD_BOT_TOKEN") or os.getenv("DISCORD_TOKEN")  # 旧名も後方互換で許容
OWNER_ID = 1301944996261400656

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
bot = commands.Bot(command_prefix="!", intents=intents)

START_TIME = datetime.now()
DAILY_ERROR_COUNT = 0
ERROR_LOGS = []

# 管理用データストレージ
blacklisted_servers = {}  # {server_id: server_name}
banned_users = {}         # {user_id: user_name}
admin_users = {OWNER_ID: "Owner"} # {user_id: user_name}

# 入力整形ユーティリティ
def normalize_input(text: str) -> list[str]:
    if not text:
        return []
    formatted = text.replace("，", ",").replace("、", ",")
    return [item.strip() for item in formatted.split(",") if item.strip()]

# データ管理クラス
class TrainData:
    def __init__(self, line_name: str, author_id: int):
        self.line_name = line_name
        self.author_id = author_id
        self.stations = []
        self.durations = []
        self.train_types = {}          # {種別名: [停車駅]}
        self.start_stations = []       # 始発駅リスト（複数対応）
        self.end_station = ""          # 終了駅（任意）
        self.start_time = "06:00"      # 開始時間
        self.round_trips = 1           # 往復数
        self.interval_mins = 3         # 運行間隔
        self.turnaround_mins = 3       # 折り返し時間（分・最大10分制御）
        self.quad_tracks = ""
        self.passing_stations = []
        self.output_target = "thread"  # "thread", "dm", "channel"

user_sessions = {}  # {message_id: TrainData}

# ==========================================
# 📊 Embed リアルタイム更新用関数
# ==========================================
def generate_status_embed(session: TrainData) -> discord.Embed:
    stations_text = ", ".join(session.stations) if session.stations else "未設定"
    types_text = ", ".join(session.train_types.keys()) if session.train_types else "未設定"
    durations_text = ", ".join(map(str, session.durations)) + "秒" if session.durations else "未設定"
    
    start_st_text = ", ".join(session.start_stations) if session.start_stations else (session.stations[0] if session.stations else "未設定")
    end_st_text = session.end_station if session.end_station else "未設定 (自動最遠駅)"
    start_time_text = session.start_time if session.start_time else "未設定"

    passing_text = ", ".join(session.passing_stations) if session.passing_stations else "なし"
    quad_text = session.quad_tracks if session.quad_tracks else "なし"
    
    target_map = {
        "thread": "🧵 スレッドに送信",
        "dm": "📩 DMに送信",
        "channel": "💬 現在のチャンネルに直接送信"
    }
    target_disp = target_map.get(session.output_target, "🧵 スレッドに送信")

    turnaround_disp = min(max(1, session.turnaround_mins), 10)

    embed = discord.Embed(
        title=f"🚉 {session.line_name} のダイヤを作成中",
        description=(
            "**現在の設定:**\n"
            f"・駅名: {stations_text}\n"
            f"・種別: {types_text}\n"
            f"・区間所要時間: {durations_text}\n"
            f"・始発駅(複数可): {start_st_text}\n"
            f"・終了駅(任意): {end_st_text}\n"
            f"・開始時間: {start_time_text}\n"
            f"・運行往復数: {session.round_trips} 往復（折り返し {turnaround_disp} 分 ※10分以内制限適用）\n"
            f"・待避可能駅: {passing_text}\n"
            f"・複々線区間: {quad_text}\n"
            f"・**出力先設定**: `{target_disp}`\n\n"
            "───────────────────\n"
            "👇 **行いたい操作を選択してください**"
        ),
        color=0x3498db
    )
    return embed

async def update_message_embed(interaction: discord.Interaction, session: TrainData):
    try:
        new_embed = generate_status_embed(session)
        await interaction.message.edit(embed=new_embed)
    except Exception as e:
        print(f"Embed update failed: {e}")

# ==========================================
# ⚙️ モーダル（入力フォーム）類
# ==========================================

class StationModal(discord.ui.Modal, title="🚉 駅名を登録・編集"):
    stations_input = discord.ui.TextInput(
        label="駅名（カンマ区切り・最大50駅）",
        style=discord.TextStyle.paragraph,
        placeholder="例: 東京, 神田, 御茶ノ水, 四ツ谷, 新宿",
        required=True
    )

    def __init__(self, session_data):
        super().__init__()
        self.session_data = session_data

    async def on_submit(self, interaction: discord.Interaction):
        stations = normalize_input(self.stations_input.value)
        if len(stations) > 50:
            await interaction.response.send_message("⚠️ 駅数は最大50駅までです！", ephemeral=True)
            return
        self.session_data.stations = stations
        await update_message_embed(interaction, self.session_data)
        await interaction.response.send_message("✅ 駅名を登録・更新しました！", ephemeral=True)

class TimeModal(discord.ui.Modal, title="⏱️ 時間・運行・過密設定"):
    durations_input = discord.ui.TextInput(
        label="各区間の基準所要時間（秒）",
        placeholder="例: 180, 120, 240, 150",
        required=True
    )
    start_stations_input = discord.ui.TextInput(
        label="始発駅（カンマ区切りで複数指定可能）",
        placeholder="例: 東京, 御茶ノ水",
        required=False
    )
    end_station_input = discord.ui.TextInput(
        label="終了駅（任意・空欄で終点駅）",
        placeholder="例: 新宿",
        required=False
    )
    start_time_input = discord.ui.TextInput(
        label="開始時間（1番列車の発車時刻）",
        placeholder="例: 06:00",
        default="06:00",
        required=True
    )
    trips_input = discord.ui.TextInput(
        label="運行往復数 と 折返し時間(分/10分以内)",
        placeholder="例: 3, 3 (3往復、折り返し3分)",
        default="2, 3",
        required=True
    )

    def __init__(self, session_data):
        super().__init__()
        self.session_data = session_data

    async def on_submit(self, interaction: discord.Interaction):
        durations = [int(d) for d in normalize_input(self.durations_input.value) if d.isdigit()]
        self.session_data.durations = durations
        
        if self.start_stations_input.value.strip():
            self.session_data.start_stations = normalize_input(self.start_stations_input.value)
        else:
            self.session_data.start_stations = []

        if self.end_station_input.value.strip():
            self.session_data.end_station = self.end_station_input.value.strip()
        else:
            self.session_data.end_station = ""
        
        self.session_data.start_time = self.start_time_input.value.strip()

        trip_parts = [int(p) for p in normalize_input(self.trips_input.value) if p.isdigit()]
        if len(trip_parts) >= 1:
            self.session_data.round_trips = trip_parts[0]
        if len(trip_parts) >= 2:
            self.session_data.turnaround_mins = min(trip_parts[1], 10)

        await update_message_embed(interaction, self.session_data)
        await interaction.response.send_message("✅ 時間・過密運行設定を更新しました！（折返しは最大10分以内に制限されます）", ephemeral=True)

class TypeAddModal(discord.ui.Modal, title="➕ 列車種別を追加"):
    type_name = discord.ui.TextInput(label="種別名", placeholder="例: 急行", required=True)
    stops = discord.ui.TextInput(label="停車駅", placeholder="例: 全駅停車 または 東京, 御茶ノ水, 新宿", required=True)

    def __init__(self, session_data):
        super().__init__()
        self.session_data = session_data

    async def on_submit(self, interaction: discord.Interaction):
        if len(self.session_data.train_types) >= 30:
            await interaction.response.send_message("⚠️ 種別は最大30個までです！", ephemeral=True)
            return
        t_name = self.type_name.value.strip()
        stop_list = normalize_input(self.stops.value)
        self.session_data.train_types[t_name] = stop_list
        await update_message_embed(interaction, self.session_data)
        await interaction.response.send_message(f"✅ 種別『{t_name}』を追加しました！", ephemeral=True)

class QuadTrackModal(discord.ui.Modal, title="🛤️ 複々線区間を設定"):
    quad_input = discord.ui.TextInput(label="複々線区間", placeholder="例: 東京〜御茶ノ水", required=True)

    def __init__(self, session_data):
        super().__init__()
        self.session_data = session_data

    async def on_submit(self, interaction: discord.Interaction):
        self.session_data.quad_tracks = self.quad_input.value.strip()
        await update_message_embed(interaction, self.session_data)
        await interaction.response.send_message("✅ 複々線区間を設定しました！", ephemeral=True)

class PassingModal(discord.ui.Modal, title="🔀 待避可能駅を設定"):
    passing_input = discord.ui.TextInput(label="待避可能駅", placeholder="例: 御茶ノ水, 四ツ谷", required=True)

    def __init__(self, session_data):
        super().__init__()
        self.session_data = session_data

    async def on_submit(self, interaction: discord.Interaction):
        self.session_data.passing_stations = normalize_input(self.passing_input.value)
        await update_message_embed(interaction, self.session_data)
        await interaction.response.send_message("✅ 待避可能駅を設定しました！", ephemeral=True)

# ==========================================
# 📩 出力先選択 View（3つの出力先に対応）
# ==========================================

class OutputTargetView(discord.ui.View):
    def __init__(self, session_data):
        super().__init__(timeout=60)
        self.session_data = session_data

    @discord.ui.button(label="🧵 スレッドを作成して出力", style=discord.ButtonStyle.primary)
    async def select_thread(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.session_data.output_target = "thread"
        await update_message_embed(interaction, self.session_data)
        await interaction.response.send_message("✅ ダイヤの出力先を **スレッド** に設定しました！", ephemeral=True)

    @discord.ui.button(label="📩 DM (ダイレクトメッセージ) に出力", style=discord.ButtonStyle.success)
    async def select_dm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.session_data.output_target = "dm"
        await update_message_embed(interaction, self.session_data)
        await interaction.response.send_message("✅ ダイヤの出力先を **DM** に設定しました！", ephemeral=True)

    @discord.ui.button(label="💬 現在のチャンネルに送信", style=discord.ButtonStyle.secondary)
    async def select_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.session_data.output_target = "channel"
        await update_message_embed(interaction, self.session_data)
        await interaction.response.send_message("✅ ダイヤの出力先を **現在のチャンネル** に設定しました！", ephemeral=True)

# ==========================================
# 🗑️ ダイヤ用 削除ドロップダウン
# ==========================================

class DeleteSelect(discord.ui.Select):
    def __init__(self, session_data):
        self.session_data = session_data
        options = [
            discord.SelectOption(label="🚉 駅名設定を削除", value="stations"),
            discord.SelectOption(label="⏱️ 時間・運行設定を削除", value="times"),
            discord.SelectOption(label="🛤️ 複々線区間を削除", value="quad"),
            discord.SelectOption(label="🎨 列車種別を選択して削除", value="types"),
            discord.SelectOption(label="🔀 待避可能駅を削除", value="passing"),
        ]
        super().__init__(placeholder="🗑️ 削除する項目を選択してください...", options=options)

    async def callback(self, interaction: discord.Interaction):
        val = self.values[0]
        if val == "stations":
            self.session_data.stations = []
            await update_message_embed(interaction, self.session_data)
            await interaction.response.send_message("🗑️ 駅名設定を削除しました。", ephemeral=True)
        elif val == "times":
            self.session_data.durations = []
            self.session_data.start_time = "06:00"
            self.session_data.round_trips = 1
            self.session_data.end_station = ""
            self.session_data.start_stations = []
            await update_message_embed(interaction, self.session_data)
            await interaction.response.send_message("🗑️ 時間・運行設定を削除しました。", ephemeral=True)
        elif val == "quad":
            self.session_data.quad_tracks = ""
            await update_message_embed(interaction, self.session_data)
            await interaction.response.send_message("🗑️ 複々線区間設定を削除しました。", ephemeral=True)
        elif val == "passing":
            self.session_data.passing_stations = []
            await update_message_embed(interaction, self.session_data)
            await interaction.response.send_message("🗑️ 待避可能駅設定を削除しました。", ephemeral=True)
        elif val == "types":
            if not self.session_data.train_types:
                await interaction.response.send_message("⚠️ 削除可能な種別がありません。", ephemeral=True)
                return
            view = discord.ui.View()
            view.add_item(TypeDeleteSelect(self.session_data))
            await interaction.response.send_message("🗑️ 削除する種別を選択してください:", view=view, ephemeral=True)

class TypeDeleteSelect(discord.ui.Select):
    def __init__(self, session_data):
        self.session_data = session_data
        options = [discord.SelectOption(label=t, value=t) for t in session_data.train_types.keys()]
        super().__init__(placeholder="🎨 削除する種別を選択してください...", options=options)

    async def callback(self, interaction: discord.Interaction):
        target = self.values[0]
        if target in self.session_data.train_types:
            del self.session_data.train_types[target]
            await update_message_embed(interaction, self.session_data)
            await interaction.response.send_message(f"🗑️ 種別『{target}』を削除しました。", ephemeral=True)

# ==========================================
# ⚙️ メイン操作パネル View
# ==========================================

class MainControlSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="🚉 駅名を登録・編集", value="stations"),
            discord.SelectOption(label="⏱️ 時間・運行設定", value="times"),
            discord.SelectOption(label="🛤️ 複々線区間を設定", value="quad"),
            discord.SelectOption(label="➕ 列車種別を追加", value="add_type"),
            discord.SelectOption(label="🔀 待避可能駅を設定", value="passing"),
            discord.SelectOption(label="🗑️ 設定を削除", value="delete"),
            discord.SelectOption(label="🔄 設定を全リセット", value="reset"),
            discord.SelectOption(label="📩 出力先を設定 (スレッド/DM/チャンネル)", value="output_target"),
            discord.SelectOption(label="🎨 この設定でダイヤを作成！", value="create"),
        ]
        super().__init__(placeholder="⚙️ 操作メニューを選択してください...", options=options)

    async def callback(self, interaction: discord.Interaction):
        session = user_sessions.get(interaction.message.id)
        if not session:
            await interaction.response.send_message("⚠️ セッションが存在しないか、タイムアウトしました。", ephemeral=True)
            return

        if interaction.user.id != session.author_id:
            await interaction.response.send_message("⚠️ この操作はコマンドを実行した本人しか使用できません！", ephemeral=True)
            return

        val = self.values[0]

        if val == "stations":
            await interaction.response.send_modal(StationModal(session))
        elif val == "times":
            await interaction.response.send_modal(TimeModal(session))
        elif val == "quad":
            await interaction.response.send_modal(QuadTrackModal(session))
        elif val == "add_type":
            await interaction.response.send_modal(TypeAddModal(session))
        elif val == "passing":
            await interaction.response.send_modal(PassingModal(session))
        elif val == "delete":
            view = discord.ui.View()
            view.add_item(DeleteSelect(session))
            await interaction.response.send_message("🗑️ どの情報を削除しますか？", view=view, ephemeral=True)
        elif val == "reset":
            session.stations = []
            session.durations = []
            session.train_types = {}
            session.start_stations = []
            session.end_station = ""
            session.start_time = "06:00"
            session.round_trips = 1
            session.turnaround_mins = 3
            session.quad_tracks = ""
            session.passing_stations = []
            session.output_target = "thread"
            await update_message_embed(interaction, session)
            await interaction.response.send_message("🔄 すべての設定をリセットしました！", ephemeral=True)
        elif val == "output_target":
            view = OutputTargetView(session)
            await interaction.response.send_message("📩 ダイヤの出力先を選択してください:", view=view, ephemeral=True)
        elif val == "create":
            await interaction.response.defer(ephemeral=True)
            result_msgs = generate_safe_timetable(session)

            if len(result_msgs) == 1 and result_msgs[0].startswith("⚠️"):
                await interaction.followup.send(result_msgs[0], ephemeral=True)
                return

            # --- 1. DM 送信（失敗時はチャンネルへフォールバック） ---
            if session.output_target == "dm":
                try:
                    for msg in result_msgs:
                        await interaction.user.send(msg)
                    await interaction.followup.send("✅ DMにダイヤを出力しました！確認してください。", ephemeral=True)
                except (discord.Forbidden, Exception):
                    try:
                        for msg in result_msgs:
                            await interaction.channel.send(msg)
                        await interaction.followup.send("⚠️ DMが受信拒否設定になっているため、このチャンネルに直接出力しました！", ephemeral=True)
                    except Exception as send_err:
                        await interaction.followup.send(f"⚠️ ダイヤの出力に失敗しました。Botのメッセージ送信権限を確認してください: {send_err}", ephemeral=True)

            # --- 2. スレッド送信（失敗時はチャンネルへフォールバック） ---
            elif session.output_target == "thread":
                try:
                    thread = await interaction.channel.create_thread(
                        name=f"🚉【{session.line_name}】ダイヤ作成結果",
                        auto_archive_duration=60
                    )
                    for msg in result_msgs:
                        await thread.send(msg)
                    await interaction.followup.send(f"✅ スレッド {thread.mention} を作成し、ダイヤを出力しました！", ephemeral=True)
                except (discord.Forbidden, Exception):
                    try:
                        for msg in result_msgs:
                            await interaction.channel.send(msg)
                        await interaction.followup.send("⚠️ スレッド作成権限がないため、このチャンネルに直接出力しました！", ephemeral=True)
                    except Exception as send_err:
                        await interaction.followup.send(f"⚠️ ダイヤの出力に失敗しました。Botのメッセージ送信権限を確認してください: {send_err}", ephemeral=True)

            # --- 3. 現在のチャンネルに直接送信 ---
            else:
                try:
                    for msg in result_msgs:
                        await interaction.channel.send(msg)
                    await interaction.followup.send("✅ このチャンネルにダイヤを出力しました！", ephemeral=True)
                except Exception as send_err:
                    await interaction.followup.send(f"⚠️ ダイヤの出力に失敗しました: {send_err}", ephemeral=True)

class MainControlView(discord.ui.View):
    def __init__(self, timeout=1800):
        super().__init__(timeout=timeout)
        self.add_item(MainControlSelect())

# ==========================================
# 📄 発車10分以内制御 & 2000文字分割対応 ダイヤ計算
# ==========================================

def generate_safe_timetable(session: TrainData) -> list[str]:
    if not session.stations or not session.durations:
        return ["⚠️ ダイヤを作成するには「駅名」と「時間・運行設定」の登録が必要です！"]

    start_sts = session.start_stations if session.start_stations else [session.stations[0]]
    end_st = session.end_station if session.end_station in session.stations else session.stations[-1]

    try:
        base_time = datetime.strptime(session.start_time, "%H:%M")
    except ValueError:
        base_time = datetime.strptime("06:00", "%H:%M")

    type_names = list(session.train_types.keys()) if session.train_types else ["普通"]
    safe_turnaround = min(max(1, session.turnaround_mins), 10)

    messages = []
    current_msg = f"**【{session.line_name} 高密度過密ダイヤ (全列車10分以内発車制御)】**\n"
    current_msg += f"⏱️ **発車間隔制御：到着・折り返し後【最大10分以内】に即時発車**\n\n"

    for st_idx, start_st in enumerate(start_sts):
        if start_st not in session.stations:
            continue
        
        s_idx = session.stations.index(start_st)
        e_idx = session.stations.index(end_st)

        if s_idx > e_idx:
            s_idx, e_idx = e_idx, s_idx

        t_type = type_names[st_idx % len(type_names)]
        curr_time = base_time + timedelta(minutes=min(st_idx * 2, 10))

        section_text = f"===============================\n"
        section_text += f"🚩 **【系統 {st_idx + 1}】始発: {start_st} ➔ 終了: {end_st} ({session.round_trips}往復)**\n"
        section_text += f"===============================\n"

        for trip in range(1, session.round_trips + 1):
            # --- 往路 ---
            section_text += f"🔹 **[{trip}往路] 種別: {t_type}**（{start_st} {curr_time.strftime('%H:%M')}発）\n"
            for i in range(s_idx, e_idx + 1):
                st_name = session.stations[i]
                if i == s_idx:
                    section_text += f"  ・{st_name}：{curr_time.strftime('%H:%M')} 発 (始発)\n"
                else:
                    dur_sec = session.durations[i - 1] if (i - 1) < len(session.durations) else 180
                    curr_time += timedelta(seconds=dur_sec)
                    arr_str = curr_time.strftime('%H:%M')
                    stops = session.train_types.get(t_type, [])
                    is_stop = ("全駅停車" in stops) or (st_name in stops) or not stops

                    if i == e_idx:
                        section_text += f"  ・{st_name}：{arr_str} 着 (終着)\n"
                    elif is_stop:
                        curr_time += timedelta(seconds=30)
                        section_text += f"  ・{st_name}：{arr_str}着 / {curr_time.strftime('%H:%M')}発\n"
                    else:
                        section_text += f"  ・{st_name}：通過\n"

            curr_time += timedelta(minutes=safe_turnaround)

            # --- 復路 ---
            section_text += f"🔸 **[{trip}復路] 種別: {t_type}**（{end_st} {curr_time.strftime('%H:%M')}発 折返し）\n"
            for i in range(e_idx, s_idx - 1, -1):
                st_name = session.stations[i]
                if i == e_idx:
                    section_text += f"  ・{st_name}：{curr_time.strftime('%H:%M')} 発 (折返始発)\n"
                else:
                    dur_sec = session.durations[i] if i < len(session.durations) else 180
                    curr_time += timedelta(seconds=dur_sec)
                    arr_str = curr_time.strftime('%H:%M')
                    stops = session.train_types.get(t_type, [])
                    is_stop = ("全駅停車" in stops) or (st_name in stops) or not stops

                    if i == s_idx:
                        section_text += f"  ・{st_name}：{arr_str} 着 (到着)\n"
                    elif is_stop:
                        curr_time += timedelta(seconds=30)
                        section_text += f"  ・{st_name}：{arr_str}着 / {curr_time.strftime('%H:%M')}発\n"
                    else:
                        section_text += f"  ・{st_name}：通過\n"

            curr_time += timedelta(minutes=safe_turnaround)
            section_text += "\n"

            # 1800文字超過で自動分割
            if len(current_msg) + len(section_text) > 1800:
                messages.append(current_msg)
                current_msg = section_text
                section_text = ""

        current_msg += section_text

    if current_msg.strip():
        messages.append(current_msg)

    return messages
# ==========================================
# 🎮 ミニゲームコーナー（おみくじ・じゃんけん・ロシアンルーレット・スロット）
# ==========================================

# 1. おみくじ (/omikuji)
@bot.tree.command(name="omikuji", description="今日の運勢を占います！")
async def omikuji(interaction: discord.Interaction):
    results = ["大吉 🌟", "中吉 ✨", "小吉 🌙", "吉 🌱", "凶 💧", "大凶 💀"]
    result = random.choice(results)
    
    embed = discord.Embed(
        title="⛩️ おみくじ",
        description=f"{interaction.user.mention} さんの今日の運勢は… **{result}** です！",
        color=discord.Color.gold()
    )
    await interaction.response.send_message(embed=embed)

# 2. じゃんけん (/janken)
@bot.tree.command(name="janken", description="ボットとじゃんけん勝負をします！")
async def janken(interaction: discord.Interaction, 選択: str):
    choices = ["グー", "チョキ", "パー"]
    if 選択 not in choices:
        await interaction.response.send_message("「グー」「チョキ」「パー」の中から選んでね！", ephemeral=True)
        return
    
    bot_choice = random.choice(choices)
    
    if 選択 == bot_choice:
        outcome = "あいこです！ 🤝"
        color = discord.Color.light_gray()
    elif (
        (選択 == "グー" and bot_choice == "チョキ") or
        (選択 == "チョキ" and bot_choice == "パー") or
        (選択 == "パー" and bot_choice == "グー")
    ):
        outcome = "あなたの勝ちです！ 🎉"
        color = discord.Color.green()
    else:
        outcome = "あなたの負けです… 😢"
        color = discord.Color.red()
        
    embed = discord.Embed(
        title="✊ ✋ ✌️ じゃんけん勝負",
        description=f"あなた: **{選択}**\nボット: **{bot_choice}**\n\n**{outcome}**",
        color=color
    )
    await interaction.response.send_message(embed=embed)

# 3. ボタン式ロシアンルーレット (/russian)
class RussianButtonView(discord.ui.View):
    def __init__(self, user):
        super().__init__(timeout=60.0)
        self.user = user
        self.loser_index = random.randint(0, 3)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user != self.user:
            await interaction.response.send_message("あなたが始めたゲームではありません！", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="ボタン①", style=discord.ButtonStyle.secondary, custom_id="btn_0")
    async def btn_0(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.check_result(interaction, 0)

    @discord.ui.button(label="ボタン②", style=discord.ButtonStyle.secondary, custom_id="btn_1")
    async def btn_1(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.check_result(interaction, 1)

    @discord.ui.button(label="ボタン③", style=discord.ButtonStyle.secondary, custom_id="btn_2")
    async def btn_2(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.check_result(interaction, 2)

    @discord.ui.button(label="ボタン④", style=discord.ButtonStyle.secondary, custom_id="btn_3")
    async def btn_3(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.check_result(interaction, 3)

    async def check_result(self, interaction, chosen_index):
        for child in self.children:
            child.disabled = True

        if chosen_index == self.loser_index:
            embed = discord.Embed(
                title="💥 ロシアンルーレット",
                description=f"{self.user.mention} さん、選んだボタンは… **ハズレ（ドカーン！）** 💥",
                color=discord.Color.red()
            )
        else:
            embed = discord.Embed(
                title="✨ ロシアンルーレット",
                description=f"{self.user.mention} さん、選んだボタンは… **セーフ！** 😌",
                color=discord.Color.green()
            )

        await interaction.response.edit_message(embed=embed, view=self)
        self.stop()

@bot.tree.command(name="russian", description="4つのボタンから1つ選ぶロシアンルーレット！")
async def russian(interaction: discord.Interaction):
    view = RussianButtonView(interaction.user)
    embed = discord.Embed(
        title="🔫 ロシアンルーレット",
        description=f"{interaction.user.mention} さん、4つのボタンのうち1つがハズレです。どれか一つを押してください！",
        color=discord.Color.dark_red()
    )
    await interaction.response.send_message(embed=embed, view=view)

import asyncio

# ==========================================
# 4. スロットゲーム (/slot) アニメーション付き
# ==========================================
@bot.tree.command(name="slot", description="スロットを回して運試しをしよう！")
async def slot(interaction: discord.Interaction):
    symbols = ["🍒", "🇯🇵", "🔔", "⭐", "💎", "📝"]
    
    # 最初に「スロットを回しています…」と送信する
    embed = discord.Embed(
        title="🎰 スロットマシン",
        description="【 🔄 | 🔄 | 🔄 】\n\n**スロット回転中……**",
        color=discord.Color.blurple()
    )
    await interaction.response.send_message(embed=embed)
    
    # パタパタと絵文字を変える演出（3回繰り返す）
    for _ in range(3):
        await asyncio.sleep(0.6) # 0.6秒ごとに切り替え
        temp_result = [random.choice(symbols) for _ in range(3)]
        temp_embed = discord.Embed(
            title="🎰 スロットマシン",
            description=f"【 {temp_result[0]} | {temp_result[1]} | {temp_result[2]} 】\n\n**回転中…… 🔄**",
            color=discord.Color.blurple()
        )
        await interaction.edit_original_response(embed=temp_embed)
    
    # 最後に最終結果を決定
    await asyncio.sleep(0.6)
    result = [random.choice(symbols) for _ in range(3)]
    
    if result[0] == result[1] == result[2]:
        outcome = "大当たり！お見事です！ 🎉✨"
        color = discord.Color.gold()
    elif result[0] == result[1] or result[1] == result[2] or result[0] == result[2]:
        outcome = "惜しい！2つ揃いました！ 👍"
        color = discord.Color.green()
    else:
        outcome = "ハズレ…また挑戦してね！ 😢"
        color = discord.Color.red()
        
    final_embed = discord.Embed(
        title="🎰 スロットマシン",
        description=f"【 {result[0]} | {result[1]} | {result[2]} 】\n\n**{outcome}**",
        color=color
    )
    await interaction.edit_original_response(embed=final_embed)
# ==========================================
# 🔍 検索コーナー（AI検索・wiki検索）
# ==========================================

# 5. AI検索 (/search)  ※ 環境変数を2つに絞るため OpenAI → Gemini に変更
@bot.tree.command(name="search", description="AI(Gemini)を使って質問や検索をします")
async def search(interaction: discord.Interaction, キーワード: str):
    await interaction.response.defer()
    
    try:
        answer_text = await asyncio.to_thread(
            gemini_generate,
            キーワード,
            "あなたは優秀なアシスタントです。簡潔でわかりやすく答えてください。",
        )
        
        if len(answer_text) > 1900:
            answer_text = answer_text[:1900] + "...\n（文字数オーバーのため省略しました）"

        embed = discord.Embed(
            title="🔍 AI検索結果 (Gemini)",
            description=f"**検索ワード:** {キーワード}\n\n{answer_text}",
            color=discord.Color.green()
        )
        await interaction.followup.send(embed=embed)
        
    except Exception as e:
        embed = discord.Embed(
            title="🔍 AI検索エラー",
            description=f"エラーが発生しました: `{e}`\n(GEMINI_API_KEY を確認してください)",
            color=discord.Color.red()
        )
        await interaction.followup.send(embed=embed)
# ==========================================
# 👑 管理者パネル & リスト表示
# ==========================================

def generate_admin_embed() -> discord.Embed:
    embed = discord.Embed(
        title="👑 BOT 管理用コントロールパネル",
        description="下のボタンを押して各種操作を行ってください。",
        color=0xe74c3c
    )
    
    # ⛔ ブラックリスト サーバー一覧
    if blacklisted_servers:
        bl_text = "\n".join([f"・{name} (`ID: {sid}`)" for sid, name in blacklisted_servers.items()])
    else:
        bl_text = "なし"
    embed.add_field(name="⛔ Blacklist Servers", value=bl_text, inline=False)

    # 🚫 BOTBAN ユーザー一覧
    if banned_users:
        ban_text = "\n".join([f"・{name} (`ID: {uid}`)" for uid, name in banned_users.items()])
    else:
        ban_text = "なし"
    embed.add_field(name="🚫 Banned Users", value=ban_text, inline=False)

    # 👑 管理者 ユーザー一覧
    if admin_users:
        admin_text = "\n".join([f"・{name} (`ID: {uid}`)" for uid, name in admin_users.items()])
    else:
        admin_text = "なし"
    embed.add_field(name="👑 Admins", value=admin_text, inline=False)

    return embed

class AdminModal(discord.ui.Modal):
    def __init__(self, action_type: str, title_text: str, label_text: str):
        super().__init__(title=title_text)
        self.action_type = action_type
        self.input_field = discord.ui.TextInput(
            label=label_text,
            placeholder="IDを入力してください",
            required=True
        )
        self.add_item(self.input_field)

    async def on_submit(self, interaction: discord.Interaction):
        target_id_str = self.input_field.value.strip()
        if not target_id_str.isdigit():
            await interaction.response.send_message("⚠️ IDは半角数字で入力してください！", ephemeral=True)
            return
        
        target_id = int(target_id_str)

        if self.action_type == "blacklist":
            guild = bot.get_guild(target_id)
            g_name = guild.name if guild else "Unknown Server"
            blacklisted_servers[target_id] = g_name
            if guild:
                await guild.leave()
            msg = f"⛔ サーバー 『{g_name}』 (ID: {target_id}) をブラックリストに追加・脱出しました。"

        elif self.action_type == "unblacklist":
            blacklisted_servers.pop(target_id, None)
            msg = f"🟢 サーバー (ID: {target_id}) のブラックリストを解除しました。"

        elif self.action_type == "botban":
            try:
                user = await bot.fetch_user(target_id)
                u_name = user.name
            except:
                u_name = "Unknown User"
            banned_users[target_id] = u_name
            msg = f"🚫 ユーザー 『{u_name}』 (ID: {target_id}) をBOTBANしました。"

        elif self.action_type == "unbotban":
            banned_users.pop(target_id, None)
            msg = f"⭕ ユーザー (ID: {target_id}) のBOTBANを解除しました。"

        elif self.action_type == "add_admin":
            try:
                user = await bot.fetch_user(target_id)
                u_name = user.name
            except:
                u_name = "Unknown User"
            admin_users[target_id] = u_name
            msg = f"👑 ユーザー 『{u_name}』 (ID: {target_id}) を管理者に設定しました。"

        elif self.action_type == "fire":
            if target_id == OWNER_ID:
                await interaction.response.send_message("⚠️ Bot作成者（Owner）の権限は解除できません！", ephemeral=True)
                return
            admin_users.pop(target_id, None)
            msg = f"🔥 ユーザー (ID: {target_id}) の管理者権限を解除しました。"

        # 管理者パネルの表示を最新情報に自動更新
        if interaction.message:
            try:
                await interaction.message.edit(embed=generate_admin_embed())
            except Exception as e:
                print(f"Panel embed update failed: {e}")

        await interaction.response.send_message(msg, ephemeral=True)

class AdminPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="BLACKLIST SERVER", style=discord.ButtonStyle.danger, custom_id="btn_bl")
    async def btn_bl(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AdminModal("blacklist", "BLACKLIST SERVER", "どのサーバーをブラックリストに入れますか？"))

    @discord.ui.button(label="UNBLACKLIST SERVER", style=discord.ButtonStyle.success, custom_id="btn_unbl")
    async def btn_unbl(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AdminModal("unblacklist", "UNBLACKLIST SERVER", "どのサーバーをブラックリストから解除しますか？"))

    @discord.ui.button(label="BOTBAN", style=discord.ButtonStyle.danger, custom_id="btn_ban")
    async def btn_ban(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AdminModal("botban", "BOTBAN", "どのユーザーのBOT機能を使えなくしますか？"))

    @discord.ui.button(label="UNBOTBAN", style=discord.ButtonStyle.success, custom_id="btn_unban")
    async def btn_unban(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AdminModal("unbotban", "UNBOTBAN", "どのユーザーのBOTBANを解除しますか？"))

    @discord.ui.button(label="ADD ADMIN", style=discord.ButtonStyle.primary, custom_id="btn_add_admin")
    async def btn_add_admin(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AdminModal("add_admin", "ADD ADMIN", "どのユーザーを管理者にしますか？"))

    @discord.ui.button(label="FIRE", style=discord.ButtonStyle.secondary, custom_id="btn_fire")
    async def btn_fire(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AdminModal("fire", "FIRE", "どのユーザーの管理者権限を解除しますか？"))

# ==========================================
# 💬 コマンド定義
# ==========================================

@bot.tree.command(name="create", description="路線ダイヤの作成を開始します")
@app_commands.describe(路線名="ダイヤを作成する路線名を入力")
async def create(interaction: discord.Interaction, 路線名: str):
    if interaction.user.id in banned_users:
        await interaction.response.send_message("⚠️ あなたは Bot の利用が制限されています。", ephemeral=True)
        return

    if interaction.guild_id in blacklisted_servers:
        await interaction.response.send_message("⚠️ このサーバーでは Bot の利用が禁止されています。", ephemeral=True)
        return

    session_data = TrainData(路線名, interaction.user.id)
    embed = generate_status_embed(session_data)
    view = MainControlView(timeout=1800)

    await interaction.response.send_message(embed=embed, view=view)
    msg = await interaction.original_response()
    user_sessions[msg.id] = session_data
# ==========================================
# 導入サーバー数と合計メンバー数を表示するコマンド (!servers)
# ==========================================
@bot.command(name="servers", description="ボットが導入されているサーバー数と合計人数を表示します")
async def servers(ctx):
    # 導入されている全サーバーの数を取得
    guild_count = len(bot.guilds)
    
    # 導入されている全サーバーのメンバー数を重複なし/あり（今回は単純な合計）で計算
    total_members = sum(guild.member_count for guild in bot.guilds if guild.member_count is not None)
    
    embed = discord.Embed(
        title="📊 ボットの導入状況",
        color=discord.Color.blue()
    )
    embed.add_field(name="導入サーバー数", value=f"**{guild_count}** サーバー", inline=False)
    embed.add_field(name="合計メンバー数", value=f"**{total_members:,}** 人", inline=False)
    
    await ctx.send(embed=embed)
# !botinfo (通常コマンドに変更)
@bot.command(name="botinfo")
async def botinfo(ctx):
    uptime = datetime.now() - START_TIME
    hours, remainder = divmod(int(uptime.total_seconds()), 3600)
    minutes, _ = divmod(remainder, 60)
    
    mem = psutil.virtual_memory()
    mem_used_mb = mem.used / (1024 * 1024)
    mem_total_mb = mem.total / (1024 * 1024)
    mem_percent = mem.percent

    ping = round(bot.latency * 1000)

    embed = discord.Embed(
        title="🤖 Bot 稼働ステータス & システム情報", 
        color=0x2ecc71,
        timestamp=datetime.now()
    )
    embed.add_field(name="🟢 ステータス", value="正常稼働中 (Active)", inline=True)
    embed.add_field(name="📶 応答速度 (Ping)", value=f"`{ping} ms`", inline=True)
    embed.add_field(name="💾 メモリ使用率", value=f"`{mem_percent}%` ({mem_used_mb:.1f}MB / {mem_total_mb:.1f}MB)", inline=False)
    embed.add_field(name="⚠️ 本日のエラー数", value=f"`{DAILY_ERROR_COUNT} 件`", inline=True)
    embed.add_field(name="⏱️ 連続稼働時間", value=f"`{hours}時間 {minutes}分`", inline=True)
    
    error_display = "\n".join(ERROR_LOGS[-3:]) if ERROR_LOGS else "なし"
    embed.add_field(name="📋 直近のエラーログ（最大3件）", value=f"```\n{error_display}\n```", inline=False)
    
    await ctx.send(embed=embed)

# 管理者用 コントロールパネル コマンド
@bot.command(name="adminpanel")
async def adminpanel(ctx):
    if ctx.author.id not in admin_users:
        return
    embed = generate_admin_embed()
    await ctx.send(embed=embed, view=AdminPanelView())

    
# ==========================================
# 🌐 Web連動 ダイヤ作成システム (/testing)
#   フロー: /testing → 使い捨てURL発行 → Webフォーム入力
#          → POST /api/submit は即応答 → BackgroundTasks で
#            Gemini解析 + ダイヤ計算 + Discord送信
# ==========================================
TESTING_OWNER_ID = 1301944996261400656
WEB_TOKEN_TTL = 1800  # 秒
MAX_TRAINS = 3000
web_tokens = {}  # {token: (user_id, expires_at)}

DEFAULT_PARAMS = {
    "dwell_seconds": 30,
    "turnaround_minutes": 3,
    "express_every": 0,
    "express_name": "快速",
    "local_name": "普通",
    "summary": "備考なし（既定値を使用）",
}


def issue_web_token(user_id: int) -> str:
    now = time.time()
    for k, (_, exp) in list(web_tokens.items()):
        if exp < now:
            del web_tokens[k]
    token = secrets.token_urlsafe(16)
    web_tokens[token] = (user_id, now + WEB_TOKEN_TTL)
    return token


def verify_web_token(token: str) -> int:
    v = web_tokens.get(token)
    if not v or v[1] < time.time():
        raise HTTPException(status_code=401, detail="リンクの有効期限切れです。/testing を再実行してください。")
    return v[0]


@bot.tree.command(name="testing", description="Web入力フォームでダイヤを作成します（管理者限定）")
async def testing(interaction: discord.Interaction):
    if interaction.user.id != TESTING_OWNER_ID:
        await interaction.response.send_message("⚠️ このコマンドは使用できません。", ephemeral=True)
        return
    token = issue_web_token(interaction.user.id)
    url = f"{BASE_URL}/form?t={token}"
    await interaction.response.send_message(
        f"入力フォームはこちらです（30分間有効・本人専用）:\n{url}", ephemeral=True
    )


# ---------- 共通サーバー / 共通チャンネル ----------
async def _member_of(guild: discord.Guild, user_id: int):
    try:
        return guild.get_member(user_id) or await guild.fetch_member(user_id)
    except discord.HTTPException:
        return None


def _both_can_send(channel, member, me) -> bool:
    up, bp = channel.permissions_for(member), channel.permissions_for(me)
    return (up.view_channel and up.send_messages
            and bp.view_channel and bp.send_messages and bp.attach_files)


async def get_shared_channels(user_id: int) -> list[dict]:
    async def one(g: discord.Guild):
        m = await _member_of(g, user_id)
        if m is None or g.me is None:
            return None
        chs = [{"id": str(c.id), "name": c.name}
               for c in g.text_channels if _both_can_send(c, m, g.me)]
        return {"id": str(g.id), "name": g.name, "channels": chs} if chs else None

    res = await asyncio.gather(*[one(g) for g in bot.guilds])
    return [r for r in res if r]


async def channel_allowed(user_id: int, channel_id: int) -> bool:
    ch = bot.get_channel(channel_id)
    if not isinstance(ch, discord.TextChannel):
        return False
    m = await _member_of(ch.guild, user_id)
    return bool(m and ch.guild.me and _both_can_send(ch, m, ch.guild.me))


# ---------- リクエストモデル ----------
class StationIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    seconds: int = Field(ge=0, le=7200)  # 次駅までの所要時間（秒）。最終駅は無視


class QuadIn(BaseModel):
    """特殊区間。kind: "quad"=複々線 / "single"=単線（未指定の区間は複線扱い）"""
    start: str
    end: str
    kind: str = "quad"


class DiagramRequest(BaseModel):
    stations: list[StationIn] = Field(min_length=2, max_length=60)
    key_stations: list[str] = []
    start_time: str
    end_time: str
    trainsets: int = Field(ge=1, le=50)
    remarks: str = Field("", max_length=1000)
    passing_stations: list[str] = []
    quad_sections: list[QuadIn] = []
    target: str  # "dm" | "channel"
    channel_id: str = ""


def _to_sec(hhmm: str) -> int:
    h, m = hhmm.split(":")
    h, m = int(h), int(m)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError
    return h * 3600 + m * 60


def _fmt(sec: int) -> str:
    sec %= 86400
    return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}"


# ---------- Gemini: 備考欄 → パラメータ ----------
async def parse_remarks(remarks: str, names: list[str]) -> dict:
    p = dict(DEFAULT_PARAMS)
    if not remarks.strip() or not GEMINI_API_KEY:
        return p
    system = "あなたは鉄道ダイヤ設計の入力解析器です。指定のJSONのみを返してください。"
    prompt = (
        f"駅一覧: {', '.join(names)}\n"
        f"備考: {remarks}\n\n"
        "備考から次のキーを持つJSONを作成。不明な項目は既定値:\n"
        '{"dwell_seconds": 停車時間秒(10-180, 既定30),'
        ' "turnaround_minutes": 折返し分(1-10, 既定3),'
        ' "express_every": 何往復に1本を速達にするか(0=速達なし, 既定0),'
        ' "express_name": 速達の種別名(既定"快速"),'
        ' "local_name": 各駅停車の種別名(既定"普通"),'
        ' "summary": 解釈内容の日本語要約(80字以内)}'
    )
    try:
        raw = await asyncio.wait_for(
            asyncio.to_thread(gemini_generate, prompt, system, True), timeout=30
        )
        d = json.loads(raw)
        if isinstance(d, list) and d:
            d = d[0]
        if not isinstance(d, dict):
            raise ValueError("JSON形式が不正")
        p["dwell_seconds"] = min(max(int(d.get("dwell_seconds", 30)), 10), 180)
        p["turnaround_minutes"] = min(max(int(d.get("turnaround_minutes", 3)), 1), 10)
        p["express_every"] = min(max(int(d.get("express_every", 0)), 0), 20)
        p["express_name"] = str(d.get("express_name") or "快速")[:10]
        p["local_name"] = str(d.get("local_name") or "普通")[:10]
        p["summary"] = str(d.get("summary") or "備考を解析しました")[:120]
    except Exception as e:
        p["summary"] = f"備考の解析に失敗したため既定値を使用 ({type(e).__name__})"
    return p


# ---------- ダイヤ計算（純Python・軽量） ----------
def build_diagram(req: DiagramRequest, params: dict) -> tuple[str, str]:
    names = [s.name for s in req.stations]
    n = len(names)
    idx = {nm: i for i, nm in enumerate(names)}
    seg = [s.seconds for s in req.stations[:-1]]
    dwell = params["dwell_seconds"]
    turn = params["turnaround_minutes"] * 60
    exp_n = params["express_every"]
    key = set(req.key_stations) | {names[0], names[-1]}
    N = req.trainsets

    t0, t1 = _to_sec(req.start_time), _to_sec(req.end_time)
    if t1 <= t0:
        t1 += 86400  # 日またぎ

    def run_trip(direction: int, dep: int, express: bool):
        order = list(range(n)) if direction == 0 else list(range(n - 1, -1, -1))
        rows, t = [], dep
        for pos, i in enumerate(order):
            if pos == 0:
                rows.append((i, t, t, True))
                continue
            t += seg[min(order[pos - 1], i)]
            if pos == n - 1:
                rows.append((i, t, t, True))
            elif (not express) or names[i] in key:
                arr = t
                t += dwell
                rows.append((i, arr, t, True))
            else:
                rows.append((i, t, t, False))
        return rows

    one_way = sum(seg) + dwell * max(n - 2, 0)
    cycle = 2 * (one_way + turn)

    trains = []
    for k in range(N):
        t, direction, j = t0 + int(k * cycle / N), 0, 0
        while t < t1 and len(trains) < MAX_TRAINS:
            express = exp_n > 0 and ((j // 2) + k) % exp_n == 0
            rows = run_trip(direction, t, express)
            trains.append({"set": k + 1, "dir": direction, "express": express, "rows": rows})
            t = rows[-1][1] + turn
            direction ^= 1
            j += 1
    trains.sort(key=lambda x: x["rows"][0][1])

    # 追い抜き簡易判定（速達が直前の各停に追いつく地点が待避駅/複々線か）
    covered = {idx[s] for s in req.passing_stations if s in idx}
    singles = []
    for q in req.quad_sections:
        if q.start in idx and q.end in idx:
            lo, hi = sorted((idx[q.start], idx[q.end]))
            if q.kind == "single":
                singles.append((lo, hi))
            else:
                covered |= set(range(lo, hi + 1))
    ok_cnt, warns = 0, []

    # 単線区間: 上下列車が同時に区間内に居る場合を要確認として報告（簡易判定）
    for lo, hi in singles[:10]:
        iv = {0: [], 1: []}
        for tr in trains:
            ts = [r for r in tr["rows"] if lo <= r[0] <= hi]
            if ts:
                iv[tr["dir"]].append((min(r[1] for r in ts), max(r[2] for r in ts)))
        hits, first = 0, None
        for a0, a1 in iv[0]:
            for b0, b1 in iv[1]:
                if a0 < b1 and b0 < a1:
                    hits += 1
                    first = first if first is not None else max(a0, b0)
        if hits:
            warns.append(f"単線区間 {names[lo]}〜{names[hi]}: 上下列車の重なり {hits} 件（初回 {_fmt(first)} 頃）")
    for d in (0, 1):
        ds = [x for x in trains if x["dir"] == d]
        for ei, e in enumerate(ds):
            if not e["express"]:
                continue
            loc = next((x for x in reversed(ds[:ei]) if not x["express"]), None)
            if loc is None:
                continue
            ldep = {r[0]: r[2] for r in loc["rows"]}
            for pos, r in enumerate(e["rows"][1:], start=1):
                i = r[0]
                if i in ldep and r[1] <= ldep[i]:
                    prev_i = e["rows"][pos - 1][0]
                    if i in covered or prev_i in covered:
                        ok_cnt += 1
                    else:
                        warns.append(f"{_fmt(e['rows'][0][1])}発 {params['express_name']} が {names[prev_i]}〜{names[i]} で各停に追いつく")
                    break

    # テキスト出力
    L = [
        "【鉄道ダイヤ】",
        f"運行時間 {req.start_time}〜{req.end_time} / 編成数 {N} / 停車{dwell}秒 / 折返し{params['turnaround_minutes']}分",
        f"区間 {names[0]} - {names[-1]} / 主要駅 {', '.join(sorted(key, key=idx.get))}",
        f"備考の解釈: {params['summary']}",
        "",
    ]
    for no, tr in enumerate(trains, 1):
        kind = params["express_name"] if tr["express"] else params["local_name"]
        way = f"{names[0]}→{names[-1]}" if tr["dir"] == 0 else f"{names[-1]}→{names[0]}"
        L.append(f"[{no:03d}] 編成{tr['set']} {way} {kind}")
        for i, arr, dep, stop in tr["rows"]:
            if not stop:
                L.append(f"  {names[i]}: 通過 {_fmt(arr)}")
            elif arr == dep:
                L.append(f"  {names[i]}: {_fmt(arr)}")
            else:
                L.append(f"  {names[i]}: {_fmt(arr)}着 / {_fmt(dep)}発")
        L.append("")

    summary = (
        f"ダイヤを作成しました。列車 {len(trains)} 本 / 編成 {N} / 追い抜き地点OK {ok_cnt} 件 / 要確認 {len(warns)} 件\n"
        f"備考の解釈: {params['summary']}"
    )
    if len(trains) >= MAX_TRAINS:
        summary += f"\n※ 上限 {MAX_TRAINS} 本で打ち切りました。"
    if warns:
        summary += "\n要確認(簡易判定):\n" + "\n".join(f"・{w}" for w in warns[:8])
    return "\n".join(L), summary[:1900]


# ---------- バックグラウンドジョブ ----------
async def run_diagram_job(user_id: int, req: DiagramRequest):
    try:
        params = await parse_remarks(req.remarks, [s.name for s in req.stations])
        text, summary = await asyncio.to_thread(build_diagram, req, params)
        f = discord.File(io.BytesIO(text.encode("utf-8")), filename="diagram.txt")
        if req.target == "dm":
            user = await bot.fetch_user(user_id)
            await user.send(summary, file=f)
        else:
            ch = bot.get_channel(int(req.channel_id)) or await bot.fetch_channel(int(req.channel_id))
            await ch.send(summary, file=f)
    except Exception as e:
        print(f"diagram job failed: {e}")
        try:
            user = await bot.fetch_user(user_id)
            await user.send(f"⚠️ ダイヤ作成に失敗しました: `{type(e).__name__}: {e}`"[:1900])
        except Exception:
            pass


# ---------- Web API ----------
@api.get("/form")
def form_page(t: str = ""):
    verify_web_token(t)
    base = os.path.dirname(os.path.abspath(__file__))
    # web/form.html を優先し、ルート直下の form.html でも動くようにする
    for p in (os.path.join(base, "web", "form.html"), os.path.join(base, "form.html")):
        if os.path.isfile(p):
            return FileResponse(p, media_type="text/html")
    raise HTTPException(500, "form.html が見つかりません。リポジトリに web/form.html を追加してデプロイしてください。")


@api.get("/api/channels")
async def api_channels(t: str = ""):
    uid = verify_web_token(t)
    return await get_shared_channels(uid)


@api.post("/api/submit")
async def api_submit(req: DiagramRequest, background: BackgroundTasks, t: str = ""):
    uid = verify_web_token(t)
    names = [s.name for s in req.stations]
    if len(set(names)) != len(names):
        raise HTTPException(400, "駅名が重複しています。")
    known = set(names)
    for nm in [*req.key_stations, *req.passing_stations,
               *[q.start for q in req.quad_sections], *[q.end for q in req.quad_sections]]:
        if nm not in known:
            raise HTTPException(400, f"駅名リストにない駅が指定されています: {nm}")
    for q in req.quad_sections:
        if q.kind not in ("single", "quad") or q.start == q.end:
            raise HTTPException(400, "特殊区間の指定が不正です。")
    try:
        _to_sec(req.start_time)
        _to_sec(req.end_time)
    except ValueError:
        raise HTTPException(400, "時刻は HH:MM 形式で入力してください。")
    if req.target == "channel":
        if not req.channel_id.isdigit() or not await channel_allowed(uid, int(req.channel_id)):
            raise HTTPException(400, "選択したチャンネルには送信できません。")
    elif req.target != "dm":
        raise HTTPException(400, "出力先が不正です。")
    background.add_task(run_diagram_job, uid, req)  # 即応答・処理は裏で実行
    return {"ok": True, "message": "受け付けました。完了するとDiscordに届きます。"}


# Bot 起動時イベント
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Failed to sync commands: {e}")


# ==========================================
# 🚀 起動: Webサーバー(FastAPI/uvicorn)と Bot を同一イベントループで同時実行
# ==========================================
async def main():
    server = uvicorn.Server(uvicorn.Config(api, host="0.0.0.0", port=PORT, log_level="warning"))
    async with bot:
        await asyncio.gather(server.serve(), bot.start(TOKEN))


if __name__ == "__main__":
    asyncio.run(main())
