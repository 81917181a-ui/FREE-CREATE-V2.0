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

# Flaskを裏で別スレッド起動
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

# 入力整形ユーティリティ（カンマ・読点の表記揺れ吸収）
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
        self.train_types = {}  # {種別名: [停車駅]}
        self.schedules = []    # [(時刻, 種別, 開始駅, 終了駅)]
        self.quad_tracks = ""
        self.passing_stations = []

user_sessions = {}  # {message_id: TrainData}

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
        self.session_data.stations = stations  # 上書き
        await interaction.response.send_message("✅ 駅名を登録・更新しました！", ephemeral=True)

class TimeModal(discord.ui.Modal, title="⏱️ 時間・運行設定"):
    durations_input = discord.ui.TextInput(
        label="各区間の基準所要時間（秒）",
        placeholder="例: 180, 120, 240, 150",
        required=True
    )
    schedules_input = discord.ui.TextInput(
        label="運行する列車",
        style=discord.TextStyle.paragraph,
        placeholder="例:\n06:00, 各停\n06:05, 急行, 御茶ノ水, 新宿\n06:12, 各停, 神田",
        required=True
    )

    def __init__(self, session_data):
        super().__init__()
        self.session_data = session_data

    async def on_submit(self, interaction: discord.Interaction):
        durations = [int(d) for d in normalize_input(self.durations_input.value) if d.isdigit()]
        raw_schedules = self.schedules_input.value.strip().split("\n")
        schedules = []
        for line in raw_schedules:
            items = normalize_input(line)
            if len(items) >= 2:
                time_str, t_type = items[0], items[1]
                start_st = items[2] if len(items) > 2 else None
                end_st = items[3] if len(items) > 3 else None
                schedules.append((time_str, t_type, start_st, end_st))

        self.session_data.durations = durations  # 上書き
        self.session_data.schedules = schedules  # 上書き
        await interaction.response.send_message("✅ 時間・運行設定を更新しました！", ephemeral=True)

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
        self.session_data.train_types[t_name] = stop_list  # 追加・更新
        await interaction.response.send_message(f"✅ 種別『{t_name}』を追加しました！", ephemeral=True)

class QuadTrackModal(discord.ui.Modal, title="🛤️ 複々線区間を設定"):
    quad_input = discord.ui.TextInput(label="複々線区間", placeholder="例: 東京〜御茶ノ水", required=True)

    def __init__(self, session_data):
        super().__init__()
        self.session_data = session_data

    async def on_submit(self, interaction: discord.Interaction):
        self.session_data.quad_tracks = self.quad_input.value.strip()  # 上書き
        await interaction.response.send_message("✅ 複々線区間を設定しました！", ephemeral=True)

class PassingModal(discord.ui.Modal, title="🔀 待避可能駅を設定"):
    passing_input = discord.ui.TextInput(label="待避可能駅", placeholder="例: 御茶ノ水, 四ツ谷", required=True)

    def __init__(self, session_data):
        super().__init__()
        self.session_data = session_data

    async def on_submit(self, interaction: discord.Interaction):
        self.session_data.passing_stations = normalize_input(self.passing_input.value)  # 上書き
        await interaction.response.send_message("✅ 待避可能駅を設定しました！", ephemeral=True)

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
            await interaction.response.send_message("🗑️ 駅名設定を削除しました。", ephemeral=True)
        elif val == "times":
            self.session_data.durations = []
            self.session_data.schedules = []
            await interaction.response.send_message("🗑️ 時間・運行設定を削除しました。", ephemeral=True)
        elif val == "quad":
            self.session_data.quad_tracks = ""
            await interaction.response.send_message("🗑️ 複々線区間設定を削除しました。", ephemeral=True)
        elif val == "passing":
            self.session_data.passing_stations = []
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
            session.schedules = []
            session.quad_tracks = ""
            session.passing_stations = []
            await interaction.response.send_message("🔄 すべての設定をリセットしました！", ephemeral=True)
        elif val == "create":
            await interaction.response.defer()
            result_msg = generate_timetable(session)
            await interaction.followup.send(result_msg)

class MainControlView(discord.ui.View):
    def __init__(self, timeout=1800):  # 30分間操作可能
        super().__init__(timeout=timeout)
        self.add_item(MainControlSelect())

# ==========================================
# 📄 ダイヤ出力ロジック（普通メッセージ形式）
# ==========================================

def generate_timetable(session: TrainData) -> str:
    if not session.stations or not session.schedules or not session.durations:
        return "⚠️ ダイヤを作成するには「駅名」「時間・運行設定」の登録が必要です！"

    output = f"**【{session.line_name} ダイヤ出力結果】**\n\n"
    target_schedules = session.schedules[:10]  # 最大10編成まで出力

    for idx, sch in enumerate(target_schedules, 1):
        dep_time_str, t_type, start_st, end_st = sch
        
        start_idx = session.stations.index(start_st) if start_st in session.stations else 0
        end_idx = session.stations.index(end_st) if end_st in session.stations else len(session.stations) - 1

        curr_time = datetime.strptime(dep_time_str, "%H:%M")
        
        output += f"■ **編成{idx} ({t_type})**\n"
        output += f"{dep_time_str} {session.stations[start_idx]}発 ➔ {session.stations[end_idx]}行き\n"

        for i in range(start_idx, end_idx + 1):
            st_name = session.stations[i]
            
            if i == start_idx:
                output += f"・{st_name}：{curr_time.strftime('%H:%M')}発（始発）\n"
            elif i == end_idx:
                curr_time += timedelta(seconds=session.durations[i-1] if i-1 < len(session.durations) else 180)
                output += f"・{st_name}：{curr_time.strftime('%H:%M')}着（終着）\n"
            else:
                curr_time += timedelta(seconds=session.durations[i-1] if i-1 < len(session.durations) else 180)
                arr_str = curr_time.strftime('%H:%M')
                
                stops = session.train_types.get(t_type, [])
                is_stop = ("全駅停車" in stops) or (st_name in stops) or not stops

                if is_stop:
                    if st_name in session.passing_stations:
                        curr_time += timedelta(seconds=180)  # 待避時間
                        dep_str = curr_time.strftime('%H:%M')
                        output += f"・{st_name}：{arr_str}着 / {dep_str}発（※通過待ち）\n"
                    else:
                        curr_time += timedelta(seconds=30)  # 通常停車30秒
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

    view = MainControlView(timeout=1800)
    embed = discord.Embed(
        title=f"{路線名} のダイヤを作成中",
        description=(
            "**現在の設定:**\n"
            "・駅名: 未設定\n"
            "・種別: 未設定\n"
            "・区間所要時間: 未設定\n"
            "・運行予定: なし\n"
            "・待避可能駅: なし\n"
            "・複々線区間: なし\n\n"
            "───────────────────\n"
            "👇 **行いたい操作を選択してください**"
        ),
        color=0x3498db
    )
    await interaction.response.send_message(embed=embed, view=view)
    msg = await interaction.original_response()
    user_sessions[msg.id] = TrainData(路線名, interaction.user.id)

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
