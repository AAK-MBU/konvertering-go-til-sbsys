"""Tests af indsatser (foranstaltninger).

Tre beslutninger fra 09-11-2026 former loaderen, og det er dem testene
her holder fast i:

* **Økonomi er ikke en del af konverteringen.** Kaldet tager vederlag,
  tillægspris og engangstillæg — vi sender dem ikke.
* **GO-nøglen må ikke skrives ind i SBSYS.** Så findes ADR-0001's
  sikkerhedsnet ikke, og genkendelsen må hvile på en naturlig nøgle:
  sag + foranstaltning + tilbud + startdato.
* **Mapningen kommer senere.** Indtil da skal indsatsen afvises pænt, ikke
  sendes med huller i.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from konvertering.loaders.indsats import ENHEDSTYPER, FIKTIV_STARTDATO, IndsatsLoader
from konvertering.mapping import Kodelister, Opslag
from konvertering.models import Indsats
from konvertering.sbsys.endpoints import Endpoints
from konvertering.sbsys.errors import (
    ManglerMapping,
    PermanentFejl,
    UdeladtMedVilje,
    ValideringsFejl,
)


def _loader(**kontekst: Any) -> IndsatsLoader:
    k = SimpleNamespace(endpoints=Endpoints(), **{"bu_klient": None, **kontekst})
    return IndsatsLoader(k)


def _indsats(**felter: Any) -> Indsats:
    standard = {
        "kilde_noegle": "TEST-INDSATS-001",
        "sag_noegle": "TEST-SAG-001",
        "paragraf": "1",
        "foranstaltning": "2",
        "tilbud": "3",
        "periode_start": datetime(2023, 2, 1),
    }
    return Indsats(**{**standard, **felter})


# --- økonomi holdes ude ---------------------------------------------

def test_oekonomifelter_sendes_ikke():
    """Kaldet tager dem, men økonomi er ikke en del af konverteringen."""
    krop = _loader().payload(_indsats(antal_enheder=12, enhedstype="Time"))

    for felt in ("antalVederlag", "tillaegsPrisPerEnhed", "engangsTillaeg", "antalDoegn"):
        assert felt not in krop, f"{felt} er økonomi og hører ikke med"


def test_periode_og_omfang_sendes():
    krop = _loader().payload(
        _indsats(periode_slut=datetime(2023, 12, 31), antal_enheder=12, enhedstype="Time")
    )
    assert krop["periodeStart"].startswith("2023-02-01")
    assert krop["periodeSlut"].startswith("2023-12-31")
    assert krop["antalEnheder"] == 12
    assert krop["antalEnhederEnhedsType"] == "Time"


def test_aaben_indsats_har_ingen_slutdato():
    """En løbende indsats skal IKKE have en opfundet slutdato."""
    assert "periodeSlut" not in _loader().payload(_indsats())


def test_id_er_sendes_som_tal():
    """.NET-enden er int32. "1" er ikke 1."""
    krop = _loader().payload(_indsats())
    assert krop["paragrafId"] == 1
    assert krop["foranstaltningId"] == 2
    assert krop["tilbudId"] == 3


# --- validering ------------------------------------------------------

def test_indsats_uden_startdato_afvises():
    """En indsats uden periode er ikke en indsats."""
    with pytest.raises(ValideringsFejl) as e:
        _loader().payload(_indsats(periode_start=None))
    assert "periode" in str(e.value).lower()


def test_enheder_uden_enhedstype_afvises():
    """12 hvad? SBSYS' enum tager kun de seks værdier, og at gætte på
    "timer" vs "Time" ville give en 400 langt senere."""
    with pytest.raises(ValideringsFejl) as e:
        _loader().payload(_indsats(antal_enheder=12, enhedstype=None))
    assert "enhedstype" in str(e.value).lower()


@pytest.mark.parametrize("enhed", ENHEDSTYPER)
def test_sbsys_egne_enhedstyper_accepteres(enhed):
    assert _loader().payload(_indsats(antal_enheder=1, enhedstype=enhed))[
        "antalEnhederEnhedsType"
    ] == enhed


def test_uden_bu_base_url_afvises_med_en_forklaring():
    """Ikke en AttributeError: Børn og Unge-API'et er en anden tjeneste,
    og det skal fejlen sige."""
    loader = _loader(idmap=SimpleNamespace(sbsys_id=lambda *_: "42"))
    with pytest.raises(ValideringsFejl) as e:
        loader.send(_indsats(), {"paragrafId": 1})
    assert "SBSYS_BU_BASE_URL" in str(e.value)


# --- naturlig nøgle i stedet for ekstern -----------------------------
#
# GO-nøglen må ikke skrives ind i SBSYS, så en indsats genkendes på sag,
# foranstaltning, tilbud og startdato. Uden den kontrol ville et tabt svar
# give en dublet ved næste kørsel.

def _med_soegesvar(traef: list[dict], kendte: set[str] | None = None) -> IndsatsLoader:
    kaldte: list[str] = []

    class Klient:
        def kald(self, _m, sti, **_kw):
            kaldte.append(sti)
            return SimpleNamespace(status=200, krop=traef, korrelations_id="x")

    idmap = SimpleNamespace(kendte_sbsys_id=lambda _t, ider: {i for i in ider if i in (kendte or set())})
    loader = _loader(bu_klient=Klient(), idmap=idmap)
    loader.kaldte = kaldte  # type: ignore[attr-defined]
    return loader


_KROP = {"sagId": 42, "paragrafId": 1, "foranstaltningId": 2, "tilbudId": 3,
         "periodeStart": "2023-02-01T00:00:00"}


def test_identisk_indsats_genkendes():
    loader = _med_soegesvar([
        {"id": 77, "tilbudId": 3, "paragrafId": 1, "startDato": "2023-02-01T00:00:00"}
    ])
    assert loader._find_identisk(_KROP) == "77"


def test_anden_startdato_er_en_anden_indsats():
    """Samme tilbud i to perioder er to indsatser, ikke én."""
    loader = _med_soegesvar([
        {"id": 77, "tilbudId": 3, "paragrafId": 1, "startDato": "2024-01-15T00:00:00"}
    ])
    assert loader._find_identisk(_KROP) is None


def test_flere_ukendte_ens_traef_stopper_indsatsen():
    """To ens indsatser som vi ikke selv har registreret: at vælge den ene
    ville være et gæt, og at oprette endnu én måske en dublet."""
    r = {"id": 77, "tilbudId": 3, "paragrafId": 1, "startDato": "2023-02-01T00:00:00"}
    with pytest.raises(PermanentFejl):
        _med_soegesvar([r, {**r, "id": 78}])._find_identisk(_KROP)


# Testdumpet 17-11-2026: to forskellige GO-foranstaltninger af samme type på
# samme sag kan have samme periode. Den første må ikke "genbruges" som den
# anden — så ville den anden aldrig blive oprettet, og ingen fejl ville vise det.

def test_allerede_oprettet_soeskende_genbruges_ikke():
    r = {"id": 77, "tilbudId": 3, "paragrafId": 1, "startDato": "2023-02-01T00:00:00"}
    assert _med_soegesvar([r], kendte={"77"})._find_identisk(_KROP) is None


def test_den_tabte_findes_blandt_kendte_soeskende():
    """77 er oprettet og registreret; 78 blev oprettet, men svaret gik tabt."""
    r = {"id": 77, "tilbudId": 3, "paragrafId": 1, "startDato": "2023-02-01T00:00:00"}
    loader = _med_soegesvar([r, {**r, "id": 78}], kendte={"77"})
    assert loader._find_identisk(_KROP) == "78"


def test_anden_slutdato_er_en_anden_indsats():
    r = {"id": 77, "tilbudId": 3, "paragrafId": 1, "startDato": "2023-02-01T00:00:00",
         "slutDato": "2023-06-30T00:00:00"}
    krop = {**_KROP, "periodeSlut": "2023-12-31T00:00:00"}
    assert _med_soegesvar([r])._find_identisk(krop) is None


def test_ukendt_slutdatofelt_giver_ikke_dubletter():
    """Har svaret slet ikke et slutdatofelt, sammenlignes der ikke på det —
    ellers så hver indsats ny ud, og hver genkørsel gav en dublet."""
    r = {"id": 77, "tilbudId": 3, "paragrafId": 1, "startDato": "2023-02-01T00:00:00"}
    krop = {**_KROP, "periodeSlut": "2023-12-31T00:00:00"}
    assert _med_soegesvar([r])._find_identisk(krop) == "77"


def test_opslaget_afgraenses_til_sagen():
    """Sagens egen liste frem for en søgning: ét id vi har i forvejen, og
    ingen kriterienavne at gætte på."""
    loader = _med_soegesvar([])
    loader._find_identisk(_KROP)
    assert loader.kaldte[0] == "/bu_api/sag/42/sag_foranstaltninger"  # type: ignore[attr-defined]


def test_periodestart_taeller_ogsaa_som_startdato():
    """Oprettelsen hedder periodeStart, og udlæsningen kan hedde det samme.
    Et felt der ikke genkendes, ville give en dublet ved hver kørsel."""
    loader = _med_soegesvar([
        {"id": 77, "tilbudId": 3, "paragrafId": 1, "periodeStart": "2023-02-01T00:00:00"}
    ])
    assert loader._find_identisk(_KROP) == "77"


# --- konverteringsarket 17-11-2026 ----------------------------------
#
# Tilbuddet ER mappetypen (Indholdstype). Tom Fra-dato bliver 1900-01-01.
# Typer der ikke skal konverteres, står med handling = ignorer.

def _kodelister(**typer: str | None) -> Kodelister:
    """typer: GO-type -> id (samme id i alle tre lister), eller None = ignorer."""
    poster = {}
    for typ, vaerdi in typer.items():
        for liste in ("paragraf", "foranstaltning", "tilbud"):
            poster[(liste, typ)] = (Opslag(None, "ignorer") if vaerdi is None
                                    else Opslag(vaerdi, "map"))
    return Kodelister(poster)


def _raekke(**felter: Any) -> dict[str, Any]:
    return {"kilde_noegle": "K", "sag_noegle": "S", "indholdstype_go": "Psykolog",
            "ydelse_go": None, "bevillingstype_go": None,
            "periode_start": datetime(2023, 2, 1), **felter}


def test_tripellet_slaas_op_paa_mappetypen_ikke_ydelsen():
    loader = _loader(kodelister=_kodelister(Psykolog="4"))
    indsats = loader.byg(_raekke(ydelse_go="ABA-supervisor", bevillingstype_go="Kat. 1"))
    assert (indsats.paragraf, indsats.foranstaltning, indsats.tilbud) == ("4", "4", "4")


def test_ukendt_mappetype_afvises():
    loader = _loader(kodelister=_kodelister(Psykolog="4"))
    with pytest.raises(ManglerMapping):
        loader.byg(_raekke(indholdstype_go="Ukendt type"))


def test_tom_fra_dato_bliver_1900():
    loader = _loader(kodelister=_kodelister(Psykolog="4"))
    indsats = loader.byg(_raekke(periode_start=None))
    assert indsats.periode_start == FIKTIV_STARTDATO == datetime(1900, 1, 1)
    assert loader.payload(indsats)["periodeStart"].startswith("1900-01-01")


def test_type_der_ikke_konverteres_springes_over():
    loader = _loader(kodelister=_kodelister(Konsulent=None))
    with pytest.raises(UdeladtMedVilje):
        loader.byg(_raekke(indholdstype_go="Konsulent"))


def test_udeladt_taeller_som_sprunget_over_ikke_afvist():
    """Der er intet at rette, så det må ikke stå i afvisningsrapporten — og
    intet skrives i id_map, så en ændret mapning slår igennem næste gang."""
    from konvertering.loaders.base import Resultat

    skrevet: list[str] = []
    idmap = SimpleNamespace(**{n: (lambda *a, n=n, **k: skrevet.append(n))
                               for n in ("reserver", "markér_fejlet", "markér_sprunget_over")})
    loader = _loader(kodelister=_kodelister(Konsulent=None), idmap=idmap)
    r = Resultat()
    loader._behandl_en(_raekke(indholdstype_go="Konsulent"), r)
    assert (r.sprunget_over, r.afvist) == (1, 0)
    assert skrevet == []


# --- registrering ----------------------------------------------------

def test_indsats_er_en_objekttype_man_kan_koere():
    from konvertering.loaders import LOADERS

    assert "indsats" in LOADERS
    assert LOADERS["indsats"].arbejdsliste == "stg.v_indsatser_til_indlaesning"


def test_stierne_peger_paa_bu_api():
    e = Endpoints()
    assert e.operation("opret_indsats").sti == "/bu_api/sag_foranstaltning"
    assert e.operation("soeg_indsats").sti == "/bu_api/sag_foranstaltning/search"
    assert e.operation("hent_katalog").sti == "/bu_api/foranstaltning/katalog"
    assert e.operation("hent_sag_indsatser", sag_id=7).sti == (
        "/bu_api/sag/7/sag_foranstaltninger")
    # Livstegnet ligger under /api/, ikke /bu_api/ — en stavefejl her ville
    # få selvtesten til at melde BU-værten død.
    assert e.operation("bu_livstegn").sti == "/api/health"


# --- kataloget er mapningskilden -------------------------------------

def test_kataloget_foldes_ud_til_tripler():
    """Kataloget kommer som et træ, men det er tripler fagpersonen skal
    vælge imellem — og tripler kaldet kræver."""
    from konvertering.cli import _fold_katalog

    traeet = [{
        "id": 11, "navn": "SEL 11.3",
        "foranstaltninger": [{
            "id": 22, "navn": "Psykologsamtaler",
            "tilbuds": [{"id": 33, "navn": "PPR intern"}, {"id": 34, "navn": "Ekstern"}],
        }],
    }]

    flade = _fold_katalog(traeet)

    assert len(flade) == 2
    assert flade[0] == {
        "paragrafId": 11, "paragraf": "SEL 11.3",
        "foranstaltningId": 22, "foranstaltning": "Psykologsamtaler",
        "tilbudId": 33, "tilbud": "PPR intern",
    }


def test_foranstaltning_uden_tilbud_forsvinder_ikke():
    """Ellers mangler den i den liste fagpersonen skal tage stilling ud fra."""
    from konvertering.cli import _fold_katalog

    flade = _fold_katalog([{"id": 1, "navn": "P", "foranstaltninger": [{"id": 2, "navn": "F"}]}])
    assert len(flade) == 1
    assert flade[0]["tilbudId"] is None


# --- udslagsgivende forhold (23-11-2026) -----------------------------

def _med_udslagsgivende(**poster: str) -> Kodelister:
    k = _kodelister(Psykolog="4")
    for go, sbsys in poster.items():
        k._poster[("udslagsgivende_forhold", go)] = Opslag(sbsys, "map")
    return k


def test_tom_udslagsgivende_slaas_op_som_tom():
    """GO har intet på PPR. Mapningens <tom>-række ("Ingen") udfylder det."""
    loader = _loader(kodelister=_med_udslagsgivende(**{"<tom>": "7"}))
    krop = loader.payload(loader.byg(_raekke(udslagsgivende_go=None)))
    assert krop["udslagsgivendeForholdIds"] == [7]


def test_udslagsgivende_fra_go_vinder_over_tom():
    loader = _loader(kodelister=_med_udslagsgivende(**{"<tom>": "7", "Vold": "3"}))
    krop = loader.payload(loader.byg(_raekke(udslagsgivende_go="Vold")))
    assert krop["udslagsgivendeForholdIds"] == [3]


def test_uden_tom_raekke_sendes_intet():
    loader = _loader(kodelister=_kodelister(Psykolog="4"))
    assert "udslagsgivendeForholdIds" not in loader.payload(loader.byg(_raekke()))


def test_indsatser_hentes_sag_for_sag_i_kronologisk_orden():
    """PPR 25-11-2026: kronologisk i SBSYS. Viewets raekkefoelge er
    sag|Fra|Til|Visiteret|kilde_noegle, og loaderen sideopdeler på den."""
    kald: dict[str, Any] = {}

    class Db:
        def hent_i_sider(self, kilde: str, *filtre: str, **kw: Any) -> list:
            kald.update(kw)
            return []

    list(_loader(db=Db(), boelge=None).hent_arbejde())
    assert kald["noeglekolonne"] == "raekkefoelge"

    from pathlib import Path
    sql = (Path(__file__).resolve().parents[1] / "sql" / "030_stg_schema.sql").read_text("utf-8")
    view = sql.split("VIEW stg.v_indsatser_til_indlaesning")[1].split("\nGO")[0]
    dele = ["i.sag_noegle", "ISNULL(i.periode_start, '99991231')",
            "ISNULL(i.periode_slut,  '99991231')", "'$.Visiteret'", "'|' + i.kilde_noegle"]
    placering = [view.index(d) for d in dele]
    assert placering == sorted(placering)
