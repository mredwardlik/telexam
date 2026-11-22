import json
from collections import Counter

import pytest
from conftest import DEMO

from telexam import Exam, ExamError, TestFormatError, load_test_file, parse_test
from telexam.exam import ActiveAttempt, Stale, to_csv_rows


def start(exam, demo, user=1):
    exam.register(user, f'User {user}')
    return exam.start(user, demo.id)


class TestFormat:
    def test_defaults(self):
        test = parse_test({'name': 'X', 'questions': {'Q': ['a', 'b']}})
        assert (test['minutes'], test['points'], test['shuffle']) == (30, 1, False)

    def test_list_of_objects(self):
        test = parse_test({'name': 'X', 'questions': [{'question': 'Q', 'answers': ['a', 'b']}]})
        assert test['questions'] == [('Q', ['a', 'b'])]

    def test_numbers_become_answers_and_spaces_are_tidied(self):
        test = parse_test({'name': '  Many   spaces ', 'questions': {' Q ': [4, ' five  answer ']}})
        assert test['name'] == 'Many spaces'
        assert test['questions'] == [('Q', ['4', 'five answer'])]

    @pytest.mark.parametrize('data, message', [
        ([], 'JSON object'),
        ({'questions': {'Q': ['a', 'b']}}, '"name"'),
        ({'name': 'X', 'questions': {}}, 'no questions'),
        ({'name': 'X', 'questions': 'Q'}, 'map each question'),
        ({'name': 'X', 'questions': {'Q': ['a', 'a']}}, 'same answer twice'),
        ({'name': 'X', 'questions': {'Q': ['a', '']}}, 'empty answer'),
        ({'name': 'X', 'questions': {'Q': 'ab'}}, 'at least 2 answers'),
        ({'name': 'X', 'questions': {'Q': [str(i) for i in range(11)]}}, 'more than 10'),
        ({'name': 'X', 'minutes': 0, 'questions': {'Q': ['a', 'b']}}, '"minutes"'),
        ({'name': 'X', 'points': True, 'questions': {'Q': ['a', 'b']}}, '"points"'),
        ({'name': 'X', 'shuffle': 'yes', 'questions': {'Q': ['a', 'b']}}, '"shuffle"'),
        ({'name': 'X', 'time': 5, 'questions': {'Q': ['a', 'b']}}, 'unknown keys: time'),
        ({'name': 'X', 'questions': [['Q', ['a', 'b']], ['Q', ['c', 'd']]]}, 'repeated'),
    ])
    def test_errors(self, data, message):
        with pytest.raises(TestFormatError, match=message):
            parse_test(data)

    def test_example_files_are_valid(self):
        from conftest import ROOT
        for path in (ROOT / 'examples').glob('*.json'):
            assert load_test_file(path)['questions']

    def test_bad_json_file(self, tmp_path):
        path = tmp_path / 'bad.json'
        path.write_text('{"name": ', 'utf-8')
        with pytest.raises(TestFormatError, match='not valid JSON'):
            load_test_file(path)


class TestTests:
    def test_add_and_find(self, exam, demo):
        assert exam.tests() == [demo]
        assert exam.find_test('  demo ') == demo
        assert demo.total_points == 6

    def test_add_twice(self, exam, demo):
        with pytest.raises(ExamError, match='already exists'):
            exam.add_test({**DEMO, 'name': 'DEMO'})
        updated = exam.add_test({**DEMO, 'minutes': 3}, replace=True)
        assert updated.id == demo.id and updated.minutes == 3

    def test_archive_and_bring_back(self, exam, demo):
        exam.archive_test(demo.id)
        assert exam.tests() == [] and exam.find_test('Demo') is None
        assert exam.add_test(DEMO).id == demo.id
        assert exam.tests()[0].archived is False

    def test_cyrillic_names_ignore_case(self, exam):
        exam.add_test({'name': 'Общие знания', 'questions': {'Q': ['a', 'b']}})
        assert exam.find_test('ОБЩИЕ ЗНАНИЯ') is not None


class TestAttempt:
    def test_answers_are_shuffled_but_the_correct_one_is_known(self, exam, demo):
        positions = Counter()
        for user in range(1, 41):
            attempt = start(exam, demo, user)
            for item, (question, answers) in zip(attempt.items, demo.questions):
                assert item.question == question
                assert sorted(item.answers) == sorted(answers)
                assert item.answers[item.correct] == answers[0]
            positions[attempt.items[0].correct] += 1
        assert len(positions) == 3  # the right answer is not always in the same place

    def test_question_order_is_kept_unless_shuffle(self, exam):
        test = exam.add_test({**DEMO, 'name': 'Shuffled', 'shuffle': True})
        orders = {tuple(item.question for item in start(exam, test, user).items) for user in range(1, 21)}
        assert len(orders) > 1

    def test_full_run(self, exam, demo, clock):
        attempt = start(exam, demo)
        clock.advance(seconds=30)
        attempt = exam.answer(attempt.id, 0, attempt.items[0].correct)
        attempt = exam.answer(attempt.id, 1, 1 - attempt.items[1].correct)
        clock.advance(seconds=45)
        attempt = exam.answer(attempt.id, 2, attempt.items[2].correct)
        assert attempt.reason == 'done'
        assert (attempt.score, attempt.total, attempt.percent, attempt.earned) == (2, 3, 67, 4)
        assert attempt.duration.total_seconds() == 75
        assert [item.question for item, _ in attempt.mistakes] == ['Capital of France?']

    def test_one_attempt_at_a_time(self, exam, demo):
        first = start(exam, demo)
        with pytest.raises(ActiveAttempt) as error:
            exam.start(1, demo.id)
        assert error.value.attempt.id == first.id

    def test_old_buttons_are_stale(self, exam, demo):
        attempt = start(exam, demo)
        exam.answer(attempt.id, 0, 0)
        with pytest.raises(Stale, match='already been answered'):
            exam.answer(attempt.id, 0, 1)
        with pytest.raises(Stale, match='no such answer'):
            exam.answer(attempt.id, 1, 9)

    def test_answer_after_the_deadline_is_not_counted(self, exam, demo, clock):
        attempt = start(exam, demo)
        clock.advance(minutes=10)
        attempt = exam.answer(attempt.id, 0, attempt.items[0].correct)
        assert attempt.reason == 'time' and attempt.answers == []
        with pytest.raises(Stale, match='already finished'):
            exam.answer(attempt.id, 0, 0)

    def test_expired(self, exam, demo, clock):
        attempt = start(exam, demo)
        other = start(exam, exam.add_test({**DEMO, 'name': 'Long', 'minutes': 60}), user=2)
        clock.advance(minutes=15)
        expired = exam.expired()
        assert [a.id for a in expired] == [attempt.id]
        assert expired[0].duration.total_seconds() == 600  # counted until the deadline
        assert exam.expired() == []
        assert exam.active(2).id == other.id

    def test_finish_happens_once(self, exam, demo):
        attempt = start(exam, demo)
        assert exam.finish(attempt, 'stopped').reason == 'stopped'
        assert exam.finish(attempt, 'time') is None
        assert exam.attempt(attempt.id).reason == 'stopped'

    def test_changing_a_test_keeps_old_results(self, exam, demo):
        attempt = start(exam, demo)
        exam.answer(attempt.id, 0, attempt.items[0].correct)
        exam.add_test({**DEMO, 'questions': {'New?': ['yes', 'no']}}, replace=True)
        kept = exam.attempt(attempt.id)
        assert kept.total == 3 and kept.items[0].question == '2 + 2 = ?'

    def test_history_and_results(self, exam, demo):
        for user in (1, 2):
            exam.finish(start(exam, demo, user), 'stopped')
        assert [a.user_id for a in exam.history(1)] == [1]
        assert [a.user_id for a in exam.results(demo.id)] == [1, 2]
        rows = to_csv_rows(exam.results())
        assert rows[0][:3] == ['test', 'user id', 'name']
        assert rows[1][:3] == ['Demo', '1', 'User 1'] and rows[1][-1] == 'stopped'

    def test_data_survives_a_restart(self, tmp_path, clock):
        path = tmp_path / 'keep.db'
        first = Exam(path, clock=clock)
        test = first.add_test(DEMO)
        first.register(5, 'Five')
        attempt = first.start(5, test.id)
        first.close()
        second = Exam(path, clock=clock)
        assert second.active(5).id == attempt.id
        assert json.loads(second.db.execute('SELECT items FROM attempts').fetchone()[0])
        second.close()
