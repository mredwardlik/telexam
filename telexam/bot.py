"""The Telegram side: commands, buttons, the timer that finishes tests when time runs out."""

from __future__ import annotations

import csv
import io
import json
import logging
import threading

import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException

from . import texts
from .exam import ActiveAttempt, Attempt, Exam, ExamError, Stale, TestFormatError, to_csv_rows
from .settings import Settings

log = logging.getLogger(__name__)

TICK = 10  # seconds between checks for tests whose time is over
FILE_LIMIT = 1024 * 1024

COMMANDS = [
    types.BotCommand('tests', 'Список тестов'),
    types.BotCommand('results', 'Мои результаты'),
    types.BotCommand('finish', 'Завершить текущий тест'),
    types.BotCommand('help', 'Помощь'),
]


class LogErrors(telebot.ExceptionHandler):
    """An error in one update is logged, the bot keeps working."""

    def handle(self, exception):
        log.error('Error while handling an update', exc_info=exception)
        return True


def button(text: str, data: str) -> types.InlineKeyboardButton:
    return types.InlineKeyboardButton(text, callback_data=data)


class ExamBot:
    def __init__(self, settings: Settings, exam: Exam | None = None, *, threaded: bool = False):
        self.settings = settings
        self.exam = exam or Exam(settings.database)
        self.bot = telebot.TeleBot(settings.token, parse_mode='HTML', threaded=threaded,
                                   exception_handler=LogErrors())
        self._stop = threading.Event()
        self._register()

    # helpers

    def is_admin(self, user: types.User | None) -> bool:
        return user is not None and user.id in self.settings.admins

    def register(self, user: types.User) -> None:
        self.exam.register(user.id, texts.name(user), user.username)

    def tests_markup(self) -> types.InlineKeyboardMarkup:
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(*(button(texts.test_button(test), f'test:{test.id}') for test in self.exam.tests()))
        return markup

    def show_tests(self, chat_id: int) -> None:
        tests = self.exam.tests()
        self.bot.send_message(chat_id, texts.test_list(tests), reply_markup=self.tests_markup())

    def show_active(self, chat_id: int, attempt: Attempt) -> None:
        markup = types.InlineKeyboardMarkup(row_width=2).add(
            button('▶️ Продолжить', 'continue'), button('⏹ Завершить', f'stop:{attempt.id}'))
        self.bot.send_message(chat_id, texts.active(attempt), reply_markup=markup)

    def current(self, user_id: int) -> Attempt | None:
        """The unfinished attempt of a user. One whose time is over is finished and reported."""
        attempt = self.exam.active(user_id)
        if attempt is not None and self.exam.clock() >= attempt.deadline:
            finished = self.exam.finish_if_expired(attempt)
            if finished is not None:
                self.report(finished)
            return None
        return attempt

    def ask(self, attempt: Attempt) -> None:
        """Send the current question with answer buttons."""
        text, labels = texts.question(attempt, self.exam.clock())
        markup = types.InlineKeyboardMarkup(row_width=len(labels) if labels[0].isdigit() else 1)
        markup.add(*(button(label, f'a:{attempt.id}:{attempt.position}:{index}')
                     for index, label in enumerate(labels)))
        message = self.bot.send_message(attempt.user_id, text, reply_markup=markup)
        self.exam.remember_message(attempt.id, message.message_id)

    def close_question(self, attempt: Attempt) -> None:
        """Take the buttons off the question message when the test ends without an answer."""
        if attempt.message is None:
            return
        try:
            self.bot.edit_message_reply_markup(attempt.user_id, attempt.message, reply_markup=None)
        except ApiTelegramException as error:
            log.debug("Can't remove the buttons: %s", error.description)

    def report(self, attempt: Attempt) -> None:
        """Send the result of a finished attempt."""
        if attempt.reason != 'done':
            self.close_question(attempt)
        markup = types.InlineKeyboardMarkup().add(button('📋 Другие тесты', 'list'))
        self.bot.send_message(attempt.user_id, texts.result(attempt), reply_markup=markup)
        log.info('User %s finished "%s": %s/%s (%s)', attempt.user_id, attempt.test_name, attempt.score,
                 attempt.total, attempt.reason)

    def tick(self) -> None:
        """Finish the tests whose time is over and tell their users."""
        for attempt in self.exam.expired():
            try:
                self.report(attempt)
            except ApiTelegramException as error:
                log.warning("Can't send the result to %s: %s", attempt.user_id, error.description)

    # handlers

    def _register(self):
        bot = self.bot

        @bot.message_handler(commands=['start'])
        def start(message: types.Message):
            self.register(message.from_user)
            attempt = self.current(message.from_user.id)
            if attempt is not None:
                return self.show_active(message.chat.id, attempt)
            tests = self.exam.tests()
            text = texts.greeting(message.from_user.first_name or texts.name(message.from_user), tests)
            if self.is_admin(message.from_user):
                text += '\n\nВы администратор: /help покажет ваши команды.'
            bot.send_message(message.chat.id, text, reply_markup=self.tests_markup())

        @bot.message_handler(commands=['tests'])
        def tests(message: types.Message):
            self.register(message.from_user)
            self.show_tests(message.chat.id)

        @bot.message_handler(commands=['help'])
        def help_(message: types.Message):
            bot.send_message(message.chat.id, texts.help_text(self.is_admin(message.from_user)))

        @bot.message_handler(commands=['results'])
        def results(message: types.Message):
            bot.send_message(message.chat.id, texts.history(self.exam.history(message.from_user.id)))

        @bot.message_handler(commands=['finish'])
        def finish(message: types.Message):
            attempt = self.current(message.from_user.id)
            if attempt is None:
                return bot.send_message(message.chat.id, texts.NOTHING_ACTIVE)
            markup = types.InlineKeyboardMarkup(row_width=2).add(
                button('⏹ Да, завершить', f'stop:{attempt.id}'), button('▶️ Продолжить', 'continue'))
            bot.send_message(message.chat.id, texts.CONFIRM_STOP, reply_markup=markup)

        # admin commands

        @bot.message_handler(commands=['stats'], func=lambda message: self.is_admin(message.from_user))
        def stats(message: types.Message):
            tests = self.exam.tests()
            grouped: dict[int, list[Attempt]] = {}
            for attempt in self.exam.results():
                grouped.setdefault(attempt.test_id, []).append(attempt)
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(*(button(f'📥 {test.name}.csv', f'csv:{test.id}') for test in tests if test.id in grouped))
            if grouped:
                markup.add(button('📥 Все результаты.csv', 'csv:all'))
            bot.send_message(message.chat.id, texts.stats(tests, grouped, self.exam.users()), reply_markup=markup)

        @bot.message_handler(commands=['add'], func=lambda message: self.is_admin(message.from_user))
        def add(message: types.Message):
            bot.send_message(message.chat.id, texts.ADD_HELP)

        @bot.message_handler(commands=['delete'], func=lambda message: self.is_admin(message.from_user))
        def delete(message: types.Message):
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(*(button(f'🗑 {test.name}', f'del:{test.id}') for test in self.exam.tests()))
            bot.send_message(message.chat.id, 'Какой тест убрать из списка? Результаты по нему сохранятся.'
                             if self.exam.tests() else 'Тестов нет.', reply_markup=markup)

        @bot.message_handler(content_types=['document'], func=lambda message: self.is_admin(message.from_user))
        def document(message: types.Message):
            self.upload(message)

        @bot.message_handler(content_types=['text', 'document', 'photo', 'sticker', 'voice', 'video'])
        def other(message: types.Message):
            if message.text and message.text.startswith('/'):
                return bot.send_message(message.chat.id, texts.UNKNOWN)
            attempt = self.current(message.from_user.id)
            if attempt is None:
                return bot.send_message(message.chat.id, texts.UNKNOWN)
            bot.send_message(message.chat.id, texts.USE_BUTTONS)
            self.ask(attempt)

        @bot.callback_query_handler(func=lambda call: True)
        def callback(call: types.CallbackQuery):
            self.on_button(call)

    def upload(self, message: types.Message) -> None:
        document = message.document
        if not (document.file_name or '').lower().endswith('.json'):
            self.bot.reply_to(message, 'Нужен файл .json с тестом. Формат: /add')
            return
        if document.file_size and document.file_size > FILE_LIMIT:
            self.bot.reply_to(message, 'Файл больше 1 МБ, это слишком много для теста.')
            return
        data = self.bot.download_file(self.bot.get_file(document.file_id).file_path)
        try:
            parsed = json.loads(data.decode('utf-8-sig'))
            exists = isinstance(parsed, dict) and self.exam.find_test(str(parsed.get('name', ''))) is not None
            test = self.exam.add_test(parsed, replace=True)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            self.bot.reply_to(message, f'Это не JSON: {texts.escape(str(error))}')
            return
        except TestFormatError as error:
            self.bot.reply_to(message, f'Тест не добавлен: {texts.escape(str(error))}')
            return
        log.info('Admin %s %s the test "%s"', message.from_user.id, 'updated' if exists else 'added', test.name)
        self.bot.reply_to(message, texts.added(test, updated=exists))

    def on_button(self, call: types.CallbackQuery) -> None:
        bot = self.bot
        user = call.from_user
        chat_id = call.message.chat.id if call.message else user.id
        kind, _, rest = (call.data or '').partition(':')
        self.register(user)

        if kind == 'list':
            bot.answer_callback_query(call.id)
            return self.show_tests(chat_id)

        if kind == 'test':
            test = self.exam.test(int(rest))
            if test is None or test.archived:
                return bot.answer_callback_query(call.id, texts.GONE, show_alert=True)
            bot.answer_callback_query(call.id)
            markup = types.InlineKeyboardMarkup(row_width=2).add(
                button('▶️ Начать', f'go:{test.id}'), button('« Все тесты', 'list'))
            return bot.send_message(chat_id, texts.test_card(test), reply_markup=markup)

        if kind == 'go':
            if self.current(user.id) is None:
                try:
                    attempt = self.exam.start(user.id, int(rest))
                except ActiveAttempt:
                    attempt = None
                except ExamError:
                    return bot.answer_callback_query(call.id, texts.GONE, show_alert=True)
                if attempt is not None:
                    bot.answer_callback_query(call.id, 'Время пошло!')
                    log.info('User %s started "%s"', user.id, attempt.test_name)
                    return self.ask(attempt)
            bot.answer_callback_query(call.id)
            attempt = self.exam.active(user.id)
            return self.show_active(chat_id, attempt) if attempt else None

        if kind == 'continue':
            attempt = self.current(user.id)
            bot.answer_callback_query(call.id, None if attempt else 'Тест уже завершён')
            if attempt is not None:
                self.close_question(attempt)
                self.ask(attempt)
            return None

        if kind == 'stop':
            attempt = self.exam.attempt(int(rest))
            if attempt is None or attempt.user_id != user.id or attempt.finished is not None:
                return bot.answer_callback_query(call.id, 'Тест уже завершён')
            bot.answer_callback_query(call.id)
            reason = 'time' if self.exam.clock() >= attempt.deadline else 'stopped'
            finished = self.exam.finish(attempt, reason)
            return self.report(finished) if finished else None

        if kind == 'a':
            return self.on_answer(call, rest)

        if kind in ('csv', 'del') and self.is_admin(user):
            return self.on_admin_button(call, kind, rest, chat_id)

        bot.answer_callback_query(call.id)
        return None

    def on_answer(self, call: types.CallbackQuery, data: str) -> None:
        try:
            attempt_id, position, choice = (int(part) for part in data.split(':'))
        except ValueError:
            return self.bot.answer_callback_query(call.id)
        attempt = self.exam.attempt(attempt_id)
        if attempt is None or attempt.user_id != call.from_user.id:
            return self.bot.answer_callback_query(call.id)
        try:
            attempt = self.exam.answer(attempt_id, position, choice)
        except Stale as error:
            text = 'На этот вопрос вы уже ответили' if 'answered' in str(error) else 'Этот тест уже завершён'
            return self.bot.answer_callback_query(call.id, text)
        self.bot.answer_callback_query(call.id)
        if attempt.reason == 'time':
            return self.report(attempt)
        if call.message:
            try:
                self.bot.edit_message_text(texts.answered(attempt, position), call.message.chat.id,
                                           call.message.message_id, reply_markup=None)
            except ApiTelegramException as error:
                log.debug("Can't edit the question: %s", error.description)
        if attempt.finished is not None:
            return self.report(attempt)
        return self.ask(attempt)

    def on_admin_button(self, call: types.CallbackQuery, kind: str, rest: str, chat_id: int) -> None:
        bot = self.bot
        if kind == 'csv':
            test = None if rest == 'all' else self.exam.test(int(rest))
            attempts = self.exam.results(None if test is None else test.id)
            buffer = io.StringIO()
            csv.writer(buffer, delimiter=';').writerows(to_csv_rows(attempts))
            data = io.BytesIO(buffer.getvalue().encode('utf-8-sig'))  # Excel opens it with Cyrillic intact
            data.name = f'{test.name if test else "results"}.csv'
            bot.answer_callback_query(call.id)
            return bot.send_document(chat_id, data, caption=f'{texts.plural(len(attempts), "попытка", "попытки", "попыток")}')
        test = self.exam.test(int(rest))
        if test is None or test.archived:
            return bot.answer_callback_query(call.id, texts.GONE)
        self.exam.archive_test(test.id)
        log.info('Admin %s removed the test "%s"', call.from_user.id, test.name)
        bot.answer_callback_query(call.id, 'Готово')
        return bot.send_message(chat_id, f'🗑 Тест «{texts.escape(test.name)}» убран из списка. Вернуть его можно, '
                                         'прислав тот же файл ещё раз.')

    # running

    def _timer(self) -> None:
        while not self._stop.wait(TICK):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the timer must not die
                log.exception('Timer error')

    def run(self) -> None:
        me = self.bot.get_me()
        commands = COMMANDS + [types.BotCommand('stats', 'Статистика (админ)')] if self.settings.admins else COMMANDS
        self.bot.set_my_commands(COMMANDS)
        for admin in self.settings.admins:
            try:
                self.bot.set_my_commands(commands, scope=types.BotCommandScopeChat(admin))
            except ApiTelegramException as error:
                log.warning("Can't set commands for admin %s: %s. The admin must start the bot first.",
                            admin, error.description)
        log.info('@%s is running: %d tests, %d admins. Stop with Ctrl+C.',
                 me.username, len(self.exam.tests()), len(self.settings.admins))
        timer = threading.Thread(target=self._timer, name='timer', daemon=True)
        timer.start()
        try:
            self.tick()  # tests that ran out while the bot was off
            self.bot.infinity_polling()
        finally:
            self._stop.set()
