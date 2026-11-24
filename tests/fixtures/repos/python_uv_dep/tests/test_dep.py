import fmantoydep


def test_third_party_import():
    assert fmantoydep.greet("fman") == "hi fman"
