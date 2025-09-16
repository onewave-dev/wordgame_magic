"""Utilities for sending timed choice prompts to players."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from telegram import InlineKeyboardMarkup, Message
from telegram.error import TelegramError
from telegram.ext import CallbackContext

logger = logging.getLogger(__name__)

ChoiceTarget = Tuple[int, Optional[int]]
SendMessageFunc = Callable[[int, Optional[int], CallbackContext, str], Awaitable[Message]]
TimeoutCallback = Callable[["ChoiceTimerHandle"], Awaitable[None]]

DEFAULT_TIMER_SEQUENCE: Tuple[str, ...] = ("5️⃣", "4️⃣", "3️⃣", "2️⃣", "1️⃣")
_UNSET = object()


class ChoiceTimerHandle:
    """Handle for managing a timed choice broadcast."""

    def __init__(
        self,
        *,
        context: CallbackContext,
        messages: List[Tuple[int, Optional[int], int]],
        timer_messages: List[Tuple[int, Optional[int], int]],
        on_timeout: TimeoutCallback,
        timer_sequence: Sequence[str],
        final_timer_text: str,
        timeout_timer_text: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.context = context
        self.messages = messages
        self.timer_messages = timer_messages
        self.on_timeout = on_timeout
        sequence = tuple(timer_sequence) or DEFAULT_TIMER_SEQUENCE
        if not sequence:
            sequence = DEFAULT_TIMER_SEQUENCE
        self._timer_sequence = sequence
        self._stop_event = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None
        self.completed = False
        self.final_timer_text = final_timer_text
        self.timeout_timer_text = timeout_timer_text
        self.data: Dict[str, Any] = data or {}
        self._start_timer_task()

    def _start_timer_task(self) -> None:
        if self._task is None:
            loop = asyncio.get_running_loop()
            self._task = loop.create_task(self._run_timer())

    async def _run_timer(self) -> None:
        try:
            # The first emoji is sent during initial broadcast. Update the rest here.
            for emoji in self._timer_sequence[1:]:
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=1.0)
                    return
                except asyncio.TimeoutError:
                    pass
                await self._edit_timer_messages(emoji)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=1.0)
                return
            except asyncio.TimeoutError:
                pass
            await self._handle_timeout()
        except asyncio.CancelledError:
            pass
        except Exception:  # pragma: no cover - safety net
            logger.exception("Choice timer task failed")

    async def _handle_timeout(self) -> None:
        if self.completed:
            return
        self.completed = True
        self._stop_event.set()
        await self._disable_choice_markup()
        if self.timeout_timer_text is not None:
            await self._edit_timer_messages(self.timeout_timer_text)
        try:
            await self.on_timeout(self)
        except Exception:  # pragma: no cover - log unexpected errors
            logger.exception("Error in timeout callback")

    async def _edit_timer_messages(self, text: str) -> None:
        for chat_id, thread_id, message_id in list(self.timer_messages):
            try:
                kwargs = {"chat_id": chat_id, "message_id": message_id}
                if thread_id is not None:
                    kwargs["message_thread_id"] = thread_id
                await self.context.bot.edit_message_text(text=text, **kwargs)
            except TelegramError:
                continue

    async def _disable_choice_markup(self) -> None:
        for chat_id, thread_id, message_id in list(self.messages):
            try:
                kwargs = {"chat_id": chat_id, "message_id": message_id}
                if thread_id is not None:
                    kwargs["message_thread_id"] = thread_id
                await self.context.bot.edit_message_reply_markup(
                    reply_markup=None, **kwargs
                )
            except TelegramError:
                continue

    async def complete(self, final_timer_text: Optional[str] = _UNSET) -> None:
        """Stop the timer and disable choice buttons."""

        if self.completed:
            return
        self.completed = True
        self._stop_event.set()
        await self._disable_choice_markup()
        text: Optional[str]
        if final_timer_text is _UNSET:
            text = self.final_timer_text
        else:
            text = final_timer_text
        if text is not None:
            await self._edit_timer_messages(text)

    def is_active(self) -> bool:
        return not self.completed


async def send_choice_with_timer(
    *,
    context: CallbackContext,
    targets: Sequence[ChoiceTarget],
    message_text: str,
    reply_markup: InlineKeyboardMarkup,
    send_func: SendMessageFunc,
    on_timeout: TimeoutCallback,
    message_kwargs: Optional[Dict[str, Any]] = None,
    timer_message_kwargs: Optional[Dict[str, Any]] = None,
    timer_sequence: Sequence[str] = DEFAULT_TIMER_SEQUENCE,
    data: Optional[Dict[str, Any]] = None,
    final_timer_text: str = "Выбор сделан",
    timeout_timer_text: str = "Случайный выбор",
) -> ChoiceTimerHandle:
    """Broadcast a choice message with a countdown timer.

    The function sends a message with inline keyboard options to all ``targets``
    and adds a separate timer message underneath.  The timer updates every
    second and triggers ``on_timeout`` after five seconds unless ``complete`` is
    called on the returned handle.
    """

    message_kwargs = dict(message_kwargs or {})
    timer_message_kwargs = dict(timer_message_kwargs or {})

    messages: List[Tuple[int, Optional[int], int]] = []
    timer_messages: List[Tuple[int, Optional[int], int]] = []

    first_timer_text = (tuple(timer_sequence) or DEFAULT_TIMER_SEQUENCE)[0]

    for chat_id, thread_id in targets:
        try:
            msg = await send_func(
                chat_id,
                thread_id,
                context,
                message_text,
                reply_markup=reply_markup,
                **message_kwargs,
            )
        except TelegramError:
            logger.exception("Failed to send choice message to %s", chat_id)
            continue
        messages.append((chat_id, thread_id, msg.message_id))
        try:
            timer_msg = await send_func(
                chat_id,
                thread_id,
                context,
                first_timer_text,
                **timer_message_kwargs,
            )
        except TelegramError:
            logger.exception("Failed to send timer message to %s", chat_id)
            continue
        timer_messages.append((chat_id, thread_id, timer_msg.message_id))

    handle = ChoiceTimerHandle(
        context=context,
        messages=messages,
        timer_messages=timer_messages,
        on_timeout=on_timeout,
        timer_sequence=timer_sequence,
        final_timer_text=final_timer_text,
        timeout_timer_text=timeout_timer_text,
        data=data,
    )
    return handle
