import json

from conftest import ADMIN, DEMO, USER, answer_question, buttons, keyboard, press, send


def begin(bot, telegram, demo):
    send(bot, '/start')
    press(bot, f'test:{demo.id}')
    press(bot, f'go:{demo.id}')


def test_start_lists_tests(bot, telegram, demo):
    send(bot, '/start')
    reply = telegram.last()
    assert 'Здравствуйте, Анна' in reply['text']
    assert buttons(reply) == {'Demo · 3 вопроса': f'test:{demo.id}'}


def test_start_without_tests(bot, telegram):
    send(bot, '/start')
    assert 'Тестов пока нет' in telegram.last()['text']


def test_start_for_admin(bot, telegram, demo):
    send(bot, '/start', user_id=ADMIN)
    assert 'администратор' in telegram.last(ADMIN)['text']


def test_card_then_start(bot, telegram, demo):
    send(bot, '/start')
    press(bot, f'test:{demo.id}')
    card = telegram.last()
    assert '3 вопроса' in card['text'] and '10 минут' in card['text'] and '2 балла' in card['text']
    assert set(buttons(card)) == {'▶️ Начать', '« Все тесты'}

    press(bot, f'go:{demo.id}')
    question = telegram.last()
    assert 'вопрос 1 из 3' in question['text'] and 'осталось 10 мин' in question['text']
    assert '2 + 2 = ?' in question['text']
    assert sorted(buttons(question)) == ['22', '4', '5']
    assert [call['text'] for call in telegram.calls('answerCallbackQuery') if 'text' in call] == ['Время пошло!']


def test_whole_test_with_one_mistake(bot, telegram, demo):
    begin(bot, telegram, demo)
    answer_question(bot, telegram, correct=True)
    edited = telegram.calls('editMessageText')[-1]
    assert 'Ваш ответ: <b>4</b>' in edited['text'] and not keyboard(edited)
    answer_question(bot, telegram, correct=False)
    answer_question(bot, telegram, correct=True)

    result = telegram.last()['text']
    assert '🏁 Тест «Demo» завершён' in result
    assert 'Правильных ответов: <b>2 из 3</b> (67%)' in result
    assert 'Баллы: 4 из 6' in result
    assert 'Разбор ошибок' in result and '✓ Paris' in result and '✗ Rome' in result
    assert bot.exam.active(USER) is None


def test_perfect_result(bot, telegram, demo):
    begin(bot, telegram, demo)
    for _ in range(3):
        answer_question(bot, telegram, correct=True)
    assert 'Все ответы верные' in telegram.last()['text'] and '🏆' in telegram.last()['text']


def test_pressing_an_old_button_again(bot, telegram, demo):
    begin(bot, telegram, demo)
    attempt = bot.exam.active(USER)
    press(bot, f'a:{attempt.id}:0:0')
    sent = len(telegram.sent())
    press(bot, f'a:{attempt.id}:0:1')
    assert telegram.calls('answerCallbackQuery')[-1]['text'] == 'На этот вопрос вы уже ответили'
    assert len(telegram.sent()) == sent
    assert bot.exam.active(USER).answers == [0]


def test_somebody_else_cannot_answer(bot, telegram, demo):
    begin(bot, telegram, demo)
    attempt = bot.exam.active(USER)
    press(bot, f'a:{attempt.id}:0:0', user_id=3000)
    assert bot.exam.active(USER).answers == []


def test_time_runs_out_and_the_timer_reports(bot, telegram, demo, clock):
    begin(bot, telegram, demo)
    answer_question(bot, telegram, correct=True)
    clock.advance(minutes=11)
    telegram.clear()
    bot.tick()
    assert '⏰ Время вышло' in telegram.last()['text']
    assert '1 из 3' in telegram.last()['text'] and '— без ответа' in telegram.last()['text']
    assert telegram.calls('editMessageReplyMarkup')  # the buttons are taken off the question
    bot.tick()
    assert len(telegram.sent()) == 1  # reported once


def test_answer_after_time_is_over(bot, telegram, demo, clock):
    begin(bot, telegram, demo)
    clock.advance(minutes=10)
    answer_question(bot, telegram, correct=True)
    assert '⏰ Время вышло' in telegram.last()['text'] and '0 из 3' in telegram.last()['text']


def test_finish_asks_first(bot, telegram, demo):
    begin(bot, telegram, demo)
    send(bot, '/finish')
    confirm = buttons(telegram.last())
    press(bot, confirm['▶️ Продолжить'])
    assert 'вопрос 1 из 3' in telegram.last()['text']
    press(bot, confirm['⏹ Да, завершить'])
    assert '⏹ Тест «Demo» остановлен' in telegram.last()['text']
    press(bot, confirm['⏹ Да, завершить'])
    assert telegram.calls('answerCallbackQuery')[-1]['text'] == 'Тест уже завершён'


def test_finish_without_a_test(bot, telegram):
    send(bot, '/finish')
    assert 'не проходите тест' in telegram.last()['text']


def test_start_during_a_test_offers_to_continue(bot, telegram, demo):
    begin(bot, telegram, demo)
    send(bot, '/start')
    assert 'не закончен тест «Demo»' in telegram.last()['text']
    press(bot, f'go:{demo.id}')
    assert 'не закончен тест' in telegram.last()['text']
    press(bot, 'continue')
    assert 'вопрос 1 из 3' in telegram.last()['text']


def test_text_during_a_test_repeats_the_question(bot, telegram, demo):
    begin(bot, telegram, demo)
    send(bot, '4')
    assert 'кнопкой' in telegram.sent()[-2]['text']
    assert 'вопрос 1 из 3' in telegram.last()['text']


def test_long_answers_are_listed_in_the_message(bot, telegram, exam):
    test = exam.add_test({'name': 'Long', 'questions': {'Which?': ['A rather long correct answer to choose',
                                                                    'Another long answer that is wrong']}})
    send(bot, '/start')
    press(bot, f'go:{test.id}')
    question = telegram.last()
    assert sorted(buttons(question)) == ['1', '2']
    assert '<b>1.</b>' in question['text'] and 'A rather long correct answer' in question['text']


def test_results(bot, telegram, demo):
    send(bot, '/results')
    assert 'ещё не прошли' in telegram.last()['text']
    begin(bot, telegram, demo)
    for _ in range(3):
        answer_question(bot, telegram, correct=True)
    send(bot, '/results')
    assert 'Demo: 3 из 3 (100%)' in telegram.last()['text']


def test_user_text_is_escaped(bot, telegram, exam):
    test = exam.add_test({'name': '<b>HTML</b>', 'questions': {'Is 1 < 2?': ['<yes>', 'no']}})
    send(bot, '/start')
    press(bot, f'go:{test.id}')
    assert 'Is 1 &lt; 2?' in telegram.last()['text']
    assert '&lt;b&gt;HTML&lt;/b&gt;' in telegram.last()['text']


def test_help_differs_for_admins(bot, telegram):
    send(bot, '/help')
    assert '/stats' not in telegram.last()['text']
    send(bot, '/help', user_id=ADMIN)
    assert '/stats' in telegram.last(ADMIN)['text']


def test_admin_commands_are_hidden_from_users(bot, telegram, demo):
    for command in ('/stats', '/add', '/delete'):
        send(bot, command)
        assert 'Я понимаю команды' in telegram.last()['text']


def test_stats_and_csv(bot, telegram, demo):
    begin(bot, telegram, demo)
    for correct in (True, True, False):
        answer_question(bot, telegram, correct)
    send(bot, '/stats', user_id=ADMIN)
    stats = telegram.last(ADMIN)
    assert 'Пользователей: 1' in stats['text']
    assert '1 попытка, в среднем 67%, лучший — Анна Смирнова (67%)' in stats['text']
    press(bot, buttons(stats)['📥 Demo.csv'], user_id=ADMIN)
    document = telegram.calls('sendDocument')[-1]
    name, data = document['files']['document']
    assert name == 'Demo.csv'
    lines = data.decode('utf-8-sig').splitlines()
    assert lines[0].startswith('test;user id;name')
    assert lines[1].startswith(f'Demo;{USER};Анна Смирнова;')


def test_admin_uploads_a_test(bot, telegram, exam):
    data = {'name': 'Uploaded', 'minutes': 5, 'questions': {'Q?': ['yes', 'no']}}
    telegram.files['f1'] = json.dumps(data).encode()
    send(bot, user_id=ADMIN, document={'file_id': 'f1', 'file_unique_id': 'u1', 'file_name': 'test.json',
                                       'file_size': 100})
    assert '✅ Тест «Uploaded» добавлен: 1 вопрос, 5 минут' in telegram.last(ADMIN)['text']
    telegram.files['f2'] = json.dumps({**data, 'minutes': 7}).encode()
    send(bot, user_id=ADMIN, document={'file_id': 'f2', 'file_unique_id': 'u2', 'file_name': 'test.json'})
    assert 'обновлён' in telegram.last(ADMIN)['text'] and exam.find_test('Uploaded').minutes == 7


def test_bad_uploads(bot, telegram):
    telegram.files['bad'] = b'{"name": "X", "questions": {"Q": ["only one"]}}'
    send(bot, user_id=ADMIN, document={'file_id': 'bad', 'file_unique_id': 'u', 'file_name': 't.json'})
    assert 'Тест не добавлен' in telegram.last(ADMIN)['text'] and 'at least 2 answers' in telegram.last(ADMIN)['text']
    telegram.files['broken'] = b'{not json'
    send(bot, user_id=ADMIN, document={'file_id': 'broken', 'file_unique_id': 'u', 'file_name': 't.json'})
    assert 'Это не JSON' in telegram.last(ADMIN)['text']
    send(bot, user_id=ADMIN, document={'file_id': 'x', 'file_unique_id': 'u', 'file_name': 'photo.png'})
    assert 'Нужен файл .json' in telegram.last(ADMIN)['text']


def test_user_upload_is_ignored(bot, telegram):
    telegram.files['f'] = json.dumps(DEMO).encode()
    send(bot, document={'file_id': 'f', 'file_unique_id': 'u', 'file_name': 'test.json'})
    assert bot.exam.tests() == []


def test_delete(bot, telegram, demo):
    send(bot, '/delete', user_id=ADMIN)
    press(bot, buttons(telegram.last(ADMIN))['🗑 Demo'], user_id=ADMIN)
    assert 'убран из списка' in telegram.last(ADMIN)['text']
    assert bot.exam.tests() == []
    press(bot, f'test:{demo.id}')
    assert telegram.calls('answerCallbackQuery')[-1]['text'] == 'Этого теста больше нет'


def test_run_sets_commands_and_polls(bot, telegram, monkeypatch):
    monkeypatch.setattr(bot.bot, 'infinity_polling', lambda *a, **k: None)
    bot.run()
    scopes = [json.loads(call['scope'])['type'] if call.get('scope') else 'default'
              for call in telegram.calls('setMyCommands')]
    assert scopes == ['default', 'chat']
