"""Tests af kodelisteforberedelsen.

En mapning har to sider. SBSYS-siden kan hentes, GO-siden kan læses — men
parringen er en faglig vurdering. Testene her holder på den grænse: alt
det der ikke er vurdering skal gøres for fagpersonen, og alt det der ER
vurdering skal lades urørt.
"""

from __future__ import annotations

from typing import Any

import pytest

from konvertering import forbered as f


class FalskDb:
    def __init__(self, svar: dict[str, list[str]]) -> None:
        self.svar = svar
        self.sql: list[str] = []

    def hent_alle(self, sql: str, *_p: Any) -> list[dict[str, Any]]:
        self.sql.append(sql)
        for noegle, vaerdier in self.svar.items():
            if noegle in sql:
                return [{"v": v} for v in vaerdier]
        raise RuntimeError("tabellen findes ikke")


MULIGHEDER = [("1", "PPR"), ("2", "Ellevangskolen"), ("3", "PPR Nord")]


# --- GO-siden --------------------------------------------------------

def test_kun_de_vaerdier_go_faktisk_bruger():
    """En kodeliste over alt der er muligt er ubrugelig. En over de
    værdier der står i data, kan udfyldes på en formiddag."""
    db = FalskDb({"kle_go": ["17.01.05", "27.24.03", "17.01.05"]})
    assert f.go_vaerdier(db, f.KILDER["kle"]) == ["17.01.05", "27.24.03"]


def test_flere_tabeller_slaas_sammen():
    """Brugere optræder på både sager, dokumenter og notater."""
    db = FalskDb({"ansvarlig_go": ["ab1234"], "forfatter_go": ["cd5678", "ab1234"]})
    assert f.go_vaerdier(db, f.KILDER["bruger"]) == ["ab1234", "cd5678"]


def test_manglende_tabel_er_ikke_fatalt():
    """stg.indsats findes måske ikke endnu. Så er listen bare tom."""
    assert f.go_vaerdier(FalskDb({}), f.KILDER["tilbud"]) == []


# --- parringen -------------------------------------------------------

def test_identisk_navn_foreslaas():
    assert f.forslag("PPR", MULIGHEDER) == [("1", "PPR")]


@pytest.mark.parametrize("skrivemaade", ["ppr nord", "PPR-Nord", "PPRNORD", " PPR  Nord "])
def test_skrivemaaden_er_ligegyldig(skrivemaade):
    """"PPR Nord" og "ppr-nord" er samme ord for et menneske. At lade
    mennesket lave den sammenligning er spild."""
    assert f.forslag(skrivemaade, MULIGHEDER) == [("3", "PPR Nord")]


def test_delvist_match_giver_kandidater_ikke_et_svar():
    kandidater = f.forslag("Nord", MULIGHEDER)
    assert ("3", "PPR Nord") in kandidater


def test_intet_der_ligner_giver_ingenting():
    """Et forslag der ligner lidt, får folk til at trykke ja uden at
    læse. Det er værre end intet forslag."""
    assert f.forslag("Tandplejen", MULIGHEDER) == []


def test_tom_go_vaerdi_giver_ingen_forslag():
    assert f.forslag("", MULIGHEDER) == []


# --- arket -----------------------------------------------------------

def test_entydigt_match_udfyldes_men_markeres_som_forslag():
    [raekke] = f.byg_raekker("afdeling", ["PPR"], MULIGHEDER)
    assert raekke["sbsys_vaerdi"] == "1"
    assert raekke["sbsys_tekst"] == "PPR"
    assert "FORSLAG" in raekke["kommentar"]
    assert "bekræft" in raekke["kommentar"]


def test_der_godkendes_aldrig_automatisk():
    """Et automatisk forslag er et forslag. Godkendelsen er en persons
    ansvar, og den skal kunne spores til et navn."""
    for raekke in f.byg_raekker("afdeling", ["PPR", "Ukendt"], MULIGHEDER):
        assert raekke["godkendt_af"] == ""
        assert raekke["godkendt_dato"] == ""


def test_flere_kandidater_udfyldes_ikke():
    [raekke] = f.byg_raekker("afdeling", ["PPR N"], MULIGHEDER)
    assert raekke["sbsys_vaerdi"] == ""
    assert "kandidater:" in raekke["kommentar"]


def test_allerede_mappet_roeres_ikke():
    """Filen skal være fuldstændig, men ingen skal lave arbejdet igen."""
    [raekke] = f.byg_raekker("afdeling", ["PPR"], MULIGHEDER, kendte={"PPR"})
    assert raekke["sbsys_vaerdi"] == ""
    assert "allerede mappet" in raekke["kommentar"]


def test_uden_sbsys_side_siges_det_ligeud():
    """KLE kommer ikke fra SBSYS' API. Så er arket stadig nyttigt — det
    skal bare ikke lade som om det kender svaret."""
    [raekke] = f.byg_raekker("kle", ["17.01.05"], [])
    assert "udfyld i hånden" in raekke["kommentar"]


def test_kolonnerne_passer_til_mapping_formatet():
    """Filen skal kunne kopieres direkte til mapping/ når den er udfyldt."""
    from tests.test_mapping_filer import KOLONNER

    [raekke] = f.byg_raekker("afdeling", ["PPR"], MULIGHEDER)
    assert list(raekke) == KOLONNER
    assert raekke["liste"] == "afdeling"


# --- SBSYS-siden -----------------------------------------------------

def test_kataloget_giver_tre_lister_fra_ét_kald():
    """Paragraf, foranstaltning og tilbud kommer fra samme svar — kun
    felterne er forskellige."""
    flade = [{"paragrafId": 11, "paragraf": "SEL 11.3",
              "foranstaltningId": 22, "foranstaltning": "Psykologsamtaler",
              "tilbudId": 33, "tilbud": "PPR intern"}]

    assert f.sbsys_muligheder(flade, f.KILDER["paragraf"]) == [("11", "SEL 11.3")]
    assert f.sbsys_muligheder(flade, f.KILDER["tilbud"]) == [("33", "PPR intern")]


def test_dubletter_fjernes():
    """Samme paragraf optræder én gang pr. tilbud i det flade katalog."""
    raekke = {"paragrafId": 11, "paragraf": "SEL 11.3"}
    assert len(f.sbsys_muligheder([raekke, dict(raekke)], f.KILDER["paragraf"])) == 1


def test_raekker_uden_id_springes_over():
    assert f.sbsys_muligheder([{"paragraf": "uden id"}], f.KILDER["paragraf"]) == []


def test_enhedstype_er_en_enum_og_ikke_et_opslag():
    """SBSYS' enhedstyper står i koden, ikke i et endpoint."""
    kilde = f.KILDER["enhedstype"]
    assert kilde.operation is None
    assert "Doegn" in kilde.faste


def test_hver_liste_i_mapping_har_en_kilde():
    """Ellers kan den ene ikke forberedes, og det opdages først når nogen
    leder efter filen."""
    from tests.test_mapping_filer import _lister_i_koden

    mangler = sorted(_lister_i_koden() - set(f.KILDER))
    assert not mangler, f"lister uden en kilde i forbered.KILDER: {mangler}"
