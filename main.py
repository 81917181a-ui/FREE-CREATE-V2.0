import asyncio
import io
import json
import math
import traceback
import re
import secrets
import unicodedata
from datetime import datetime
import os
import time
from threading import Thread
import psutil
import requests  # セルフ・ピン用

import uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

import discord
from discord.ext import commands

# ==========================================
# 🔑 環境変数（Render に登録するのは DISCORD_BOT_TOKEN だけ）
#   DISCORD_BOT_TOKEN のみ（AIは使わないので GEMINI_API_KEY は不要）
#   ※ PORT・RENDER_EXTERNAL_URL は Render が自動設定する
# ==========================================
BASE_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://free-create-v2-0-ubpy.onrender.com").rstrip("/")
PORT = int(os.environ.get("PORT", 8080))


# --- 👇 ここを追加・確認する ---
intents = discord.Intents.default()
intents.members = (
    True  # サーバーメンバーの取得に必須
)
intents.message_content = True  # メッセージ内容の取得に必須
intents.dm_messages = True  # DMの送受信に必須

# ボットの初期化（intents=intents を必ず渡してください）
bot = commands.Bot(command_prefix="!", intents=intents)
# ------------------------------


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

# ==========================================
# 管理パネル / 使用不可ユーザー・出禁サーバー（Firebase Firestore に保存）
#   - 鍵ファイルは Render の Secret Files に firebase.json として登録する
#     （環境変数は増やさない。コードやリポジトリには鍵を入れない）
#   - 保存先: Firestore の botdata/state ドキュメント（1件・JSON文字列）
#   - 使用不可ユーザーが1人もいなければ、誰でもBOTを使える
# ==========================================
FIREBASE_KEY_PATHS = [
    "/etc/secrets/firebase.json",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "firebase.json"),
]
FIRESTORE_DOC = "botdata/state"
_fs = {"session": None, "pid": None}
server_invites = {}  # {server_id: 招待リンク}


def _fs_session():
    if _fs["session"] is None:
        from google.oauth2 import service_account
        from google.auth.transport.requests import AuthorizedSession
        path = next((p for p in FIREBASE_KEY_PATHS if os.path.isfile(p)), None)
        if not path:
            raise RuntimeError("Firebase の鍵ファイルが見つかりません（Render の Secret Files に firebase.json を登録してください）")
        creds = service_account.Credentials.from_service_account_file(
            path, scopes=["https://www.googleapis.com/auth/datastore"])
        _fs["session"], _fs["pid"] = AuthorizedSession(creds), creds.project_id
    return _fs["session"], _fs["pid"]


def _fs_url(pid: str) -> str:
    return f"https://firestore.googleapis.com/v1/projects/{pid}/databases/(default)/documents/{FIRESTORE_DOC}"


def _fs_load_sync() -> dict:
    s, pid = _fs_session()
    r = s.get(_fs_url(pid), timeout=15)
    if r.status_code == 404:
        return {}
    if not r.ok:
        raise RuntimeError(f"Firestore {r.status_code}: {r.text[:200]}")
    raw = r.json().get("fields", {}).get("data", {}).get("stringValue", "")
    return json.loads(raw) if raw else {}


def _fs_save_sync(data: dict):
    s, pid = _fs_session()
    body = {"fields": {"data": {"stringValue": json.dumps(data, ensure_ascii=False)}}}
    r = s.patch(_fs_url(pid), params={"updateMask.fieldPaths": "data"}, json=body, timeout=15)
    if not r.ok:
        raise RuntimeError(f"Firestore {r.status_code}: {r.text[:200]}")


def _state_dict() -> dict:
    return {
        "banned_users": {str(k): v for k, v in banned_users.items()},
        "blacklisted_servers": {
            str(k): {"name": v, "invite": server_invites.get(k, "")} for k, v in blacklisted_servers.items()
        },
    }


async def load_state():
    """起動時に Firebase から読み込み、保存内容をログに出す。失敗時はメモリ上の内容を保つ。"""
    try:
        data = await asyncio.to_thread(_fs_load_sync)
    except Exception as e:
        print(f"[Firebase] 読み込みに失敗しました（保存データなしで起動します）: {e}")
        return
    banned_users.clear()
    blacklisted_servers.clear()
    server_invites.clear()
    for k, v in (data.get("banned_users") or {}).items():
        banned_users[int(k)] = str(v)
    for k, v in (data.get("blacklisted_servers") or {}).items():
        blacklisted_servers[int(k)] = str((v or {}).get("name", "不明"))
        if (v or {}).get("invite"):
            server_invites[int(k)] = v["invite"]
    print(f"[Firebase] 読み込み完了: 使用不可ユーザー {len(banned_users)} 人 / 出禁サーバー {len(blacklisted_servers)} 件")
    for uid, name in banned_users.items():
        print(f"[Firebase]   使用不可ユーザー: {name} ({uid})")
    for sid, name in blacklisted_servers.items():
        print(f"[Firebase]   出禁サーバー: {name} ({sid}) {server_invites.get(sid, '')}")


async def save_state() -> str:
    """Firebase へ保存する。失敗した場合はエラー文を返す（成功なら空文字）。"""
    try:
        await asyncio.to_thread(_fs_save_sync, _state_dict())
        return ""
    except Exception as e:
        print(f"[Firebase] 保存に失敗しました: {e}")
        return f"Firebase への保存に失敗しました（再起動すると元に戻ります）: {e}"[:300]


# ---------- 管理パネル UI ----------
ADMIN_ACTIONS = {
    "blacklist": ("出禁サーバー", "出禁にするサーバーのID"),
    "unblacklist": ("出禁解除", "出禁を解除するサーバーのID"),
    "botban": ("使用不可ユーザー", "使用不可にするユーザーのID"),
    "unbotban": ("ユーザーバン解除", "使用不可を解除するユーザーのID"),
}

# !adminpanel / !banned / 管理パネルを使えるのは、このロールかそれより上位のロールを持つ人のみ（Owner は常に可）
ADMIN_ROLE_ID = 1510405214811852900


def can_manage(user) -> bool:
    if user.id == OWNER_ID:
        return True
    guild = getattr(user, "guild", None)  # DM では Member ではないので不可
    if guild is None:
        return False
    role = guild.get_role(ADMIN_ROLE_ID)
    return bool(role) and user.top_role.position >= role.position


def generate_admin_embed() -> discord.Embed:
    embed = discord.Embed(
        title="BOT 管理パネル",
        description="下の選択メニューから操作を選ぶと、ID入力フォームが開きます。\n一覧は !banned で確認できます。",
        color=0xe74c3c
    )
    embed.add_field(name="出禁サーバー", value=f"{len(blacklisted_servers)} 件", inline=True)
    embed.add_field(name="使用不可ユーザー", value=f"{len(banned_users)} 人", inline=True)
    return embed


async def make_invite(guild: discord.Guild) -> str:
    """出禁にする前に、そのサーバーの招待リンクを作って記録する（作れなければ空文字）。"""
    me = guild.me
    for c in [c for c in guild.text_channels if me and c.permissions_for(me).create_instant_invite][:3]:
        try:
            inv = await c.create_invite(max_age=0, max_uses=0, unique=False, reason="出禁サーバー記録用")
            return inv.url
        except discord.HTTPException:
            continue
    return ""


class AdminModal(discord.ui.Modal):
    def __init__(self, action_type: str, title_text: str, label_text: str):
        super().__init__(title=title_text)
        self.action_type = action_type
        self.input_field = discord.ui.TextInput(label=label_text[:45], placeholder="IDを入力してください", required=True)
        self.add_item(self.input_field)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.input_field.value.strip()
        if not raw.isdigit():
            await interaction.response.send_message("IDは半角数字で入力してください。", ephemeral=True)
            return
        target_id = int(raw)
        await interaction.response.defer(ephemeral=True)  # 招待作成や保存で3秒を超えることがあるため
        persist = self.action_type in ("blacklist", "unblacklist", "botban", "unbotban")
        guild_to_leave = None

        if self.action_type == "blacklist":
            guild = bot.get_guild(target_id)
            if guild:
                invite = await make_invite(guild)
                blacklisted_servers[target_id] = guild.name
                if invite:
                    server_invites[target_id] = invite
                guild_to_leave = guild
                msg = f"サーバー「{guild.name}」(ID: {target_id}) を出禁にしました。" + ("" if invite else "（招待リンクは作成できませんでした）")
            else:
                blacklisted_servers.setdefault(target_id, "不明なサーバー")
                msg = f"サーバー (ID: {target_id}) を出禁にしました。BOTは参加していないため、名前と招待リンクは記録できません。"

        elif self.action_type == "unblacklist":
            if blacklisted_servers.pop(target_id, None) is None:
                msg = f"サーバー (ID: {target_id}) は出禁に登録されていません。"
            else:
                server_invites.pop(target_id, None)
                msg = f"サーバー (ID: {target_id}) の出禁を解除しました。"

        elif self.action_type == "botban":
            if target_id == OWNER_ID:
                await interaction.followup.send("BOT作成者（Owner）は使用不可にできません。", ephemeral=True)
                return
            try:
                u_name = (await bot.fetch_user(target_id)).name
            except discord.HTTPException:
                u_name = "不明なユーザー"
            banned_users[target_id] = u_name
            msg = f"ユーザー「{u_name}」(ID: {target_id}) を使用不可にしました。"

        elif self.action_type == "unbotban":
            if banned_users.pop(target_id, None) is None:
                msg = f"ユーザー (ID: {target_id}) は使用不可に登録されていません。"
            else:
                msg = f"ユーザー (ID: {target_id}) の使用不可を解除しました。"

        elif self.action_type == "add_admin":
            try:
                u_name = (await bot.fetch_user(target_id)).name
            except discord.HTTPException:
                u_name = "不明なユーザー"
            admin_users[target_id] = u_name
            msg = f"ユーザー「{u_name}」(ID: {target_id}) を管理者にしました。"

        else:  # fire
            if target_id == OWNER_ID:
                await interaction.followup.send("BOT作成者（Owner）の権限は解除できません。", ephemeral=True)
                return
            admin_users.pop(target_id, None)
            msg = f"ユーザー (ID: {target_id}) の管理者権限を解除しました。"

        if persist:
            err = await save_state()
            if err:
                msg += "\n" + err
        if guild_to_leave:
            try:
                await guild_to_leave.leave()
            except discord.HTTPException as e:
                msg += f"\nサーバーからの退出に失敗しました: {e}"

        # パネルの表示を更新（選択状態もリセットされる）
        if interaction.message:
            try:
                await interaction.message.edit(embed=generate_admin_embed(), view=AdminPanelView())
            except Exception as e:
                print(f"Panel embed update failed: {e}")
        await interaction.followup.send(msg, ephemeral=True)


class AdminPanelSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=label, value=key) for key, (label, _) in ADMIN_ACTIONS.items()]
        super().__init__(placeholder="操作を選択してください", options=options,
                         min_values=1, max_values=1, custom_id="admin_panel_select")

    async def callback(self, interaction: discord.Interaction):
        if not can_manage(interaction.user):
            await interaction.response.send_message("この操作を使用する権限がありません。", ephemeral=True)
            return
        key = self.values[0]
        title, label = ADMIN_ACTIONS[key]
        await interaction.response.send_modal(AdminModal(key, title, label))


class AdminPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(AdminPanelSelect())


# ---------- 使用制限の適用 ----------
async def _tree_check(interaction: discord.Interaction) -> bool:
    uid = interaction.user.id
    if uid == OWNER_ID:
        return True
    if uid in banned_users:
        msg = "あなたはこのBOTの使用が制限されています。"
    elif interaction.guild_id in blacklisted_servers:
        msg = "このサーバーではBOTを使用できません。"
    else:
        return True
    try:
        await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass
    return False


bot.tree.interaction_check = _tree_check  # スラッシュコマンド全体に適用


@bot.check
async def _global_ban_check(ctx):  # !コマンド全体に適用
    if ctx.author.id == OWNER_ID:
        return True
    return not (ctx.author.id in banned_users or (ctx.guild and ctx.guild.id in blacklisted_servers))


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, (commands.CheckFailure, commands.CommandNotFound)):
        return
    traceback.print_exception(type(error), error, error.__traceback__)


@bot.event
async def on_guild_join(guild: discord.Guild):
    if guild.id in blacklisted_servers:  # 出禁サーバーには入らない
        try:
            await guild.leave()
        except discord.HTTPException:
            pass


@bot.command(name="banned")
async def banned_list(ctx):
    if not can_manage(ctx.author):
        return
    lines = ["【使用不可サーバー】"]
    if blacklisted_servers:
        for sid, name in blacklisted_servers.items():
            inv = server_invites.get(sid)
            lines.append(f"ID: {sid}\nサーバー名: {name}\n招待リンク: {f'<{inv}>' if inv else '記録なし'}\n")
    else:
        lines.append("なし\n")
    lines.append("【使用不可ユーザー】")
    if banned_users:
        for uid, saved in banned_users.items():
            u = bot.get_user(uid)
            if u is None:
                try:
                    u = await bot.fetch_user(uid)
                except discord.HTTPException:
                    u = None
            lines.append(f"<@{uid}> / 表示名: {u.display_name if u else saved} / ID: {uid}")
    else:
        lines.append("なし")
    for chunk in split_text("\n".join(lines), 1900):
        await ctx.send(chunk, allowed_mentions=discord.AllowedMentions.none())  # メンション表示だけで通知はしない


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
    if not can_manage(ctx.author):
        return
    embed = generate_admin_embed()
    await ctx.send(embed=embed, view=AdminPanelView())

    
# ==========================================
# 🌐 Web連動 ダイヤ作成システム (/create)
#   フロー: /create → 使い捨てURL発行 → Webフォーム入力
#          → POST /api/submit は即応答 → BackgroundTasks で
#            備考の読み取り + ダイヤ計算 + Discord送信
# ==========================================
WEB_TOKEN_TTL = 1800  # 秒
MAX_TRAINS = 3000
web_tokens = {}  # {token: (user_id, expires_at)}

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
        raise HTTPException(status_code=401, detail="リンクの有効期限切れです。/create を再実行してください。")
    return v[0]


@bot.tree.command(name="create", description="ダイヤ作成機能")
async def create(interaction: discord.Interaction):
    # 使用不可ユーザー・出禁サーバーは _tree_check で弾かれる。それ以外は誰でも使える
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


# ---------- 備考欄 → パラメータ（ルールベースの読み取り。AIは使わない） ----------
def default_params() -> dict:
    return {
        "dwell_seconds": 30,
        "turnaround_minutes": 3,
        "express_every": 0,
        "express_name": "快速",
        "local_name": "普通",
        "express_speed": 1.0,        # 速達の区間所要時間の倍率
        "interval_minutes": 0,       # 発車間隔(分)。0=編成数から自動
        "station_dwell": {},         # {駅名: 停車秒}
        "express_stops": [],         # 速達の停車駅（空なら主要駅）
        "express_pass": [],          # 速達だけが通過する駅
        "extra_key": [],             # 備考で追加された主要駅
        "skip_stations": [],         # 全列車通過駅
        "segment_times": [],         # [{a, b, seconds}]
        "short_turn_station": "",    # 途中折返し駅
        "short_turn_every": 0,
        "extra_passing": [],         # 備考で追加された待避可能駅
        "special_sections": [],      # [{start, end, kind}]
        "service_range": None,       # {a, b}: 全列車が走る区間（始発駅/終着駅の指定）
        "trip_rules": [],            # 特定の列車を○○駅始発/止まりにする指定
        "start_time": "",            # 備考での開始/終了時刻の上書き
        "end_time": "",
        "applied": [],               # 実際に反映した内容
        "ignored": [],               # 反映できなかった内容
        "summary": "備考なし（既定値を使用）",
    }


def _norm(s) -> str:
    return re.sub(r"\s|駅$", "", str(s or "")).strip()


def resolve_station(raw, names: list[str]):
    """備考中の駅名を駅名リスト上の正式名称に対応付ける（「駅」の有無や表記ゆれを許容）。"""
    r = _norm(raw)
    if not r:
        return None
    for nm in names:
        if _norm(nm) == r:
            return nm
    cand = [nm for nm in names if r in _norm(nm) or _norm(nm) in r]
    return cand[0] if len(cand) == 1 else None


def _num(v, lo, hi):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return min(max(x, lo), hi)


def _lst(v):
    return v if isinstance(v, list) else []


# ----- 読み取り用の部品 -----
_SBASE = 0xE100                      # 駅名は私用領域の1文字(U+E100〜)に置き換えて扱う
_SC = "[-]"
_SL = f"{_SC}(?:(?:[・,、]|と|や|および|及び|か)?{_SC})*"   # 駅の並び（A・B・C / AとB）
_EXPN = "(?:[一-龥ァ-ヶー]{0,3}?(?:快速|急行|特急|準急|特快|速達|ライナー|エクスプレス))"


def _dur(p: str) -> str:
    """所要時間(「40秒」「3分」「1分30秒」)のパターン。グループ名は p_m / p_s / p_so。"""
    return (rf"(?:(?P<{p}_m>\d+(?:\.\d+)?)\s*分(?:\s*(?P<{p}_s>\d+)\s*秒)?|(?P<{p}_so>\d+(?:\.\d+)?)\s*秒)")


def _dur_sec(m, p: str) -> float:
    if m.group(f"{p}_so"):
        return float(m.group(f"{p}_so"))
    return float(m.group(f"{p}_m")) * 60 + (float(m.group(f"{p}_s")) if m.group(f"{p}_s") else 0)


_KAN = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _kan2int(s: str) -> int:
    if not any(c in s for c in "十百"):
        return int("".join(str(_KAN[c]) for c in s))
    total, cur = 0, 0
    for c in s:
        if c in _KAN:
            cur = _KAN[c]
        elif c == "十":
            total += (cur or 1) * 10
            cur = 0
        elif c == "百":
            total += (cur or 1) * 100
            cur = 0
    return total + cur


_NOT_STATION = ("主要", "全", "各", "途中", "始発", "終着", "終点", "起点", "待避", "退避", "通過", "停車",
                "次の", "前の", "隣の", "他の", "その", "この", "同じ", "どの", "各駅停車", "最寄り")

_FILLER = re.compile(
    r"[\s、。,.．・「」『』()（）\[\]【】:：;；!！?？〜~\-ー…]"
    r"|ください|下さい|お願い(?:します|いたします)?|にして|にする|して|する|します|したい|ほしい|欲しい|たい|ように|こと|もの|ため"
    r"|など|等|すべて|全て|全部|全列車|列車|電車|ダイヤ|作成|作って|また|あと|それと|さらに|なお|ただし|基本|ちなみに"
    r"|です|ます|ので|から|まで|だけ|のみ|その|この|は|が|を|に|で|と|も|の|へ|や|か|ね|よ")

_TIME = r"(\d{1,2})\s*(?::|時)\s*(\d{1,2})?\s*分?"
_PERIODS = {"早朝": (4, 6), "朝": (6, 10), "昼": (10, 16), "夕方": (16, 20), "夜": (20, 24), "深夜": (0, 4)}


def _hhmm(h, m):
    h, m = int(h), int(m or 0)
    if h == 24 and m == 0:
        h = 0
    if not (0 <= h < 24 and 0 <= m < 60):
        return None
    return f"{h:02d}:{m:02d}"


def parse_remarks(remarks: str, names: list[str]) -> dict:
    """備考(自由記述)を読み取り、ダイヤ作成用のパラメータにする。書き方のルールに沿った指示を実行する。"""
    p = default_params()
    text = unicodedata.normalize("NFKC", remarks or "").replace("~", "〜").replace("～", "〜")
    if not text.strip():
        return p
    ap, ig = p["applied"], p["ignored"]
    n = len(names)

    # --- 駅名を1文字の記号に置き換える ---
    key_of, order = {}, []
    for i, nm in enumerate(names):
        k = unicodedata.normalize("NFKC", _norm(nm)).lower()
        if k and k not in key_of:
            key_of[k] = i
            order.append(k)
    order.sort(key=len, reverse=True)

    def _alt(k: str) -> str:
        pre = r"(?<![A-Za-z0-9])" if re.match(r"[A-Za-z0-9]", k) else ""
        post = r"(?![A-Za-z0-9])" if re.search(r"[A-Za-z0-9]$", k) else ""
        return f"{pre}{re.escape(k)}{post}"

    if order:
        st_re = re.compile("(?:" + "|".join(_alt(k) for k in order) + ")駅?", re.I)
        text = st_re.sub(lambda m: chr(_SBASE + key_of[re.sub(r"駅$", "", m.group(0)).lower()]), text)

    def S(c: str) -> str:
        return names[ord(c) - _SBASE]

    def SL(s: str) -> list[str]:
        out = []
        for c in s:
            if "" <= c <= "" and ord(c) - _SBASE < n and S(c) not in out:
                out.append(S(c))
        return out

    # --- 漢数字 → 数字、「編成1、2」のような数字の並びを「・」でつなぐ ---
    text = re.sub(r"[〇零一二三四五六七八九十百]+(?=[本往分秒時編割番回両倍])", lambda m: str(_kan2int(m.group(0))), text)
    text = re.sub(r"(?<=\d)\s*[、,]\s*(?=\d)", "・", text)
    text = re.sub(r"(?<=\d)\s*(?:と|や)\s*(?=\d+\s*(?:番|編成))", "・", text)

    # --- 文ごとに分ける（駅名の並びの途中の読点では切らない） ---
    pieces = re.split(rf"[。\n;；]+|(?<!{_SC})、|、(?!{_SC})|(?<=[^\d])\s*[,，]\s*(?!{_SC})", text)
    pieces = [x.strip() for x in pieces if x and x.strip()]

    unknown_all = []

    def need_end(nm):  # 始終点は通過駅/途中折返しにできない
        return nm in (names[0], names[-1])

    for raw_piece in pieces:
        w = raw_piece
        raw_piece = re.sub(_SC, lambda m: S(m.group(0)), raw_piece)  # 表示用に駅名へ戻す
        matched = [False]

        def eat(pat, flags=0):
            m = re.search(pat, w, flags)
            if m:
                matched[0] = True
                return m
            return None

        def cut(m):
            nonlocal w
            w = w[:m.start()] + " " + w[m.end():]

        # 1. 特殊区間（単線 / 複々線）
        for pat in (rf"({_SC})(?:から|〜|-)({_SC})(?:間|まで)?(?:の区間)?(?:は|を|が|も)?(単線|複々線|複線)",
                    rf"(単線|複々線|複線)(?:区間)?(?:は|を|が)?({_SC})(?:から|〜)({_SC})(?:間|まで)?"):
            while True:
                m = eat(pat)
                if not m:
                    break
                cut(m)
                if m.group(1) in ("単線", "複々線", "複線"):
                    kind_s, a, b = m.group(1), S(m.group(2)), S(m.group(3))
                else:
                    a, b, kind_s = S(m.group(1)), S(m.group(2)), m.group(3)
                if a == b:
                    continue
                if kind_s == "複線":
                    ap.append(f"{a}〜{b}は複線（標準のため指定は不要です）")
                    continue
                kind = "single" if kind_s == "単線" else "quad"
                p["special_sections"].append({"start": a, "end": b, "kind": kind})
                ap.append(f"特殊区間 {a}〜{b} {kind_s}")

        # 2. 区間ごとの所要時間
        pat = (rf"({_SC})(?:から|〜|→|-)({_SC})(?:間|まで)?(?:の所要時間|の運転時間|の走行時間)?(?:は|を|が|で)?(?:約)?\s*" + _dur("d") +
               r"(?:にする|にして|とする|で|かかる)?")
        while True:
            m = eat(pat)
            if not m:
                break
            cut(m)
            a, b, sec = S(m.group(1)), S(m.group(2)), _dur_sec(m, "d")
            if a == b:
                continue
            if 10 <= sec <= 7200:
                p["segment_times"].append({"a": a, "b": b, "seconds": int(sec)})
                ap.append(f"区間所要時間 {a}〜{b} {int(sec)}秒")
            else:
                ig.append(f"{a}〜{b}の所要時間は10秒〜2時間の範囲で指定してください")

        # 3. 駅ごとの停車時間
        pat = (rf"({_SL})(?:では|は|で|の|も)?(?:停車時間(?:は|を)?|停車(?:は|を)?|停止(?:は)?)?\s*(?:約)?" + _dur("d") +
               r"(?:の)?(?:停車|停止|止まる|止まり)?(?:にする|にして|とする|で|ずつ)?")
        while True:
            m = eat(pat)
            if not m:
                break
            cut(m)
            sec = _dur_sec(m, "d")
            sts = SL(m.group(1))
            if 0 <= sec <= 600:
                for nm in sts:
                    p["station_dwell"][nm] = int(sec)
                ap.append(f"停車時間 {'・'.join(sts)} {int(sec)}秒")
            else:
                ig.append(f"{'・'.join(sts)}の停車時間は0〜600秒の範囲で指定してください")

        # 4. 全駅の停車時間（「他は20秒」も含む）
        for pat in (r"(?:全駅(?:で|の|は|とも)?|各駅(?:で|の|は)?)?(?:停車時間|停止時間|停車)(?:は|を|が)?(?:一律|各駅)?(?:約)?\s*" + _dur("d") +
                    r"(?:ずつ|にする|にして|とする|で)?",
                    r"(?:他|その他|ほか|それ以外)(?:の駅)?(?:は|で)?(?:停車)?\s*" + _dur("d") + r"(?:ずつ)?(?:停車)?(?:にする|にして|とする|で)?",
                    r"" + _dur("d") + r"(?:ずつ)?(?:停車|停止)(?:する|にする|にして)?"):
            m = eat(pat)
            if m:
                cut(m)
                sec = _dur_sec(m, "d")
                if 10 <= sec <= 180:
                    p["dwell_seconds"] = int(sec)
                    ap.append(f"全駅の停車時間 {int(sec)}秒")
                else:
                    ig.append("全駅の停車時間は10〜180秒の範囲で指定してください")
                break

        # 5. 折返し時間
        for pat in (rf"(?<!{_SC})(?:終点(?:での)?)?折り?返し(?:時間|に要する時間|にかかる時間)?(?:は|を|に|で)?(?:約)?\s*" + _dur("d") + r"(?:にする|にして|とする|で)?",
                    r"" + _dur("d") + r"(?:で|の)折り?返し"):
            m = eat(pat)
            if m:
                cut(m)
                sec = _dur_sec(m, "d")
                if 30 <= sec <= 600:
                    p["turnaround_minutes"] = round(sec / 60, 2) if sec % 60 else int(sec // 60)
                    ap.append(f"折返し時間 {p['turnaround_minutes']:g}分")
                else:
                    ig.append("折返し時間は30秒〜10分の範囲で指定してください")
                break

        # 6. 発車間隔
        for pat in (r"(?:発車)?間隔(?:は|を)?(?:約)?\s*(?P<v>\d+(?:\.\d+)?)\s*分(?:おき|ごと|毎)?(?:にする|にして|とする|で)?",
                    r"(?P<v>\d+(?:\.\d+)?)\s*分\s*(?:間隔|おき|ごと|毎)(?:に|で)?(?:運行|運転|発車)?(?:にする|にして|とする)?",
                    r"(?P<v>\d+(?:\.\d+)?)\s*分(?:に|毎に|ごとに)\s*1\s*本"):
            m = eat(pat)
            if m:
                cut(m)
                v = float(m.group("v"))
                if 1 <= v <= 60:
                    p["interval_minutes"] = v
                    ap.append(f"発車間隔 {v:g}分")
                else:
                    ig.append("発車間隔は1〜60分の範囲で指定してください")
                break

        # 7. 速達（快速など）
        for pat in (rf"(?P<n>{_EXPN})(?:を|は|が)?(?:の本数(?:は|を)?)?(?:約)?\s*(?P<e>\d+)\s*(?:往復|本)(?:に|毎に|ごとに|おきに?)\s*1\s*(?:本|往復)?(?:ずつ)?(?:走らせ(?:る|て)?|運行|運転|設定|入れ(?:る|て)?|にする|にして|で)?",
                    rf"(?P<e>\d+)\s*(?:往復|本)(?:に|毎に|ごとに)\s*1\s*(?:本|往復)(?:の|は|を)?(?P<n>{_EXPN})"):
            m = eat(pat)
            if m:
                cut(m)
                ev = int(m.group("e"))
                name = m.group("n").strip()
                if 1 <= ev <= 20:
                    p["express_every"] = ev
                    p["express_name"] = name
                    ap.append(f"{name}を{ev}往復に1本")
                else:
                    ig.append(f"{name}の本数は1〜20の範囲で指定してください")
                break
        m = eat(rf"(?:(?P<n>{_EXPN})(?:の)?(?:所要時間|運転時間)?(?:は|を)?)?(?:各停|各駅停車|普通)(?:より|の)\s*(?P<v>\d+(?:\.\d+)?)\s*(?P<u>割|%|パーセント|倍)(?P<t>程度|ほど|くらい)?(?P<w>速い|早い|短い|短縮|短く|速く|早く|減|少ない)?(?:する|にする|にして|で|になる)?")
        if m:
            cut(m)
            v, u, w_ = float(m.group("v")), m.group("u"), m.group("w")
            if u == "倍":
                f = v
            elif w_:                       # 「各停より1割速い」= 所要時間が1割短い
                f = 1 - (v / 10 if u == "割" else v / 100)
            else:                          # 「各停の9割」= 各停の9割の時間
                f = v / 10 if u == "割" else v / 100
            if 0.5 <= f < 1.0:
                p["express_speed"] = round(f, 2)
                if m.group("n"):
                    p["express_name"] = m.group("n").strip()
                ap.append(f"{p['express_name']}の所要時間は各停の{p['express_speed']:g}倍")
            else:
                ig.append("速達の所要時間は各停の0.5〜0.99倍の範囲で指定してください")
        m = eat(rf"(?P<n>{_EXPN})(?:の停車駅|は|では)?(?:は|を|:)?(?P<s>{_SL})以外(?:は|を|では)?通過")
        if m:
            cut(m)
            stops = SL(m.group("s"))
            p["express_stops"] = stops
            p["express_name"] = m.group("n").strip()
            ap.append(f"{p['express_name']}の停車駅: {'・'.join(stops)}（始終点は自動で停車）")
        for pat in (rf"(?P<n>{_EXPN})(?:の停車駅)?(?:は|では|を|:)?(?P<s>{_SL})(?:のみ|だけ)?(?:に)?(?:のみ|だけ)?(?:は)?(?:停車|止まる|停まる)(?:する|します|とする|にする)?",
                    rf"(?P<n>{_EXPN})の停車駅(?:は|を|:)?(?P<s>{_SL})(?:のみ|だけ)?(?:にする|とする|にして|です)?"):
            m = eat(pat)
            if m:
                cut(m)
                stops = SL(m.group("s"))
                p["express_stops"] = stops
                p["express_name"] = m.group("n").strip()
                ap.append(f"{p['express_name']}の停車駅: {'・'.join(stops)}（始終点は自動で停車）")
                break
        m = eat(rf"(?P<n>{_EXPN})(?:は|では)?(?P<s>{_SL})(?:を|は|が)?(?:通過|飛ば)(?:する|します|とする|にする)?")
        if m:
            cut(m)
            sts = [x for x in SL(m.group("s")) if not need_end(x)]
            p["express_name"] = m.group("n").strip()
            if sts:
                p["express_pass"].extend(x for x in sts if x not in p["express_pass"])
                ap.append(f"{p['express_name']}の通過駅: {'・'.join(sts)}")
            else:
                ig.append("始発駅・終着駅は通過にできません")
        m = eat(rf"(?P<n>{_EXPN})(?:は|では)?主要駅(?:のみ|だけ)?(?:に)?(?:のみ)?停車")
        if m:
            cut(m)
            p["express_name"] = m.group("n").strip()
            ap.append(f"{p['express_name']}は主要駅に停車（既定）")

        # 8. 全列車が通過する駅 / 主要駅の追加 / 待避可能駅の追加
        m = eat(rf"(?P<s>{_SL})(?:を|は|が|も)?(?:全列車|すべての列車|全ての列車|全種別|全部)?(?:は|が|も)?(?:通過|飛ば|停車しない|止まらない)(?:する|します|とする|にする|させる)?")
        if m:
            cut(m)
            sts = SL(m.group("s"))
            ok = [x for x in sts if not need_end(x)]
            if ok:
                p["skip_stations"].extend(x for x in ok if x not in p["skip_stations"])
                ap.append(f"全列車通過: {'・'.join(ok)}")
            if len(ok) < len(sts):
                ig.append("始発駅・終着駅は通過にできません")
        for pat in (rf"主要駅(?:は|に|を|として)?(?P<s>{_SL})(?:を|も)?(?:追加|加え|含め|にする|とする|にして|に加え)?(?:る|て)?",
                    rf"(?P<s>{_SL})(?:を|も|は)主要駅(?:に|として)?(?:追加|加え|含め|にする|とする|にして)?(?:る|て)?"):
            m = eat(pat)
            if m:
                cut(m)
                sts = SL(m.group("s"))
                p["extra_key"].extend(x for x in sts if x not in p["extra_key"])
                ap.append(f"主要駅に追加: {'・'.join(sts)}")
                break
        for pat in (rf"(?:待避|退避|追い越し|追越し?|追い抜き)(?:可能|できる)?(?:駅|設備)?(?:は|に|を|として)?(?P<s>{_SL})(?:を|も)?(?:追加|設定|にする|とする|にして)?",
                    rf"(?P<s>{_SL})(?:を|も|は|に)(?:待避|退避|追い越し|追越し?|追い抜き)(?:可能|できる|駅|設備)?(?:に|として)?(?:追加|設定|にする|とする|にして)?"):
            m = eat(pat)
            if m:
                cut(m)
                sts = SL(m.group("s"))
                p["extra_passing"].extend(x for x in sts if x not in p["extra_passing"])
                ap.append(f"待避可能駅に追加: {'・'.join(sts)}")
                break

        # 9. 開始・終了時刻
        pat_rng = (rf"(?:運行(?:時間|時刻)?(?:は)?)?{_TIME}\s*(?:〜|から)\s*{_TIME}\s*(?:まで)?(?:の間)?(?:で|に)?(?:運行|運転)")
        m = eat(pat_rng) or eat(rf"運行(?:時間|時刻)(?:は)?{_TIME}\s*(?:〜|から)\s*{_TIME}")
        if m:
            cut(m)
            a, b = _hhmm(m.group(1), m.group(2)), _hhmm(m.group(3), m.group(4))
            if a and b:
                p["start_time"], p["end_time"] = a, b
                ap.append(f"運行時間 {a}〜{b}")
            else:
                ig.append("運行時間の時刻を読み取れませんでした")
        for pat, key, label in (
                (rf"(?<!{_SC})(?:運行|ダイヤ)?(?:開始|始発|初発)(?:時刻|時間)?(?:は|を)?\s*{_TIME}(?:から|に|にする|とする|で)?", "start_time", "開始時刻"),
                (rf"{_TIME}\s*(?:から|より)(?:運行|運転|ダイヤ)?(?:を)?(?:開始|スタート|始め)", "start_time", "開始時刻"),
                (rf"(?:運行|ダイヤ)?(?:終了|終電|最終)(?:時刻|時間)?(?:は|を)?\s*{_TIME}(?:まで|に|にする|とする|で)?", "end_time", "終了時刻"),
                (rf"{_TIME}\s*(?:まで)(?:に)?(?:運行|運転|ダイヤ)?(?:を)?(?:終了|終わり|終える)", "end_time", "終了時刻")):
            m = eat(pat)
            if m:
                cut(m)
                v = _hhmm(m.group(1), m.group(2))
                if v:
                    p[key] = v
                    ap.append(f"{label} {v}")
                else:
                    ig.append(f"{label}を読み取れませんでした")

        # 10. 始発/止まりの指定（特定の列車 / 運行区間 / 途中折返し）
        P_A = rf"(?P<s>{_SC})(?:を|から|で)?(?:始発駅?)"
        P_B = rf"(?P<s>{_SC})(?:を|で|にて)?(?:止まり|止り|行き|ゆき|折り?返し|終着|終点)"
        P_A2 = rf"(?:始発駅?|起点|出発駅?)(?:は|を|:)\s*(?P<s>{_SC})"
        P_B2 = rf"(?:終着駅?|終点|終着)(?:は|を|:)\s*(?P<s>{_SC})"
        sA, sB, sA2, sB2 = (re.search(x, w) for x in (P_A, P_B, P_A2, P_B2))
        rng = re.search(rf"(?P<a>{_SC})(?:から|〜|-)(?P<b>{_SC})(?:まで)?(?:間)?(?:のみ|だけ|の区間)?(?:で|を|は)?(?:運行|運転|走る|走ります)", w) \
            or re.search(rf"運行区間(?:は|を|:)?\s*(?P<a>{_SC})(?:から|〜|-)(?P<b>{_SC})", w)
        if rng:
            matched[0] = True
            cut(rng)
            a, b = S(rng.group("a")), S(rng.group("b"))
            if a != b:
                lo, hi = sorted((names.index(a), names.index(b)))
                p["service_range"] = {"a": names[lo], "b": names[hi]}
                ap.append(f"運行区間 {names[lo]}〜{names[hi]}（始発駅・終着駅の指定）")
        elif sA or sB or sA2 or sB2:
            matched[0] = True
            start_nm = S((sA or sA2).group("s")) if (sA or sA2) else ""
            end_nm = S((sB or sB2).group("s")) if (sB or sB2) else ""
            conds, has_cond = [], False
            m = re.search(r"(?P<n>\d+)\s*(?P<u>本|往復)(?:に|毎に|ごとに|おきに?)\s*1\s*(?:本|往復)?", w)
            every, every_unit = 0, ""
            if m:
                every, every_unit = int(m.group("n")), m.group("u")
                w = w[:m.start()] + " " + w[m.end():]
                has_cond = True
            dr = "both"
            ds = set(re.findall(r"下り|上り", w))
            if len(ds) == 1:
                dr = "down" if "下り" in ds else "up"
                has_cond = True
            w = re.sub(r"下り|上り", " ", w)
            sets = []
            for pt in (r"(?:第)?編成\s*(\d+(?:・\d+)*)(?:番)?(?:目)?", r"(\d+(?:・\d+)*)\s*(?:号)?(?:編成|番編成)(?:目)?"):
                m = re.search(pt, w)
                if m:
                    sets = [int(x) for x in m.group(1).split("・") if 1 <= int(x) <= 50][:20]
                    w = w[:m.start()] + " " + w[m.end():]
                    has_cond = True
                    break
            tf = tt = None
            m = re.search(rf"{_TIME}\s*(?:〜|から)\s*{_TIME}\s*(?:の間|まで|の)?", w)
            if m:
                a_, b_ = _hhmm(m.group(1), m.group(2)), _hhmm(m.group(3), m.group(4))
                if a_ and b_:
                    tf, tt = _to_sec(a_), _to_sec(b_)
                    has_cond = True
                w = w[:m.start()] + " " + w[m.end():]
            else:
                m = re.search(r"(\d{1,2})\s*時台(?:の|に)?", w)
                if m and 0 <= int(m.group(1)) < 24:
                    tf, tt = int(m.group(1)) * 3600, int(m.group(1)) * 3600 + 3599
                    has_cond = True
                    w = w[:m.start()] + " " + w[m.end():]
                else:
                    m = re.search(r"(早朝|朝|昼|夕方|深夜|夜)(?:の|に)?", w)
                    if m:
                        h0, h1 = _PERIODS[m.group(1)]
                        tf, tt = h0 * 3600, h1 * 3600 - 1
                        has_cond = True
                        w = w[:m.start()] + " " + w[m.end():]
            if re.search(_EXPN, w):
                ig.append(f"「{raw_piece}」: 種別(快速など)を指定した始発/止まりの指定には対応していません")
                w = re.sub(_EXPN, " ", w)
                start_nm = end_nm = ""
            for x in (P_A, P_B, P_A2, P_B2):
                w = re.sub(x, " ", w)
            if start_nm and start_nm == end_nm:
                ig.append(f"「{raw_piece}」: 始発駅と終着駅が同じです")
            elif start_nm or end_nm:
                is_end_only = bool(end_nm) and not start_nm
                if not has_cond:
                    # 条件なし: 全列車の運行区間（始発駅/終着駅の指定）
                    a_i = names.index(start_nm) if start_nm else 0
                    b_i = names.index(end_nm) if end_nm else n - 1
                    cur = p["service_range"]
                    if cur:
                        if not start_nm:
                            a_i = names.index(cur["a"])
                        if not end_nm:
                            b_i = names.index(cur["b"])
                    lo, hi = sorted((a_i, b_i))
                    p["service_range"] = {"a": names[lo], "b": names[hi]}
                    ap.append(f"運行区間 {names[lo]}〜{names[hi]}（始発駅・終着駅の指定）")
                elif is_end_only and every_unit == "往復" and dr == "both" and not sets and tf is None:
                    if need_end(end_nm):
                        ig.append(f"「{raw_piece}」: 途中折返し駅に始発駅/終着駅は指定できません")
                    else:
                        p["short_turn_station"], p["short_turn_every"] = end_nm, every or 2
                        ap.append(f"{end_nm}止まりの折返し列車を{p['short_turn_every']}往復に1本")
                else:
                    if len(p["trip_rules"]) >= 10:
                        ig.append(f"「{raw_piece}」: 始発/止まりの指定は10件までです")
                    else:
                        rule = {"start": start_nm, "end": end_nm, "dir": dr, "every": every if every > 1 else 0,
                                "sets": sets, "from": tf, "to": tt}
                        cond = []
                        if dr != "both":
                            cond.append("下り" if dr == "down" else "上り")
                        if rule["every"] > 1:
                            cond.append(f"{rule['every']}本に1本")
                        if sets:
                            cond.append("編成" + "・".join(map(str, sets)))
                        if tf is not None:
                            cond.append(f"{_fmt(tf)}〜{_fmt(tt)}")
                        what = "、".join(x for x in (f"{start_nm}始発" if start_nm else "", f"{end_nm}止まり" if end_nm else "") if x)
                        rule["desc"] = (" ".join(cond) + " の列車を" if cond else "全列車を") + what
                        p["trip_rules"].append(rule)
                        ap.append(rule["desc"])

        # --- 読み取れなかった部分の報告 ---
        unk = [t for t in re.findall(r"([A-Za-z0-9一-龥ァ-ヶー]{1,10})駅", w)
               if not any(t.endswith(x) for x in _NOT_STATION)]
        if unk:
            unknown_all.extend(unk)
            ig.append(f"「{raw_piece}」: 駅名リストにない駅（{'・'.join(dict.fromkeys(unk))}）が含まれるため反映できませんでした")
            continue
        rest = _FILLER.sub("", re.sub(_SC, "", w))
        if len(rest) >= 2:
            ig.append(f"「{raw_piece}」: 読み取れませんでした" if not matched[0] else f"「{raw_piece}」: 一部を読み取れませんでした（{rest[:20]}）")

    if (p["express_stops"] or p["express_pass"] or p["express_speed"] < 1.0) and p["express_every"] == 0:
        ig.append(f"{p['express_name']}の停車駅/速度の指定がありますが本数の指定がないため走らせていません（例: 「{p['express_name']}を2往復に1本」）")
    p["summary"] = "、".join(ap) if ap else "備考から反映できる指定はありませんでした"
    return p


# ---------- 文字数制限に合わせた分割 ----------
def split_text(text: str, limit: int = 1900) -> list[str]:
    """Discord の文字数制限に収まるよう分割する。空行（=列車の区切り）→行→文字の順で切る。"""
    pieces = []
    for block in text.split("\n\n"):
        if len(block) <= limit:
            pieces.append(block)
            continue
        cur = ""
        for line in block.split("\n"):
            while len(line) > limit:
                if cur:
                    pieces.append(cur)
                    cur = ""
                pieces.append(line[:limit])
                line = line[limit:]
            if cur and len(cur) + len(line) + 1 > limit:
                pieces.append(cur)
                cur = line
            else:
                cur = f"{cur}\n{line}" if cur else line
        if cur:
            pieces.append(cur)
    chunks, cur = [], ""
    for pc in pieces:
        if cur and len(cur) + 2 + len(pc) > limit:
            chunks.append(cur)
            cur = pc
        else:
            cur = f"{cur}\n\n{pc}" if cur else pc
    if cur:
        chunks.append(cur)
    return [c for c in chunks if c.strip()]


# ---------- ダイヤ計算（純Python・軽量） ----------
def build_diagram(req: DiagramRequest, params: dict) -> tuple[str, str]:
    names = [s.name for s in req.stations]
    n = len(names)
    idx = {nm: i for i, nm in enumerate(names)}
    seg = [s.seconds for s in req.stations[:-1]]

    # 備考: 区間所要時間の上書き（複数区間なら元の比率で按分）
    for o in params["segment_times"]:
        lo, hi = sorted((idx[o["a"]], idx[o["b"]]))
        if hi - lo == 1:
            seg[lo] = o["seconds"]
        else:
            total = sum(seg[lo:hi])
            for i in range(lo, hi):
                seg[i] = round(o["seconds"] * seg[i] / total) if total > 0 else round(o["seconds"] / (hi - lo))

    dwell = params["dwell_seconds"]
    dwell_at = {idx[k]: v for k, v in params["station_dwell"].items()}
    turn = params["turnaround_minutes"] * 60
    exp_n, exp_f = params["express_every"], params["express_speed"]
    lo_b, hi_b = 0, n - 1  # 運行区間（備考の始発駅・終着駅の指定）
    if params["service_range"]:
        lo_b, hi_b = idx[params["service_range"]["a"]], idx[params["service_range"]["b"]]
    ends = {lo_b, hi_b}
    key_idx = {idx[k] for k in req.key_stations if k in idx} | {idx[k] for k in params["extra_key"]} | ends
    exp_idx = ({idx[k] for k in params["express_stops"]} | ends) if params["express_stops"] else key_idx
    exp_idx -= {idx[k] for k in params["express_pass"]} - ends  # 速達だけが通過する駅
    skip_idx = {idx[k] for k in params["skip_stations"]} - ends
    short_idx = idx.get(params["short_turn_station"])
    if short_idx is not None and not (lo_b < short_idx < hi_b):
        params["ignored"].append("途中折返し駅が運行区間の内側にないため、折返し列車は走らせていません")
        short_idx = None
    short_n = params["short_turn_every"]
    start_time = params["start_time"] or req.start_time
    end_time = params["end_time"] or req.end_time
    N = req.trainsets

    t0, t1 = _to_sec(start_time), _to_sec(end_time)
    if t1 <= t0:
        t1 += 86400  # 日またぎ

    def run_trip(direction: int, dep: int, express: bool, hi: int):
        order = list(range(lo_b, hi + 1)) if direction == 0 else list(range(hi, lo_b - 1, -1))
        last = len(order) - 1
        rows, t = [], dep
        for pos, i in enumerate(order):
            if pos == 0:
                rows.append((i, t, t, True))
                continue
            sec = seg[min(order[pos - 1], i)]
            t += int(sec * exp_f) if express else sec
            if pos == last:
                rows.append((i, t, t, True))
            elif i in skip_idx or (express and i not in exp_idx):
                rows.append((i, t, t, False))
            else:
                arr = t
                t += dwell_at.get(i, dwell)
                rows.append((i, arr, t, True))
        return rows

    one_way = sum(seg[lo_b:hi_b]) + sum(dwell_at.get(i, dwell) for i in range(lo_b + 1, hi_b))
    cycle = max(2 * (one_way + turn), 60)

    # 発車間隔の指定: 必要な編成数が足りていればその間隔で、足りなければ均等配置
    used, stagger, interval_note = N, cycle / N, ""
    if params["interval_minutes"] > 0:
        interval = int(params["interval_minutes"] * 60)
        want = max(1, math.ceil(cycle / interval))
        if want <= N:
            used, stagger = want, interval
        else:
            interval_note = (f"編成数不足: {params['interval_minutes']:g}分間隔には {want} 編成必要です"
                             f"（現在 {N}）。約{cycle / N / 60:.1f}分間隔で作成しました")

    trains = []
    for k in range(used):
        t, direction, j = t0 + int(k * stagger), 0, 0
        while t < t1 and len(trains) < MAX_TRAINS:
            rt = (j // 2) + k
            short = short_idx is not None and short_n > 0 and rt % short_n == 0
            express = exp_n > 0 and rt % exp_n == 0 and not short
            hi = short_idx if short else hi_b
            rows = run_trip(direction, t, express, hi)
            trains.append({"set": k + 1, "dir": direction, "express": express, "short": short, "rows": rows})
            t = rows[-1][1] + turn
            direction ^= 1
            j += 1
    trains.sort(key=lambda x: x["rows"][0][1])

    # 備考: 特定の列車を○○駅始発/止まりにする（全区間の時刻はそのままに、前後を切り落とす）
    rules = params["trip_rules"]
    rule_counts = [0] * len(rules)
    counters = [{0: 0, 1: 0} for _ in rules]
    for tr in trains:
        rows = tr["rows"]
        pos_of = {r[0]: pp for pp, r in enumerate(rows)}
        for ri, rl in enumerate(rules):
            if (rl["dir"] == "down" and tr["dir"] != 0) or (rl["dir"] == "up" and tr["dir"] != 1):
                continue
            s_i = idx[rl["start"]] if rl["start"] else None
            e_i = idx[rl["end"]] if rl["end"] else None
            if (s_i is not None and s_i not in pos_of) or (e_i is not None and e_i not in pos_of):
                continue
            sp = pos_of[s_i] if s_i is not None else 0
            ep = pos_of[e_i] if e_i is not None else len(rows) - 1
            if sp >= ep:
                continue
            if rl["sets"] and tr["set"] not in rl["sets"]:
                continue
            if rl["from"] is not None or rl["to"] is not None:
                x = (rows[sp][2] if s_i is not None else rows[ep][1]) % 86400
                f0, t0_ = (rl["from"] if rl["from"] is not None else 0), (rl["to"] if rl["to"] is not None else 86399)
                if not ((f0 <= x <= t0_) if f0 <= t0_ else (x >= f0 or x <= t0_)):
                    continue
            counters[ri][tr["dir"]] += 1
            if rl["every"] > 1 and (counters[ri][tr["dir"]] - 1) % rl["every"] != 0:
                continue
            new = rows[sp:ep + 1]
            new[0] = (new[0][0], new[0][2], new[0][2], True)
            new[-1] = (new[-1][0], new[-1][1], new[-1][1], True)
            tr["rows"] = new
            tr["tag"] = "・".join(x for x in (f"{names[s_i]}始発" if s_i is not None else "", f"{names[e_i]}止まり" if e_i is not None else "") if x)
            rule_counts[ri] += 1
            break
    if rules:
        trains.sort(key=lambda x: x["rows"][0][1])

    # 待避可能駅 / 特殊区間（フォームと備考の両方）
    covered = {idx[s] for s in [*req.passing_stations, *params["extra_passing"]] if s in idx}
    sections = [(q.start, q.end, q.kind) for q in req.quad_sections]
    sections += [(s["start"], s["end"], s["kind"]) for s in params["special_sections"]]
    singles = []
    for a, b, kind in sections:
        if a in idx and b in idx:
            lo, hi = sorted((idx[a], idx[b]))
            if kind == "single":
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

    # 追い抜き簡易判定（速達が直前の各停に追いつく地点が待避駅/複々線か）
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
        f"運行時間 {start_time}〜{end_time} / 編成数 {used} / 停車{dwell}秒 / 折返し{params['turnaround_minutes']}分",
        f"区間 {names[lo_b]} - {names[hi_b]} / 主要駅 {', '.join(names[i] for i in sorted(key_idx))}",
        f"備考の解釈: {params['summary']}",
        "",
    ]
    for no, tr in enumerate(trains, 1):
        if tr["express"]:
            kind = params["express_name"]
        else:
            kind = params["local_name"] + ("(途中折返し)" if tr["short"] else "")
        way = f"{names[tr['rows'][0][0]]}→{names[tr['rows'][-1][0]]}"
        tag = f"[{tr['tag']}]" if tr.get("tag") else ""
        L.append(f"[{no:03d}] 編成{tr['set']} {way} {kind}{tag}")
        for i, arr, dep, stop in tr["rows"]:
            if not stop:
                L.append(f"  {names[i]}: 通過 {_fmt(arr)}")
            elif arr == dep:
                L.append(f"  {names[i]}: {_fmt(arr)}")
            else:
                L.append(f"  {names[i]}: {_fmt(arr)}着 / {_fmt(dep)}発")
        L.append("")

    S = [f"ダイヤを作成しました。列車 {len(trains)} 本 / 編成 {used}（指定 {N}） / 追い抜き地点OK {ok_cnt} 件 / 要確認 {len(warns)} 件"]
    if params["applied"]:
        S.append("備考の反映:\n" + "\n".join(f"・{x}" for x in params["applied"]))
    if rules:
        S.append("列車指定の適用結果:\n" + "\n".join(
            f"・{rl['desc']}: {c} 本" if c else f"・{rl['desc']}: 該当する列車がありませんでした"
            for rl, c in zip(rules, rule_counts)))
    if params["ignored"]:
        S.append("反映できなかった内容:\n" + "\n".join(f"・{x}" for x in params["ignored"]))
    if interval_note:
        S.append(interval_note)
    if len(trains) >= MAX_TRAINS:
        S.append(f"※ 上限 {MAX_TRAINS} 本で打ち切りました。")
    if warns:
        S.append("要確認(簡易判定):\n" + "\n".join(f"・{w}" for w in warns[:8]))
    return "\n".join(L), "\n\n".join(S)


# ---------- バックグラウンドジョブ ----------
MAX_DIAGRAM_MESSAGES = 25  # これを超える長さならメッセージ分割ではなくファイルで送る


async def deliver(dest, summary: str, text: str):
    """要約とダイヤ本文を、文字数制限(2000)に収まるよう分割して送る。"""
    for c in split_text(summary, 1900):
        await dest.send(c)
    chunks = split_text(text, 1850)  # コードブロックの囲み分を引く
    if len(chunks) <= MAX_DIAGRAM_MESSAGES:
        for c in chunks:
            await dest.send(f"```\n{c}\n```")
    else:
        await dest.send(f"ダイヤが長く約{len(chunks)}通になるため、テキストファイルで送信します。")
        await dest.send(file=discord.File(io.BytesIO(text.encode("utf-8")), filename="diagram.txt"))


async def run_diagram_job(user_id: int, req: DiagramRequest):
    try:
        params = parse_remarks(req.remarks, [s.name for s in req.stations])  # ルールベースで読み取り（AI不使用）
        text, summary = await asyncio.to_thread(build_diagram, req, params)
        if req.target == "dm":
            dest = await bot.fetch_user(user_id)
        else:
            dest = bot.get_channel(int(req.channel_id)) or await bot.fetch_channel(int(req.channel_id))
        await deliver(dest, summary, text)
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
    if not getattr(bot, "_admin_view_added", False):
        bot.add_view(AdminPanelView())  # 再起動後も管理パネルの選択メニューが動くようにする
        bot._admin_view_added = True
    for g in list(bot.guilds):  # 出禁サーバーに残っていたら退出
        if g.id in blacklisted_servers:
            try:
                await g.leave()
            except discord.HTTPException:
                pass
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Failed to sync commands: {e}")


# ==========================================
# 🚀 起動: Webサーバー(FastAPI/uvicorn)と Bot を同一イベントループで同時実行
# ==========================================
async def main():
    await load_state()  # 起動のたびに Firebase の保存内容を読み込み、ログに表示
    server = uvicorn.Server(uvicorn.Config(api, host="0.0.0.0", port=PORT, log_level="warning"))
    async with bot:
        await asyncio.gather(server.serve(), bot.start(TOKEN))


if __name__ == "__main__":
    asyncio.run(main())
