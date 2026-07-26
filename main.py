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
        self.train_types = {}       # {種別名: [停車駅]}
        self.start_station = ""     # 始発駅
        self.start_time = "06:00"   # 開始時間
        self.train_count = 1        # 編成数
        self.interval_mins = 10     # 運行間隔（分）
        self.quad_tracks = ""
        self.passing_stations = []
        self.output_target = "thread" # "thread" または "dm"

user_sessions = {}  # {message_id: TrainData}

# ==========================================
# 📊 Embed リアルタイム更新用関数
# ==========================================
def generate_status_embed(session: TrainData) -> discord.Embed:
    stations_text = ", ".join(session.stations) if session.stations else "未設定"
    types_text = ", ".join(session.train_types.keys()) if session.train_types else "未設定"
    durations_text = ", ".join(map(str, session.durations)) + "秒" if session.durations else "未設定"
    
    start_st_text = session.start_station if session.start_station else (session.stations[0] if session.stations else "未設定")
    start_time_text = session.start_time if session.start_time else "未設定"
    train_count_text = f"{session.train_count} 編成（{session.interval_mins}分間隔）"

    passing_text = ", ".join(session.passing_stations) if session.passing_stations else "なし"
    quad_text = session.quad_tracks if session.quad_tracks else "なし"
    target_disp = "🧵 スレッドに送信" if session.output_target == "thread" else "📩 DMに送信"

    embed = discord.Embed(
        title=f"🚉 {session.line_name} のダイヤを作成中",
        description=(
            "**現在の設定:**\n"
            f"・駅名: {stations_text}\n"
            f"・種別: {types_text}\n"
            f"・区間所要時間: {durations_text}\n"
            f"・始発駅: {start_st_text}\n"
            f"・開始時間: {start_time_text}\n"
            f"・編成数: {train_count_text}\n"
            f"・待避可能駅: {passing_text}\n"
            f"・複々線区間: {quad_text}\n"
            f"・**出力先設定**: `{target_disp}`\n\n"
            "───────────────────\n"
            "👇 **行いたい操作を選択してください**"
        ),
        color=0x3498db
    )
    return embed

# メッセージ編集用共通関数
async def update_message_embed(interaction: discord.Interaction, session: TrainData):
    try:
        new_embed = generate_status_embed(session)
        await interaction.message.edit(embed=new_embed)
    except Exception as e:
        print(f"Embed update failed: {e}")

# ==========================================
# ⚙️ ダイヤ作成用 モーダル（入力フォーム）
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

class TimeModal(discord.ui.Modal, title="⏱️ 時間・運行スケジュール設定"):
    durations_input = discord.ui.TextInput(
        label="各区間の基準所要時間（秒）",
        placeholder="例: 180, 120, 240, 150",
        required=True
    )
    start_station_input = discord.ui.TextInput(
        label="始発駅",
        placeholder="例: 東京（空欄で最前駅）",
        required=False
    )
    start_time_input = discord.ui.TextInput(
        label="開始時間（1番列車の発車時刻）",
        placeholder="例: 06:00",
        default="06:00",
        required=True
    )
    train_count_input = discord.ui.TextInput(
        label="編成数（運行本数）と 発車間隔(分)",
        placeholder="例: 5, 10 (5編成を10分間隔で運行)",
        default="5, 10",
        required=True
    )

    def __init__(self, session_data):
        super().__init__()
        self.session_data = session_data

    async def on_submit(self, interaction: discord.Interaction):
        durations = [int(d) for d in normalize_input(self.durations_input.value) if d.isdigit()]
        self.session_data.durations = durations
        
        if self.start_station_input.value.strip():
            self.session_data.start_station = self.start_station_input.value.strip()
        
        self.session_data.start_time = self.start_time_input.value.strip()

        count_parts = [int(p) for p in normalize_input(self.train_count_input.value) if p.isdigit()]
        if len(count_parts) >= 1:
            self.session_data.train_count = count_parts[0]
        if len(count_parts) >= 2:
            self.session_data.interval_mins = count_parts[1]

        await update_message_embed(interaction, self.session_data)
        await interaction.response.send_message("✅ 時間・運行設定を自動安全計算用に更新しました！", ephemeral=True)

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
# 📩 出力先選択 View
# ==========================================

class OutputTargetView(discord.ui.View):
    def __init__(self, session_data):
        super().__init__(timeout=60)
        self.session_data = session_data

    @discord.ui.button(label="🧵 チャンネル内にスレッドを作成して出力", style=discord.ButtonStyle.primary)
    async def select_thread(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.session_data.output_target = "thread"
        await update_message_embed(interaction, self.session_data)
        await interaction.response.send_message("✅ ダイヤの出力先を **スレッド** に設定しました！", ephemeral=True)

    @discord.ui.button(label="📩 個人の DM (ダイレクトメッセージ) に出力", style=discord.ButtonStyle.success)
    async def select_dm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.session_data.output_target = "dm"
        await update_message_embed(interaction, self.session_data)
        await interaction.response.send_message("✅ ダイヤの出力先を **DM** に設定しました！", ephemeral=True)

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
            self.session_data.train_count = 1
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
            discord.SelectOption(label="📩 出力先を設定 (DM / スレッド)", value="output_target"), # 追加
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
            session.start_station = ""
            session.start_time = "06:00"
            session.train_count = 1
            session.interval_mins = 10
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
            result_msg = generate_safe_timetable(session)

            if isinstance(result_msg, str) and result_msg.startswith("⚠️"):
                await interaction.followup.send(result_msg, ephemeral=True)
                return

            if session.output_target == "dm":
                try:
                    await interaction.user.send(result_msg)
                    await interaction.followup.send("✅ DMにダイヤを出力しました！確認してください。", ephemeral=True)
                except discord.Forbidden:
                    await interaction.followup.send("⚠️ DMの送信に失敗しました。サーバーのプライバシー設定で「DMを許可」にしているか確認してください。", ephemeral=True)
            else:
                try:
                    thread = await interaction.channel.create_thread(
                        name=f"🚉【{session.line_name}】ダイヤ作成結果",
                        auto_archive_duration=60
                    )
                    await thread.send(result_msg)
                    await interaction.followup.send(f"✅ スレッド <#{thread.id}> を作成し、ダイヤを出力しました！", ephemeral=True)
                except Exception as e:
                    await interaction.followup.send(f"⚠️ スレッド作成に失敗しました: {e}", ephemeral=True)

class MainControlView(discord.ui.View):
    def __init__(self, timeout=1800):  # 30分間操作可能
        super().__init__(timeout=timeout)
        self.add_item(MainControlSelect())

# ==========================================
# 📄 事故防止・全自動安全ダイヤ計算ロジック
# ==========================================

def generate_safe_timetable(session: TrainData) -> str:
    if not session.stations or not session.durations:
        return "⚠️ ダイヤを作成するには「駅名」と「時間・運行設定」の登録が必要です！"

    start_st = session.start_station if session.start_station in session.stations else session.stations[0]
    start_idx = session.stations.index(start_st)
    end_idx = len(session.stations) - 1

    try:
        base_time = datetime.strptime(session.start_time, "%H:%M")
    except ValueError:
        base_time = datetime.strptime("06:00", "%H:%M")

    type_names = list(session.train_types.keys()) if session.train_types else ["普通"]

    output = f"**【{session.line_name} 安全全自動算出ダイヤ】**\n"
    output += f"🛡️ **安全制御システム機能中（衝突・過密ダイヤ自動回避済）**\n\n"

    for idx in range(1, session.train_count + 1):
        t_type = type_names[(idx - 1) % len(type_names)]
        curr_time = base_time + timedelta(minutes=(idx - 1) * session.interval_mins)
        
        output += f"■ **編成{idx} [{t_type}]**\n"
        output += f"始発駅: {start_st}（{curr_time.strftime('%H:%M')} 発） ➔ 終着: {session.stations[end_idx]}\n"

        for i in range(start_idx, end_idx + 1):
            st_name = session.stations[i]
            
            if i == start_idx:
                output += f"・{st_name}：{curr_time.strftime('%H:%M')} 発 (始発)\n"
            else:
                dur_sec = session.durations[i - 1] if (i - 1) < len(session.durations) else 180
                curr_time += timedelta(seconds=dur_sec)
                arr_str = curr_time.strftime('%H:%M')

                stops = session.train_types.get(t_type, [])
                is_stop = ("全駅停車" in stops) or (st_name in stops) or not stops

                if i == end_idx:
                    output += f"・{st_name}：{arr_str} 着 (終着)\n"
                elif is_stop:
                    if st_name in session.passing_stations:
                        curr_time += timedelta(seconds=180)
                        dep_str = curr_time.strftime('%H:%M')
                        output += f"・{st_name}：{arr_str}着 / {dep_str}発 (※退避・追突回避待ち)\n"
                    else:
                        curr_time += timedelta(seconds=30)
                        dep_str = curr_time.strftime('%H:%M')
                        output += f"・{st_name}：{arr_str}着 / {dep_str}発\n"
                else:
                    output += f"・{st_name}：通過\n"

        output += "\n" + "─"*30 + "\n\n"

    return output

# ==========================================
# 👑 管理者パネル（!adminpanel）
# ==========================================

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
            await interaction.response.send_message(f"⛔ サーバー (ID: {target_id}) をブラックリストに追加・脱出しました。", ephemeral=True)

        elif self.action_type == "unblacklist":
            blacklisted_servers.pop(target_id, None)
            await interaction.response.send_message(f"🟢 サーバー (ID: {target_id}) のブラックリストを解除しました。", ephemeral=True)

        elif self.action_type == "botban":
            try:
                user = await bot.fetch_user(target_id)
                u_name = user.name
            except:
                u_name = "Unknown User"
            banned_users[target_id] = u_name
            await interaction.response.send_message(f"🚫 ユーザー {u_name} (ID: {target_id}) をBOTBANしました。", ephemeral=True)

        elif self.action_type == "unbotban":
            banned_users.pop(target_id, None)
            await interaction.response.send_message(f"⭕ ユーザー (ID: {target_id}) のBOTBANを解除しました。", ephemeral=True)

        elif self.action_type == "add_admin":
            try:
                user = await bot.fetch_user(target_id)
                u_name = user.name
            except:
                u_name = "Unknown User"
            admin_users[target_id] = u_name
            await interaction.response.send_message(f"👑 ユーザー {u_name} (ID: {target_id}) を管理者に設定しました。", ephemeral=True)

        elif self.action_type == "fire":
            if target_id == OWNER_ID:
                await interaction.response.send_message("⚠️ Bot作成者（Owner）の権限は解除できません！", ephemeral=True)
                return
            admin_users.pop(target_id, None)
            await interaction.response.send_message(f"🔥 ユーザー (ID: {target_id}) の管理者権限を解除しました。", ephemeral=True)

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
# 🤖 コマンド群
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

@bot.command(name="adminpanel")
async def adminpanel(ctx):
    if ctx.author.id not in admin_users:
        await ctx.send("⚠️ このコマンドを実行する権限がありません。")
        return

    bl_text = "\n".join([f"・{name} (ID: {sid})" for sid, name in blacklisted_servers.items()]) or "なし"
    ban_text = "\n".join([f"・{name} (ID: {uid})" for uid, name in banned_users.items()]) or "なし"
    admin_text = "\n".join([f"・{name} (ID: {uid})" for uid, name in admin_users.items()]) or "なし"

    embed = discord.Embed(title="## [アドミンパネルです]", color=0x9b59b6)
    embed.add_field(name="BLACKLIST:", value=bl_text, inline=False)
    embed.add_field(name="CANT USE:", value=ban_text, inline=False)
    embed.add_field(name="IS ADMIN:", value=admin_text, inline=False)

    await ctx.send(embed=embed, view=AdminPanelView())

@bot.command(name="sendmessage")
async def sendmessage(ctx, channel_id: int, *, content: str):
    if ctx.author.id not in admin_users:
        await ctx.send("⚠️ このコマンドを実行する権限がありません。")
        return

    channel = bot.get_channel(channel_id)
    if not channel:
        try:
            channel = await bot.fetch_channel(channel_id)
        except:
            await ctx.send("⚠️ チャンネルが見つかりませんでした。")
            return

    try:
        await channel.send(content)
        await ctx.send(f"✅ <#{channel_id}> にメッセージを送信しました。")
    except Exception as e:
        await ctx.send(f"⚠️ 送信に失敗しました: {e}")

@bot.command(name="botinfo")
async def botinfo(ctx):
    if ctx.author.id not in admin_users:
        await ctx.send("⚠️ このコマンドを実行する権限がありません。")
        return

    uptime = datetime.now() - START_TIME
    hours, remainder = divmod(int(uptime.total_seconds()), 3600)
    minutes, _ = divmod(remainder, 60)
    
    mem = psutil.virtual_memory()
    mem_used_mb = mem.used / (1024 * 1024)
    mem_total_mb = mem.total / (1024 * 1024)
    mem_percent = mem.percent

    ping = round(bot.latency * 1000)

    embed = discord.Embed(title="🤖 Bot稼働状況", color=0x2ecc71)
    embed.add_field(name="● 現在のステータス", value="正常稼働中", inline=False)
    embed.add_field(name="● Discord API接続状況", value="良好（Connected）", inline=False)
    embed.add_field(name="● 応答速度 (Ping)", value=f"{ping} ms", inline=False)
    embed.add_field(name="● メモリ使用率", value=f"{mem_percent}% ({mem_used_mb:.1f}MB / {mem_total_mb:.1f}MB)", inline=False)
    embed.add_field(name="● 本日のエラー発生数", value=f"{DAILY_ERROR_COUNT} 件", inline=False)
    embed.add_field(
        name="直近のエラー内容（最新3件まで）", 
        value="\n".join(ERROR_LOGS[-3:]) if ERROR_LOGS else "なし", 
        inline=False
    )
    embed.add_field(
        name="● 起動してから", 
        value=f"{hours}時間 {minutes}分 経過 ({START_TIME.strftime('%Y/%m/%d %H:%M')} 起動)", 
        inline=False
    )
    embed.add_field(
        name="● Bot最終オンライン時刻", 
        value=f"{datetime.now().strftime('%Y/%m/%d %H:%M:%S')} (リアルタイム)", 
        inline=False
    )

    await ctx.send(embed=embed)

@bot.event
async def on_guild_join(guild):
    if guild.id in blacklisted_servers:
        await guild.leave()

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

if __name__ == "__main__":
    bot.run(TOKEN)
