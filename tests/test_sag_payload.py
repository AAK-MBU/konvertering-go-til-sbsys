"""Sagens og dokumentets payload mod SBSYS' egen API-beskrivelse.

Baggrund (16-11-2026): sagspayloaden indeholdt fire felter oprettelsen ikke
kender — SagsNummer, SagsStatusId, SagsDato og SagsTypeId — og afdelingen
blev sendt som AnsaettelsesstedId på et objekt der hedder Id. Attrappen tager
imod alt, så det blev først opdaget ved at læse payloaden op mod modellen.

Kontrakttesten nederst gør det maskinelt: hvert feltnavn vi sender, skal
findes i modellen for det endpoint det sendes til, i
rapporter/sbsys-spec.json (SBSYS' OpenAPI-beskrivelse, hentet med
`konv spec gem`).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from konvertering.loaders.sag import EFTER_OPRETTELSE, STATUSKOMMENTAR, SagLoader
from konvertering.models import Sag
from konvertering.sbsys.endpoints import STANDARD_FELTER, Endpoints
from konvertering.sbsys.errors import PermanentFejl

SPEC = Path(__file__).resolve().parents[1] / "rapporter" / "sbsys-spec.json"


# --- en lille verden at koere loaderen i -------------------------------

class Klient:
    """Optager kaldene og svarer efter sti, som SBSYS' modeller beskriver."""

    def __init__(self, soegeresultat=None, status_fejler=False):
        self.log: list[dict[str, Any]] = []
        self.soegeresultat = soegeresultat or []
        self.status_fejler = status_fejler

    def kald(self, metode, sti, **kw):
        self.log.append({"metode": metode, "sti": sti, **kw})
        if sti.endswith("/person/search"):
            return SimpleNamespace(status=200, krop=[{"Id": 7}], korrelations_id="k")
        if sti.endswith("/sag/search"):
            return SimpleNamespace(status=200, krop={"Results": self.soegeresultat},
                                   korrelations_id="k")
        if sti.endswith("/status"):
            if self.status_fejler:
                raise PermanentFejl("opdater_sagsstatus: HTTP 400 — kommentar påkrævet",
                                    http_status=400)
            return SimpleNamespace(status=200, krop={}, korrelations_id="k")
        if sti.endswith("/sag/template"):
            return SimpleNamespace(
                status=200, korrelations_id="k",
                krop={"Id": 1001, "Nummer": "17.01.05-G01-1-26",
                      "SagsNummer": {"Aarstal": 2026, "SekvensNummer": 1}},
            )
        raise AssertionError(f"uventet kald: {metode} {sti}")


def _loader(klient: Klient) -> tuple[SagLoader, list[tuple]]:
    afviste: list[tuple] = []
    db = SimpleNamespace(
        hent_en=lambda _sql, *_p: {"part_type": "person", "cpr": "0101011234",
                                   "cvr": None, "navn": "x"},
        udfoer=lambda _sql, *p: afviste.append(p),
    )
    k = SimpleNamespace(endpoints=Endpoints(), klient=klient, db=db, run_id=1, boelge="ppr")
    return SagLoader(k), afviste


def _sag(**felter: Any) -> Sag:
    standard = dict(kilde_noegle="PPR-2015-123456-001", titel="Psykolog - Testbarn",
                    kle="17.01.05", facet="G01", skabelon="2", ansvarlig="19",
                    afdeling="5", status="7", primaer_part_noegle="0101011234")
    return Sag(**{**standard, **felter})


# --- payloaden ----------------------------------------------------------

def test_oprettelsen_bruger_modellens_feltnavne():
    loader, _ = _loader(Klient())
    p = loader.payload(_sag())

    assert p["SagsTitel"] == "Psykolog - Testbarn"
    assert p["EmneplanNummer"] == "17.01.05", "KLE hedder EmneplanNummer ved oprettelse"
    assert p["Facet"] == "G01"
    assert p["SkabelonId"] == 2
    assert p["SagsBehandlerID"] == 19
    assert p["Ansaettelsessted"] == {"Id": 5}
    assert p["PrimaryPart"] == {"PartId": 7, "PartType": "Person"}
    assert p["Parts"] == [p["PrimaryPart"]]


@pytest.mark.parametrize("felt", ["SagsNummer", "SagsStatusId", "SagsDato", "SagsTypeId",
                                  "Afsluttetdato", "Beskrivelse"])
def test_felter_oprettelsen_ikke_kender_sendes_ikke(felt):
    loader, _ = _loader(Klient())
    assert felt not in loader.payload(_sag(beskrivelse="b", sagstype="3"))


def test_status_ligger_til_efter_oprettelsen_med_kommentar():
    """Synlig i --vis-payload og med i hashen — men ikke i oprettelsen."""
    loader, _ = _loader(Klient())
    efter = loader.payload(_sag())[EFTER_OPRETTELSE]
    assert efter["status"] == {"SagsStatusID": 7, "Kommentar": STATUSKOMMENTAR}


def test_uden_status_intet_efterfoelgende_kald():
    loader, _ = _loader(Klient())
    assert EFTER_OPRETTELSE not in loader.payload(_sag(status=None))


# --- afsendelsen ----------------------------------------------------------

def test_oprettelse_og_derefter_status():
    klient = Klient()
    loader, _ = _loader(klient)
    s = _sag()
    sbsys_id, nummer = loader.send(s, loader.payload(s))

    assert sbsys_id == "1001"
    assert nummer == "17.01.05-G01-1-26", "Nummer — ikke SagsNummer, der er et objekt"
    stier = [k["sti"] for k in klient.log]
    assert stier[-2:] == ["/api/sag/template", "/api/sag/1001/status"]
    oprettelse = next(k for k in klient.log if k["sti"] == "/api/sag/template")["json"]
    assert EFTER_OPRETTELSE not in oprettelse, "den interne noegle maa aldrig sendes"
    assert next(k for k in klient.log if k["sti"].endswith("/status"))["metode"] == "PUT"


def test_fejlet_status_afviser_ikke_den_oprettede_sag():
    """Sagen findes i SBSYS. Rejses fejlen, mister konv.id_map koblingen,
    og naeste koersel opretter en dublet. Derfor: advarsel, ikke afvisning."""
    loader, afviste = _loader(Klient(status_fejler=True))
    s = _sag()
    sbsys_id, _ = loader.send(s, loader.payload(s))

    assert sbsys_id == "1001"
    assert len(afviste) == 1
    assert "status_ikke_sat" in afviste[0]
    assert "advarsel" in afviste[0]


def test_status_saettes_ogsaa_paa_en_genfundet_sag():
    """Blev svaret tabt efter oprettelsen, naaede statussen aldrig at blive sat."""
    klient = Klient(soegeresultat=[
        {"Id": 555, "SagsTitel": "Psykolog - Testbarn", "PrimaryPart": {"PartId": 7}}])
    loader, _ = _loader(klient)
    s = _sag()
    sbsys_id, _ = loader.send(s, loader.payload(s))

    assert sbsys_id == "555"
    stier = [k["sti"] for k in klient.log]
    assert "/api/sag/template" not in stier, "den genfundne sag maa ikke oprettes igen"
    assert "/api/sag/555/status" in stier


# --- genfinding paa naturlig noegle -------------------------------------

def test_soegningen_bruger_soegemodellens_felter():
    klient = Klient()
    loader, _ = _loader(klient)
    s = _sag()
    loader.send(s, loader.payload(s))

    soeg = next(k for k in klient.log if k["sti"] == "/api/sag/search")
    assert soeg["json"]["Titel"] == "Psykolog - Testbarn"
    assert soeg["json"]["PrimaerPerson"] == {"CprNummer": "010101-1234"}
    assert "SagsTitel" not in soeg["json"] and "PrimaryPart" not in soeg["json"]
    assert soeg["params"] == {"InkluderParterIResultat": "true"}


def test_anden_part_med_samme_titel_genbruges_ikke():
    """Ignorerer SBSYS et kriterium, kommer vilkaarlige sager retur. En sag
    paa et andet barn med samme titel maa aldrig blive genbrugt."""
    klient = Klient(soegeresultat=[
        {"Id": 555, "SagsTitel": "Psykolog - Testbarn", "PrimaryPart": {"PartId": 999}}])
    loader, _ = _loader(klient)
    s = _sag()
    sbsys_id, _ = loader.send(s, loader.payload(s))
    assert sbsys_id == "1001", "skulle have oprettet en ny, ikke genbrugt 555"


def test_resultat_uden_part_genbruges_ikke():
    klient = Klient(soegeresultat=[{"Id": 555, "SagsTitel": "Psykolog - Testbarn"}])
    loader, _ = _loader(klient)
    s = _sag()
    assert loader.send(s, loader.payload(s))[0] == "1001"


def test_to_ens_traef_er_ved_ikke():
    r = {"SagsTitel": "Psykolog - Testbarn", "PrimaryPart": {"PartId": 7}}
    klient = Klient(soegeresultat=[{**r, "Id": 1}, {**r, "Id": 2}])
    loader, _ = _loader(klient)
    s = _sag()
    assert loader._find_identisk(s, loader.payload(s)) is None


# --- dokumentet -----------------------------------------------------------

def test_dokumentets_felter_foelger_journaliseringens_model():
    from datetime import datetime

    from konvertering.loaders.dokument import DokumentLoader
    from konvertering.models import Dokument

    loader = DokumentLoader(SimpleNamespace(endpoints=Endpoints()))
    p = loader.payload(Dokument(kilde_noegle="5097", sag_noegle="S", titel="Referat",
                                kategori="12", forfatter="19",
                                dokument_dato=datetime(2023, 1, 16)))
    assert p == {
        "DokumentNavn": "Referat",
        "DokumentArt": {"Id": 12},
        "RegistreringsDato": "2023-01-16T00:00:00",
        "RegistreretAfID": 19,
    }


# --- kontrakten: hvert feltnavn skal findes i SBSYS' egen model ----------

def _model(navn: str) -> set[str]:
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    return set(spec["definitions"][navn]["properties"])


def _undermodel(navn: str, felt: str) -> set[str]:
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    ref = spec["definitions"][navn]["properties"][felt]["$ref"].split("/")[-1]
    return set(spec["definitions"][ref]["properties"])


KONTRAKT = {
    # feltgruppe -> (model, kanoniske felter der bevidst ikke udfyldes)
    "sag": ("ApiServer.Dto.Sag.CreateSagFromSkabelonDto", {"ekstern_noegle"}),
    "sagsstatus": ("ApiServer.Dto.Sag.SagsStatusModel", set()),
    "sagssoegning": ("ApiServer.Dto.Sag.V29.SagSearchQueryV29", set()),
    "dokument": ("ApiServer.Dto.Dokument.JournaliserDokumentInputDtoV10", set()),
    "kladde": ("ApiServer.Dto.Kladder.KladdeRedigerDto", set()),
}


@pytest.mark.parametrize("gruppe", sorted(KONTRAKT))
def test_feltnavne_findes_i_sbsys_model(gruppe):
    model, undtaget = KONTRAKT[gruppe]
    forkerte = []
    for kanonisk, sbsys in STANDARD_FELTER[gruppe].items():
        if kanonisk in undtaget:
            continue
        foerste, _, rest = sbsys.partition(".")
        if foerste not in _model(model):
            forkerte.append(f"{kanonisk} -> {sbsys}")
        elif rest and rest not in _undermodel(model, foerste):
            forkerte.append(f"{kanonisk} -> {sbsys} (undermodellen har ikke {rest})")
    assert not forkerte, f"{model} kender ikke: {forkerte}"


def test_eksempelfilen_overskriver_ikke_med_forkerte_felter(tmp_path):
    """endpoints.toml overskriver standardfelterne. Kopieres eksempelfilen,
    maa den ikke bringe de gamle, forkerte navne tilbage."""
    import shutil

    kopi = tmp_path / "endpoints.toml"
    shutil.copy(Path(__file__).resolve().parents[1] / "sbsys" / "endpoints.example.toml", kopi)
    e = Endpoints.fra_fil(kopi)
    for gruppe, (model, undtaget) in KONTRAKT.items():
        for kanonisk in STANDARD_FELTER[gruppe]:
            if kanonisk in undtaget:
                continue
            foerste = e.felt(gruppe, kanonisk).partition(".")[0]
            assert foerste in _model(model), f"eksempelfilen: {gruppe}.{kanonisk} = {foerste}"


# --- svaret fra SBSYS: hvilket felt er objektets id? -------------------
#
# Kladder og dokumenter har hver to id'er: registreringens og det
# underliggende objekts. SBSYS' kald tager registreringens. Testmiljøet
# 17-11-2026: kladden {"ID": 183, "KladdeID": 199, "SagID": 183} findes med
# GET /api/kladde/183, mens /199 giver 404. Dokumentet: Id 185, DokumentID 187.
# (16-11 tog vi fejlagtigt KladdeID, fordi ID tilfældigvis lig SagID.)

def _dokumentloader(tmp_path, svar: dict):
    from konvertering.config import FilIndstillinger
    from konvertering.files import FilOpslag
    from konvertering.loaders.dokument import DokumentLoader

    (tmp_path / "S").mkdir()
    (tmp_path / "S" / "1.docx").write_bytes(b"indhold")
    sendt: list[dict] = []

    class K:
        def kald(self, metode, sti, **kw):
            sendt.append({"sti": sti, **kw})
            return SimpleNamespace(status=200, krop=svar, korrelations_id="k")

    k = SimpleNamespace(endpoints=Endpoints(), klient=K(),
                        idmap=SimpleNamespace(sbsys_id=lambda *_: "183"),
                        filer=FilOpslag(FilIndstillinger(rod=tmp_path)))
    return DokumentLoader(k), sendt


def test_kladdens_id_er_registreringens_id(tmp_path):
    from konvertering.models import Dokument

    loader, sendt = _dokumentloader(tmp_path, {"ID": 183, "KladdeID": 199, "SagID": 183})
    d = Dokument(kilde_noegle="K", sag_noegle="S", titel="Udkast", er_kladde=True,
                 fil_sti="S/1.docx", fil_navn="Udkast.docx")
    assert loader.send(d, loader.payload(d)) == ("183", "kladde KladdeID=199")
    assert sendt[0]["sti"] == "/api/kladde"


def test_kladde_uden_registrerings_id_afvises_frem_for_at_gaette(tmp_path):
    from konvertering.models import Dokument

    loader, _ = _dokumentloader(tmp_path, {"KladdeID": 199, "SagID": 183})
    d = Dokument(kilde_noegle="K", sag_noegle="S", titel="Udkast", er_kladde=True,
                 fil_sti="S/1.docx", fil_navn="Udkast.docx")
    with pytest.raises(PermanentFejl):
        loader.send(d, loader.payload(d))


def test_journaliseret_dokument_bruger_registreringens_id(tmp_path):
    from konvertering.models import Dokument

    loader, sendt = _dokumentloader(tmp_path, {"Id": 185, "DokumentID": 187})
    d = Dokument(kilde_noegle="D", sag_noegle="S", titel="Indstilling",
                 fil_sti="S/1.docx", fil_navn="Indstilling.docx")
    assert loader.send(d, loader.payload(d)) == ("185", "journaliseret DokumentID=187")
    assert sendt[0]["sti"] == "/api/dokument/journaliser"


def test_dokument_uden_registrerings_id_afvises_frem_for_at_gaette(tmp_path):
    from konvertering.models import Dokument

    loader, _ = _dokumentloader(tmp_path, {"DokumentID": 187})
    d = Dokument(kilde_noegle="D", sag_noegle="S", titel="Indstilling",
                 fil_sti="S/1.docx", fil_navn="Indstilling.docx")
    with pytest.raises(PermanentFejl):
        loader.send(d, loader.payload(d))


def test_artens_beskrivelse_uden_det_andet_id(tmp_path):
    """Mangler KladdeID/DokumentID i svaret, står arten der stadig."""
    from konvertering.models import Dokument

    loader, _ = _dokumentloader(tmp_path, {"Id": 185})
    d = Dokument(kilde_noegle="D", sag_noegle="S", titel="Indstilling",
                 fil_sti="S/1.docx", fil_navn="Indstilling.docx")
    assert loader.send(d, loader.payload(d)) == ("185", "journaliseret")


# --- tom afdeling i GO (alle PPR-sager i dumpet, 20-11-2026) ----------------

def test_tom_afdeling_slaas_op_paa_sagstypen():
    from konvertering.loaders.sag import afdeling_noegle

    assert afdeling_noegle({"afdeling_go": None, "sagstype_go": "ABA"}) == "<tom>|ABA"
    assert afdeling_noegle({"afdeling_go": "  ", "sagstype_go": "ABA"}) == "<tom>|ABA"
    assert afdeling_noegle({"afdeling_go": "PPR", "sagstype_go": "ABA"}) == "PPR"


def test_tom_afdeling_uden_mapning_afvises_stadig():
    """Ansættelsesstedet styrer adgangen på PPR-sager — det gættes aldrig."""
    from konvertering.mapping import Kodelister, Opslag
    from konvertering.sbsys.errors import ManglerMapping

    m = Kodelister({("afdeling", "<tom>|ABA"): Opslag("15", "map")})
    assert m.slaa_op("afdeling", "<tom>|ABA") == "15"
    with pytest.raises(ManglerMapping):
        m.slaa_op("afdeling", "<tom>|Psykolog")


# --- sagsbehandleren: beskyttede PPR-sager kræver en (prod 20-11-2026) -------

class BrugerKlient:
    """Svarer på brugersøgningen. SBSYS kan søge bredere end på identen."""

    def __init__(self, brugere):
        self.brugere, self.soegninger = brugere, []

    def kald(self, metode, sti, **kw):
        assert sti.endswith("/bruger/search")
        self.soegninger.append(kw["json"])
        return SimpleNamespace(status=200, krop={"Results": self.brugere}, korrelations_id="k")


def _bruger_loader(brugere, kodelister=None):
    from konvertering.mapping import Kodelister

    k = SimpleNamespace(endpoints=Endpoints(), klient=BrugerKlient(brugere),
                        kodelister=kodelister or Kodelister({}))
    return SagLoader(k)


BRUGERE = [{"Id": 41, "LogonId": "AAK\\AZ12345"}, {"Id": 42, "LogonId": "az123456"}]


def test_gos_sagsbehandler_slaas_op_paa_az_identen(monkeypatch):
    monkeypatch.delenv("TEST_SAGSBEHANDLER_AZ", raising=False)
    loader = _bruger_loader(BRUGERE)
    assert loader._ansvarlig({"ansvarlig_go": "az12345", "kilde_noegle": "K"}) == "41"
    assert loader.k.klient.soegninger == [{"LogonId": "az12345"}]


def test_kun_et_praecist_traef_taeller(monkeypatch):
    """az12345 må ikke ramme az123456, selv om SBSYS svarer med begge."""
    monkeypatch.delenv("TEST_SAGSBEHANDLER_AZ", raising=False)
    loader = _bruger_loader([{"Id": 42, "LogonId": "az123456"}])
    assert loader._ansvarlig({"ansvarlig_go": "az12345", "kilde_noegle": "K"}) is None


def test_mapningen_vinder_over_opslaget(monkeypatch):
    from konvertering.mapping import Kodelister, Opslag

    monkeypatch.delenv("TEST_SAGSBEHANDLER_AZ", raising=False)
    loader = _bruger_loader(BRUGERE, Kodelister({("bruger", "az12345"): Opslag("99", "map")}))
    assert loader._ansvarlig({"ansvarlig_go": "az12345", "kilde_noegle": "K"}) == "99"
    assert loader.k.klient.soegninger == []


def test_tvungen_sagsbehandler_vinder_over_alt(monkeypatch):
    monkeypatch.setenv("TEST_SAGSBEHANDLER_AZ", "az12345")
    loader = _bruger_loader(BRUGERE)
    assert loader._ansvarlig({"ansvarlig_go": "az999", "kilde_noegle": "K"}) == "41"
    loader._ansvarlig({"ansvarlig_go": "az888", "kilde_noegle": "K2"})
    assert len(loader.k.klient.soegninger) == 1, "opslaget huskes"


def test_tvungen_sagsbehandler_der_ikke_findes_afviser_sagen(monkeypatch):
    from konvertering.sbsys.errors import ValideringsFejl

    monkeypatch.setenv("TEST_SAGSBEHANDLER_AZ", "az000")
    with pytest.raises(ValideringsFejl):
        _bruger_loader(BRUGERE)._ansvarlig({"ansvarlig_go": None, "kilde_noegle": "K"})


# --- dokumentets opretter (PPR 25-11-2026) --------------------------------

def _forfatter_loader(brugere: list[dict]):
    from konvertering.loaders.dokument import DokumentLoader

    kald: list[dict] = []

    class K:
        def kald(self, metode, sti, **kw):
            kald.append(kw.get("json"))
            return SimpleNamespace(status=200, krop=brugere, korrelations_id="k")

    class Kodelister:
        def slaa_op(self, *a, **kw):
            return None

    k = SimpleNamespace(endpoints=Endpoints(), klient=K(), kodelister=Kodelister())
    return DokumentLoader(k), kald


def test_dokumentets_opretter_slaas_op_paa_az_identen():
    loader, kald = _forfatter_loader([{"Id": 4711, "LogonId": "AAK\\AZ12345"},
                                      {"Id": 1, "LogonId": "AAK\\AZ123456"}])
    assert loader._forfatter({"forfatter_go": "az12345", "kilde_noegle": "1"}) == "4711"
    assert loader._forfatter({"forfatter_go": "AZ12345", "kilde_noegle": "2"}) == "4711"
    assert len(kald) == 1                                    # huskes for kørslen


@pytest.mark.parametrize("raekke", [
    {"forfatter_go": "Systemkonto (Inaktiv)"},
    {"forfatter_go": None},
    {"forfatter_go": "az12345", "er_kladde": 1},             # kladder kan ikke bære det
])
def test_ingen_opretter_uden_az_eller_paa_kladder(raekke):
    loader, kald = _forfatter_loader([{"Id": 4711, "LogonId": "az12345"}])
    assert loader._forfatter({"kilde_noegle": "1", **raekke}) is None
    assert kald == []


def test_opretter_der_ikke_findes_i_sbsys_sendes_ikke():
    loader, _ = _forfatter_loader([])
    assert loader._forfatter({"forfatter_go": "az99999", "kilde_noegle": "1"}) is None


def test_extract_tager_oprettetaf_foer_ansvarlig():
    sql = (Path(__file__).resolve().parents[1] / "sql" / "100_extract_skabelon.sql"
           ).read_text(encoding="utf-8")
    assert "COALESCE(NULLIF(LTRIM(RTRIM(d.[OprettetAf])), N''), d.[DokumentAnsvarlig])" in sql


def test_test_sagsbehandler_tvinges_som_opretter(monkeypatch):
    monkeypatch.setenv("TEST_SAGSBEHANDLER_AZ", "az54321")
    loader, kald = _forfatter_loader([{"Id": 77, "LogonId": "az54321"}])
    assert loader._forfatter({"forfatter_go": "Systemkonto (Inaktiv)", "kilde_noegle": "1"}) == "77"
    assert loader._forfatter({"forfatter_go": "az12345", "kilde_noegle": "2"}) == "77"
    assert loader._forfatter({"forfatter_go": "az12345", "er_kladde": 1,
                              "kilde_noegle": "3"}) is None
    assert kald == [{"LogonId": "az54321"}]


def test_test_sagsbehandler_der_ikke_findes_stopper_dokumentet(monkeypatch):
    from konvertering.sbsys.errors import ValideringsFejl

    monkeypatch.setenv("TEST_SAGSBEHANDLER_AZ", "az54321")
    loader, _ = _forfatter_loader([])
    with pytest.raises(ValideringsFejl):
        loader._forfatter({"forfatter_go": "az12345", "kilde_noegle": "1"})


def test_kle_og_facet_kommer_fra_skabelonen_og_slaas_ikke_op():
    """26-11-2026: skabelonen sætter KLE og facet. En GO-KLE uden mapping
    må ikke længere afvise sagen."""
    slaaet_op: list[str] = []

    class Kodelister:
        def slaa_op(self, liste, vaerdi, paakraevet=False):
            slaaet_op.append(liste)
            return "1"

    loader = SagLoader(SimpleNamespace(endpoints=Endpoints(), kodelister=Kodelister()))
    loader._ansvarlig = lambda raekke: None
    sag = loader.byg({"kilde_noegle": "S", "kle_go": "17.03.83 Ukendt", "facet_go": "G01"})
    assert sag.kle is None and sag.facet is None
    assert "kle" not in slaaet_op and "facet" not in slaaet_op


@pytest.mark.parametrize("raekke, titel", [
    ({"organisering_go": "PPR", "sagstype_go": "Psykolog", "titel": "Barn Barnesen 0101"},
     "Psykolog"),
    ({"organisering_go": "ppr ", "sagstype_go": " ABA ", "titel": "Noget andet"}, "ABA"),
    ({"organisering_go": "EMN", "sagstype_go": "Emnesag", "titel": "Budget 2024"},
     "Budget 2024"),
    ({"organisering_go": "BOR", "sagstype_go": "X", "titel": "Borgersag"}, "Borgersag"),
    ({"organisering_go": "ELM", "sagstype_go": "X", "titel": " "}, "Uden titel"),
])
def test_ppr_sager_faar_sagsprofilen_som_titel(raekke, titel):
    from konvertering.loaders.sag import sagstitel

    assert sagstitel(raekke) == titel
