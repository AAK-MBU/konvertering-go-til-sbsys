"""Antallet af ?-pladsholdere skal matche antallet af parametre.

Dette er den ene klasse fejl der ellers først opdages når koden rammer en
rigtig SQL Server — typisk kl. 02 under en bølgekørsel. Tabellerne kan vi
ikke teste uden en database, men aritet kan vi, og det er den fejl vi
faktisk laver når en kolonne tilføjes til en INSERT eller MERGE.
"""

from __future__ import annotations

import pytest

from konvertering.idmap import IdMap, afslut_run, opret_run
from konvertering.loaders.base import Kontekst, Loader
from konvertering.models import Objekttype


class OptagendeDatabase:
    """Fanger hver eneste SQL-sætning og dens parametre."""

    def __init__(self) -> None:
        self.sætninger: list[tuple[str, tuple]] = []

    def udfoer(self, sql, *params):
        self.sætninger.append((sql, params))
        return 1

    def hent_en(self, sql, *params):
        self.sætninger.append((sql, params))
        if "konv.run" in sql:
            return {"run_id": 1}
        # Formen skal matche IdMapPost, ellers fanger testen ikke aritet
        # men kolonnenavne — og så tester den noget andet end den skal.
        return {"objekt_type": "sag", "kilde_noegle": "k",
                "sbsys_id": None, "sbsys_nummer": None, "payload_hash": None,
                "status": "pending", "forsoeg": 1, "boelge": None}

    def hent_alle(self, sql, *params):
        self.sætninger.append((sql, params))
        return []

    def kontrollér(self) -> None:
        for sql, params in self.sætninger:
            antal = sql.count("?")
            assert antal == len(params), (
                f"{antal} pladsholdere, men {len(params)} parametre i:\n{sql.strip()}"
            )


class MinimalLoader(Loader):
    objekt_type = Objekttype.SAG

    def hent_arbejde(self, graense=None):
        return []

    def byg(self, raekke):
        raise NotImplementedError

    def payload(self, objekt):
        raise NotImplementedError

    def send(self, objekt, payload):
        raise NotImplementedError


@pytest.fixture
def db() -> OptagendeDatabase:
    return OptagendeDatabase()


def test_idmap_reserver(db):
    IdMap(db).reserver(Objekttype.SAG, "noegle-1", "a" * 64, run_id=7, boelge="ppr")
    db.kontrollér()


def test_idmap_reserver_uden_boelge(db):
    IdMap(db).reserver(Objekttype.DOKUMENT, "noegle-2", "b" * 64)
    db.kontrollér()


def test_idmap_statusopdateringer(db):
    m = IdMap(db)
    m.markér_indlaest(Objekttype.SAG, "k", "4711", "2026-1234")
    m.markér_fejlet(Objekttype.SAG, "k", "noget gik galt")
    m.markér_sprunget_over(Objekttype.SAG, "k", "bevidst")
    m.hent(Objekttype.SAG, "k")
    db.kontrollér()


def test_run_livscyklus(db):
    run_id = opret_run(db, fase="load", miljoe="test", dry_run=True,
                       objekt_type="sag", kodeversion="abc123",
                       as_session_id="s-1", boelge="ppr")
    afslut_run(db, run_id, "ok", "færdig")
    db.kontrollér()


def test_afvisning(db):
    k = Kontekst(settings=None, db=db, idmap=None, klient=None, endpoints=None,
                 kodelister=None, filer=None, run_id=3, boelge="ppr")
    MinimalLoader(k)._afvis("k", "manglende_kle", "blok", "mangler KLE", "kle", "17.01.05")
    db.kontrollér()


def test_afvisning_uden_felt_og_vaerdi(db):
    k = Kontekst(settings=None, db=db, idmap=None, klient=None, endpoints=None,
                 kodelister=None, filer=None)
    MinimalLoader(k)._afvis("k", "sbsys_afviste", "blok", "HTTP 400")
    db.kontrollér()


def test_boelge_hjaelpere(db):
    from konvertering import boelge

    boelge.markér_afstemt(db, "ppr", "ab1234")
    boelge.gate_status(db, "ppr")
    boelge.gate_status(db)
    db.kontrollér()


# --- fejl i et SQL-script skal kunne placeres ------------------------

def test_sql_scriptfejl_peger_paa_saetningen():
    """SQL Servers besked peger ofte midt i en sætning. Uden filnavn og
    sætningsnummer er den svær at finde i en fil på 180 linjer."""
    from konvertering.db import SqlScriptFejl

    fejl = SqlScriptFejl(
        "030_stg_schema.sql", 3, 13,
        "CREATE TABLE stg.part (\n    by NVARCHAR(100) NULL\n);",
        Exception("Incorrect syntax near the keyword 'by'"),
    )
    tekst = str(fejl)
    assert "030_stg_schema.sql" in tekst
    assert "sætning 3 af 13" in tekst
    assert "CREATE TABLE stg.part" in tekst
    assert "Incorrect syntax" in tekst


def test_scriptfejl_forkorter_lange_blokke():
    from konvertering.db import SqlScriptFejl

    blok = "\n".join(f"linje {i}" for i in range(20))
    tekst = str(SqlScriptFejl("f.sql", 1, 1, blok, Exception("x")))
    assert "linjer mere" in tekst
    assert "linje 19" not in tekst


# --- nulstilling -----------------------------------------------------
#
# En manglende mapning skal kunne rettes og objektet køres igen. Blev den
# markeret 'manual', var den terminal, og hele mapping-sløjfen — projektets
# centrale arbejdsgang — virkede ikke.

def test_valideringsfejl_bliver_failed_ikke_manual():
    import inspect

    from konvertering.loaders import base

    kilde = inspect.getsource(base.Loader._behandl_en)
    afsnit = kilde.split("except ValideringsFejl")[1].split("except FilFejl")[0]
    assert "manuel=True" not in afsnit, (
        "'manual' er terminal: objektet kommer aldrig med igen, heller ikke "
        "efter at mapningen er rettet"
    )


def test_nulstil_kraever_et_filter(db):
    from konvertering.idmap import IdMap
    from konvertering.sbsys.errors import KonverteringsFejl

    with pytest.raises(KonverteringsFejl):
        IdMap(db).nulstil()
    assert db.sætninger == [], "intet må være udført"


def test_nulstil_udelader_indlaeste_som_standard(db):
    from konvertering.idmap import IdMap

    IdMap(db).nulstil(boelge="ppr")
    sql, _ = db.sætninger[-1]
    assert "status <> 'loaded'" in sql
    db.kontrollér()


def test_nulstil_kan_tage_indlaeste_med_bevidst(db):
    from konvertering.idmap import IdMap

    IdMap(db).nulstil(boelge="ppr", ogsaa_indlaeste=True)
    sql, _ = db.sætninger[-1]
    assert "status <> 'loaded'" not in sql
    db.kontrollér()


def test_nulstil_filtrerer_paa_afvisningsregel(db):
    from konvertering.idmap import IdMap

    IdMap(db).nulstil(boelge="ppr", regel="manglende_mapping")
    sql, params = db.sætninger[-1]
    assert "konv.afvist" in sql
    assert "manglende_mapping" in params
    db.kontrollér()


def test_nulstil_saetter_status_pending(db):
    from konvertering.idmap import IdMap

    IdMap(db).nulstil(kilde_noegle="TEST-SAG-004")
    sql, _ = db.sætninger[-1]
    assert "status = 'pending'" in sql
    assert "sidste_fejl = NULL" in sql
    db.kontrollér()


def test_overblik_bruger_kolonner_der_findes():
    """Et overblik der fejler med "Invalid column name" er værre end
    ingen: man opdager det først når man står og skal svare nogen."""
    import re
    from pathlib import Path

    skema = "\n".join(
        Path(f"sql/{n}").read_text(encoding="utf-8")
        for n in ("010_konv_schema.sql", "020_map_schema.sql", "030_stg_schema.sql")
    )
    kendte = set(re.findall(r"^\s{4}(\w+)\s+\w", skema, flags=re.MULTILINE))
    kendte |= {"afsnit", "retning", "sbsys", "tilstand", "i_alt", "indlaest", "venter",
               "fejlet", "manuel", "sprunget_over", "pct_indlaest", "senest", "sekunder",
               "go_noegle", "go_titel", "sidst_forsoegt", "sag_i_sbsys", "antal",
               "forskellige_vaerdier", "eksempel", "boelge"}

    tekst = Path("sql/910_overblik.sql").read_text(encoding="utf-8")
    tekst = re.sub(r"/\*.*?\*/", "", tekst, flags=re.DOTALL)
    # Kun alias.kolonne — ikke skema.tabel, som har samme form.
    brugte = {
        kolonne
        for praefiks, kolonne in re.findall(r"\b([a-z]{1,3})\.(\w+)\b", tekst)
        if praefiks not in ("konv", "stg", "map")
    }

    ukendte = {k for k in brugte if k not in kendte}
    assert not ukendte, f"kolonner der ikke findes i skemaet: {sorted(ukendte)}"


def test_testdata_rydder_op_i_alt_hvad_den_skriver():
    """999_testdata.sql skal kunne køres igen. Gør den ikke det, fejler
    anden kørsel på en primærnøgle — og det er en irriterende måde at
    opdage at en tabel blev glemt i oprydningen."""
    import re
    from pathlib import Path

    tekst = Path("sql/999_testdata.sql").read_text(encoding="utf-8")
    skrevet = set(re.findall(r"INSERT\s+INTO\s+(stg\.\w+)", tekst, flags=re.IGNORECASE))
    ryddet = set(re.findall(r"DELETE\s+FROM\s+(stg\.\w+)", tekst, flags=re.IGNORECASE))

    assert skrevet, "fandt ingen INSERT INTO stg.* — er mønsteret ændret?"
    assert skrevet <= ryddet, f"skrives men ryddes ikke: {sorted(skrevet - ryddet)}"


def test_testdata_sletter_ikke_id_map():
    """konv.id_map er ikke testdata — den er fortegnelsen over hvad der
    faktisk findes i SBSYS. Slettes en 'loaded'-række mens objektet stadig
    ligger i SBSYS, er forbindelsen væk: dokumenter kan ikke placeres på
    sagen, og næste kørsel opretter en dublet."""
    import re
    from pathlib import Path

    tekst = Path("sql/999_testdata.sql").read_text(encoding="utf-8")
    tekst = re.sub(r"/\*.*?\*/", "", tekst, flags=re.DOTALL)
    assert not re.search(r"DELETE\s+FROM\s+konv\.id_map", tekst, flags=re.IGNORECASE)
