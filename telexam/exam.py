"""Tests, users and attempts in SQLite, and the rules of an exam: time limit, order, scoring.

Nothing here knows about Telegram, so the whole exam can be run from Python:

>>> exam = Exam(':memory:')
>>> test = exam.add_test({'name': 'Demo', 'minutes': 5, 'questions': {'2 + 2 = ?': ['4', '5', '22']}})
>>> exam.register(1, 'Anna')
>>> attempt = exam.start(1, test.id)
>>> item = attempt.item()
>>> attempt = exam.answer(attempt.id, 0, item.answers.index('4'))
>>> attempt.finished is not None, attempt.score, attempt.percent
(True, 1, 100)
"""

from __future__ import annotations

import json
import random
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

NAME_LIMIT = 64
QUESTION_LIMIT = 3000   # a question and its header must fit into one Telegram message
ANSWER_LIMIT = 200
ANSWERS_LIMIT = 10

SCHEMA = """
CREATE TABLE IF NOT EXISTS tests (
    id        INTEGER PRIMARY KEY,
    name      TEXT NOT NULL UNIQUE,
    questions TEXT NOT NULL,              -- JSON: [[question, [correct answer, other answers...]], ...]
    minutes   INTEGER NOT NULL,
    points    INTEGER NOT NULL,
    shuffle   INTEGER NOT NULL DEFAULT 0, -- shuffle the order of questions too
    archived  INTEGER NOT NULL DEFAULT 0,
    created   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY,       -- Telegram user id
    name       TEXT NOT NULL,
    username   TEXT,
    registered TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
    id       INTEGER PRIMARY KEY,
    test_id  INTEGER NOT NULL REFERENCES tests(id),
    user_id  INTEGER NOT NULL REFERENCES users(id),
    items    TEXT NOT NULL,               -- JSON: the questions as this user sees them
    answers  TEXT NOT NULL DEFAULT '[]',  -- JSON: index of the chosen answer for each question so far
    started  TEXT NOT NULL,
    deadline TEXT NOT NULL,
    finished TEXT,
    reason   TEXT,                        -- done, time or stopped
    message  INTEGER                      -- Telegram message with the current question
);
CREATE INDEX IF NOT EXISTS attempts_open ON attempts(user_id, finished);
"""


class ExamError(Exception):
    """Something the user asked for can't be done; the text explains why."""


class TestFormatError(ExamError):
    """A test file is not valid."""

    __test__ = False  # not a pytest test class


class ActiveAttempt(ExamError):
    def __init__(self, attempt: Attempt):
        super().__init__(f'the test "{attempt.test_name}" is not finished yet')
        self.attempt = attempt


class Stale(ExamError):
    """An answer to a question that is not the current one: an old button was pressed."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- tests

@dataclass(frozen=True)
class Test:
    __test__ = False  # not a pytest test class

    id: int
    name: str
    questions: list[tuple[str, list[str]]]
    minutes: int
    points: int
    shuffle: bool = False
    archived: bool = False

    @property
    def total_points(self) -> int:
        return self.points * len(self.questions)


def parse_test(data: Mapping[str, Any]) -> dict:
    """Check a test given as a dict, like a JSON file, and normalise it.

    The first answer of every question is the correct one.

    >>> parse_test({'name': ' Demo ', 'questions': {'Q?': ['yes', 'no']}})['minutes']
    30
    >>> parse_test({'name': 'Demo', 'questions': {'Q?': ['yes']}})
    Traceback (most recent call last):
    ...
    telexam.exam.TestFormatError: question 1 ("Q?") needs at least 2 answers
    """
    if not isinstance(data, Mapping):
        raise TestFormatError('a test must be a JSON object with "name" and "questions"')
    unknown = set(data) - {'name', 'minutes', 'points', 'shuffle', 'questions'}
    if unknown:
        raise TestFormatError(f'unknown keys: {", ".join(sorted(unknown))}')
    name = data.get('name')
    if not isinstance(name, str) or not name.strip():
        raise TestFormatError('"name" must be a non-empty string')
    name = ' '.join(name.split())
    if len(name) > NAME_LIMIT:
        raise TestFormatError(f'"name" is longer than {NAME_LIMIT} characters')
    minutes = _number(data, 'minutes', 30, 1, 24 * 60)
    points = _number(data, 'points', 1, 1, 100)
    shuffle = data.get('shuffle', False)
    if not isinstance(shuffle, bool):
        raise TestFormatError('"shuffle" must be true or false')

    raw = data.get('questions')
    if isinstance(raw, Mapping):
        raw = list(raw.items())
    elif isinstance(raw, list):  # [{"question": ..., "answers": [...]}, ...] or [[question, answers], ...]
        try:
            raw = [(entry['question'], entry['answers']) if isinstance(entry, Mapping) else tuple(entry)
                   for entry in raw]
        except (TypeError, KeyError):
            raise TestFormatError('"questions" must map each question to its answers') from None
        if any(len(entry) != 2 for entry in raw):
            raise TestFormatError('"questions" must map each question to its answers')
    else:
        raise TestFormatError('"questions" must map each question to its answers')
    if not raw:
        raise TestFormatError('the test has no questions')

    questions, seen = [], set()
    for number, (question, answers) in enumerate(raw, 1):
        label = f'question {number}'
        if not isinstance(question, str) or not question.strip():
            raise TestFormatError(f'{label} is empty')
        question = question.strip()
        label += f' ("{question[:40]}")'
        if question in seen:
            raise TestFormatError(f'{label} is repeated')
        seen.add(question)
        if len(question) > QUESTION_LIMIT:
            raise TestFormatError(f'{label} is longer than {QUESTION_LIMIT} characters')
        if not isinstance(answers, list) or len(answers) < 2:
            raise TestFormatError(f'{label} needs at least 2 answers')
        if len(answers) > ANSWERS_LIMIT:
            raise TestFormatError(f'{label} has more than {ANSWERS_LIMIT} answers')
        if not all(isinstance(answer, (str, int, float)) and str(answer).strip() for answer in answers):
            raise TestFormatError(f'{label} has an empty answer')
        answers = [' '.join(str(answer).split()) for answer in answers]
        if len(set(answers)) != len(answers):
            raise TestFormatError(f'{label} has the same answer twice')
        if any(len(answer) > ANSWER_LIMIT for answer in answers):
            raise TestFormatError(f'{label} has an answer longer than {ANSWER_LIMIT} characters')
        questions.append((question, answers))
    return {'name': name, 'questions': questions, 'minutes': minutes, 'points': points, 'shuffle': shuffle}


def _number(data: Mapping, key: str, default: int, low: int, high: int) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise TestFormatError(f'"{key}" must be a whole number from {low} to {high}')
    return value


def load_test_file(path: str | Path) -> dict:
    try:
        data = json.loads(Path(path).read_text('utf-8-sig'))
    except json.JSONDecodeError as error:
        raise TestFormatError(f'not valid JSON: {error}') from None
    return parse_test(data)


# ---------------------------------------------------------------- attempts

@dataclass(frozen=True)
class Item:
    """A question as one user sees it: answers already shuffled."""

    question: str
    answers: list[str]
    correct: int


@dataclass
class Attempt:
    id: int
    test_id: int
    test_name: str
    user_id: int
    points: int
    items: list[Item]
    answers: list[int]
    started: datetime
    deadline: datetime
    finished: datetime | None = None
    reason: str | None = None
    message: int | None = None
    user_name: str = ''

    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def position(self) -> int:
        """Index of the current question, the number of answers given."""
        return len(self.answers)

    def item(self) -> Item:
        return self.items[self.position]

    @property
    def score(self) -> int:
        return sum(1 for item, answer in zip(self.items, self.answers) if answer == item.correct)

    @property
    def earned(self) -> int:
        return self.score * self.points

    @property
    def percent(self) -> int:
        return round(100 * self.score / self.total) if self.total else 0

    @property
    def mistakes(self) -> list[tuple[Item, int | None]]:
        """Questions answered wrong or not at all, with the chosen answer."""
        out = []
        for index, item in enumerate(self.items):
            answer = self.answers[index] if index < len(self.answers) else None
            if answer != item.correct:
                out.append((item, answer))
        return out

    def remaining(self, now: datetime) -> timedelta:
        return max(self.deadline - now, timedelta(0))

    @property
    def duration(self) -> timedelta:
        return (self.finished or self.deadline) - self.started


@dataclass
class Exam:
    """The database and the rules. Safe to use from the bot thread and the timer thread."""

    path: str | Path = 'telexam.db'
    clock: Callable[[], datetime] = utcnow
    rng: random.Random = field(default_factory=random.Random)

    def __post_init__(self):
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys = ON')
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()

    def close(self) -> None:
        self.db.close()

    def _all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.db.execute(sql, params).fetchall()

    def _one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self.lock:
            return self.db.execute(sql, params).fetchone()

    # tests

    def add_test(self, data: Mapping[str, Any], replace: bool = False) -> Test:
        """Add a test, or with ``replace`` update the test of the same name.

        Attempts keep their own copy of the questions, so changing a test does
        not spoil the results of people who already took it.
        """
        test = parse_test(data)
        with self.lock, self.db:
            wanted = test['name'].casefold()
            row = next((row for row in self.db.execute('SELECT id, name, archived FROM tests')
                        if row['name'].casefold() == wanted), None)
            values = (json.dumps(test['questions'], ensure_ascii=False), test['minutes'], test['points'],
                      int(test['shuffle']))
            if row is None:
                cursor = self.db.execute(
                    'INSERT INTO tests (name, questions, minutes, points, shuffle, created) VALUES (?, ?, ?, ?, ?, ?)',
                    (test['name'], *values, self.clock().isoformat()))
                test_id = cursor.lastrowid
            elif replace or row['archived']:
                self.db.execute('UPDATE tests SET name = ?, questions = ?, minutes = ?, points = ?, shuffle = ?, '
                                'archived = 0 WHERE id = ?', (test['name'], *values, row['id']))
                test_id = row['id']
            else:
                raise ExamError(f'a test named "{test["name"]}" already exists')
        return self.test(test_id)

    def test(self, test_id: int) -> Test | None:
        row = self._one('SELECT * FROM tests WHERE id = ?', (test_id,))
        return _test(row) if row else None

    def find_test(self, name: str) -> Test | None:
        wanted = ' '.join(name.split()).casefold()  # SQLite NOCASE only knows Latin letters
        return next((test for test in self.tests() if test.name.casefold() == wanted), None)

    def tests(self) -> list[Test]:
        return [_test(row) for row in self._all('SELECT * FROM tests WHERE archived = 0 ORDER BY id')]

    def archive_test(self, test_id: int) -> None:
        """Hide a test from the list. Its results stay."""
        with self.lock, self.db:
            self.db.execute('UPDATE tests SET archived = 1 WHERE id = ?', (test_id,))

    # users

    def register(self, user_id: int, name: str, username: str | None = None) -> None:
        with self.lock, self.db:
            self.db.execute(
                'INSERT INTO users (id, name, username, registered) VALUES (?, ?, ?, ?) '
                'ON CONFLICT(id) DO UPDATE SET name = excluded.name, username = excluded.username',
                (user_id, name, username, self.clock().isoformat()))

    def users(self) -> int:
        return self._one('SELECT COUNT(*) FROM users')[0]

    # attempts

    def active(self, user_id: int) -> Attempt | None:
        """The unfinished attempt of a user, even if its time is already over."""
        row = self._one(f'{_ATTEMPTS} WHERE a.user_id = ? AND a.finished IS NULL ORDER BY a.id DESC', (user_id,))
        return _attempt(row) if row else None

    def attempt(self, attempt_id: int) -> Attempt | None:
        row = self._one(f'{_ATTEMPTS} WHERE a.id = ?', (attempt_id,))
        return _attempt(row) if row else None

    def start(self, user_id: int, test_id: int) -> Attempt:
        with self.lock:
            active = self.active(user_id)
            if active is not None:
                raise ActiveAttempt(active)
            test = self.test(test_id)
            if test is None or test.archived:
                raise ExamError('this test is no longer available')
            questions = list(test.questions)
            if test.shuffle:
                self.rng.shuffle(questions)
            items = []
            for question, answers in questions:
                order = list(range(len(answers)))
                self.rng.shuffle(order)
                items.append({'question': question, 'answers': [answers[i] for i in order],
                              'correct': order.index(0)})
            now = self.clock()
            with self.db:
                cursor = self.db.execute(
                    'INSERT INTO attempts (test_id, user_id, items, started, deadline) VALUES (?, ?, ?, ?, ?)',
                    (test.id, user_id, json.dumps(items, ensure_ascii=False), now.isoformat(),
                     (now + timedelta(minutes=test.minutes)).isoformat()))
            return self.attempt(cursor.lastrowid)

    def answer(self, attempt_id: int, position: int, choice: int) -> Attempt:
        """Save an answer to the question at ``position`` and return the attempt.

        When the time is over the answer is not counted and the attempt is
        finished with the reason ``time``. After the last answer the attempt
        is finished with ``done``.
        """
        with self.lock:
            attempt = self.attempt(attempt_id)
            if attempt is None or attempt.finished is not None:
                raise Stale('this test is already finished')
            if position != attempt.position:
                raise Stale('this question has already been answered')
            if self.clock() >= attempt.deadline:
                return self.finish(attempt, 'time')  # never None: the lock is held and it was unfinished
            if not 0 <= choice < len(attempt.item().answers):
                raise Stale('no such answer')
            attempt.answers.append(choice)
            with self.db:
                self.db.execute('UPDATE attempts SET answers = ? WHERE id = ?',
                                (json.dumps(attempt.answers), attempt.id))
            if attempt.position == attempt.total:
                return self.finish(attempt, 'done')
            return attempt

    def remember_message(self, attempt_id: int, message_id: int | None) -> None:
        with self.lock, self.db:
            self.db.execute('UPDATE attempts SET message = ? WHERE id = ?', (message_id, attempt_id))

    def finish(self, attempt: Attempt, reason: str) -> Attempt | None:
        """Finish an attempt and return it, or None if it was already finished.

        Only the caller that really finished it gets the attempt back, so the
        result is sent once even when the timer and the user act together.
        """
        with self.lock, self.db:
            now = min(self.clock(), attempt.deadline)
            cursor = self.db.execute('UPDATE attempts SET finished = ?, reason = ? WHERE id = ? AND finished IS NULL',
                                     (now.isoformat(), reason, attempt.id))
        return self.attempt(attempt.id) if cursor.rowcount else None

    def finish_if_expired(self, attempt: Attempt) -> Attempt | None:
        """Finish an attempt whose time is over; None if it is still running or already finished."""
        with self.lock:
            if self.clock() < attempt.deadline:
                return None
            return self.finish(attempt, 'time')

    def expired(self) -> list[Attempt]:
        """Finish every attempt whose time is over and return them, to tell their users."""
        with self.lock:
            now = self.clock().isoformat()
            rows = self._all(f'{_ATTEMPTS} WHERE a.finished IS NULL AND a.deadline <= ?', (now,))
            return [attempt for row in rows if (attempt := self.finish(_attempt(row), 'time'))]

    def history(self, user_id: int, limit: int = 10) -> list[Attempt]:
        rows = self._all(f'{_ATTEMPTS} WHERE a.user_id = ? AND a.finished IS NOT NULL ORDER BY a.id DESC LIMIT ?',
                         (user_id, limit))
        return [_attempt(row) for row in rows]

    def results(self, test_id: int | None = None) -> list[Attempt]:
        """Finished attempts of everybody, oldest first."""
        where = 'WHERE a.finished IS NOT NULL' + (' AND a.test_id = ?' if test_id is not None else '')
        rows = self._all(f'{_ATTEMPTS} {where} ORDER BY a.id', () if test_id is None else (test_id,))
        return [_attempt(row) for row in rows]


_ATTEMPTS = ('SELECT a.*, t.name AS test_name, t.points AS points, u.name AS user_name '
             'FROM attempts a JOIN tests t ON t.id = a.test_id JOIN users u ON u.id = a.user_id')


def _test(row: sqlite3.Row) -> Test:
    return Test(id=row['id'], name=row['name'], questions=[(q, a) for q, a in json.loads(row['questions'])],
                minutes=row['minutes'], points=row['points'], shuffle=bool(row['shuffle']),
                archived=bool(row['archived']))


def _attempt(row: sqlite3.Row) -> Attempt:
    return Attempt(
        id=row['id'], test_id=row['test_id'], test_name=row['test_name'], user_id=row['user_id'],
        points=row['points'], items=[Item(**item) for item in json.loads(row['items'])],
        answers=json.loads(row['answers']), started=datetime.fromisoformat(row['started']),
        deadline=datetime.fromisoformat(row['deadline']),
        finished=datetime.fromisoformat(row['finished']) if row['finished'] else None,
        reason=row['reason'], message=row['message'], user_name=row['user_name'],
    )


def to_csv_rows(attempts: Iterable[Attempt]) -> list[list[str]]:
    """Results as rows for a spreadsheet, with a header."""
    rows = [['test', 'user id', 'name', 'started (UTC)', 'minutes', 'correct', 'questions', 'percent',
             'points', 'finished by']]
    for attempt in attempts:
        rows.append([attempt.test_name, str(attempt.user_id), attempt.user_name,
                     attempt.started.strftime('%Y-%m-%d %H:%M'), f'{attempt.duration.total_seconds() / 60:.1f}',
                     str(attempt.score), str(attempt.total), str(attempt.percent), str(attempt.earned),
                     attempt.reason or ''])
    return rows
