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
OWNER_ID              = int(os.getenv("OWNER_ID", "0"))
GAME_MASTER_BOT_ID    = int(os.getenv("GAME_MASTER_BOT_ID", "1103932552701550622"))
REQUIRED_REACTIONS    = int(os.getenv("REQUIRED_REACTIONS", "1"))
MIN_SEND_INTERVAL     = int(os.getenv("MIN_SEND_INTERVAL", "120"))
# How many recently-used words to remember before allowing repeats
USED_WORDS_MAXLEN     = int(os.getenv("USED_WORDS_MAXLEN", "500"))
# How many recent messages to re-scan right before sending (triple-check)
# when the channel is calm (< BUSY_TYPER_THRESHOLD people typing).
# Default is 1 -- just the single latest channel message.
TRIPLE_CHECK_HISTORY_LIMIT = int(os.getenv("TRIPLE_CHECK_HISTORY_LIMIT", "1"))
# How many recent messages to check when the channel is busy (see below).
# Kept modest on purpose -- this is still a single history fetch either way,
# so it doesn't add extra API calls or risk a rate limit, it just looks
# a bit further back in that one fetch.
BUSY_CHECK_HISTORY_LIMIT = int(os.getenv("BUSY_CHECK_HISTORY_LIMIT", "5"))
# Number of people concurrently typing in the channel at which we consider
# it "busy" and switch to the deeper check above.
BUSY_TYPER_THRESHOLD = int(os.getenv("BUSY_TYPER_THRESHOLD", "3"))
# For high-concurrency events (e.g. a temporary channel unlock drawing ~20
# active members at once instead of the usual handful), 3+ typers isn't a
# fine enough signal -- 10-12+ people typing simultaneously is a different
# situation from 3. This adds a third "packed" tier that looks back further
# still, for when things are genuinely crowded.
PACKED_TYPER_THRESHOLD    = int(os.getenv("PACKED_TYPER_THRESHOLD", "8"))
PACKED_CHECK_HISTORY_LIMIT = int(os.getenv("PACKED_CHECK_HISTORY_LIMIT", "12"))
# How long a "user is typing" indicator counts as still active. Discord's own
# UI typing indicator times out after ~10s if no message follows.
TYPING_WINDOW_SECONDS = int(os.getenv("TYPING_WINDOW_SECONDS", "10"))

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

        # None = auto (use typing detection); "calm"/"busy"/"packed" = forced
        self.force_mode: str | None = None

        # user_id -> monotonic timestamp of their most recent typing event,
        # used to gauge how busy the channel is right before we send.
        self.typing_users: dict[int, float] = {}

        self.client = commands.Bot(command_prefix="nt!", help_command=None)
        self.client.event(self.on_ready)
        self.client.event(self.on_message)
        self.client.event(self.on_reaction_add)
        self.client.event(self.on_typing)

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

    def _active_typer_count(self) -> int:
        """
        How many distinct users currently count as "typing" in the target
        channel, based on typing events seen in the last TYPING_WINDOW_SECONDS.
        Stale entries are pruned as a side effect.
        """
        now = time.monotonic()
        stale = [uid for uid, ts in self.typing_users.items() if now - ts > TYPING_WINDOW_SECONDS]
        for uid in stale:
            del self.typing_users[uid]
        return len(self.typing_users)

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

    async def on_typing(
        self, channel: discord.abc.Messageable, user: discord.abc.User, when
    ) -> None:
        channel_id = getattr(channel, "id", None)
        if channel_id != self.channel_id:
            return
        if user.id == self.client.user.id:
            return
        self.typing_users[user.id] = time.monotonic()

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
        # ---- Owner DM commands ----------------------------------------
        # DM the bot (from OWNER_ID) to force-lock the detection mode
        # without touching .env or restarting.
        #
        #   !calm   -- single-message check, ignore typer count
        #   !busy   -- 5-message check, ignore typer count
        #   !packed -- 12-message check, ignore typer count
        #   !auto   -- back to automatic typing-based detection
        #   !mode   -- show current mode
        #
        if (
            isinstance(message.channel, discord.DMChannel)
            and OWNER_ID
            and message.author.id == OWNER_ID
        ):
            cmd = message.content.strip().lower()
            if cmd == "!calm":
                self.force_mode = "calm"
                await message.channel.send("✅ Mode locked to **calm** (1-message check).")
                log.info("Owner forced mode: calm")
            elif cmd == "!busy":
                self.force_mode = "busy"
                await message.channel.send("✅ Mode locked to **busy** (5-message check).")
                log.info("Owner forced mode: busy")
            elif cmd == "!packed":
                self.force_mode = "packed"
                await message.channel.send("✅ Mode locked to **packed** (12-message check).")
                log.info("Owner forced mode: packed")
            elif cmd == "!auto":
                self.force_mode = None
                await message.channel.send("✅ Mode set back to **auto** (typing-based detection).")
                log.info("Owner cleared force mode -> auto")
            elif cmd == "!mode":
                typers = self._active_typer_count()
                if self.force_mode:
                    status = f"🔒 Forced to **{self.force_mode}** (auto is disabled)"
                else:
                    if typers >= PACKED_TYPER_THRESHOLD:
                        current = "packed"
                    elif typers >= BUSY_TYPER_THRESHOLD:
                        current = "busy"
                    else:
                        current = "calm"
                    status = f"🔄 Auto — currently **{current}** ({typers} typers detected)"
                await message.channel.send(status)
            return
        # ---------------------------------------------------------------

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
        self, channel: discord.abc.Messageable, history_limit: int
    ) -> tuple[str | None, int | None]:
        """
        Re-check the channel right before we send, so a word that landed
        while we were sleeping out MIN_SEND_INTERVAL isn't missed.

        history_limit controls how deep we look, in a single history fetch
        (one API call either way -- this does not add extra requests, it just
        asks for more messages in that one call):

        - history_limit == 1 (calm channel, <= BUSY_TYPER_THRESHOLD - 1
          people typing): only the single latest message is considered. If
          it's not itself usable, we don't dig further -- fall back to
          in-memory state. This is the fast/normal path.

        - history_limit > 1 (busy channel, several people typing/chatting at
          once): several messages can land almost simultaneously, so the
          single latest one may not be the one that's actually valid (could
          be an off-topic message, an un-reacted-to guess, etc). In that
          case we keep scanning a few messages back to find the true latest
          usable word instead of giving up after one miss.
        """
        deep = history_limit > 1
        try:
            async for message in channel.history(limit=history_limit):
                if message.author.id == self.client.user.id:
                    if deep:
                        continue
                    break
                if message.id in self.xd_messages:
                    if deep:
                        continue
                    break
                if self._bot_x_reaction(message, self.client.user.id):
                    self.xd_messages.add(message.id)
                    if deep:
                        continue
                    break

                if message.author.id == GAME_MASTER_BOT_ID:
                    phrase = self._extract_gm_st
