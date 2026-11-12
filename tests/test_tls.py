"""Tests af SBSYS_VERIFY — certifikatkontrollen.

Et internt testmiljø har næsten altid et certifikat underskrevet af
kommunens egen rod-CA, som Pythons CA-lager ikke kender. Den rigtige
løsning er at pege på CA-bundtet, ikke at slå verifikationen fra; derfor
accepterer SBSYS_VERIFY en sti og ikke kun et ja/nej.

To fælder dækkes her: en stavefejl i stien giver nøjagtig samme SSL-fejl
som et manglende certifikat (så den skal fanges ved opstart i stedet), og
en certifikatfejl må ikke genforsøges fem gange pr. objekt.
"""

from __future__ import annotations

import httpx
import pytest

from konvertering.config import (
    KonfigurationsFejl,
    SbsysIndstillinger,
    tls_verifikation,
)
from konvertering.sbsys.errors import er_certifikatfejl, raad_om_certifikat

SELVSIGNERET = (
    "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
    "self-signed certificate in certificate chain (_ssl.c:1028)"
)


@pytest.fixture(autouse=True)
def _ryd(monkeypatch):
    monkeypatch.delenv("SBSYS_VERIFY", raising=False)
    monkeypatch.delenv("SBSYS_VERIFICER_TLS", raising=False)


def test_standard_er_at_verificere():
    assert tls_verifikation() is True


@pytest.mark.parametrize("vaerdi", ["0", "false", "nej", "NO", "off"])
def test_kan_slaas_fra(monkeypatch, vaerdi):
    monkeypatch.setenv("SBSYS_VERIFY", vaerdi)
    assert tls_verifikation() is False


@pytest.mark.parametrize("vaerdi", ["1", "true", "ja", "YES", "on"])
def test_kan_slaas_til(monkeypatch, vaerdi):
    monkeypatch.setenv("SBSYS_VERIFY", vaerdi)
    assert tls_verifikation() is True


def test_sti_til_ca_bundt_accepteres(monkeypatch, tmp_path):
    ca = tmp_path / "aarhus-rod-ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\n...\n")
    monkeypatch.setenv("SBSYS_VERIFY", str(ca))
    assert tls_verifikation() == str(ca)


def test_sti_i_anfoerselstegn_accepteres(monkeypatch, tmp_path):
    """Windows-brugere kopierer stien med gåseøjne om. Det er ikke en fejl."""
    ca = tmp_path / "ca.pem"
    ca.write_text("x")
    monkeypatch.setenv("SBSYS_VERIFY", f'"{ca}"')
    assert tls_verifikation() == str(ca)


def test_stavefejl_i_stien_fanges_ved_opstart(monkeypatch, tmp_path):
    """Ellers giver den præcis samme SSL-fejl som et manglende certifikat,
    og man leder efter fejlen det forkerte sted."""
    monkeypatch.setenv("SBSYS_VERIFY", str(tmp_path / "findes-ikke.pem"))
    with pytest.raises(KonfigurationsFejl) as e:
        tls_verifikation()
    assert "ikke findes" in str(e.value)


def test_tomt_ca_bundt_fanges(monkeypatch, tmp_path):
    ca = tmp_path / "tom.pem"
    ca.write_text("")
    monkeypatch.setenv("SBSYS_VERIFY", str(ca))
    with pytest.raises(KonfigurationsFejl):
        tls_verifikation()


def test_gammelt_navn_virker_stadig(monkeypatch):
    monkeypatch.setenv("SBSYS_VERIFICER_TLS", "0")
    assert tls_verifikation() is False


def test_nyt_navn_vinder_over_gammelt(monkeypatch):
    monkeypatch.setenv("SBSYS_VERIFICER_TLS", "0")
    monkeypatch.setenv("SBSYS_VERIFY", "1")
    assert tls_verifikation() is True


# --- hvad vises og hvad genkendes -----------------------------------

@pytest.mark.parametrize(
    "vaerdi,forventet",
    [
        (True, "systemets CA-lager"),
        (False, "SLÅET FRA"),
        ("C:/certs/ca.pem", "CA-bundt: C:/certs/ca.pem"),
    ],
)
def test_beskrivelsen_siger_hvad_der_sker(vaerdi, forventet):
    i = SbsysIndstillinger(base_url="https://x", verificer_tls=vaerdi)
    assert forventet in i.tls_beskrivelse


def test_selvsigneret_certifikat_genkendes():
    fejl = httpx.ConnectError("x")
    fejl.__cause__ = Exception(SELVSIGNERET)
    assert er_certifikatfejl(fejl)


def test_almindelig_netvaerksfejl_er_ikke_en_certifikatfejl():
    assert not er_certifikatfejl(httpx.ConnectError("connection refused"))


def test_raadet_naevner_loesningen_foer_noedudgangen():
    raad = raad_om_certifikat("https://sbsys-test")
    assert raad.index("SBSYS_VERIFY=C:/") < raad.index("SBSYS_VERIFY=0")
    assert "Afvises i produktion" in raad


def test_certifikatfejl_genforsoeges_ikke():
    """Fem genforsøg ændrer ikke hvem der har underskrevet certifikatet —
    de koster bare ventetid på hvert eneste objekt."""
    from konvertering.sbsys.client import SbsysKlient
    from konvertering.sbsys.errors import CertifikatFejl

    forsoeg = {"n": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        forsoeg["n"] += 1
        fejl = httpx.ConnectError("forbindelsen mislykkedes")
        fejl.__cause__ = Exception(SELVSIGNERET)
        raise fejl

    klient = SbsysKlient(
        SbsysIndstillinger(base_url="https://sbsys-test", statisk_token="t",
                           maks_forsoeg=5, kald_pr_sekund=1000),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(CertifikatFejl) as e:
        klient.kald("GET", "/api/version", operation="hent_version")

    assert forsoeg["n"] == 1, "certifikatfejl skal fejle med det samme"
    assert "SBSYS_VERIFY" in str(e.value)


def test_produktion_afviser_at_springe_verifikationen_over():
    from pathlib import Path

    from konvertering.config import DatabaseIndstillinger, FilIndstillinger, Settings

    s = Settings(
        miljoe="prod",
        db=DatabaseIndstillinger(server="s", database="d", brugernavn="u", kodeord="p",
                                 trust_server_certificate=False),
        sbsys=SbsysIndstillinger(base_url="https://sbsys", statisk_token="t",
                                 verificer_tls=False),
        filer=FilIndstillinger(rod=Path("/tmp")),
    )
    with pytest.raises(KonfigurationsFejl) as e:
        s.valider()
    assert "SBSYS_VERIFY" in str(e.value)


# --- Børn og Unge-værten --------------------------------------------
#
# Stierne indeholder selv /bu_api/. En vært skrevet MED /bu_api giver
# /bu_api/bu_api/... og en 404 der peger på en sti man aldrig har bedt om.

def test_bu_vaert_uden_bu_api(monkeypatch):
    from konvertering.config import _bu_base_url

    monkeypatch.setenv("SBSYS_BU_BASE_URL", "https://bu.example/")
    assert _bu_base_url() == "https://bu.example"


def test_bu_vaert_med_bu_api_rettes(monkeypatch):
    from konvertering.config import _bu_base_url

    monkeypatch.setenv("SBSYS_BU_BASE_URL", "https://bu.example/bu_api/")
    assert _bu_base_url() == "https://bu.example"


def test_bu_vaert_er_valgfri(monkeypatch):
    from konvertering.config import _bu_base_url

    monkeypatch.delenv("SBSYS_BU_BASE_URL", raising=False)
    assert _bu_base_url() is None
