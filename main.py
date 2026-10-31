import asyncio
import io
import json
import math
import traceback
import re
import secrets
from datetime import datetime
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
from discord.ext import commands

# ==========================================
# 🔑 環境変数（Render に登録するのはこの2つだけ）
#   DISCORD_BOT_TOKEN / GEMINI_API_KEY
#   ※ PORT・RENDER_EXTERNAL_URL は Render が自動設定する
# ==========================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
# 先頭が優先。タイムアウトや混雑のときは順に次のモデルへ切り替える（コード側で固定・環境変数は増やさない）
# 安定版モデルを優先。Gemini 3 系では旧 thinkingBudget / temperature を送らない。
GEMINI_MODELS = ["gemini-2.5-flash", "gemini-flash"]
BASE_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://free-create-v2-0-ubpy.onrender.com").rstrip("/")
PORT = int(os.environ.get("PORT", 8080))


def gemini_generate(prompt: str, system: str = "", json_mode: bool = False,
                    timeout: int = 20, budget: int = 60, google_search: bool = False) -> str:
  """Gemini REST を同期で呼ぶ。google_search=True の場合は Google 検索で根拠付けする。"""
  if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY が未設定です")

  body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
  if system:
    body["systemInstruction"] = {"parts": [{"text": system}]}
  if google_search:
    # Gemini API の Google Search grounding（検索結果の出典は groundingMetadata から取得）
    body["tools"] = [{"google_search": {}}]

  generation_config = {}
  if json_mode:
    # Gemini 3 系では temperature / thinkingBudget を指定しない。
    generation_config["responseMimeType"] = "application/json"
  if generation_config:
    body["generationConfig"] = generation_config

  deadline = time.time() + budget
  errors = []
  for model in GEMINI_MODELS:
    remain = deadline - time.time()
    if remain < 5:
      break
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    try:
      r = requests.post(url, json=body,
                        headers={"x-goog-api-key": GEMINI_API_KEY},
                        timeout=(8, min(timeout, remain)))
    except requests.exceptions.RequestException as e:
      errors.append(f"{model}: {type(e).__name__}")
      continue

    if r.status_code in (401, 403):
      raise RuntimeError(f"APIキーが無効か、権限がありません (HTTP {r.status_code})")
    if r.status_code in (400, 404, 429, 500, 502, 503, 504):
      # モデルごとの提供状況・一時的な制限を考慮して次候補へ
      try:
        detail = r.json().get("error", {}).get("message", "")
      except ValueError:
        detail = ""
      errors.append(f"{model}: HTTP {r.status_code}" + (f" ({detail[:90]})" if detail else ""))
      continue
    if not r.ok:
      raise RuntimeError(f"Gemini APIエラー (HTTP {r.status_code}): {r.text[:150]}")

    try:
      data = r.json()
      candidate = data["candidates"][0]
      parts = candidate["content"]["parts"]
      answer = "\n".join(p["text"] for p in parts if isinstance(p, dict) and "text" in p).strip()
      if not answer:
        raise ValueError("空の応答")

      if google_search:
        metadata = candidate.get("groundingMetadata") or {}
        chunks = metadata.get("groundingChunks") or []
        sources = []
        seen = set()
        for chunk in chunks:
          web = chunk.get("web") if isinstance(chunk, dict) else None
          if not isinstance(web, dict):
            continue
          uri = web.get("uri")
          title = web.get("title") or uri
          if uri and uri not in seen:
            seen.add(uri)
            sources.append(f"・[{str(title)[:100]}]({uri})")
          if len(sources) >= 5:
            break
        if sources:
          answer += "\n\n**参照元（Google検索）**\n" + "\n".join(sources)
      return answer
    except (KeyError, IndexError, TypeError, ValueError) as e:
      errors.append(f"{model}: 応答形式エラー ({type(e).__name__})")
      continue

  raise RuntimeError("Geminiから応答を得られませんでした: " + " / ".join(errors[-3:]))


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
# 🔍 検索コーナー（AI検索・wiki検索）
# ==========================================

# 5. AI検索 (/search)  ※ Gemini API を使用。OWNER_ID 以外は1分に1回まで
SEARCH_COOLDOWN = 60  # 秒
_search_last = {}  # {user_id: 最後に使った時刻}


@bot.tree.command(name="search", description="Google検索を使って質問や最新情報を調べます")
async def search(interaction: discord.Interaction, キーワード: str):
    uid = interaction.user.id
    limited = uid != OWNER_ID
    if limited:
        now = time.time()
        for k, t in list(_search_last.items()):  # 古い記録を掃除
            if now - t >= SEARCH_COOLDOWN:
                del _search_last[k]
        wait = SEARCH_COOLDOWN - (now - _search_last.get(uid, 0))
        if uid in _search_last and wait > 0:
            await interaction.response.send_message(f"/search は1分に1回まで使えます。あと{int(wait) + 1}秒お待ちください。", ephemeral=True)
            return
        _search_last[uid] = now

    await interaction.response.defer()
    
    try:
        answer_text = await asyncio.to_thread(
            gemini_generate,
            キーワード,
            "Google検索で確認できる最新情報を優先し、事実と推測を区別して日本語で簡潔に回答してください。検索結果に基づく出典リンクを最後に付けてください。",
            google_search=True,
        )
        
        if len(answer_text) > 1900:
            answer_text = answer_text[:1900] + "...\n（文字数オーバーのため省略しました）"

        embed = discord.Embed(
            title="🔍 Google検索結果 (Gemini)",
            description=f"**検索ワード:** {キーワード}\n\n{answer_text}",
            color=discord.Color.green()
        )
        await interaction.followup.send(embed=embed)
        
    except Exception as e:
        if limited:
            _search_last.pop(uid, None)  # 失敗した場合は回数を消費しない
        embed = discord.Embed(
            title="🔍 Google検索エラー",
            description=f"エラーが発生しました: `{str(e)[:300]}`\n" + (
                "(GEMINI_API_KEY を確認してください)" if "APIキー" in str(e) or "未設定" in str(e)
                else "(Geminiが混み合っている可能性があります。少し待ってからもう一度お試しください)"),
            color=discord.Color.red()
        )
        await interaction.followup.send(embed=embed)
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
#            Gemini解析 + ダイヤ計算 + Discord送信
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


# ---------- Gemini: 備考欄 → パラメータ ----------
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


PARSE_SCHEMA = (
    "{\n"
    '  "dwell_seconds": 全駅の標準停車時間(秒) または null,\n'
    '  "station_dwell": [{"station": 駅名, "seconds": 秒}],\n'
    '  "turnaround_minutes": 終点での折返し時間(分) または null,\n'
    '  "interval_minutes": 発車間隔(分) または null,\n'
    '  "express_every": 速達列車を何往復に1本走らせるか(整数) または null,\n'
    '  "express_name": 速達の種別名 または null,\n'
    '  "local_name": 各駅停車の種別名 または null,\n'
    '  "express_speed": 速達の区間所要時間の倍率(0.5〜1.0。各停より1割速いなら0.9) または null,\n'
    '  "express_stops": [速達の停車駅(始終点は自動で含まれる)],\n'
    '  "extra_key_stations": [主要駅に追加する駅],\n'
    '  "skip_stations": [全列車が通過する駅(始終点を除く)],\n'
    '  "segment_times": [{"from": 駅名, "to": 駅名, "seconds": その区間の所要秒}],\n'
    '  "short_turn_station": 途中で折り返す駅名 または null,\n'
    '  "short_turn_every": 途中折返し列車を何往復に1本走らせるか(整数) または null,\n'
    '  "extra_passing_stations": [待避可能駅に追加する駅],\n'
    '  "special_sections": [{"start": 駅名, "end": 駅名, "kind": "single" または "quad"}],\n'
    '  "service_range": {"from": 駅名, "to": 駅名} または null (全列車がこの2駅の間だけを走る。始発駅/終着駅の指定),\n'
    '  "trip_rules": [{"start": 始発にする駅名 または null, "end": 止まりにする駅名 または null, '
    '"direction": "down"(駅一覧の上から下へ向かう列車) / "up"(その逆) / "both", "every": 何本に1本か または null(全部), '
    '"sets": [対象の編成番号](空なら全編成), "from": "HH:MM" または null, "to": "HH:MM" または null (その駅を通る時刻帯)}],\n'
    '  "start_time": "HH:MM" または null,\n'
    '  "end_time": "HH:MM" または null,\n'
    '  "unknown_stations": [駅一覧に無い駅名],\n'
    '  "ignored": [上のキーで表現できず対応できなかった指示の要約]\n'
    "}"
)


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


def sanitize_params(d: dict, names: list[str]) -> dict:
    """Gemini の出力を検証し、駅名を正式名称に直して、実際に使える値だけを残す。"""
    p = default_params()
    ap, ig = p["applied"], p["ignored"]
    unknown = []

    def st(raw):
        nm = resolve_station(raw, names)
        if nm is None and _norm(raw):
            unknown.append(str(raw)[:20])
        return nm

    def st_list(v):
        out = []
        for x in _lst(v):
            nm = st(x)
            if nm and nm not in out:
                out.append(nm)
        return out

    ends = (names[0], names[-1])

    x = _num(d.get("dwell_seconds"), 10, 180)
    if x is not None:
        p["dwell_seconds"] = int(x)
        ap.append(f"全駅の停車時間 {int(x)}秒")
    x = _num(d.get("turnaround_minutes"), 1, 10)
    if x is not None:
        p["turnaround_minutes"] = int(round(x))
        ap.append(f"折返し時間 {p['turnaround_minutes']}分")
    x = _num(d.get("interval_minutes"), 1, 60)
    if x is not None:
        p["interval_minutes"] = x
        ap.append(f"発車間隔 {x:g}分")

    en = str(d.get("express_name") or "").strip()[:10]
    ln = str(d.get("local_name") or "").strip()[:10]
    if en:
        p["express_name"] = en
    if ln:
        p["local_name"] = ln
    x = _num(d.get("express_every"), 0, 20)
    if x is not None and int(x) > 0:
        p["express_every"] = int(x)
        ap.append(f"{p['express_name']}を{int(x)}往復に1本")
    x = _num(d.get("express_speed"), 0.5, 1.0)
    if x is not None and x < 1.0:
        p["express_speed"] = round(x, 2)
        ap.append(f"{p['express_name']}の所要時間 x{p['express_speed']}")
    stops = st_list(d.get("express_stops"))
    if stops:
        p["express_stops"] = stops
        ap.append(f"{p['express_name']}の停車駅: {', '.join(stops)}（始終点は自動で停車）")
    if (p["express_stops"] or p["express_speed"] < 1.0) and p["express_every"] == 0:
        ig.append(f"{p['express_name']}の停車駅/速度の指定がありますが本数の指定がないため走らせていません（例: 「{p['express_name']}を2往復に1本」）")

    keys = st_list(d.get("extra_key_stations"))
    if keys:
        p["extra_key"] = keys
        ap.append(f"主要駅に追加: {', '.join(keys)}")
    skips = [s for s in st_list(d.get("skip_stations")) if s not in ends]
    if skips:
        p["skip_stations"] = skips
        ap.append(f"全列車通過: {', '.join(skips)}")

    sd = {}
    for o in _lst(d.get("station_dwell")):
        if isinstance(o, dict):
            nm, sec = st(o.get("station")), _num(o.get("seconds"), 0, 600)
            if nm and sec is not None:
                sd[nm] = int(sec)
    if sd:
        p["station_dwell"] = sd
        ap.append("駅別の停車時間: " + ", ".join(f"{k} {v}秒" for k, v in sd.items()))

    for o in _lst(d.get("segment_times")):
        if isinstance(o, dict):
            a, b, sec = st(o.get("from")), st(o.get("to")), _num(o.get("seconds"), 10, 7200)
            if a and b and a != b and sec is not None:
                p["segment_times"].append({"a": a, "b": b, "seconds": int(sec)})
    if p["segment_times"]:
        ap.append("区間所要時間: " + ", ".join(f"{o['a']}〜{o['b']} {o['seconds']}秒" for o in p["segment_times"]))

    if d.get("short_turn_station"):
        nm = st(d.get("short_turn_station"))
        if nm and nm not in ends:
            ev = _num(d.get("short_turn_every"), 1, 20)
            p["short_turn_station"] = nm
            p["short_turn_every"] = int(ev) if ev else 2
            ap.append(f"{nm}止まりの折返し列車を{p['short_turn_every']}往復に1本" + ("" if ev else "（本数の指定がないため2往復に1本）"))
        elif nm:
            ig.append("途中折返し駅に始発駅/終着駅は指定できません")

    ps = st_list(d.get("extra_passing_stations"))
    if ps:
        p["extra_passing"] = ps
        ap.append(f"待避可能駅に追加: {', '.join(ps)}")

    for o in _lst(d.get("special_sections")):
        if isinstance(o, dict):
            a, b, kind = st(o.get("start")), st(o.get("end")), o.get("kind")
            if a and b and a != b and kind in ("single", "quad"):
                p["special_sections"].append({"start": a, "end": b, "kind": kind})
    if p["special_sections"]:
        ap.append("特殊区間: " + ", ".join(f"{o['start']}〜{o['end']} {'単線' if o['kind'] == 'single' else '複々線'}" for o in p["special_sections"]))

    sr = d.get("service_range")
    if isinstance(sr, dict):
        a, b = st(sr.get("from")), st(sr.get("to"))
        if a and b and a != b:
            lo, hi = sorted((names.index(a), names.index(b)))
            p["service_range"] = {"a": names[lo], "b": names[hi]}
            ap.append(f"運行区間: {names[lo]}〜{names[hi]}（始発駅・終着駅の指定）")

    def _tsec(v):
        try:
            return _to_sec(str(v)) if v else None
        except ValueError:
            return None

    for o in _lst(d.get("trip_rules"))[:10]:
        if not isinstance(o, dict):
            continue
        s0 = st(o.get("start")) if o.get("start") else ""
        e0 = st(o.get("end")) if o.get("end") else ""
        if not (s0 or e0) or (s0 and s0 == e0):
            continue
        dr = o.get("direction") if o.get("direction") in ("down", "up") else "both"
        ev = _num(o.get("every"), 1, 50)
        sets = [int(x) for x in _lst(o.get("sets")) if _num(x, 1, 50) is not None][:20]
        tf, tt = _tsec(o.get("from")), _tsec(o.get("to"))
        rule = {"start": s0, "end": e0, "dir": dr, "every": int(ev) if ev else 0, "sets": sets, "from": tf, "to": tt}
        cond = []
        if dr != "both":
            cond.append("下り" if dr == "down" else "上り")
        if rule["every"] > 1:
            cond.append(f"{rule['every']}本に1本")
        if sets:
            cond.append("編成" + "・".join(map(str, sets)))
        if tf is not None or tt is not None:
            cond.append(f"{_fmt(tf or 0)}〜{_fmt(tt) if tt is not None else '終日'}")
        what = "、".join(x for x in (f"{s0}始発" if s0 else "", f"{e0}止まり" if e0 else "") if x)
        rule["desc"] = (" ".join(cond) + " の列車を" if cond else "全列車を") + what
        p["trip_rules"].append(rule)
        ap.append(rule["desc"])

    for key, label in (("start_time", "開始時刻"), ("end_time", "終了時刻")):
        v = str(d.get(key) or "").strip()
        if v:
            try:
                p[key] = _fmt(_to_sec(v))
                ap.append(f"{label} {p[key]}")
            except ValueError:
                ig.append(f"{label}「{v}」は HH:MM 形式として読み取れませんでした")

    unk = list(dict.fromkeys(unknown + [str(x)[:20] for x in _lst(d.get("unknown_stations")) if x]))
    if unk:
        ig.append("駅名リストにない駅: " + ", ".join(unk))
    ig.extend(str(x)[:80] for x in _lst(d.get("ignored"))[:8] if x)

    p["summary"] = "、".join(ap) if ap else "備考から反映できる指定はありませんでした"
    return p


async def parse_remarks(remarks: str, names: list[str], info: str = "") -> dict:
    p = default_params()
    if not remarks.strip():
        return p
    if not GEMINI_API_KEY:
        p["ignored"].append("GEMINI_API_KEY が未設定のため備考は反映されていません")
        p["summary"] = "備考は反映されていません"
        return p
    system = "あなたは鉄道ダイヤ作成システムの入力解析器です。備考の指示を指定のJSONに変換し、JSONのみを返してください。"
    prompt = (
        f"駅一覧(路線の上から順): {', '.join(names)}\n"
        f"フォーム設定: {info}\n"
        f"備考:\n{remarks}\n\n"
        "ルール:\n"
        "- 備考中の駅名は必ず駅一覧の表記に直して出力する(「駅」の有無や表記ゆれを補正)。駅一覧に無い駅名は unknown_stations に入れる。\n"
        "- 備考に書かれていない項目は null か空配列にする。推測で埋めない。\n"
        "- 下のキーで表現できない指示は、内容を短く ignored に入れる。\n"
        "- 全列車が走る区間(始発駅・終着駅の指定)は service_range、特定の列車(何本に1本・編成・時間帯)を途中の駅始発/止まりにする指示は trip_rules に入れる。\n"
        "  例: 「C駅始発を3本に1本」→ start=C, every=3, direction=both。「7時台の下りはD駅止まり」→ end=D, direction=down, from=07:00, to=07:59。\n"
        "出力JSON:\n" + PARSE_SCHEMA
    )
    last = None
    for _ in range(1):  # モデルの切り替えは gemini_generate 内で行う
        try:
            raw = await asyncio.wait_for(asyncio.to_thread(gemini_generate, prompt, system, True, 20, 50), timeout=60)
            raw = raw.strip()
            if raw.startswith("```"):
                raw = re.sub(r"^```(?:json)?|```$", "", raw).strip()
            d = json.loads(raw)
            if isinstance(d, list) and d:
                d = d[0]
            if not isinstance(d, dict):
                raise ValueError("JSON形式が不正")
            return sanitize_params(d, names)
        except Exception as e:
            last = e
            await asyncio.sleep(1.5)
    p["ignored"].append(f"備考の解析に失敗したため反映されていません ({str(last)[:150] or type(last).__name__})")
    p["summary"] = "備考は反映されていません"
    return p


def _load_json_obj(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?|```$", "", raw).strip()
    d = json.loads(raw)
    if isinstance(d, list) and d:
        d = d[0]
    if not isinstance(d, dict):
        raise ValueError("JSON形式が不正")
    return d


AI_SYSTEM = "あなたは鉄道ダイヤ作成フォームの設定アシスタントです。ユーザーの依頼に合わせてフォームの設定を作り、指定のJSONのみを返してください。"

AI_FORM_GUIDE = (
    "フォームの項目(formのキー):\n"
    '- stations: [{"name": 駅名, "seconds": 次の駅までの所要時間(秒)}] 路線の上から順。最後の駅のsecondsは0。\n'
    "- key_stations: 主要駅の駅名の配列(速達が停車する駅)\n"
    '- start_time / end_time: "HH:MM"(運行の開始/終了時刻)\n'
    "- trainsets: 編成数(1〜50の整数)\n"
    "- remarks: 備考(自由記述)。次のような指示を日本語で書くと、ダイヤ作成時に自動で実行される: "
    "停車時間(全駅/駅別)、折返し時間、発車間隔、快速など速達の本数・停車駅・速度、通過駅、区間ごとの所要時間、"
    "途中駅止まりの列車、始発駅/終着駅の指定(運行区間)、特定の列車(何本に1本・編成・時間帯)を○○駅始発/止まりにする、"
    "待避可能駅や単線/複々線区間の追加、開始/終了時刻\n"
    "- passing_stations: 待避可能駅の駅名の配列\n"
    '- special_sections: [{"start": 駅名, "end": 駅名, "kind": "single"(単線) または "quad"(複々線)}] ここに載せない区間は複線\n'
    "出力先(DM/チャンネル)は設定できません。ユーザー自身が選びます。\n"
)

AI_OUTPUT_RULES = (
    '出力JSON: {"reply": 日本語の返答, "form": {変更する項目だけ}}\n'
    "ルール:\n"
    "- 変更しない項目は form に含めない。stations を変更するときは全駅を含む完全なリストにする。"
    "remarks を変更するときは、残したい既存の内容も含めた全文にする。\n"
    "- 駅名や所要時間の指定がなく実在路線を求められたら、知っている範囲で作る。所要時間は推定で不正確な可能性があると reply で伝える。\n"
    "- 情報が足りないときは推測で埋めすぎず、reply で質問する。\n"
    "- reply は簡潔な日本語で、設定した内容を要約する。絵文字は使わない。\n"
)


def build_ai_prompt(message: str, history: list, form_json: str) -> str:
    hist = "\n".join(f"{'ユーザー' if m.role == 'user' else 'AI'}: {m.text}" for m in history[-10:]) or "なし"
    return (f"現在のフォームの内容(JSON):\n{form_json}\n\n" + AI_FORM_GUIDE
            + f"\nこれまでの会話:\n{hist}\n\nユーザーの依頼:\n{message}\n\n" + AI_OUTPUT_RULES)


def sanitize_ai_form(raw, cur_names: list[str]) -> dict:
    """AI が返したフォーム設定を検証し、使える項目だけを返す。"""
    out = {}
    if not isinstance(raw, dict):
        return out
    names = list(cur_names)
    sts = raw.get("stations")
    if isinstance(sts, list):
        rows = []
        for o in sts[:60]:
            if isinstance(o, dict):
                nm = str(o.get("name") or "").strip()[:40]
                sec = _num(o.get("seconds"), 0, 7200)
                if nm and nm not in [r["name"] for r in rows]:
                    rows.append({"name": nm, "seconds": int(sec) if sec is not None else 0})
        if len(rows) >= 2:
            out["stations"] = rows
            names = [r["name"] for r in rows]

    def pick(v):
        res = []
        for x in _lst(v):
            nm = resolve_station(x, names)
            if nm and nm not in res:
                res.append(nm)
        return res

    if raw.get("key_stations") is not None:
        out["key_stations"] = pick(raw.get("key_stations"))
    if raw.get("passing_stations") is not None:
        out["passing_stations"] = pick(raw.get("passing_stations"))
    if raw.get("special_sections") is not None:
        secs = []
        for o in _lst(raw.get("special_sections"))[:20]:
            if isinstance(o, dict):
                a, b = resolve_station(o.get("start"), names), resolve_station(o.get("end"), names)
                if a and b and a != b and o.get("kind") in ("single", "quad"):
                    secs.append({"start": a, "end": b, "kind": o["kind"]})
        out["special_sections"] = secs
    for key in ("start_time", "end_time"):
        v = str(raw.get(key) or "").strip()
        if v:
            try:
                out[key] = _fmt(_to_sec(v))
            except ValueError:
                pass
    x = _num(raw.get("trainsets"), 1, 50)
    if x is not None:
        out["trainsets"] = int(x)
    if isinstance(raw.get("remarks"), str):
        out["remarks"] = raw["remarks"].strip()[:1000]
    return out


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
        info = (f"開始 {req.start_time} / 終了 {req.end_time} / 編成数 {req.trainsets}"
                f" / 主要駅 {', '.join(req.key_stations) or 'なし'}")
        params = await parse_remarks(req.remarks, [s.name for s in req.stations], info)
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


# ---------- AI アシスタント（フォームの設定を Gemini が作る） ----------
class AIMsg(BaseModel):
    role: str
    text: str = Field("", max_length=2000)


class AIRequest(BaseModel):
    message: str = Field(min_length=1, max_length=1000)
    history: list[AIMsg] = Field(default=[], max_length=10)
    form: dict = {}


@api.post("/api/ai")
async def api_ai(req: AIRequest, t: str = ""):
    verify_web_token(t)
    if not GEMINI_API_KEY:
        raise HTTPException(503, "GEMINI_API_KEY が未設定のため、AIは使えません。")
    form_json = json.dumps(req.form, ensure_ascii=False)
    if len(form_json) > 20000:
        raise HTTPException(400, "フォームの内容が大きすぎます。")
    cur_names = [str(x.get("name", "")).strip() for x in _lst(req.form.get("stations"))
                 if isinstance(x, dict) and str(x.get("name", "")).strip()]
    prompt = build_ai_prompt(req.message, req.history, form_json)
    last = None
    for _ in range(1):  # モデルの切り替えは gemini_generate 内で行う
        try:
            raw = await asyncio.wait_for(asyncio.to_thread(gemini_generate, prompt, AI_SYSTEM, True, 20, 50), timeout=60)
            d = _load_json_obj(raw)
            return {"reply": str(d.get("reply") or "")[:1500], "form": sanitize_ai_form(d.get("form"), cur_names)}
        except Exception as e:
            last = e
            await asyncio.sleep(1)
    raise HTTPException(502, f"AIの応答に失敗しました: {str(last)[:150] or type(last).__name__}")


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
