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
# Testmiljøet 16-11-2026: kladden svarede med {"ID": 183, "KladdeID": 199,
# "SagID": 183}. ID er sagens id. Den første udgave tog ID før KladdeID og
# registrerede sagen (183) som kladdens id i konv.id_map.

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


def test_kladdens_id_er_kladdeid_ikke_sagens_id(tmp_path):
    from konvertering.models import Dokument

    loader, sendt = _dokumentloader(tmp_path, {"ID": 183, "KladdeID": 199, "SagID": 183})
    d = Dokument(kilde_noegle="K", sag_noegle="S", titel="Udkast", er_kladde=True,
                 fil_sti="S/1.docx", fil_navn="Udkast.docx")
    assert loader.send(d, loader.payload(d))[0] == "199"
    assert sendt[0]["sti"] == "/api/kladde"


def test_kladde_uden_kladdeid_afvises_frem_for_at_gaette(tmp_path):
    from konvertering.models import Dokument

    loader, _ = _dokumentloader(tmp_path, {"ID": 183, "SagID": 183})
    d = Dokument(kilde_noegle="K", sag_noegle="S", titel="Udkast", er_kladde=True,
                 fil_sti="S/1.docx", fil_navn="Udkast.docx")
    with pytest.raises(PermanentFejl):
        loader.send(d, loader.payload(d))


def test_journaliseret_dokument_bruger_id(tmp_path):
    from konvertering.models import Dokument

    loader, sendt = _dokumentloader(tmp_path, {"Id": 185, "DokumentID": 185})
    d = Dokument(kilde_noegle="D", sag_noegle="S", titel="Indstilling",
                 fil_sti="S/1.docx", fil_navn="Indstilling.docx")
    assert loader.send(d, loader.payload(d))[0] == "185"
    assert sendt[0]["sti"] == "/api/dokument/journaliser"
