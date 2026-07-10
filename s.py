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
import signal
import asyncio
import logging
import unicodedata
from collections import defaultdict, deque

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config -- tune these via .env without touching code
# ---------------------------------------------------------------------------
DISCORD_TOKEN         = os.getenv("DISCORD_TOKEN")
CHANNEL_ID_RAW        = os.getenv("CHANNEL_ID")
GAME_MASTER_BOT_ID    = int(os.getenv("GAME_MASTER_BOT_ID", "1103932552701550622"))
REQUIRED_REACTIONS    = int(os.getenv("REQUIRED_REACTIONS", "2"))
MIN_SEND_INTERVAL     = int(os.getenv("MIN_SEND_INTERVAL", "120"))
# How many recently-used words to remember before allowing repeats
USED_WORDS_MAXLEN     = int(os.getenv("USED_WORDS_MAXLEN", "500"))
# How many recent messages to re-scan right before sending (triple-check).
# Default is 1 -- just the single latest channel message.
TRIPLE_CHECK_HISTORY_LIMIT = int(os.getenv("TRIPLE_CHECK_HISTORY_LIMIT", "2"))

WORDS_FILE = "vietnamese_words.txt"
LOG_FILE   = "channel_messages.log"

# ---------------------------------------------------------------------------
# Logging setup -- replaces all the scattered print() calls
# ---------------------------------------------------------------------------
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

        self.phrases: set[str]                          = set()
        self.phrases_by_first_syllable: dict[str, list[str]] = defaultdict(list)

        self.last_word:            str | None = None
        self.last_word_message_id: int | None = None

        # Bounded deque so old words become eligible again after USED_WORDS_MAXLEN entries
        self.used_words: deque[str] = deque(maxlen=USED_WORDS_MAXLEN)
        self._used_words_set: set[str] = set()   # mirror for O(1) lookup

        self.xd_messages:        set[int] = set()
        self.validated_messages: set[int] = set()

        self.word_ready     = asyncio.Event()
        self.last_send_time = 0.0

        self.client = commands.Bot(command_prefix="nt!", help_command=None)
        self.client.event(self.on_ready)
        self.client.event(self.on_message)
        self.client.event(self.on_reaction_add)

    MAX_SYLLABLES = 2

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _track_used(self, word: str) -> None:
        """Add word to the bounded used-words tracker."""
        if len(self.used_words) == self.used_words.maxlen:
            # The deque will evict the oldest entry; mirror the removal
            self._used_words_set.discard(self.used_words[0])
        self.used_words.append(word)
        self._used_words_set.add(word)

    def _log_to_file(self, message: discord.Message, prefix: str = "") -> None:
        """Single helper that writes a structured line to the log file."""
        content = message.content.replace("\n", " ")
        log.info("%s%s: %s", prefix, self._display_author(message), content)

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
        if not last_phrase:
            return None
        last_phrase  = normalize(last_phrase)
        syllables    = last_phrase.split()
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
        tokens = re.findall(r"\w+", text)   # re.UNICODE is default for str in Python 3
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

    @staticmethod
    def _display_author(message: discord.Message) -> str:
        author = message.author
        # discriminator is "0" or absent on the new username system
        disc = getattr(author, "discriminator", "0")
        if not disc or disc == "0":
            return author.name
        return f"{author.name}#{disc}"

    @staticmethod
    def _bot_x_reaction(message: discord.Message, bot_id: int) -> bool:
        """
        Check whether the bot has already placed an 'X' reaction on this message.
        Uses reaction.me (cached) instead of paginating reaction.users() -- no extra API call.
        """
        return any(str(r.emoji) == "X" and r.me for r in message.reactions)

    def _extract_gm_started_word(self, message: discord.Message) -> str | None:
        """
        Parse a game-master "new round" announcement, e.g.
        "Luot noi tu moi da bat dau voi tu **{word}**!"
        Returns the normalized phrase only if it's a valid MAX_SYLLABLES word,
        and only if the message isn't the "not in dictionary" error text.
        """
        content_lower = message.content.lower()
        if "khong co trong tu dien" in content_lower:
            return None

        match = re.search(r"\*\*(.+?)\*\*", message.content)
        if not match:
            return None

        phrase    = normalize(match.group(1))
        syllables = phrase.split()
        if len(syllables) != self.MAX_SYLLABLES:
            return None
        return phrase

    # ------------------------------------------------------------------
    # Discord events
    # ------------------------------------------------------------------

    async def on_ready(self) -> None:
        log.info("Logged in as %s (ID: %s)", self.client.user, self.client.user.id)
        log.info("Targeting channel ID %s", self.channel_id)
        self.client.loop.create_task(self.game_loop())
        self.client.loop.create_task(self.heartbeat_check())

    async def heartbeat_check(self) -> None:
        await self.client.wait_until_ready()
        while not self.client.is_closed():
            await asyncio.sleep(30)
            log.info(
                "[heartbeat] last_word=%s | last_msg_id=%s | xd=%d | validated=%d",
                self.last_word,
                self.last_word_message_id,
                len(self.xd_messages),
                len(self.validated_messages),
            )

    async def _evaluate_message(
        self, message: discord.Message, allow_update: bool = True
    ) -> bool:
        if message.author.id == self.client.user.id:
            return False
        if message.id in self.xd_messages or message.id in self.validated_messages:
            return False
        if self._bot_x_reaction(message, self.client.user.id):
            self.xd_messages.add(message.id)
            return False

        total_reactions = sum(r.count for r in message.reactions)
        if total_reactions < REQUIRED_REACTIONS:
            return False

        phrase = self.find_last_valid_phrase(message.content)
        if not phrase:
            return False

        self.validated_messages.add(message.id)
        self._log_to_file(message, prefix=f"VALIDATED ({total_reactions} reactions) ")
        log.info("Message %d validated (%d reactions) -> '%s'",
                 message.id, total_reactions, phrase)

        if allow_update:
            if (
                self.last_word_message_id is None
                or message.id > self.last_word_message_id
            ):
                self.last_word            = phrase
                self.last_word_message_id = message.id
                self.word_ready.set()
                log.info("  -> last_word updated to '%s' (msg_id=%d)", phrase, message.id)
            else:
                log.info(
                    "  -> skipped (message %d is older than current last_word from msg %d)",
                    message.id, self.last_word_message_id,
                )
                return False
        return True

    async def on_reaction_add(self, reaction: discord.Reaction, user: discord.User) -> None:
        message = reaction.message
        if message.channel.id != self.channel_id:
            return

        # Bot placed an 'X' -- invalidate the message
        if user.id == self.client.user.id and str(reaction.emoji) == "X":
            self.xd_messages.add(message.id)
            self.validated_messages.discard(message.id)
            log.info("Bot X'd message %d from %s", message.id, message.author.name)
            if self.last_word:
                phrase = self.find_last_valid_phrase(message.content)
                if (
                    phrase
                    and phrase == self.last_word
                    and message.id == self.last_word_message_id
                ):
                    self.last_word            = None
                    self.last_word_message_id = None
                    log.info("Cleared last_word because bot X'd the validated message")
            return

        if message.author.id == self.client.user.id:
            return

        await self._evaluate_message(message)

    async def on_message(self, message: discord.Message) -> None:
        if message.channel.id != self.channel_id:
            return
        if message.author.id == self.client.user.id:
            return
        if message.id in self.xd_messages:
            return

        if message.author.id == GAME_MASTER_BOT_ID:
            if "khong co trong tu dien" in message.content.lower():
                log.info(
                    "GM says 'khong co trong tu dien' -- ignoring, keeping last_word='%s'",
                    self.last_word,
                )
                return

            phrase = self._extract_gm_started_word(message)
            if phrase is None:
                return

            self.last_word            = phrase
            self.last_word_message_id = message.id
            self.word_ready.set()
            log.info("New round from GM! Starting word: '%s' (msg_id=%d)",
                     phrase, message.id)
            return

        if self._bot_x_reaction(message, self.client.user.id):
            self.xd_messages.add(message.id)
            return

        self._log_to_file(message)
        await self._evaluate_message(message)

    # ------------------------------------------------------------------
    # Triple-check right before sending
    # ------------------------------------------------------------------

    async def _get_freshest_last_word(
        self, channel: discord.abc.Messageable
    ) -> tuple[str | None, int | None]:
        """
        Check the single latest message in the channel right before we send.
        This exists because MIN_SEND_INTERVAL can leave the bot idle for up
        to a couple of minutes -- during that window a player can post a
        valid word, or the game master can start a fresh round, and we want
        to be sure we're replying to whatever is actually newest rather than
        to whatever self.last_word happened to be set to earlier.

        Only looks at the most recent message (TRIPLE_CHECK_HISTORY_LIMIT=1
        by default). If that message isn't itself a usable word (not enough
        reactions yet, GM error text, wrong syllable count, already X'd),
        this falls back to the in-memory self.last_word / last_word_message_id
        rather than digging further back -- the point is to catch something
        that just landed, not to re-run full history validation.
        """
        try:
            async for message in channel.history(limit=TRIPLE_CHECK_HISTORY_LIMIT):
                if message.author.id == self.client.user.id:
                    break
                if message.id in self.xd_messages:
                    break
                if self._bot_x_reaction(message, self.client.user.id):
                    self.xd_messages.add(message.id)
                    break

                if message.author.id == GAME_MASTER_BOT_ID:
                    phrase = self._extract_gm_started_word(message)
                    if phrase is not None:
                        return phrase, message.id
                    break

                total_reactions = sum(r.count for r in message.reactions)
                if total_reactions < REQUIRED_REACTIONS:
                    break

                phrase = self.find_last_valid_phrase(message.content)
                if phrase:
                    return phrase, message.id
                break
        except discord.HTTPException as exc:
            log.warning("Triple-check history fetch failed, using cached state: %s", exc)

        return self.last_word, self.last_word_message_id

    # ------------------------------------------------------------------
    # Game loop
    # ------------------------------------------------------------------

    async def game_loop(self) -> None:
        await self.client.wait_until_ready()
        channel = self.client.get_channel(self.channel_id)
        if channel is None:
            log.error("Channel %d not found, bailing out.", self.channel_id)
            return

        log.info("Game loop started (min %ds between replies)", MIN_SEND_INTERVAL)

        while not self.client.is_closed():
            await self.word_ready.wait()
            self.word_ready.clear()

            if self.last_word is None:
                continue

            # Respect the minimum send interval
            elapsed   = time.monotonic() - self.last_send_time
            wait_left = MIN_SEND_INTERVAL - elapsed
            if wait_left > 0:
                await asyncio.sleep(wait_left)

            # last_word may have been cleared while we were waiting
            if self.last_word is None:
                continue

            current_word = self.last_word

            # --- Triple-check -------------------------------------------------
            # Right before we actually send, re-verify against live channel
            # state. If a valid word landed while we were sleeping out the
            # interval (and for whatever reason wasn't already picked up by
            # on_message/on_reaction_add), use that instead of the stale word.
            fresh_word, fresh_id = await self._get_freshest_last_word(channel)
            if fresh_id is not None and (
                self.last_word_message_id is None or fresh_id > self.last_word_message_id
            ):
                if fresh_word != current_word:
                    log.info(
                        "Triple-check: found newer word '%s' (msg_id=%d), "
                        "overriding stale '%s' (msg_id=%s)",
                        fresh_word, fresh_id, current_word, self.last_word_message_id,
                    )
                current_word              = fresh_word
                self.last_word_message_id = fresh_id
            # -------------------------------------------------------------------

            next_word = self.get_next_word(current_word) if current_word else None

            try:
                if current_word is None:
                    log.info("Triple-check found nothing valid to respond to, skipping.")
                elif next_word is None:
                    last_syl = current_word.split()[-1]
                    log.info("No word starts with '%s', skipping silently.", last_syl)
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
