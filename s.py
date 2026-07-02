"""
noi_tu_selfbot.py

NOTE ON THE LIBRARY: modern discord.py removed the `self_bot=True` param
entirely -- Discord doesn't allow user-account automation through the
official library. If this script is meant to log in with a *user* token
(not a real bot token), you need the community fork instead:

    pip uninstall discord.py
    pip install -U discord.py-self

Heads up: automating a personal Discord account like this is against
Discord's Terms of Service and can get the account disabled. Worth
knowing before you leave this running unattended.
"""

# -*- coding: utf-8 -*-

import os
import re
import time
import random
import asyncio
import unicodedata
from collections import defaultdict
from datetime import datetime

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID_RAW = os.getenv("CHANNEL_ID")

WORDS_FILE = "vietnamese_words.txt"
LOG_FILE = "channel_messages.log"

GAME_MASTER_BOT_ID = 1103932552701550622
REQUIRED_REACTIONS = 2
MIN_SEND_INTERVAL = 120


def normalize(text: str) -> str:
    return unicodedata.normalize("NFC", text).strip().lower()


class NoiTuSelfbot:
    def __init__(self):
        if not DISCORD_TOKEN:
            raise SystemExit("DISCORD_TOKEN missing from .env")
        if not CHANNEL_ID_RAW:
            raise SystemExit("CHANNEL_ID missing from .env")

        self.token = DISCORD_TOKEN
        self.channel_id = int(CHANNEL_ID_RAW)

        self.phrases: set[str] = set()
        self.phrases_by_first_syllable: dict[str, list[str]] = defaultdict(list)

        self.last_word: str | None = None
        self.last_word_message_id: int | None = None
        self.used_words: set[str] = set()
        self.xd_messages: set[int] = set()
        self.validated_messages: set[int] = set()

        self.word_ready = asyncio.Event()
        self.last_send_time = 0.0

        self.client = commands.Bot(command_prefix="nt!", help_command=None)

        self.client.event(self.on_ready)
        self.client.event(self.on_message)
        self.client.event(self.on_reaction_add)

    MAX_SYLLABLES = 2

    async def load_words(self):
        skipped = 0
        try:
            with open(WORDS_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    phrase = normalize(line)
                    if not phrase:
                        continue
                    syllables = phrase.split()
                    if len(syllables) != self.MAX_SYLLABLES:
                        skipped += 1
                        continue
                    self.phrases.add(phrase)
                    self.phrases_by_first_syllable[syllables[0]].append(phrase)
            print(f"Loaded {len(self.phrases)} 2-syllable words (skipped {skipped})")
        except FileNotFoundError:
            print(f"{WORDS_FILE} not found!")
            raise SystemExit(1)

    def get_next_word(self, last_phrase: str) -> str | None:
        if not last_phrase:
            return None
        last_phrase = normalize(last_phrase)
        syllables = last_phrase.split()
        if not syllables:
            return None

        last_syllable = syllables[-1]
        candidates = self.phrases_by_first_syllable.get(last_syllable, [])
        if not candidates:
            return None

        fresh = [c for c in candidates if c != last_phrase and c not in self.used_words]
        pool = fresh or [c for c in candidates if c != last_phrase] or candidates
        return random.choice(pool)

    def find_last_valid_phrase(self, content: str) -> str | None:
        text = normalize(content)
        tokens = re.findall(r"\w+", text, re.UNICODE)
        if not tokens:
            return None

        last_tokens = tokens[-self.MAX_SYLLABLES:]
        if len(last_tokens) != self.MAX_SYLLABLES:
            return None

        phrase = " ".join(last_tokens)
        if phrase in self.phrases:
            return phrase
        return None

    @staticmethod
    def _display_author(message: discord.Message) -> str:
        author = message.author
        disc = getattr(author, "discriminator", "0")
        if not disc or disc == "0":
            return author.name
        return f"{author.name}#{disc}"

    async def check_bot_x_reaction(self, message: discord.Message) -> bool:
        for reaction in message.reactions:
            if str(reaction.emoji) != "X":
                continue
            async for user in reaction.users():
                if user.id == self.client.user.id:
                    return True
        return False

    def log_message(self, message: discord.Message):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        content = message.content.replace("\n", " ")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {self._display_author(message)}: {content}\n")

    def log_reaction(self, message: discord.Message, count: int):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        content = message.content.replace("\n", " ")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] VALIDATED ({count} reactions): "
                    f"{self._display_author(message)}: {content}\n")

    async def on_ready(self):
        print(f"Logged in as {self.client.user} (ID: {self.client.user.id})")
        print(f"Targeting channel ID {self.channel_id}")
        self.client.loop.create_task(self.game_loop())
        self.client.loop.create_task(self.heartbeat_check())

    async def heartbeat_check(self):
        await self.client.wait_until_ready()
        while not self.client.is_closed():
            await asyncio.sleep(30)
            print(f"[heartbeat] last_word: {self.last_word} | "
                  f"last_msg_id: {self.last_word_message_id} | "
                  f"xd: {len(self.xd_messages)} | validated: {len(self.validated_messages)}")

    async def _evaluate_message(
        self, message: discord.Message, allow_update: bool = True
    ) -> bool:
        if message.author.id == self.client.user.id:
            return False
        if message.id in self.xd_messages or message.id in self.validated_messages:
            return False
        if await self.check_bot_x_reaction(message):
            self.xd_messages.add(message.id)
            return False

        total_reactions = sum(r.count for r in message.reactions)
        if total_reactions < REQUIRED_REACTIONS:
            return False

        phrase = self.find_last_valid_phrase(message.content)
        if not phrase:
            return False

        self.validated_messages.add(message.id)
        self.log_reaction(message, total_reactions)
        print(f"Message {message.id} validated ({total_reactions} reactions) -> '{phrase}'")

        if allow_update:
            if (
                self.last_word_message_id is None
                or message.id > self.last_word_message_id
            ):
                self.last_word = phrase
                self.last_word_message_id = message.id
                self.word_ready.set()
                print(f"  -> last_word updated to '{phrase}' (msg_id={message.id})")
            else:
                print(f"  -> skipped (message {message.id} is older than "
                      f"current last_word from msg {self.last_word_message_id})")
                return False
        return True

    async def on_reaction_add(self, reaction: discord.Reaction, user: discord.User):
        message = reaction.message
        if message.channel.id != self.channel_id:
            return

        if user.id == self.client.user.id and str(reaction.emoji) == "X":
            self.xd_messages.add(message.id)
            self.validated_messages.discard(message.id)
            print(f"Bot X'd message {message.id} from {message.author.name}")
            if self.last_word:
                phrase = self.find_last_valid_phrase(message.content)
                if (
                    phrase
                    and phrase == self.last_word
                    and message.id == self.last_word_message_id
                ):
                    self.last_word = None
                    self.last_word_message_id = None
                    print("Cleared last_word because bot X'd the validated message")
            return

        if message.author.id == self.client.user.id:
            return

        await self._evaluate_message(message)

    async def on_message(self, message: discord.Message):
        if message.channel.id != self.channel_id:
            return
        if message.author.id == self.client.user.id:
            return
        if message.id in self.xd_messages:
            return

        if message.author.id == GAME_MASTER_BOT_ID:
            content_lower = message.content.lower()

            # "khong co trong tu dien" -- just ignore it entirely.
            # Don't clear last_word. The bot should keep whatever word
            # was last validated and wait for the next round to start.
            if "khong co trong tu dien" in content_lower:
                print(
                    f"GM says 'khong co trong tu dien' -- "
                    f"ignoring, keeping last_word='{self.last_word}'"
                )
                return

            # "Luot noi tu moi da bat dau voi tu **{word}**!"
            match = re.search(r"\*\*(.+?)\*\*", message.content)
            if match:
                phrase = normalize(match.group(1))
                syllables = phrase.split()

                if len(syllables) != self.MAX_SYLLABLES:
                    print(
                        f"GM announced '{phrase}' ({len(syllables)} syllables) -- "
                        f"not a valid 2-syllable word. Ignoring."
                    )
                    return

                self.last_word = phrase
                self.last_word_message_id = message.id
                self.word_ready.set()
                print(
                    f"New round from GM! Starting word: '{phrase}' "
                    f"(msg_id={message.id})"
                )
            return

        if await self.check_bot_x_reaction(message):
            self.xd_messages.add(message.id)
            return

        self.log_message(message)
        await self._evaluate_message(message)

    async def game_loop(self):
        await self.client.wait_until_ready()
        channel = self.client.get_channel(self.channel_id)
        if channel is None:
            print("Channel not found, I'm out.")
            return

        print(f"Game loop started (min {MIN_SEND_INTERVAL}s between replies)")

        while not self.client.is_closed():
            await self.word_ready.wait()
            self.word_ready.clear()

            if self.last_word is None:
                continue

            elapsed = time.monotonic() - self.last_send_time
            wait_left = MIN_SEND_INTERVAL - elapsed
            if wait_left > 0:
                await asyncio.sleep(wait_left)

            if self.last_word is None:
                continue

            current_word = self.last_word
            next_word = self.get_next_word(current_word)

            try:
                if next_word is None:
                    last_syl = current_word.split()[-1]
                    print(f"No word starts with '{last_syl}', skipping silently.")
                else:
                    await channel.send(next_word)
                    self.used_words.add(next_word)
                    print(f"Sent: '{next_word}'")
            except discord.HTTPException as e:
                print(f"Failed to send message: {e}")

            self.last_send_time = time.monotonic()
            self.last_word = None
            self.last_word_message_id = None


if __name__ == "__main__":
    bot = NoiTuSelfbot()
    asyncio.run(bot.load_words())
    try:
        print("Starting bot...")
        bot.client.run(bot.token)
    except discord.LoginFailure:
        print("Invalid token, fix your .env dude")
    except Exception as e:
        print(f"Bot crashed: {e}")