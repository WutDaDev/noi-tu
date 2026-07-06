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
            content_lower = message.content.lower()

            # "khong co trong tu dien" -- not a real word, ignore.
            # Keep last_word so we can still respond when a valid word arrives.
            if "khong co trong tu dien" in content_lower:
                log.info(
                    "GM says 'khong co trong tu dien' -- ignoring, keeping last_word='%s'",
                    self.last_word,
                )
                return

            # "Luot noi tu moi da bat dau voi tu **{word}**!"
            match = re.search(r"\*\*(.+?)\*\*", message.content)
            if match:
# -*- coding: utf-8 -*-

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
USED_WORDS_MAXLEN     = int(os.getenv("USED_WORDS_MAXLEN", "500"))
RECOVERY_SCAN_LIMIT   = int(os.getenv("RECOVERY_SCAN_LIMIT", "20"))

# Jitter: random extra seconds added on top of MIN_SEND_INTERVAL each send
SEND_JITTER_MIN       = float(os.getenv("SEND_JITTER_MIN", "8"))
SEND_JITTER_MAX       = float(os.getenv("SEND_JITTER_MAX", "45"))

# Owner DM stats
OWNER_ID              = int(os.getenv("OWNER_ID", "1369831885462835252"))
XP_TRIGGER_PHRASE     = os.getenv("XP_TRIGGER_PHRASE", "accurateindiabro")
XP_EVENT_MULTIPLIER   = float(os.getenv("XP_EVENT_MULTIPLIER", "11"))
XP_SELF_MULTIPLIER    = float(os.getenv("XP_SELF_MULTIPLIER", "4"))
XP_DEFAULT_GOAL       = int(os.getenv("XP_DEFAULT_GOAL", "12000"))

WORDS_FILE = "vietnamese_words.txt"
LOG_FILE   = "channel_messages.log"

# ---------------------------------------------------------------------------
# Logging setup
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

        self.phrases: set[str]                               = set()
        self.phrases_by_first_syllable: dict[str, list[str]] = defaultdict(list)

        self.last_word:            str | None = None
        self.last_word_message_id: int | None = None

        self.used_words: deque[str]  = deque(maxlen=USED_WORDS_MAXLEN)
        self._used_words_set: set[str] = set()

        self.xd_messages:        set[int] = set()
        self.validated_messages: set[int] = set()

        self.word_ready     = asyncio.Event()
        self.last_send_time = 0.0

        # Stats
        self.bot_start_time = time.time()
        self.messages_sent  = 0
        self.xp_goal        = XP_DEFAULT_GOAL

        self.client = commands.Bot(command_prefix="nt!", help_command=None)
        self.client.event(self.on_ready)
        self.client.event(self.on_message)
        self.client.event(self.on_reaction_add)

    MAX_SYLLABLES = 2

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _track_used(self, word: str) -> None:
        if len(self.used_words) == self.used_words.maxlen:
            self._used_words_set.discard(self.used_words[0])
        self.used_words.append(word)
        self._used_words_set.add(word)

    def _log_to_file(self, message: discord.Message, prefix: str = "") -> None:
        content = message.content.replace("\n", " ")
        log.info("%s%s: %s", prefix, self._display_author(message), content)

    def _current_xp(self) -> float:
        return self.messages_sent * XP_EVENT_MULTIPLIER * XP_SELF_MULTIPLIER

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

    @staticmethod
    def _display_author(message: discord.Message) -> str:
        author = message.author
        disc   = getattr(author, "discriminator", "0")
        if not disc or disc == "0":
            return author.name
        return f"{author.name}#{disc}"

    @staticmethod
    def _bot_x_reaction(message: discord.Message, bot_id: int) -> bool:
        return any(str(r.emoji) == "X" and r.me for r in message.reactions)

    # ------------------------------------------------------------------
    # Recovery scan
    # ------------------------------------------------------------------

    async def _recover_last_word(self, channel: discord.TextChannel) -> bool:
        """
        Scan recent messages to find the newest validated word to chain from.
        Called after the cooldown wait when last_word has been cleared.
        """
        log.info("[recovery] last_word is None -- scanning last %d messages...", RECOVERY_SCAN_LIMIT)

        candidates: list[tuple[int, str]] = []

        async for msg in channel.history(limit=RECOVERY_SCAN_LIMIT):
            if msg.author.id == self.client.user.id:
                continue
            if msg.author.id == GAME_MASTER_BOT_ID:
                bold_match = re.search(r"\*\*(.+?)\*\*", msg.content)
                if bold_match:
                    phrase    = normalize(bold_match.group(1))
                    syllables = phrase.split()
                    if len(syllables) == self.MAX_SYLLABLES:
                        candidates.append((msg.id, phrase))
                continue
            if msg.id in self.xd_messages:
                continue
            if self._bot_x_reaction(msg, self.client.user.id):
                self.xd_messages.add(msg.id)
                continue
            total_reactions = sum(r.count for r in msg.reactions)
            if total_reactions < REQUIRED_REACTIONS:
                continue
            phrase = self.find_last_valid_phrase(msg.content)
            if phrase:
                candidates.append((msg.id, phrase))

        if not candidates:
            log.info("[recovery] no valid word found in recent history.")
            return False

        # history() is newest-first so candidates[0] is the most recent
        best_id, best_phrase = candidates[0]

        if self.last_word_message_id is not None and best_id <= self.last_word_message_id:
            log.info(
                "[recovery] found '%s' (msg %d) but not newer than current msg %d -- skipping.",
                best_phrase, best_id, self.last_word_message_id,
            )
            return False

        self.last_word            = best_phrase
        self.last_word_message_id = best_id
        log.info("[recovery] recovered last_word='%s' from msg %d", best_phrase, best_id)
        return True

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
                "[heartbeat] last_word=%s | last_msg_id=%s | xd=%d | validated=%d | sent=%d | xp=%d",
                self.last_word,
                self.last_word_message_id,
                len(self.xd_messages),
                len(self.validated_messages),
                self.messages_sent,
                int(self._current_xp()),
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
        # --- Owner-only DM command, never reply to anyone else in DMs ---
        if isinstance(message.channel, discord.DMChannel):
            if (
                message.author.id == OWNER_ID
                and message.author.id != self.client.user.id
            ):
                content = message.content.strip()

                # Plain integer -> set a new goal, then show current status
                if content.isdigit():
                    self.xp_goal  = int(content)
                    xp            = self._current_xp()
                    remaining     = max(0, self.xp_goal - xp)
                    await message.channel.send(
                        f"Goal: {self.xp_goal}\n"
                        f"XP now: {int(xp)}\n"
                        f"{int(remaining)} to go"
                    )
                    return

                # Trigger phrase -> stats readout
                if content.lower() == XP_TRIGGER_PHRASE.lower():
                    xp        = self._current_xp()
                    remaining = max(0, self.xp_goal - xp)
                    await message.channel.send(
                        f"Messages sent: {self.messages_sent}\n"
                        f"XP: {int(xp)}\n"
                        f"Goal: {self.xp_goal}\n"
                        f"{int(remaining)} to go"
                    )
            # DMs from anyone else: total silence
            return

        # --- Game channel ---
        if message.channel.id != self.channel_id:
            return
        if message.author.id == self.client.user.id:
            return
        if message.id in self.xd_messages:
            return

        if message.author.id == GAME_MASTER_BOT_ID:
            content_lower = message.content.lower()

            if "khong co trong tu dien" in content_lower:
                log.info(
                    "GM says 'khong co trong tu dien' -- ignoring, keeping last_word='%s'",
                    self.last_word,
                )
                return

            match = re.search(r"\*\*(.+?)\*\*", message.content)
            if match:
                phrase    = normalize(match.group(1))
                syllables = phrase.split()

                if len(syllables) != self.MAX_SYLLABLES:
                    log.info(
                        "GM announced '%s' (%d syllables) -- not a valid 2-syllable word. Ignoring.",
                        phrase, len(syllables),
                    )
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
    # Game loop
    # ------------------------------------------------------------------

    async def game_loop(self) -> None:
        await self.client.wait_until_ready()
        channel = self.client.get_channel(self.channel_id)
        if channel is None:
            log.error("Channel %d not found, bailing out.", self.channel_id)
            return

        log.info(
            "Game loop started (min %ds + %.0f-%.0fs jitter between replies)",
            MIN_SEND_INTERVAL, SEND_JITTER_MIN, SEND_JITTER_MAX,
        )

        while not self.client.is_closed():
            await self.word_ready.wait()
            self.word_ready.clear()

            if self.last_word is None:
                continue

            # Base cooldown + random jitter so timing looks human
            elapsed   = time.monotonic() - self.last_send_time
            jitter    = random.uniform(SEND_JITTER_MIN, SEND_JITTER_MAX)
            wait_left = (MIN_SEND_INTERVAL + jitter) - elapsed
            if wait_left > 0:
                log.info("Cooldown: waiting %.1fs (incl. %.1fs jitter)...", wait_left, jitter)
                await asyncio.sleep(wait_left)

            # After the wait, last_word may have been cleared (we got X'd).
            # Try to recover from recent channel history before giving up.
            if self.last_word is None:
                recovered = await self._recover_last_word(channel)
                if not recovered:
                    log.info("[game_loop] Could not recover last_word, skipping turn.")
                    continue

            current_word = self.last_word
            next_word    = self.get_next_word(current_word)

            try:
                if next_word is None:
                    last_syl = current_word.split()[-1]
                    log.info("No word starts with '%s', skipping silently.", last_syl)
                else:
                    await channel.send(next_word)
                    self.messages_sent += 1
                    self._track_used(next_word)
                    log.info("Sent: '%s' (total sent: %d | XP: %d)",
                             next_word, self.messages_sent, int(self._current_xp()))
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
ain__":
    asyncio.run(main())
