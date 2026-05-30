from meeting_minutes_backend.itn import normalize


def test_percentage():
    assert normalize("百分之三十") == "30%"
    assert normalize("百分之五點五") == "5.5%"


def test_phone():
    assert normalize("零九一二三四五六七八") == "0912-345-678"


def test_date():
    assert normalize("二○二六年五月二十八日") == "2026年05月28日"


def test_time():
    assert normalize("下午三點十五分") == "下午3:15"
    assert normalize("上午十點半") == "上午10:30"


def test_number():
    assert normalize("一千五百") == "1,500"
    assert normalize("兩萬三千") == "23,000"
