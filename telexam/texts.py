"""Everything the bot says, in Russian."""

from __future__ import annotations

from datetime import timedelta
from html import escape

from .exam import Attempt, Test

MESSAGE_LIMIT = 4096
LONG_ANSWER = 32  # longer answers are listed in the message, the buttons only show numbers


def plural(number: int, one: str, few: str, many: str) -> str:
    """Russian plural: 1 вопрос, 2 вопроса, 5 вопросов.

    >>> [plural(n, 'вопрос', 'вопроса', 'вопросов') for n in (1, 2, 5, 11, 21, 104)]
    ['1 вопрос', '2 вопроса', '5 вопросов', '11 вопросов', '21 вопрос', '104 вопроса']
    """
    if number % 10 == 1 and number % 100 != 11:
        word = one
    elif 2 <= number % 10 <= 4 and not 12 <= number % 100 <= 14:
        word = few
    else:
        word = many
    return f'{number} {word}'


def questions(number: int) -> str:
    return plural(number, 'вопрос', 'вопроса', 'вопросов')


def minutes(number: int) -> str:
    return plural(number, 'минута', 'минуты', 'минут')


def points(number: int) -> str:
    return plural(number, 'балл', 'балла', 'баллов')


def duration(delta: timedelta) -> str:
    """
    >>> duration(timedelta(minutes=4, seconds=12)), duration(timedelta(seconds=9)), duration(timedelta(hours=1, minutes=5))
    ('4 мин 12 с', '9 с', '1 ч 5 мин')
    """
    seconds = int(delta.total_seconds())
    hours, rest = divmod(seconds, 3600)
    mins, secs = divmod(rest, 60)
    if hours:
        return f'{hours} ч {mins} мин'
    return f'{mins} мин {secs} с' if mins else f'{secs} с'


def remaining(delta: timedelta) -> str:
    """
    >>> remaining(timedelta(minutes=29, seconds=1)), remaining(timedelta(seconds=40))
    ('осталось 30 мин', 'осталось меньше минуты')
    """
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return 'осталось меньше минуты'
    return f'осталось {-(-seconds // 60)} мин'


def name(user) -> str:
    return ' '.join(part for part in (user.first_name, user.last_name) if part) or user.username or str(user.id)


# ---------------------------------------------------------------- messages

def greeting(user_name: str, tests: list[Test]) -> str:
    text = f'Здравствуйте, {escape(user_name)}! 👋\n\nЯ провожу тестирование.'
    if not tests:
        return text + ' Тестов пока нет, загляните позже.'
    return text + ' Выберите тест, чтобы узнать о нём подробнее:'


def test_list(tests: list[Test]) -> str:
    return 'Выберите тест:' if tests else 'Тестов пока нет.'


def test_button(test: Test) -> str:
    return f'{test.name} · {questions(len(test.questions))}'


def test_card(test: Test) -> str:
    return (f'📝 <b>{escape(test.name)}</b>\n\n'
            f'• {questions(len(test.questions))}\n'
            f'• на всё {minutes(test.minutes)}\n'
            f'• {points(test.points)} за правильный ответ\n\n'
            'Время пойдёт сразу после нажатия «Начать». Ответ нельзя изменить, '
            'а неотвеченные вопросы засчитываются как неверные.')


def question(attempt: Attempt, now) -> tuple[str, list[str]]:
    """The text of the current question and the labels of its buttons."""
    item = attempt.item()
    text = (f'<b>{escape(attempt.test_name)}</b> · вопрос {attempt.position + 1} из {attempt.total}\n'
            f'⏱ {remaining(attempt.remaining(now))}\n\n{escape(item.question)}')
    if any(len(answer) > LONG_ANSWER for answer in item.answers):
        text += '\n\n' + '\n'.join(f'<b>{number}.</b> {escape(answer)}'
                                   for number, answer in enumerate(item.answers, 1))
        return text, [str(number) for number in range(1, len(item.answers) + 1)]
    return text, list(item.answers)


def answered(attempt: Attempt, position: int) -> str:
    """The question after it was answered, with the answer and without buttons."""
    item = attempt.items[position]
    return (f'<b>{escape(attempt.test_name)}</b> · вопрос {position + 1} из {attempt.total}\n\n'
            f'{escape(item.question)}\n\n'
            f'Ваш ответ: <b>{escape(item.answers[attempt.answers[position]])}</b>')


def grade(percent: int) -> str:
    return '🏆' if percent >= 90 else '👍' if percent >= 70 else '🙂' if percent >= 50 else '📚'


HEADERS = {
    'done': '🏁 Тест «{}» завершён',
    'time': '⏰ Время вышло, тест «{}» завершён',
    'stopped': '⏹ Тест «{}» остановлен',
}


def result(attempt: Attempt) -> str:
    total_points = attempt.points * attempt.total
    text = (f'<b>{HEADERS.get(attempt.reason, HEADERS["done"]).format(escape(attempt.test_name))}</b>\n\n'
            f'{grade(attempt.percent)} Правильных ответов: <b>{attempt.score} из {attempt.total}</b> '
            f'({attempt.percent}%)\n'
            f'Баллы: {attempt.earned} из {total_points}\n'
            f'Время: {duration(attempt.duration)}')
    mistakes = attempt.mistakes
    if not mistakes:
        return text + '\n\nВсе ответы верные!'
    text += '\n\n<b>Разбор ошибок</b>'
    shown = 0
    for item, answer in mistakes:
        number = attempt.items.index(item) + 1
        chosen = f'✗ {escape(item.answers[answer])}' if answer is not None else '— без ответа'
        block = (f'\n\n{number}. {escape(_short(item.question))}\n{chosen}\n'
                 f'✓ {escape(item.answers[item.correct])}')
        if len(text) + len(block) > MESSAGE_LIMIT - 60:
            break
        text += block
        shown += 1
    if shown < len(mistakes):
        text += f'\n\n…и ещё {plural(len(mistakes) - shown, "ошибка", "ошибки", "ошибок")}'
    return text


def _short(text: str, limit: int = 300) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def history(attempts: list[Attempt]) -> str:
    if not attempts:
        return 'Вы ещё не прошли ни одного теста. Выбрать тест: /tests'
    lines = ['<b>Ваши результаты</b>\n']
    for attempt in attempts:
        lines.append(f'{grade(attempt.percent)} {escape(attempt.test_name)}: {attempt.score} из {attempt.total} '
                     f'({attempt.percent}%), {attempt.started:%d.%m.%Y}')
    return '\n'.join(lines)


def active(attempt: Attempt) -> str:
    return (f'У вас не закончен тест «{escape(attempt.test_name)}»: отвечено {attempt.position} '
            f'из {attempt.total}. Продолжить его или завершить?')


CONFIRM_STOP = 'Завершить тест сейчас? Неотвеченные вопросы будут засчитаны как неверные.'
USE_BUTTONS = 'Выберите ответ кнопкой под вопросом 👇'
NOTHING_ACTIVE = 'Сейчас вы не проходите тест. Выбрать тест: /tests'
UNKNOWN = 'Я понимаю команды и кнопки. Список тестов: /tests, помощь: /help'
GONE = 'Этого теста больше нет'


def help_text(is_admin: bool) -> str:
    text = ('<b>Команды</b>\n'
            '/tests — список тестов\n'
            '/results — ваши результаты\n'
            '/finish — завершить текущий тест\n'
            '/help — эта справка')
    if is_admin:
        text += ('\n\n<b>Для администратора</b>\n'
                 '/stats — результаты по тестам и выгрузка в CSV\n'
                 '/add — как добавить или обновить тест\n'
                 '/delete — убрать тест из списка')
    return text


ADD_HELP = """Пришлите JSON-файл с тестом. Первый ответ в списке — правильный, бот перемешает их сам:

<pre>{
  "name": "Физика, 8 класс",
  "minutes": 15,
  "points": 1,
  "shuffle": true,
  "questions": {
    "Единица измерения силы тока:": ["Ампер", "Вольт", "Ватт"],
    "Сатурн — какая по счёту планета от Солнца?": ["6", "7", "8"]
  }
}</pre>

<code>minutes</code> — время на весь тест, <code>points</code> — баллы за ответ, <code>shuffle</code> — перемешивать вопросы. Если тест с таким названием уже есть, он будет обновлён: прошлые результаты сохранятся."""


def stats(tests: list[Test], results: dict[int, list[Attempt]], users: int) -> str:
    lines = [f'<b>Статистика</b>\nПользователей: {users}\n']
    if not tests:
        lines.append('Тестов пока нет. Добавить: /add')
    for test in tests:
        attempts = results.get(test.id, [])
        if attempts:
            average = round(sum(attempt.percent for attempt in attempts) / len(attempts))
            best = max(attempts, key=lambda attempt: (attempt.percent, -attempt.duration.total_seconds()))
            lines.append(f'📝 <b>{escape(test.name)}</b>: {plural(len(attempts), "попытка", "попытки", "попыток")}, '
                         f'в среднем {average}%, лучший — {escape(best.user_name)} ({best.percent}%)')
        else:
            lines.append(f'📝 <b>{escape(test.name)}</b>: попыток ещё нет')
    return '\n'.join(lines)


def added(test: Test, updated: bool) -> str:
    verb = 'обновлён' if updated else 'добавлен'
    return (f'✅ Тест «{escape(test.name)}» {verb}: {questions(len(test.questions))}, '
            f'{minutes(test.minutes)}, {points(test.points)} за ответ.')
