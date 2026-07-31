# ==========================================
# 🐺 人狼ゲーム システム（game.py用完全版・3秒制限対策済み）
# ==========================================

import discord
import random
import asyncio

active_games = {} # { channel_id: GameSession }

class WolfLobbyView(discord.ui.View):
    def __init__(self, host):
        super().__init__(timeout=300.0)
        self.host = host
        self.joined = [host]

    def get_embed(self):
        return discord.Embed(
            title="🐺 人狼ゲーム 参加者募集中！",
            description=f"**ホスト:** {self.host.mention}\n**現在の参加者 ({len(self.joined)}人):**\n" + "\n".join([f"- {p.mention}" for p in self.joined]),
            color=discord.Color.blue()
        )

    @discord.ui.button(label="参加する", style=discord.ButtonStyle.success, custom_id="wolf_join")
    async def join_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        
        if interaction.user in self.joined:
            await interaction.followup.send("すでに参加しています！", ephemeral=True)
            return
        self.joined.append(interaction.user)
        # defer後の編集には followup を使う
        await interaction.followup.edit_message(message_id=interaction.message.id, embed=self.get_embed(), view=self)

    @discord.ui.button(label="スタート", style=discord.ButtonStyle.primary, custom_id="wolf_start")
    async def start_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        if interaction.user != self.host:
            await interaction.followup.send("ホストのみがスタートできます！", ephemeral=True)
            return
        
        is_admin_user = (interaction.user.id == 1510405214811852900)
        min_players = 1 if is_admin_user else 4

        if len(self.joined) < min_players:
            await interaction.followup.send("占い師を含めるため、最低4人必要です！", ephemeral=True)
            return
        
        for item in self.children:
            item.disabled = True
        
        start_embed = discord.Embed(
            title="🐺 人狼ゲームが開始されました！",
            description=f"参加者: {', '.join([p.mention for p in self.joined])}\n\n各プレイヤーの **DM** に役職を送信しました。確認してください！",
            color=discord.Color.dark_purple()
        )
        # defer後の編集には followup を使う
        await interaction.followup.edit_message(message_id=interaction.message.id, embed=start_embed, view=self)
        self.stop()

        session = WolfGameSession(interaction.channel, self.joined, self.host, interaction.client)
        active_games[interaction.channel.id] = session
        asyncio.create_task(session.run_game_loop())

class WolfGameSession:
    def __init__(self, channel, players, host, bot):
        self.channel = channel
        self.players = players
        self.host = host
        self.bot = bot
        self.is_running = True
        
        if len(players) == 1:
            roles_list = ["🐺 人狼"]
        else:
            roles_list = ["🐺 人狼", "🔮 占い師"] + ["🧑‍🌾 村人"] * (len(players) - 2)
            random.shuffle(roles_list)
            
        self.roles = dict(zip(players, roles_list))
        self.alive = list(players)
        self.day_count = 0

    def get_wolf_player(self):
        for p, role in self.roles.items():
            if role == "🐺 人狼":
                return p
        return None

    def get_seer_player(self):
        for p, role in self.roles.items():
            if role == "🔮 占い師":
                return p
        return None

    async def send_roles(self):
        for p, role in self.roles.items():
            try:
                await p.send(f"🔒 【役職通知】\n今回のあなたの役職は【 **{role}** 】です！この内容は他の人には秘密にしてください。")
            except:
                await self.channel.send(f"{p.mention} さんのDMが閉じているため役職を送信できませんでした！設定を確認してください。", delete_after=10)

    async def get_text_input(self, user, prompt_text, valid_targets):
        try:
            await user.send(prompt_text)
        except Exception:
            return None

        def check(m):
            if m.author != user or not isinstance(m.channel, discord.DMChannel):
                return False
            content = m.content.strip()
            for p in valid_targets:
                if (content == p.name or 
                    content == p.global_name or 
                    content == p.display_name or 
                    content == f"<@{p.id}>" or 
                    content == str(p.id)):
                    return True
            return False

        while self.is_running:
            try:
                msg = await self.bot.wait_for('message', check=check, timeout=60.0)
                content = msg.content.strip()
                for p in valid_targets:
                    if (content == p.name or 
                        content == p.global_name or 
                        content == p.display_name or 
                        content == f"<@{p.id}>" or 
                        content == str(p.id)):
                        return p
            except asyncio.TimeoutError:
                continue
            except Exception:
                break
        return None

    async def run_game_loop(self):
        await self.send_roles()

        await self.channel.send(embed=discord.Embed(
            title="🔒 役職の配布が完了しました",
            description="全員のDMに役職を送信しました。これより夜のフェーズが始まります！",
            color=discord.Color.dark_purple()
        ))

        while self.is_running:
            self.day_count += 1
            
            wolves = [p for p in self.alive if self.roles[p] == "🐺 人狼"]
            seer = self.get_seer_player()
            
            night_embed = discord.Embed(
                title=f"🌙 第{self.day_count}日目 - 夜が訪れました",
                description="村に暗闇が包み込みました...\n人狼と占い師はそれぞれの行動をDMで行ってください。",
                color=discord.Color.dark_purple()
            )
            await self.channel.send(embed=night_embed)

            if seer and seer in self.alive:
                seer_valid_targets = [p for p in self.alive if p != seer]
                if seer_valid_targets:
                    target_list_str = "\n".join([f"- {p.name} (表示名: {p.display_name})" for p in seer_valid_targets])
                    prompt = (
                        f"🔮 **【占い師の予言】**\n"
                        f"今夜、誰の正体を知りたいですか？対象の**ユーザーネーム**（または表示名）をDMに送信してください：\n\n"
                        f"**【生存者一覧】**\n{target_list_str}"
                    )
                    seer_target = await self.get_text_input(seer, prompt, seer_valid_targets)
                    if not self.is_running: break
                    
                    if seer_target:
                        target_role = self.roles[seer_target]
                        try:
                            await seer.send(f"🔮 【結果通知】\n**{seer_target.display_name}** の正体は 【 **{target_role}** 】 です。")
                        except:
                            pass

            killed_target = None
            attack_failed = False
            valid_targets = [p for p in self.alive if self.roles[p] != "🐺 人狼"]
            if not valid_targets:
                valid_targets = self.alive

            for wolf in wolves:
                if wolf not in self.alive:
                    continue
                
                target_list_str = "\n".join([f"- {p.name} (表示名: {p.display_name})" for p in valid_targets])
                prompt = (
                    f"🐺 **【人狼の夜襲】**\n"
                    f"今夜襲撃する相手の**ユーザーネーム**（または表示名）をDMに送信してください：\n\n"
                    f"**【生存者一覧】**\n{target_list_str}"
                )
                
                target = await self.get_text_input(wolf, prompt, valid_targets)
                if not self.is_running: break
                
                if target:
                    if random.random() < 0.25:
                        attack_failed = True
                    else:
                        killed_target = target
                        
                    try:
                        await wolf.send(f"✅ 【 **{target.display_name}** 】への襲撃を受け付けました。")
                    except:
                        pass
                    break

            if not self.is_running: break

            if not killed_target and not attack_failed and self.alive:
                killed_target = random.choice(valid_targets or self.alive)

            if attack_failed:
                morning_embed = discord.Embed(
                    title=f"☀️ 第{self.day_count}日目 - 朝が来ました",
                    description="昨夜、人狼が襲撃を試みましたが、ターゲットが反撃して人狼が致命傷を負いました…！\n\n本日の犠牲者はいません（襲撃失敗）。",
                    color=discord.Color.orange()
                )
                await self.channel.send(embed=morning_embed)
            else:
                if killed_target in self.alive:
                    self.alive.remove(killed_target)

                morning_embed = discord.Embed(
                    title=f"☀️ 第{self.day_count}日目 - 朝が来ました",
                    description=f"昨夜の犠牲者が発見されました...\n\n惨たらしい姿で発見されたのは **{killed_target.mention}** さんでした。",
                    color=discord.Color.orange()
                )
                await self.channel.send(content=killed_target.mention, embed=morning_embed)

            wolf_player = self.get_wolf_player()
            seer_player = self.get_seer_player()
            alive_wolves = [p for p in self.alive if self.roles[p] == "🐺 人狼"]

            if len(self.alive) == 2 and len(alive_wolves) == 1:
                seer_text = f"今回の占い師は **{seer_player.mention}** でした！" if seer_player else ""
                await self.channel.send(
                    f"🐺 **人狼陣営の勝利！**\n"
                    f"生き残ったのは人狼（{alive_wolves[0].mention}）と村人1人のみになりました！\n"
                    f"今回の人狼は **{wolf_player.mention}** でした！\n"
                    f"{seer_text}"
                )
                break

            if not alive_wolves:
                seer_text = f"今回の占い師は **{seer_player.mention}** でした！" if seer_player else ""
                await self.channel.send(
                    f"🎉 **村人陣営の勝利！**\n"
                    f"人狼が排除されました！\n"
                    f"今回の人狼は **{wolf_player.mention}** でした！\n"
                    f"{seer_text}"
                )
                break

            if not self.is_running: break

            disc_embed = discord.Embed(
                title="🗣️ 昼の議論タイム",
                description="生き残ったメンバーで自由に話し合い、誰が人狼か推理してください。\n（順番に各プレイヤーのDMに処刑投票用の案内が届きます）",
                color=discord.Color.green()
            )
            await self.channel.send(embed=disc_embed)

            votes = {}
            for voter in list(self.alive):
                valid_targets = [p for p in self.alive if p != voter]
                target_list_str = "\n".join([f"- {p.name} (表示名: {p.display_name})" for p in valid_targets])
                
                prompt = (
                    f"⚖️ **【処刑投票】**\n"
                    f"本日処刑するプレイヤーの**ユーザーネーム**（または表示名）をDMに送信してください：\n\n"
                    f"**【投票先候補】**\n{target_list_str}"
                )
                
                target = await self.get_text_input(voter, prompt, valid_targets)
                if not self.is_running: break
                
                if target:
                    votes[voter] = target
                    try:
                        await voter.send(f"✅ 【 **{target.display_name}** 】への投票を受け付けました。")
                    except:
                        pass

            if not self.is_running: break

            if votes:
                vote_counts = {}
                for target in votes.values():
                    vote_counts[target] = vote_counts.get(target, 0) + 1
                
                executed_target = max(vote_counts, key=vote_counts.get)
                if executed_target in self.alive:
                    self.alive.remove(executed_target)

                exec_embed = discord.Embed(
                    title="⚖️ 処刑結果",
                    description=f"村人たちの投票により、**{executed_target.mention}** さんが処刑されました。\n\n彼の正体は… 【 **{self.roles[executed_target]}** 】 でした！",
                    color=discord.Color.red()
                )
                await self.channel.send(embed=exec_embed)

                alive_wolves = [p for p in self.alive if self.roles[p] == "🐺 人狼"]

                if not alive_wolves:
                    seer_text = f"今回の占い師は **{seer_player.mention}** でした！" if seer_player else ""
                    await self.channel.send(
                        f"🎉 **村人陣営の勝利！**\n"
                        f"人狼を見つけ出して処刑しました！\n"
                        f"今回の人狼は **{wolf_player.mention}** でした！\n"
                        f"{seer_text}"
                    )
                    break

                if len(self.alive) == 2 and len(alive_wolves) == 1:
                    seer_text = f"今回の占い師は **{seer_player.mention}** でした！" if seer_player else ""
                    await self.channel.send(
                        f"🐺 **人狼陣営の勝利！**\n"
                        f"生き残ったのは人狼（{alive_wolves[0].mention}）と村人1人のみになりました！\n"
                        f"今回の人狼は **{wolf_player.mention}** でした！\n"
                        f"{seer_text}"
                    )
                    break
            else:
                await self.channel.send("有効な投票がなかったため、本日の処刑は見送られました。")

        if self.channel.id in active_games:
            del active_games[self.channel.id]

async def setup(bot):
    @bot.tree.command(name="wolfgame", description="人狼ゲームの募集を開始します")
    async def wolfgame_command(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)

        if interaction.channel.id in active_games:
            await interaction.followup.send("このチャンネルではすでに人狼ゲームが進行中です！", ephemeral=True)
            return
        
        view = WolfLobbyView(host=interaction.user)
        await interaction.followup.send(embed=view.get_embed(), view=view)

    @bot.tree.command(name="wolfend", description="進行中の人狼ゲームを強制終了します")
    async def wolfend_command(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)

        session = active_games.get(interaction.channel.id)
        if not session:
            await interaction.followup.send("このチャンネルで進行中の人狼ゲームはありません。", ephemeral=True)
            return
        if interaction.user != session.host and not interaction.user.guild_permissions.administrator:
            await interaction.followup.send("ゲームを強制終了できるのはホストまたは管理者のみです！", ephemeral=True)
            return
        
        session.is_running = False
        if interaction.channel.id in active_games:
            del active_games[interaction.channel.id]
        
        await interaction.followup.send("🛑 ホストによって人狼ゲームが強制終了されました。")
