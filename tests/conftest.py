"""A fake Telegram API and a clock that the tests move by hand."""

from __future__ import annotations

import itertools
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from telebot import apihelper, types

from telexam import Exam, Settings
from telexam.bot import ExamBot

ROOT = Path(__file__).resolve().parent.parent
TOKEN = '123456789:' + 'A' * 35
ADMIN = 1000
USER = 2000

DEMO = {'name': 'Demo', 'minutes': 10, 'points': 2,
        'questions': {'2 + 2 = ?': ['4', '5', '22'], 'Capital of France?': ['Paris', 'Rome'],
                      'Largest planet?': ['Jupiter', 'Mars', 'Venus', 'Earth']}}


class Response:
    def __init__(self, payload: dict, status: int = 200):
        self.payload, self.status_code = payload, status
        self.text = json.dumps(payload)
        self.reason = 'OK'

    def json(self):
        return self.payload


class FakeTelegram:
    def __init__(self):
        self.requests: list[tuple[str, dict]] = []
        self.files: dict[str, bytes] = {}
        self._ids = itertools.count(100)

    def __call__(self, method, url, params=None, files=None, **kwargs):
        name = url.rsplit('/', 1)[-1]
        params = dict(params or {})
        if files:
            params['files'] = {key: (getattr(value, 'name', None), value.getvalue() if hasattr(value, 'getvalue')
                                     else value) for key, value in files.items()}
        self.requests.append((name, params))
        if name in ('sendMessage', 'sendDocument', 'editMessageText'):
            chat = int(params.get('chat_id') or USER)
            return Response({'ok': True, 'result': {'message_id': next(self._ids), 'date': 0,
                                                    'chat': {'id': chat, 'type': 'private'},
                                                    'text': params.get('text', '')}})
        if name == 'getFile':
            return Response({'ok': True, 'result': {'file_id': params['file_id'], 'file_unique_id': 'u',
                                                    'file_path': params['file_id']}})
        if name == 'getMe':
            return Response({'ok': True, 'result': {'id': 1, 'is_bot': True, 'first_name': 'Exam',
                                                    'username': 'test_exam_bot'}})
        return Response({'ok': True, 'result': True})

    def calls(self, name: str) -> list[dict]:
        return [params for method, params in self.requests if method == name]

    def sent(self, chat_id: int = USER) -> list[dict]:
        return [params for method, params in self.requests
                if method in ('sendMessage', 'sendDocument') and int(params['chat_id']) == chat_id]

    def last(self, chat_id: int = USER) -> dict:
        return self.sent(chat_id)[-1]

    def clear(self):
        self.requests.clear()


def keyboard(params: dict) -> list[list[dict]]:
    return json.loads(params['reply_markup'])['inline_keyboard'] if params.get('reply_markup') else []


def buttons(params: dict) -> dict[str, str]:
    """Button text → callback data."""
    return {button['text']: button['callback_data'] for row in keyboard(params) for button in row}


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)


@pytest.fixture
def telegram(monkeypatch):
    fake = FakeTelegram()
    monkeypatch.setattr(apihelper, 'CUSTOM_REQUEST_SENDER', fake)
    monkeypatch.setattr(apihelper, 'download_file', lambda token, path: fake.files[path])
    return fake


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def exam(tmp_path, clock):
    exam = Exam(tmp_path / 'test.db', clock=clock, rng=random.Random(7))
    yield exam
    exam.close()


@pytest.fixture
def demo(exam):
    return exam.add_test(DEMO)


@pytest.fixture
def bot(telegram, exam):
    return ExamBot(Settings(token=TOKEN, admins=frozenset({ADMIN})), exam)


_ids = itertools.count(1)


def user_json(user_id: int) -> dict:
    return {'id': user_id, 'is_bot': False, 'first_name': 'Анна' if user_id == USER else 'Админ',
            'last_name': 'Смирнова', 'username': 'anna'}


def send(bot: ExamBot, text: str = '', user_id: int = USER, document: dict | None = None):
    message = {'message_id': next(_ids), 'date': 0, 'chat': {'id': user_id, 'type': 'private'},
               'from': user_json(user_id)}
    if document:
        message['document'] = document
    else:
        message['text'] = text
        if text.startswith('/'):
            message['entities'] = [{'type': 'bot_command', 'offset': 0, 'length': len(text.split()[0])}]
    bot.bot.process_new_updates([types.Update.de_json({'update_id': next(_ids), 'message': message})])


def press(bot: ExamBot, data: str, user_id: int = USER, message_id: int = 55):
    bot.bot.process_new_updates([types.Update.de_json({'update_id': next(_ids), 'callback_query': {
        'id': str(next(_ids)), 'chat_instance': '1', 'data': data, 'from': user_json(user_id),
        'message': {'message_id': message_id, 'date': 0, 'text': '...', 'chat': {'id': user_id, 'type': 'private'}},
    }})])


def answer_question(bot: ExamBot, telegram: FakeTelegram, correct: bool, user_id: int = USER):
    """Press the right or a wrong answer under the last question sent."""
    question = telegram.last(user_id)
    attempt = bot.exam.active(user_id)
    item = attempt.item()
    wanted = item.correct if correct else next(i for i in range(len(item.answers)) if i != item.correct)
    data = f'a:{attempt.id}:{attempt.position}:{wanted}'
    assert data in buttons(question).values()
    press(bot, data, user_id)
