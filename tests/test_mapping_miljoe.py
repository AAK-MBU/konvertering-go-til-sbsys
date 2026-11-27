"""Kodelisterne følger KONV_MILJOE (18-11-2026).

Produktionens værdier i mapping/*.csv; testmiljøets afvigelser i
mapping/testmiljoe/*.csv, lagt ovenpå KUN i test. Testmiljøets id'er og
feltnavne må aldrig følge med ind i en produktionskørsel.
"""

from __future__ import annotations

from pathlib import Path

from konvertering.mapping import TESTMAERKE, aktive_testvaerdier, indlaes_mapping

KOLONNER = "liste;go_vaerdi;go_tekst;sbsys_vaerdi;sbsys_tekst;handling;kommentar;godkendt_af;godkendt_dato\n"


class Db:
    """map.kodeliste i hukommelsen — nok til MERGE og deaktivering."""

    def __init__(self) -> None:
        self.raekker: dict[tuple[str, str], dict] = {}

    def udfoer_mange(self, _sql, params, hurtig=False):
        for p in params:
            liste, go, gt, sv, st, h, k, *_ = p
            self.raekker[(liste, go)] = {"sbsys_vaerdi": sv, "kommentar": k, "gyldig": 1}
        return len(params)

    def udfoer(self, sql, *params):
        assert "SET gyldig = 0" in sql
        n = 0
        for r in self.raekker.values():
            if r["gyldig"] and (r["kommentar"] or "").startswith(TESTMAERKE):
                r["gyldig"], n = 0, n + 1
        return n

    def hent_en(self, _sql, *_params):
        return {"antal": sum(1 for r in self.raekker.values()
                             if r["gyldig"] and (r["kommentar"] or "").startswith(TESTMAERKE))}


def _mapper(tmp_path: Path) -> Path:
    (tmp_path / "testmiljoe").mkdir()
    (tmp_path / "tilbud.csv").write_text(
        KOLONNER + "tilbud;ABA;;1;ABA;map;produktion;;\n", encoding="utf-8")
    (tmp_path / "testmiljoe" / "tilbud.csv").write_text(
        KOLONNER + "tilbud;ABA;;3;ABA;map;testkatalog;;\n"
        + "tilbud;Kun-i-test;;9;X;map;kun test;;\n", encoding="utf-8")
    return tmp_path


def test_i_test_laegges_testvaerdierne_ovenpaa(tmp_path):
    db = Db()
    indlaes_mapping(db, _mapper(tmp_path), "test")
    assert db.raekker[("tilbud", "ABA")]["sbsys_vaerdi"] == "3"
    assert db.raekker[("tilbud", "ABA")]["kommentar"].startswith(TESTMAERKE)


def test_i_produktion_bruges_kun_produktionens(tmp_path):
    db = Db()
    mappe = _mapper(tmp_path)
    indlaes_mapping(db, mappe, "test")           # efter en testkørsel ...
    indlaes_mapping(db, mappe, "prod")           # ... og så produktion
    assert db.raekker[("tilbud", "ABA")]["sbsys_vaerdi"] == "1"
    assert db.raekker[("tilbud", "Kun-i-test")]["gyldig"] == 0
    assert aktive_testvaerdier(db) == 0


def test_testvaerdier_taelles_saa_produktion_kan_naegte(tmp_path):
    db = Db()
    indlaes_mapping(db, _mapper(tmp_path), "test")
    assert aktive_testvaerdier(db) == 2
