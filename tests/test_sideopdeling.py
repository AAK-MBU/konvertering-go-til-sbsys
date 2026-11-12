"""Tests af sideopdelt læsning.

Baggrund: loaderen læste arbejdslisten med en åben cursor og skrev til
konv.id_map undervejs. Et commit river tæppet væk under cursoren, og pyodbc
svarer "Function sequence error (SQLFetch)". Fejlen kommer først efter første
side — altså efter 500 rækker i produktion, og slet ikke i en lille test.

Derfor sideopdeles der nu efter nøgle. Testene her holder på de tre
egenskaber der gør det korrekt: ingen række leveres to gange, ingen springes
over når listen krymper undervejs, og `--graense` respekteres.
"""

from __future__ import annotations

from typing import Any

from konvertering.db import Database


class FalskDatabase(Database):
    """Simulerer SELECT TOP n ... WHERE kilde_noegle > ? ORDER BY kilde_noegle."""

    def __init__(self, noegler: list[str]) -> None:  # noqa: D107
        self.rest = sorted(noegler)
        self.forespoergsler: list[tuple[str, tuple]] = []

    def hent_alle(self, sql: str, *parametre: Any) -> list[dict[str, Any]]:
        self.forespoergsler.append((sql, parametre))
        top = int(sql.split("TOP ")[1].split(")")[0].split(" ")[0])
        efter = parametre[0]
        passende = [n for n in self.rest if n > efter]
        return [{"kilde_noegle": n} for n in passende[:top]]

    def fjern(self, noegle: str) -> None:
        """Svarer til at objektet bliver indlæst og falder ud af viewet."""
        self.rest.remove(noegle)


def test_alle_raekker_leveres_praecis_een_gang():
    db = FalskDatabase([f"n{i:03}" for i in range(12)])
    set_ = list(db.hent_i_sider("v", side=5))
    noegler = [r["kilde_noegle"] for r in set_]
    assert noegler == sorted(noegler)
    assert len(noegler) == len(set(noegler)) == 12


def test_flere_sider_end_een():
    db = FalskDatabase([f"n{i:03}" for i in range(12)])
    list(db.hent_i_sider("v", side=5))
    # 5 + 5 + 2, plus en afsluttende tom side
    assert len(db.forespoergsler) == 4


def test_graense_respekteres():
    db = FalskDatabase([f"n{i:03}" for i in range(100)])
    assert len(list(db.hent_i_sider("v", graense=7, side=5))) == 7


def test_graense_mindre_end_en_side():
    db = FalskDatabase([f"n{i:03}" for i in range(100)])
    raekker = list(db.hent_i_sider("v", graense=3, side=500))
    assert len(raekker) == 3
    assert "TOP 3" in db.forespoergsler[0][0]


def test_tom_liste_giver_ingen_raekker():
    db = FalskDatabase([])
    assert list(db.hent_i_sider("v")) == []


def test_raekker_der_forsvinder_undervejs_springer_ikke_noget_over():
    """Det er netop her OFFSET ville fejle.

    Hver indlæst række falder ud af viewet. Med OFFSET 5 ville anden side
    starte fem pladser inde i en liste der nu er fem kortere — og springe
    fem rækker over. Nøglebaseret fremrykning rammer dem alle.
    """
    db = FalskDatabase([f"n{i:03}" for i in range(20)])
    set_ = []
    for raekke in db.hent_i_sider("v", side=5):
        set_.append(raekke["kilde_noegle"])
        db.fjern(raekke["kilde_noegle"])      # "indlæst"
    assert set_ == [f"n{i:03}" for i in range(20)]


def test_fejlede_raekker_laases_ikke_fast_i_en_evig_sloejfe():
    """Fejlede rækker bliver liggende i viewet. Fordi vi går frem efter
    nøgle og ikke genlæser fra begyndelsen, leveres de kun én gang."""
    db = FalskDatabase([f"n{i:03}" for i in range(10)])
    antal = 0
    for _ in db.hent_i_sider("v", side=3):
        antal += 1
        assert antal <= 10, "evig sløjfe: samme rækker leveres igen"
    assert antal == 10


def test_filtre_og_parametre_sendes_med():
    db = FalskDatabase(["n001"])
    list(db.hent_i_sider("stg.dokument", " AND fil_status IS NULL",
                         " AND boelge = ?", parametre=("ppr",)))
    sql, params = db.forespoergsler[0]
    assert "AND fil_status IS NULL" in sql
    assert "AND boelge = ?" in sql
    assert params == ("", "ppr"), "nøglemarkøren først, så filterparametrene"


# --- regressionsværn -------------------------------------------------

def test_loaderne_streamer_ikke():
    """Den oprindelige fejl var at loaderen brugte stroem() og skrev til
    konv.id_map undervejs. Skifter nogen tilbage, fanges det her."""
    import inspect

    from konvertering.loaders import dokument, sag

    for modul in (sag, dokument):
        kilde = inspect.getsource(modul)
        assert ".stroem(" not in kilde, (
            f"{modul.__name__} bruger stroem(). Loadere skriver undervejs og "
            f"skal bruge hent_i_sider()."
        )
        assert "hent_i_sider(" in kilde


def test_filscan_streamer_ikke():
    """`filer scan` skriver fil_status for hver 1000. række — samme fælde."""
    import inspect

    from konvertering import cli

    scan = inspect.getsource(cli._scan_filer)
    assert ".stroem(" not in scan
    assert "hent_i_sider(" in scan
