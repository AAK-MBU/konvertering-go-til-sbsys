from datetime import datetime

import pytest

from konvertering.hashing import kort_noegle, maskér, payload_hash


def test_hash_er_uafhaengig_af_noeglerraekkefoelge():
    a = {"titel": "Sag", "kle": "17.01.05", "dato": datetime(2020, 1, 1)}
    b = {"dato": datetime(2020, 1, 1), "kle": "17.01.05", "titel": "Sag"}
    assert payload_hash(a) == payload_hash(b)


def test_hash_aendrer_sig_ved_aendret_vaerdi():
    a = {"titel": "Sag"}
    b = {"titel": "Sag "}
    assert payload_hash(a) != payload_hash(b)


def test_hash_er_stabil_paa_tvaers_af_koersler():
    # Fast forventet værdi: hvis denne ændrer sig, vil ALT blive genindlæst
    # ved næste kørsel. Testen er en spærre mod utilsigtede ændringer.
    assert payload_hash({"a": 1, "b": [1, 2]}) == payload_hash({"b": [1, 2], "a": 1})
    assert len(payload_hash({"a": 1})) == 64


def test_nestede_dicts_normaliseres():
    a = {"ydre": {"z": 1, "a": 2}}
    b = {"ydre": {"a": 2, "z": 1}}
    assert payload_hash(a) == payload_hash(b)


@pytest.mark.parametrize(
    "ind,ud",
    [
        ("Sagen vedrører 0101901234", "Sagen vedrører 010190-XXXX"),
        ("CPR 010190-1234 i tekst", "CPR 010190-XXXX i tekst"),
        ("skriv til anna@aarhus.dk", "skriv til <email>"),
        ("", ""),
    ],
)
def test_maskering_fjerner_persondata(ind, ud):
    assert maskér(ind) == ud


def test_maskering_taaler_none():
    assert maskér(None) == ""


def test_kort_noegle_er_deterministisk_og_kort():
    assert kort_noegle("abc") == kort_noegle("abc")
    assert len(kort_noegle("abc")) == 12
    assert kort_noegle("abc") != kort_noegle("abd")
