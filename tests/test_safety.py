import pytest

from creator_desk.security import age_concern


@pytest.mark.parametrize("text", ["Мне 17 лет", "I'm 16 years old", "I am a minor", "я несовершеннолетний"])
def test_age_concern(text):
    assert age_concern(text)



def test_adult_age_not_false_positive():
    assert not age_concern("Мне 25 лет")
