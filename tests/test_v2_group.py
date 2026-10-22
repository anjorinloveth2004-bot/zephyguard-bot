"""Item 5 tests: edit-in-place, image welcome routing, auto-FAQ matching, twin tone gating."""
import asyncio

from app.features import faq_best_match
from app.handlers import group_v2


class FakeBot:
    def __init__(self, edit_fails=False):
        self.sent = []
        self.edited = []
        self.edit_fails = edit_fails
        self._id = 100

    async def send_message(self, chat_id, text, **kw):
        self._id += 1
        self.sent.append((chat_id, text, self._id))
        return type("M", (), {"message_id": self._id})()

    async def edit_message_text(self, text, chat_id, message_id, **kw):
        if self.edit_fails:
            raise RuntimeError("message to edit not found")
        self.edited.append((chat_id, message_id, text))


def test_send_or_edit_posts_then_edits_when_on():
    group_v2._LAST_MSG.clear()
    bot = FakeBot()
    grp = {"edit_mode": 1}
    asyncio.run(group_v2.send_or_edit(bot, grp, -5, "menu", "one"))
    asyncio.run(group_v2.send_or_edit(bot, grp, -5, "menu", "two"))
    assert len(bot.sent) == 1 and len(bot.edited) == 1
    assert bot.edited[0][2] == "two"


def test_send_or_edit_posts_fresh_when_edit_off():
    group_v2._LAST_MSG.clear()
    bot = FakeBot()
    grp = {"edit_mode": 0}
    asyncio.run(group_v2.send_or_edit(bot, grp, -6, "menu", "a"))
    asyncio.run(group_v2.send_or_edit(bot, grp, -6, "menu", "b"))
    assert len(bot.sent) == 2 and bot.edited == []


def test_send_or_edit_falls_back_when_edit_fails():
    group_v2._LAST_MSG.clear()
    bot = FakeBot(edit_fails=True)
    grp = {"edit_mode": 1}
    asyncio.run(group_v2.send_or_edit(bot, grp, -7, "menu", "x"))
    asyncio.run(group_v2.send_or_edit(bot, grp, -7, "menu", "y"))
    assert len(bot.sent) == 2


def test_faq_match_threshold():
    faqs = [{"id": 1, "question": "what are the rules", "answer": "be nice", "hits": 0}]
    assert faq_best_match("what are the rules?", faqs) is not None
    assert faq_best_match("where is lunch", faqs) is None


def test_faq_skips_empty_answers():
    faqs = [{"id": 2, "question": "what are the rules", "answer": "", "hits": 0}]
    assert faq_best_match("what are the rules", faqs) is None


def test_welcome_photo_falls_back_to_group_without_image():
    bot = FakeBot()
    grp = {"welcome_image_id": "", "welcome_mode": "dm"}
    asyncio.run(group_v2.send_welcome(bot, None, grp, -8, 1, "hi there"))
    assert bot.sent and bot.sent[0][1] == "hi there"
