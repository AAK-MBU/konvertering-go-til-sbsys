from datetime import datetime

import pytest
from pydantic import ValidationError

from konvertering.mapping import Kodelister, Opslag
from konvertering.models import Dokument, Part, PartType, Sag
from konvertering.sbsys.endpoints import Endpoints, ManglendeOperation
from konvertering.sbsys.errors import ManglerMapping


def _kodelister() -> Kodelister:
    return Kodelister(
        {
            ("kle", "17.01.05"): Opslag("17.01.05", "map"),
            ("sagsstatus", "Draft"): Opslag(None, "ignorer"),
            ("sagsstatus", "Ukendt"): Opslag(None, "map"),
            ("kle", "GO-LOKAL-042"): Opslag("00.00.00", "bloker"),
        }
    )


def test_kendt_vaerdi_oversaettes():
    assert _kodelister().slaa_op("kle", "17.01.05") == "17.01.05"


def test_ukendt_vaerdi_blokerer():
    with pytest.raises(ManglerMapping):
        _kodelister().slaa_op("kle", "99.99.99")


def test_ukendt_vaerdi_er_tilladt_naar_ikke_paakraevet():
    assert _kodelister().slaa_op("kle", "99.99.99", paakraevet=False) is None


def test_ignorer_giver_none_uden_fejl():
    assert _kodelister().slaa_op("sagsstatus", "Draft", paakraevet=False) is None


def test_bloker_kaster_selv_med_sbsys_vaerdi():
    """'bloker' bruges når nogen har skrevet en værdi ind, men den ikke må
    bruges endnu. Den må ikke kunne slippe igennem ved et uheld."""
    with pytest.raises(ManglerMapping):
        _kodelister().slaa_op("kle", "GO-LOKAL-042", paakraevet=False)


def test_uudfyldt_mapping_blokerer():
    with pytest.raises(ManglerMapping):
        _kodelister().slaa_op("sagsstatus", "Ukendt")


def test_tom_kildevaerdi_blokerer_naar_paakraevet():
    with pytest.raises(ManglerMapping):
        _kodelister().slaa_op("kle", "   ")


# --- kanonisk model -------------------------------------------------


def test_sag_kraever_titel():
    with pytest.raises(ValidationError):
        Sag(kilde_noegle="a", titel="")


def test_sag_afviser_afslutning_foer_oprettelse():
    with pytest.raises(ValidationError):
        Sag(
            kilde_noegle="a",
            titel="Sag",
            oprettet_dato=datetime(2020, 5, 1),
            afsluttet_dato=datetime(2019, 1, 1),
        )


def test_cpr_normaliseres():
    assert Part(kilde_noegle="p", part_type=PartType.PERSON, cpr="010190-1234").cpr == "0101901234"


def test_ugyldigt_cpr_afvises():
    with pytest.raises(ValidationError):
        Part(kilde_noegle="p", part_type=PartType.PERSON, cpr="123")


def test_ukendt_felt_afvises():
    """extra='forbid': en tastefejl i extract-laget skal fejle højlydt,
    ikke stille droppe et felt."""
    with pytest.raises(ValidationError):
        Sag(kilde_noegle="a", titel="Sag", titl="stavefejl")


def test_dokument_kraever_sagstilknytning():
    with pytest.raises(ValidationError):
        Dokument(kilde_noegle="d", titel="Bilag", sag_noegle="")


# --- endpoints ------------------------------------------------------


def test_sti_parametre_indsaettes():
    op = Endpoints().operation("hent_dokumenter", sag_id="123")
    assert op.sti == "/api/sag/123/dokumenter"


def test_ukendt_operation_fejler_forklarende():
    with pytest.raises(ManglendeOperation):
        Endpoints().operation("slet_alt")


def test_payload_oversaetter_feltnavne_og_dropper_none():
    payload = Endpoints().byg_payload("sag", {"titel": "Sag", "beskrivelse": None})
    assert payload == {"SagsTitel": "Sag"}
    assert "Beskrivelse" not in payload


def test_felter_kan_overskrives():
    e = Endpoints(felter={"sag": {"titel": "SagsTitel"}})
    assert e.byg_payload("sag", {"titel": "x"}) == {"SagsTitel": "x"}


# --- MERGE må ikke kunne lyve ---------------------------------------
#
# `mapping load` sagde "Indlæste 1 rækker fra afdeling.csv", men rækken
# landede aldrig i map.kodeliste. Tallet var filens rækketal, ikke
# databasens, så en MERGE der matchede ingenting så vellykket ud.

def test_kodelister_merges_en_ad_gangen():
    """fast_executemany binder alle rækker efter den FØRSTES typer, og en
    MERGE hvis ON-led sammenligner sådan en parameter kan stilfærdigt
    matche ingenting."""
    import inspect

    from konvertering import mapping as mapping_modul

    kilde = inspect.getsource(mapping_modul.indlaes_csv)
    assert "hurtig=False" in kilde


def test_udfoer_mange_returnerer_beroerte_raekker():
    from konvertering.db import Database

    class Markoer:
        rowcount = 1

        def execute(self, *_a):
            return self

    class Db(Database):
        def __init__(self):
            pass

        def transaktion(self):
            from contextlib import contextmanager

            @contextmanager
            def ctx():
                yield Markoer()

            return ctx()

    assert Db().udfoer_mange("MERGE ...", [(1,), (2,), (3,)], hurtig=False) == 3


# --- vejen tilbage fra databasen -------------------------------------
#
# Filerne er kilden, databasen kopien. Men kopien er den der overlever en
# udpakning der overskriver mapping/*.csv. Uden `mapping gem` er en
# fagpersons formiddag væk, selv om værdierne står i databasen hele tiden.

class _FalskDb:
    def __init__(self, raekker):
        self._raekker = raekker

    def hent_alle(self, _sql, *_p):
        return self._raekker


def _db_med(*raekker):
    from konvertering.mapping import KOLONNER

    fulde = [{**{k: "" for k in KOLONNER}, **r} for r in raekker]
    return _FalskDb(fulde)


def test_gem_skriver_en_fil_pr_liste(tmp_path):
    from konvertering.mapping import skriv_mappe

    db = _db_med(
        {"liste": "paragraf", "go_vaerdi": "SEL 11.3", "sbsys_vaerdi": "4"},
        {"liste": "tilbud", "go_vaerdi": "Psykologsamtaler", "sbsys_vaerdi": "1"},
    )
    assert skriv_mappe(db, tmp_path) == {"paragraf": 1, "tilbud": 1}
    assert (tmp_path / "paragraf.csv").exists()
    assert (tmp_path / "tilbud.csv").exists()


def test_gem_og_load_bruger_samme_format(tmp_path):
    """Det skrevne skal kunne læses igen — ellers er det ikke en sikkerhed."""
    from konvertering.mapping import skriv_mappe
    from tests.test_mapping_filer import KOLONNER

    db = _db_med({"liste": "paragraf", "go_vaerdi": "SEL 11.3",
                  "sbsys_vaerdi": "4", "handling": "map", "godkendt_af": "RKU"})
    skriv_mappe(db, tmp_path)

    import csv

    with (tmp_path / "paragraf.csv").open(encoding="utf-8-sig") as f:
        [raekke] = list(csv.DictReader(f, delimiter=";"))
    assert list(raekke) == KOLONNER
    assert raekke["sbsys_vaerdi"] == "4"
    assert raekke["godkendt_af"] == "RKU"


def test_null_bliver_tomt_felt_ikke_ordet_none(tmp_path):
    from konvertering.mapping import skriv_mappe

    db = _db_med({"liste": "kle", "go_vaerdi": "GO-LOKAL-042", "sbsys_vaerdi": None})
    skriv_mappe(db, tmp_path)
    assert "None" not in (tmp_path / "kle.csv").read_text(encoding="utf-8-sig")


@pytest.mark.parametrize("navn", ["../slem", ".skjult", "?", "b?lge", "a:b", "en liste"])
def test_listenavn_der_ikke_kan_blive_filnavn_springes_over(tmp_path, navn):
    """Windows afviser ? * : < > | " — og et '?' er typisk et tabt æøå fra
    en INSERT uden N-præfiks. Resten af listerne skal ud alligevel."""
    from konvertering.mapping import skriv_mappe

    db = _db_med({"liste": navn, "go_vaerdi": "x"},
                 {"liste": "kle", "go_vaerdi": "17.01.05"})
    assert skriv_mappe(db, tmp_path) == {"kle": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["kle.csv"]


# --- kladder må ikke journaliseres -----------------------------------
#
# GO's Journaliseret = 0 betyder kladde. Begge former findes på en sag i
# SBSYS, men journalisering ændrer dokumentets status i en børnesag og
# kan ikke rulles tilbage. Derfor sin egen sti: POST /api/kladde.

def test_kladde_sendes_ad_sin_egen_sti():
    import inspect

    from konvertering.loaders import dokument

    send = inspect.getsource(dokument.DokumentLoader.send)
    assert "opret_kladde" in send
    assert "journaliser" in send, "et almindeligt dokument journaliseres stadig"


def test_kladde_faar_sin_egen_krop():
    import inspect

    from konvertering.loaders import dokument

    kilde = inspect.getsource(dokument.DokumentLoader.payload)
    assert "er_kladde" in kilde
    assert '"kladde"' in kilde, "kladden bruger feltkortet 'kladde', ikke 'dokument'"


def test_kladdestierne_er_rigtige():
    from konvertering.sbsys.endpoints import Endpoints

    e = Endpoints()
    assert e.operation("opret_kladde").sti == "/api/kladde"
    assert e.operation("soeg_kladde").sti == "/api/kladde/search"
    # Den modsatte vej findes ogsaa i SBSYS (POST /api/kladde/{id}/journaliser),
    # men den er bevidst IKKE defineret: konverteringen må ikke journalisere
    # en kladde, for så skifter dokumentets status i en børnesag.
    assert "journaliser_kladde" not in e._operationer


def test_dokumentmodellen_baerer_kladdeflaget():
    from konvertering.models import Dokument

    d = Dokument(kilde_noegle="X", sag_noegle="S", titel="T")
    assert d.er_kladde is False
    assert Dokument(kilde_noegle="X", sag_noegle="S", titel="T", er_kladde=True).er_kladde
