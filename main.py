import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import psutil
import os
import time
from datetime import datetime, timedelta
from flask import Flask
from threading import Thread
from openai import OpenAI
import os

openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# --- 👇 ここを追加・確認する ---
intents = discord.Intents.default()
intents.members = True          # サーバーメンバーの取得に必須
intents.message_content = True  # メッセージ内容の取得に必須
intents.dm_messages = True      # DMの送受信に必須

# 進行中の人狼ゲームを管理する辞書
active_games = {} # {channel_id: WolfGameSession}

# ボットの初期化（intents=intents を必ず渡してください）
bot = commands.Bot(command_prefix="!", intents=intents)
# ------------------------------

# （Webサーバー用などの既存コードがここに続く）

# game.pyを読み込む処理（まだ書いていなければ追加）
@bot.event
async def on_ready():
    await bot.load_extension("game") # game.pyを読み込む
    await bot.tree.sync()
    print(f"Logged in as {bot.user}")

# bot.run("あなたのトークン") は一番下に記述
# ==========================================
# 🌐 Render ポート監視（Health Check）対策
# ==========================================
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

Thread(target=run_flask).start()

# ==========================================
# 🤖 Discord Bot 基本設定
# ==========================================
TOKEN = os.getenv("DISCORD_TOKEN")
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

from openai import OpenAI
import os

# OpenAIクライアントの初期化
openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# 5. ChatGPT検索 (/search)
@bot.tree.command(name="search", description="ChatGPTを使って質問や検索をします")
async def search(interaction: discord.Interaction, キーワード: str):
    await interaction.response.defer()
    
    try:
        # OpenAI APIを使った回答の生成
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",  # コスパが良く高速なモデル
            messages=[
                {"role": "system", "content": "あなたは優秀なアシスタントです。簡潔でわかりやすく答えてください。"},
                {"role": "user", "content": キーワード}
            ]
        )
        answer_text = response.choices[0].message.content
        
        if len(answer_text) > 1900:
            answer_text = answer_text[:1900] + "...\n（文字数オーバーのため省略しました）"

        embed = discord.Embed(
            title="🔍 AI検索結果 (ChatGPT)",
            description=f"**検索ワード:** {キーワード}\n\n{answer_text}",
            color=discord.Color.green()
        )
        await interaction.followup.send(embed=embed)
        
    except Exception as e:
        embed = discord.Embed(
            title="🔍 AI検索エラー",
            description=f"エラーが発生しました: `{e}`\n(OpenAI APIキーを確認してください)",
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

# ==========================================
# !freecreate グループコマンド (botinfo / restart)
# ==========================================
@bot.group(name="freecreate", invoke_without_command=True)
async def freecreate(ctx):
    # サブコマンドなしで !freecreate だけ実行された場合のメッセージ
    await ctx.send("⚠️ 使用例: `!freecreate botinfo` または `!freecreate restart` を指定してください。")

# !freecreate botinfo
@freecreate.command(name="botinfo")
async def freecreate_botinfo(ctx):
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

# !freecreate restart (管理者限定・Render再起動)
@freecreate.command(name="restart")
async def freecreate_restart(ctx):
    # 管理者チェック
    if ctx.author.id not in admin_users:
        await ctx.send("⚠️ このコマンドを実行する権限がありません。")
        return

    await ctx.send("ボットを再起動しています... (Render側で自動再起動されます)")
    
    # Botの切断処理を行った後、プロセスを終了してRenderの自動再起動を誘発する
    await bot.close()
    os._exit(0)

# 管理者用 コントロールパネル コマンド
@bot.command(name="adminpanel")
async def adminpanel(ctx):
    if ctx.author.id not in admin_users:
        return
    embed = generate_admin_embed()
    await ctx.send(embed=embed, view=AdminPanelView())
    
# Bot 起動時イベント
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Failed to sync commands: {e}")

bot.run(TOKEN)
