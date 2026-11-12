"""Tests af bølgeafgrænsningen.

Den farligste fejl i hele løsningen er ikke en exception — det er en
bølgekørsel der stilfærdigt behandler nul rækker, rapporterer nul fejl og
ser grøn ud. Den bliver opdaget efter undervisningen i stedet for før.
Testene her handler næsten alle om at den fejl ikke kan opstå.
"""

from __future__ import annotations

import pytest

from konvertering import boelge as b
from konvertering.loaders.base import Kontekst, Loader, boelge_filter
from konvertering.models import Objekttype

BOELGER = [
    {"boelge": "ppr", "navn": "PPR-sag", "raekkefoelge": 10,
     "planlagt_uge": "51-53", "status": "planlagt"},
    {"boelge": "dagtilbud-4", "navn": "Dagtilbud uge 4", "raekkefoelge": 60,
     "planlagt_uge": "03", "status": "planlagt"},
]


class FalskDatabase:
    """Minimal stand-in. Tester SQL-sammensætning og beslutninger, ikke ODBC."""

    def __init__(self, boelger=BOELGER, antal: int = 5) -> None:
        self._boelger = boelger
        self._antal = antal
        self.kald: list[tuple] = []

    def hent_alle(self, sql, *params):
        self.kald.append((sql, params))
        if "konv.boelge" in sql:
            return list(self._boelger)
        return []

    def hent_en(self, sql, *params):
        self.kald.append((sql, params))
        if "COUNT(*)" in sql:
            return {"antal": self._antal}
        return None

    def udfoer(self, sql, *params):
        self.kald.append((sql, params))
        return 1


# --- filteret ------------------------------------------------------


def test_uden_boelge_ingen_filtrering():
    assert boelge_filter(None) == ("", ())
    assert boelge_filter("") == ("", ())


def test_med_boelge_giver_parameteriseret_filter():
    sql, params = boelge_filter("ppr")
    assert sql == " AND boelge = ?"
    assert params == ("ppr",)


def test_alias_praefikses():
    sql, _ = boelge_filter("ppr", "s")
    assert sql == " AND s.boelge = ?"


def test_vaerdien_indsaettes_aldrig_i_sql():
    """Bølgekoden kommer fra en kommandolinje og et web-felt. Den må
    bindes som parameter, aldrig interpoleres ind i SQL'en."""
    sql, params = boelge_filter("'; DROP TABLE stg.sag; --")
    assert "DROP" not in sql
    assert params == ("'; DROP TABLE stg.sag; --",)


# --- validering ----------------------------------------------------


def test_kendt_boelge_accepteres():
    assert b.valider(FalskDatabase(), "ppr") == "ppr"


def test_none_betyder_paa_tvaers_af_boelger():
    assert b.valider(FalskDatabase(), None) is None


def test_stavefejl_stopper_koerslen():
    with pytest.raises(b.UkendtBoelge) as fejl:
        b.valider(FalskDatabase(), "dagtilbud4")   # mangler bindestreg
    assert "dagtilbud4" in str(fejl.value)
    # Fejlbeskeden skal vise de gyldige koder — ellers skal brugeren gætte
    assert "dagtilbud-4" in str(fejl.value)


def test_tom_boelge_er_en_fejl_ikke_en_succes():
    with pytest.raises(b.TomBoelge):
        b.kraev_arbejde(FalskDatabase(antal=0), "sag", "ppr")


def test_nul_objekter_uden_boelge_er_ok():
    """Uden bølgeafgrænsning er nul rækker et legitimt 'alt er indlæst'."""
    assert b.kraev_arbejde(FalskDatabase(antal=0), "sag", None) == 0


def test_antal_filtreres_paa_boelgen():
    db = FalskDatabase()
    b.antal_til_indlaesning(db, "dokument", "ppr")
    sql, params = db.kald[-1]
    assert "WHERE boelge = ?" in sql
    assert params == ("ppr",)


# --- loaderens valg af bølge ---------------------------------------


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


def _loader(koerslens_boelge: str | None) -> MinimalLoader:
    k = Kontekst(
        settings=None, db=None, idmap=None, klient=None,
        endpoints=None, kodelister=None, filer=None,
        boelge=koerslens_boelge,
    )
    return MinimalLoader(k)


def test_raekkens_boelge_vinder_over_koerslens():
    """Rækken bærer den faktiske tildeling fra extract-laget. Kørslens
    bølge er kun en afgrænsning af hvad der blev hentet."""
    assert _loader("ppr")._boelge({"boelge": "skole-1"}) == "skole-1"


def test_koerslens_boelge_bruges_naar_raekken_mangler_en():
    assert _loader("ppr")._boelge({"boelge": None}) == "ppr"
    assert _loader("ppr")._boelge({}) == "ppr"


def test_ingen_boelge_nogen_af_stederne_giver_none():
    assert _loader(None)._boelge({}) is None


# --- payload-gennemsyn før første rigtige kald ----------------------

def test_payload_formateres_laesbart():
    from konvertering.loaders.base import formatér_payload

    tekst = formatér_payload({"Titel": "Testsag", "SagsNummer": "17.01.05"}, maskeret=False)
    assert '"Titel": "Testsag"' in tekst
    assert tekst.index("SagsNummer") < tekst.index("Titel"), "sorteret, så diff er læsbar"


def test_payload_maskeres_som_standard():
    """Payloaden havner i en terminal og formentlig i en mail bagefter."""
    from konvertering.loaders.base import formatér_payload

    tekst = formatér_payload({"CprNummer": "0101901234", "Mail": "a@b.dk"})
    assert "0101901234" not in tekst
    assert "010190-XXXX" in tekst
    assert "a@b.dk" not in tekst


# --- arbejdslisten må kun stå ét sted -------------------------------
#
# Viewets navn lå både på loaderen og i en tabel i boelge.py. Da
# notat-loaderen kom til, blev tabellen glemt, og `konv load notat` faldt
# med en KeyError før den nåede at prøve noget som helst.

def test_hver_loader_kender_sin_arbejdsliste():
    from konvertering.loaders import LOADERS

    for navn, klasse in LOADERS.items():
        assert getattr(klasse, "arbejdsliste", None), f"{navn} mangler arbejdsliste"
        assert klasse.arbejdsliste.startswith("stg.v_"), klasse.arbejdsliste


def test_arbejdslisterne_findes_i_skemaet():
    import re
    from pathlib import Path

    from konvertering.loaders import LOADERS

    skema = Path("sql/030_stg_schema.sql").read_text(encoding="utf-8")
    views = set(re.findall(r"CREATE OR ALTER VIEW (stg\.\w+)", skema))
    for navn, klasse in LOADERS.items():
        assert klasse.arbejdsliste in views, (
            f"{navn} peger på {klasse.arbejdsliste}, som ikke oprettes af db init"
        )


def test_ukendt_objekttype_siger_hvad_der_findes():
    from konvertering import boelge as b

    with pytest.raises(b.KonverteringsFejl) as e:
        b.antal_til_indlaesning(None, "foranstaltning", None)
    assert "Kendte" in str(e.value)


# --- borgersager og emnesager er to bølger ---------------------------
#
# De deler vindue, men de er to områder i GO (Cases.Organisering = BOR og
# EMN). Med én fælles bølge kunne NC's følgeseddel ikke lægges op ad vores
# egen afstemning: deres tal kommer pr. område, vores pr. bølge.

def _seedede_boelger() -> set[str]:
    import re
    from pathlib import Path

    kilde = Path("sql/010_konv_schema.sql").read_text(encoding="utf-8")
    blok = kilde.split("MERGE konv.boelge", 1)[1].split("AS s (boelge", 1)[0]
    return set(re.findall(r"\(\s*'([a-z0-9-]+)'", blok))


def test_borgersager_og_emnesager_er_adskilte():
    seedet = _seedede_boelger()
    assert {"borgersager", "emnesager"} <= seedet
    assert "borger-emnesager" not in seedet, "den gamle samlede bølge er væk"


def test_der_er_ti_boelger():
    assert len(_seedede_boelger()) == 10


def test_hver_organisering_vaerdi_peger_paa_en_seedet_boelge():
    """mapping/boelge.csv oversætter Cases.Organisering. Peger en af dem
    på en bølge der ikke findes, får sagerne en bølge ingen kender."""
    import csv
    from pathlib import Path

    seedet = _seedede_boelger()
    with Path("mapping/boelge.csv").open(encoding="utf-8-sig", newline="") as f:
        raekker = list(csv.DictReader(f, delimiter=";"))

    koder = {r["go_vaerdi"] for r in raekker}
    assert koder == {"PPR", "ELM", "BOR", "EMN"}, "de fire værdier fra GO"

    for r in raekker:
        if r["sbsys_vaerdi"]:
            assert r["sbsys_vaerdi"] in seedet, f"{r['go_vaerdi']} peger på en ukendt bølge"
    # ELM står bevidst tom: området dækker seks bølger, og enheden afgør.
    assert next(r for r in raekker if r["go_vaerdi"] == "ELM")["sbsys_vaerdi"] == ""
