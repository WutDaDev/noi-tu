"""
noi_tu_selfbot.py

pip uninstall discord.py
pip install -U discord.py-self
"""

# -*- coding: utf-8 -*-

import os
import re
import time
import random
import signal
import asyncio
import logging
import unicodedata
from collections import defaultdict, deque

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN      = os.getenv("DISCORD_TOKEN")
CHANNEL_ID_RAW     = os.getenv("CHANNEL_ID")
GAME_MASTER_BOT_ID = int(os.getenv("GAME_MASTER_BOT_ID", "1103932552701550622"))
MIN_SEND_INTERVAL  = int(os.getenv("MIN_SEND_INTERVAL", "3"))
SEND_JITTER_MIN    = float(os.getenv("SEND_JITTER_MIN", "1"))
SEND_JITTER_MAX    = float(os.getenv("SEND_JITTER_MAX", "3"))
USED_WORDS_MAXLEN  = int(os.getenv("USED_WORDS_MAXLEN", "500"))

# How long (seconds) to wait after a player word before accepting it,
# so the GM has time to reject it first if it's invalid.
PLAYER_CONFIRM_WINDOW = float(os.getenv("PLAYER_CONFIRM_WINDOW", "4"))

WORDS_FILE = "vietnamese_words.txt"
LOG_FILE   = "channel_messages.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


def normalize(text: str) -> str:
    return unicodedata.normalize("NFC", text).strip().lower()


class NoiTuSelfbot:
    def __init__(self):
        if not DISCORD_TOKEN:
            raise SystemExit("DISCORD_TOKEN missing from .env")
        if not CHANNEL_ID_RAW:
            raise SystemExit("CHANNEL_ID missing from .env")

        self.token      = DISCORD_TOKEN
        self.channel_id = int(CHANNEL_ID_RAW)

        self.phrases: set[str] = set()
        self.phrases_by_first_syllable: dict[str, list[str]] = defaultdict(list)

        self.last_word:            str | None = None
        self.last_word_message_id: int | None = None

        self.used_words: deque[str]    = deque(maxlen=USED_WORDS_MAXLEN)
        self._used_words_set: set[str] = set()

        self.xd_messages: set[int] = set()

        self.word_ready     = asyncio.Event()
        self.last_send_time = 0.0

        # Tracks pending player words waiting for GM confirmation.
        # Maps message_id -> (phrase, asyncio.Task)
        self._pending_player: dict[int, tuple[str, asyncio.Task]] = {}

        # Set to the message_id of a player word the GM just rejected,
        # so the confirmation task can drop it.
        self._gm_rejected_id: int | None = None

        self.client = commands.Bot(command_prefix="nt!", help_command=None)
        self.client.event(self.on_ready)
        self.client.event(self.on_message)
        self.client.event(self.on_reaction_add)

    MAX_SYLLABLES = 2

    # ------------------------------------------------------------------
    # Word loading & selection
    # ------------------------------------------------------------------

    async def load_words(self) -> None:
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
            log.info("Loaded %d 2-syllable words (skipped %d)", len(self.phrases), skipped)
        except FileNotFoundError:
            log.error("%s not found!", WORDS_FILE)
            raise SystemExit(1)

    def get_next_word(self, last_phrase: str) -> str | None:
        last_phrase   = normalize(last_phrase)
        syllables     = last_phrase.split()
        if not syllables:
            return None
        last_syllable = syllables[-1]
        candidates    = self.phrases_by_first_syllable.get(last_syllable, [])
        if not candidates:
            return None
        fresh = [c for c in candidates if c != last_phrase and c not in self._used_words_set]
        pool  = fresh or [c for c in candidates if c != last_phrase] or candidates
        return random.choice(pool)

    def find_last_valid_phrase(self, content: str) -> str | None:
        text   = normalize(content)
        tokens = re.findall(r"\w+", text)
        if not tokens:
            return None
        last_tokens = tokens[-self.MAX_SYLLABLES:]
        if len(last_tokens) != self.MAX_SYLLABLES:
            return None
        phrase = " ".join(last_tokens)
        return phrase if phrase in self.phrases else None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _track_used(self, word: str) -> None:
        if len(self.used_words) == self.used_words.maxlen:
            self._used_words_set.discard(self.used_words[0])
        self.used_words.append(word)
        self._used_words_set.add(word)

    @staticmethod
    def _bot_x_reaction(message: discord.Message) -> bool:
        return any(str(r.emoji) == "X" and r.me for r in message.reactions)

    def _set_last_word(self, phrase: str, message_id: int) -> None:
        """Commit a word as the current last_word and wake the game loop."""
        self.last_word            = phrase
        self.last_word_message_id = message_id
        self.word_ready.set()
        log.info("last_word confirmed: '%s' (msg_id=%d)", phrase, message_id)

    def _cancel_pending(self) -> None:
        """Cancel all in-flight player confirmation tasks."""
        for msg_id, (phrase, task) in list(self._pending_player.items()):
            task.cancel()
            log.info("Cancelled pending player word '%s' (msg_id=%d)", phrase, msg_id)
        self._pending_player.clear()

    # ------------------------------------------------------------------
    # Player word confirmation
    # ------------------------------------------------------------------

    async def _confirm_player_word(self, phrase: str, message_id: int) -> None:
        """
        Wait PLAYER_CONFIRM_WINDOW seconds. If the GM hasn't rejected this
        message by then, treat the word as valid and update last_word.
        """
        try:
            await asyncio.sleep(PLAYER_CONFIRM_WINDOW)
        except asyncio.CancelledError:
            self._pending_player.pop(message_id, None)
            return

        self._pending_player.pop(message_id, None)

        # GM rejected this specific message while we were waiting
        if self._gm_rejected_id == message_id:
            self._gm_rejected_id = None
            log.info("Player word '%s' (msg_id=%d) rejected by GM — discarding.",
                     phrase, message_id)
            return

        # Only accept if it's still newer than our current last_word
        if (
            self.last_word_message_id is None
            or message_id > self.last_word_message_id
        ):
            self._set_last_word(phrase, message_id)
        else:
            log.info("Player word '%s' expired (older than current last_word) — discarding.",
                     phrase)

    # ------------------------------------------------------------------
    # Discord events
    # ------------------------------------------------------------------

    async def on_ready(self) -> None:
        log.info("Logged in as %s (ID: %s)", self.client.user, self.client.user.id)
        log.info("Targeting channel ID %s", self.channel_id)
        log.info("Player confirm window: %.1fs", PLAYER_CONFIRM_WINDOW)
        self.client.loop.create_task(self.game_loop())
        self.client.loop.create_task(self.heartbeat_check())

    async def heartbeat_check(self) -> None:
        await self.client.wait_until_ready()
        while not self.client.is_closed():
            await asyncio.sleep(30)
            log.info(
                "[heartbeat] last_word=%s | last_msg_id=%s | pending=%d | xd=%d",
                self.last_word, self.last_word_message_id,
                len(self._pending_player), len(self.xd_messages),
            )

    async def on_reaction_add(self, reaction: discord.Reaction, user: discord.User) -> None:
        message = reaction.message
        if message.channel.id != self.channel_id:
            return
        if user.id == self.client.user.id and str(reaction.emoji) == "X":
            self.xd_messages.add(message.id)
            log.info("Bot X'd message %d", message.id)
            if self.last_word and message.id == self.last_word_message_id:
                self.last_word            = None
                self.last_word_message_id = None
                log.info("Cleared last_word — X'd message was current anchor")

    async def on_message(self, message: discord.Message) -> None:
        if message.channel.id != self.channel_id:
            return
        if message.author.id == self.client.user.id:
            return
        if message.id in self.xd_messages:
            return

        # ── Game Master bot ──────────────────────────────────────────────
        if message.author.id == GAME_MASTER_BOT_ID:
            content_lower = normalize(message.content)

            # GM rejected the last player word — figure out which one
            if "không có trong từ điển" in content_lower or "khong co trong tu dien" in content_lower:
                # The most recently seen pending player message is the one that got rejected.
                # Cancel all pending tasks — only one player word is in play at a time.
                if self._pending_player:
                    rejected_id = max(self._pending_player.keys())
                    self._gm_rejected_id = rejected_id
                    log.info("GM rejected word — cancelling pending msg_id=%d", rejected_id)
                self._cancel_pending()
                # Do NOT clear last_word — keep the last GM-confirmed or
                # bot-sent word so the round can continue when someone gets it right.
                return

            # GM announced a new round word: **word**
            match = re.search(r"\*\*(.+?)\*\*", message.content)
            if match:
                phrase    = normalize(match.group(1))
                syllables = phrase.split()
                if len(syllables) != self.MAX_SYLLABLES:
                    log.info("GM announced '%s' (%d syllables) — not 2-syllable, ignoring.",
                             phrase, len(syllables))
                    return
                # Cancel any player words in flight — GM word takes priority
                self._cancel_pending()
                self._set_last_word(phrase, message.id)
            return

        # ── Regular player message ───────────────────────────────────────
        if self._bot_x_reaction(message):
            self.xd_messages.add(message.id)
            return

        phrase = self.find_last_valid_phrase(message.content)
        if not phrase:
            return

        # Only consider words newer than our current anchor
        if self.last_word_message_id is not None and message.id <= self.last_word_message_id:
            return

        log.info("Player word candidate: '%s' (msg_id=%d) — waiting %.1fs for GM...",
                 phrase, message.id, PLAYER_CONFIRM_WINDOW)

        # Cancel any older pending task — we only track the latest candidate
        self._cancel_pending()

        task = self.client.loop.create_task(
            self._confirm_player_word(phrase, message.id)
        )
        self._pending_player[message.id] = (phrase, task)

    # ------------------------------------------------------------------
    # Game loop
    # ------------------------------------------------------------------

    async def game_loop(self) -> None:
        await self.client.wait_until_ready()
        channel = self.client.get_channel(self.channel_id)
        if channel is None:
            log.error("Channel %d not found, bailing out.", self.channel_id)
            return

        log.info("Game loop started (cooldown %ds + %.0f-%.0fs jitter)",
                 MIN_SEND_INTERVAL, SEND_JITTER_MIN, SEND_JITTER_MAX)

        while not self.client.is_closed():
            await self.word_ready.wait()
            self.word_ready.clear()

            if self.last_word is None:
                continue

            elapsed   = time.monotonic() - self.last_send_time
            jitter    = random.uniform(SEND_JITTER_MIN, SEND_JITTER_MAX)
            wait_left = (MIN_SEND_INTERVAL + jitter) - elapsed
            if wait_left > 0:
                log.info("Cooldown: waiting %.1fs...", wait_left)
                await asyncio.sleep(wait_left)

            if self.last_word is None:
                continue

            current_word = self.last_word
            next_word    = self.get_next_word(current_word)

            try:
                if next_word is None:
                    log.info("No word starts with '%s', skipping.", current_word.split()[-1])
                else:
                    await channel.send(next_word)
                    self._track_used(next_word)
                    log.info("Sent: '%s'", next_word)
            except discord.HTTPException as exc:
                log.error("Failed to send message: %s", exc)

            self.last_send_time       = time.monotonic()
            self.last_word            = None
            self.last_word_message_id = None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    bot = NoiTuSelfbot()
    await bot.load_words()

    loop = asyncio.get_running_loop()

    def _shutdown(sig, frame):
        log.info("Received %s, shutting down...", signal.Signals(sig).name)
        loop.create_task(bot.client.close())

    for _sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(_sig, _shutdown)

    try:
        log.info("Starting bot...")
        await bot.client.start(bot.token)
    except discord.LoginFailure:
        log.error("Invalid token -- fix your .env")
    except Exception:
        log.exception("Bot crashed")


if __name__ == "__main__":
    asyncio.run(main())
