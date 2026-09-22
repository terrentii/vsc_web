from rooms import MAX_TEXT, _split_text


def test_short_text_stays_one_part():
    assert _split_text('привет') == ['привет']


def test_empty_text_is_one_empty_part():
    # сообщение только с вложением
    assert _split_text('') == ['']


def test_long_text_is_split_and_nothing_is_lost():
    text = ' '.join(['слово'] * 3000)
    parts = _split_text(text)
    assert len(parts) > 1
    assert all(len(p) <= MAX_TEXT for p in parts)
    assert ' '.join(parts) == text


def test_split_prefers_line_break():
    text = 'а' * (MAX_TEXT - 10) + '\n' + 'б' * 100
    parts = _split_text(text)
    assert parts[0] == 'а' * (MAX_TEXT - 10)
    assert parts[1] == 'б' * 100


def test_solid_blob_is_cut_hard():
    parts = _split_text('я' * (MAX_TEXT * 2 + 5))
    assert [len(p) for p in parts] == [MAX_TEXT, MAX_TEXT, 5]
