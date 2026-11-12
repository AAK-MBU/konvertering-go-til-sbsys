"""Tests af målkontrollen: er id_map bygget mod DETTE SBSYS?

Den farligste tilstand i hele løsningen er også den mest tavse. Efter en
attraptest står objekterne som 'loaded' med id'er fra localhost. Peger man
så på det rigtige testmiljø, springes de over for evigt, og dokumenterne
hægtes på sags-id'er der ikke findes. Hver eneste rapport er grøn.

Derfor skrives målet på kørslen, og derfor stopper en --live-kørsel hvis
id_map peger et andet sted hen.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from konvertering.cli import _kontroller_maal
from konvertering.config import (
    DatabaseIndstillinger,
    FilIndstillinger,
    SbsysIndstillinger,
    Settings,
)
from konvertering.sbsys.errors import KonverteringsFejl


class FalskDb:
    def __init__(self, raekker: list[dict[str, Any]]) -> None:
        self.raekker = raekker

    def hent_alle(self, _sql: str, *_p: Any) -> list[dict[str, Any]]:
        return self.raekker


def _settings(base_url: str, dry_run: bool = False) -> Settings:
    return Settings(
        miljoe="test",
        db=DatabaseIndstillinger(server="s", database="d", brugernavn="u", kodeord="p"),
        sbsys=SbsysIndstillinger(base_url=base_url, statisk_token="t"),
        filer=FilIndstillinger(rod=Path("/tmp")),
        dry_run=dry_run,
    )


def test_samme_maal_gaar_igennem():
    db = FalskDb([{"maal": "https://sbsys-test", "antal": 5, "senest": None}])
    _kontroller_maal(db, _settings("https://sbsys-test"), "sag", "ppr")


def test_skraastreg_til_sidst_er_ikke_et_andet_maal():
    """Ellers ville en harmløs forskel i .env standse kørslen."""
    db = FalskDb([{"maal": "https://sbsys-test/", "antal": 5, "senest": None}])
    _kontroller_maal(db, _settings("https://sbsys-test"), "sag", "ppr")


def test_intet_tidligere_maal_gaar_igennem():
    _kontroller_maal(FalskDb([]), _settings("https://sbsys-test"), "sag", "ppr")


def test_attrap_derefter_rigtigt_sbsys_stopper_koerslen():
    db = FalskDb([{"maal": "http://localhost:8080", "antal": 5, "senest": None}])

    with pytest.raises(KonverteringsFejl) as e:
        _kontroller_maal(db, _settings("https://sbsys-test"), "sag", "ppr")

    besked = str(e.value)
    assert "localhost:8080" in besked
    assert "nulstil" in besked
    assert "--ogsaa-indlaeste" in besked
    # Den omvendte vej må ikke anbefales blindt — dér giver nulstilling dubletter
    assert "dubletter" in besked


def test_dry_run_advarer_men_stopper_ikke(caplog):
    """En dry-run opretter intet, så den skal kunne køres til at se hvad der
    ville ske — men man skal vide at listen er misvisende."""
    db = FalskDb([{"maal": "http://localhost:8080", "antal": 5, "senest": None}])

    with caplog.at_level("WARNING"):
        _kontroller_maal(db, _settings("https://sbsys-test", dry_run=True), "sag", "ppr")

    assert "localhost:8080" in caplog.text


def test_beskeden_naevner_boelgen_i_nulstil_kommandoen():
    db = FalskDb([{"maal": "http://localhost:8080", "antal": 5, "senest": None}])
    with pytest.raises(KonverteringsFejl) as e:
        _kontroller_maal(db, _settings("https://sbsys-test"), "dokument", "ppr")
    assert "--objekt-type dokument --boelge ppr" in str(e.value)


# --- "ingen objekter tilbage" er ikke det samme som "tom bølge" ------

def test_tom_boelge_skelner_mellem_faerdig_og_aldrig_tildelt():
    from konvertering import boelge as boelge_modul

    class Db:
        def __init__(self, indlaest: int) -> None:
            self.indlaest = indlaest

        def hent_en(self, sql: str, *_p: Any) -> dict[str, Any]:
            if "SUM(" in sql:
                return {"indlaest": self.indlaest, "i_alt": self.indlaest}
            return {"antal": 0}

    with pytest.raises(boelge_modul.TomBoelge) as faerdig:
        boelge_modul.kraev_arbejde(Db(5), "sag", "ppr")
    assert "allerede indlæst" in str(faerdig.value)
    assert "nulstil" in str(faerdig.value)

    with pytest.raises(boelge_modul.TomBoelge) as tom:
        boelge_modul.kraev_arbejde(Db(0), "sag", "ppr")
    assert "bølgetildelingen" in str(tom.value)


def test_uden_boelge_kraeves_der_ikke_arbejde():
    """På tværs af bølger er nul objekter et gyldigt svar, ikke en fejl."""
    from konvertering import boelge as boelge_modul

    class Db:
        def hent_en(self, _sql: str, *_p: Any) -> dict[str, Any]:
            return {"antal": 0}

    assert boelge_modul.kraev_arbejde(Db(), "sag", None) == 0


def test_maal_skrives_paa_koerslen():
    """Uden maal på konv.run kan kontrollen ovenfor ikke stille spørgsmålet."""
    from konvertering.idmap import opret_run

    fanget: dict[str, Any] = {}

    class Db:
        def hent_en(self, sql: str, *p: Any) -> dict[str, Any]:
            fanget["sql"] = sql
            fanget["params"] = p
            return {"run_id": 1}

    opret_run(Db(), fase="load", miljoe="test", dry_run=False,
              maal="https://sbsys-test")

    assert "maal" in fanget["sql"]
    assert "https://sbsys-test" in fanget["params"]


def test_skemaet_tilfoejer_maal_til_eksisterende_databaser():
    """konv.run oprettes kun hvis den ikke findes, så en ny kolonne skal
    tilføjes separat — ellers får ingen eksisterende database den."""
    sql = Path("sql/010_konv_schema.sql").read_text(encoding="utf-8")
    assert "COL_LENGTH('konv.run', 'maal')" in sql
    assert "ALTER TABLE konv.run ADD maal" in sql


def test_falsk_db_matcher_rigtig_signatur():
    """Værn mod at testene her bliver grønne på en forældet signatur."""
    import inspect

    from konvertering.db import Database

    assert "sql" in inspect.signature(Database.hent_alle).parameters


# --- hvad kørslen har lagt i SBSYS -----------------------------------
#
# GO-nøglen skrives ikke ind i SBSYS, så konv.id_map er det eneste sted
# koblingen findes. Uden en udskrift kan ingen andre end os svare på hvad
# en kørsel har lagt derind — hverken til afstemning, til et opslag på én
# sag, eller til den oprydning andre står for.

class _Opslag:
    def __init__(self, raekker):
        self.raekker = raekker
        self.sql = ""
        self.params = ()

    def hent_alle(self, sql, *params):
        self.sql, self.params = sql, params
        return self.raekker


def test_kun_det_der_faktisk_blev_oprettet():
    from konvertering.idmap import oprettede_objekter

    db = _Opslag([])
    oprettede_objekter(db, boelge="ppr")
    assert "status = 'loaded'" in db.sql
    assert "sbsys_id IS NOT NULL" in db.sql
    assert "ppr" in db.params


def test_boern_foer_foraeldre():
    """Slettes sagen først, kan dokumenter og indsatser ikke findes."""
    from konvertering.idmap import oprettede_objekter

    db = _Opslag([])
    oprettede_objekter(db)
    raekkefoelge = db.sql.split("ORDER  BY", 1)[1]
    for tidlig, sen in (("indsats", "notat"), ("notat", "dokument"), ("dokument", "sag")):
        assert raekkefoelge.index(f"'{tidlig}'") < raekkefoelge.index(f"'{sen}'")


def test_oprettede_er_en_kommando():
    from konvertering.cli import _parser

    args = _parser().parse_args(["oprettede", "--boelge", "ppr"])
    assert args.kommando == "oprettede"
    assert args.boelge == "ppr"


# --- værktøjet sletter ikke ------------------------------------------
#
# Besluttet 11-11-2026. Oprydningen af SBSYS og af konverteringsdatabasen
# håndteres af andre, inden den rigtige konvertering. Der skal derfor ikke
# findes en vej til en DELETE herfra — heller ikke en man selv åbner med
# --jatak kl. 03, fordi det lige var det hurtigste.

def test_delete_er_ikke_en_lovlig_metode():
    from konvertering.cli import _FORBUDTE_METODER, _SIKRE_METODER

    assert "DELETE" in _FORBUDTE_METODER
    assert not _FORBUDTE_METODER & _SIKRE_METODER


def test_ingen_loader_sender_en_delete():
    from pathlib import Path

    for sti in Path("src/konvertering").rglob("*.py"):
        tekst = sti.read_text(encoding="utf-8")
        for nr, linje in enumerate(tekst.splitlines(), 1):
            if linje.lstrip().startswith("#") or "_FORBUDTE" in linje:
                continue
            assert '"DELETE"' not in linje, f"{sti.name}:{nr} kan sende en DELETE"


# --- nulstilling af VORES EGEN database ------------------------------
#
# SBSYS røres ikke. Men konverteringsdatabasen skal kunne tømmes, så det
# endelige dump kan læses ind i et rent bord: ellers siger konv.id_map
# stadig 'loaded' om testkørslens objekter, og den rigtige konvertering
# springer dem over.

def test_kodelisterne_overlever_en_nulstilling():
    """Kodelisterne er fagpersonernes arbejde og har intet med et dump at
    gøre. Ryger de med, skal en formiddags udfyldning laves om."""
    from konvertering.idmap import NULSTIL_TABELLER

    assert "map.kodeliste" not in NULSTIL_TABELLER
    assert "konv.boelge" not in NULSTIL_TABELLER


def test_boern_slettes_foer_foraeldre():
    """En fremmednøgle gør rækkefølgen bindende."""
    from konvertering.idmap import NULSTIL_TABELLER

    i = NULSTIL_TABELLER.index
    assert i("konv.id_map") < i("konv.run")
    assert i("stg.dokument") < i("stg.sag")
    assert i("stg.sag_part") < i("stg.part")


def test_alle_arbejdstabeller_er_med():
    """En tabel der glemmes, efterlader rækker fra testkørslen i det
    endelige datasæt — og de ligner rigtige data."""
    import re
    from pathlib import Path

    from konvertering.idmap import NULSTIL_TABELLER

    fundne = set()
    for navn in ("010_konv_schema", "030_stg_schema"):
        tekst = Path(f"sql/{navn}.sql").read_text(encoding="utf-8")
        fundne |= set(re.findall(r"CREATE TABLE (konv|stg)\.(\w+)", tekst))

    tabeller = {f"{skema}.{navn}" for skema, navn in fundne}
    mangler = tabeller - set(NULSTIL_TABELLER) - {"konv.boelge"}
    assert not mangler, f"tabeller der ikke tømmes ved nulstilling: {sorted(mangler)}"


class _Taeller:
    def __init__(self, antal=0):
        self.antal = antal
        self.udfoert: list[str] = []

    def hent_alle(self, _sql, *_p):
        return [{"n": self.antal}]

    def udfoer(self, sql, *_p):
        self.udfoert.append(sql)
        return 1


def test_uden_jatak_slettes_intet(tmp_path):
    from konvertering.cli import _db_nulstil, _parser

    args = _parser().parse_args(["db", "nulstil"])
    db = _Taeller(antal=5)
    assert _db_nulstil(db, args) == 2
    assert db.udfoert == [], "intet må slettes uden --jatak"


def test_sporet_gemmes_foer_sletningen(tmp_path):
    """GO-nøglen står ikke i SBSYS. Efter nulstillingen findes koblingen
    ingen steder — filen er det sidste spor."""
    from konvertering.cli import _db_nulstil, _parser

    ud = tmp_path / "spor.csv"
    args = _parser().parse_args(["db", "nulstil", "--jatak", "--ud", str(ud)])

    class Db(_Taeller):
        def hent_alle(self, sql, *_p):
            if "id_map" in sql and "sbsys_id" in sql:
                return [{"objekt_type": "sag", "kilde_noegle": "X", "sbsys_id": "42",
                         "boelge": "ppr", "run_id": 1, "opdateret": None}]
            return [{"n": 1}]

    db = Db()
    assert _db_nulstil(db, args) == 0
    assert ud.exists()
    assert "42" in ud.read_text(encoding="utf-8-sig")
    assert db.udfoert, "der skulle slettes når --jatak er givet"
