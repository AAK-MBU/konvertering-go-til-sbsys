"""Tegnsæt på databaseforbindelsen (src/konvertering/db.py).

20-11-2026: alt stod på UTF-8, og på Windows blev alt vi skrev gemt
forvansket ("Åben" som "Ã…ben"). Det så rigtigt ud indtil ægte data fra
GOStaging gav UnicodeDecodeError. Se tools/tegnsaet_tjek.py.
"""

from types import SimpleNamespace

from konvertering.db import indstil_tegnsaet


class Forbindelse:
    def __init__(self):
        self.afkodning, self.kodning = {}, None

    def setdecoding(self, type_, encoding):
        self.afkodning[type_] = encoding

    def setencoding(self, encoding):
        self.kodning = encoding


PYODBC = SimpleNamespace(SQL_CHAR="char", SQL_WCHAR="wchar")


def test_unicode_ind_og_ud_er_utf16():
    f = Forbindelse()
    indstil_tegnsaet(f, PYODBC)
    assert f.afkodning["wchar"] == "utf-16le"
    assert f.kodning == "utf-16le", "tekst vi SENDER skal ikke omkodes af driveren"


def test_varchar_laeses_i_klientens_tegnsaet(monkeypatch):
    import locale
    monkeypatch.setattr(locale, "getpreferredencoding", lambda _=True: "cp1252")
    f = Forbindelse()
    indstil_tegnsaet(f, PYODBC)
    assert f.afkodning["char"] == "cp1252"
