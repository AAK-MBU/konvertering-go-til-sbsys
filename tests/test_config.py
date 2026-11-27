"""Tests af konfigurationen.

Fokus på de to ting der gør ondt hvis de er forkerte: forbindelsen til en
SQL Server på Linux, og at et kodeord aldrig havner i en logfil.
"""

from __future__ import annotations

import pytest

from konvertering.config import DatabaseIndstillinger, KonfigurationsFejl, Settings


def test_standard_er_sql_autentificering():
    """Serveren er SQL Server på Linux; integreret sikkerhed er ikke normen."""
    db = DatabaseIndstillinger(server="s", database="KONV", brugernavn="u", kodeord="p")
    assert db.trusted_connection is False
    assert "UID=u" in db.odbc_connection_string
    assert "Trusted_Connection" not in db.odbc_connection_string


def test_manglende_bruger_fejler_ved_opstart_ikke_midt_i_en_koersel():
    with pytest.raises(KonfigurationsFejl) as fejl:
        DatabaseIndstillinger(server="s", database="KONV")
    # Beskeden skal sige hvad man gør ved det
    assert "KONV_DB_BRUGER" in str(fejl.value)
    assert "KONV_DB_TRUSTED=1" in str(fejl.value)


def test_trusted_connection_kan_vaelges_bevidst():
    db = DatabaseIndstillinger(server="s", database="KONV", trusted_connection=True)
    assert "Trusted_Connection=yes" in db.odbc_connection_string


def test_kodeord_laekker_ikke_i_repr():
    """repr() ender i exception-traces og logfiler."""
    db = DatabaseIndstillinger(
        server="s", database="KONV", brugernavn="konv_service", kodeord="hemmeligt123"
    )
    assert "hemmeligt123" not in repr(db)
    assert "konv_service" in repr(db)


def test_kodeord_er_stadig_med_i_forbindelsesstrengen():
    db = DatabaseIndstillinger(server="s", database="KONV", brugernavn="u", kodeord="p")
    assert "PWD=p" in db.odbc_connection_string


def test_testserver_accepterer_selvsigneret_certifikat():
    """ODBC Driver 18 krypterer som standard; uden dette kan man ikke
    forbinde til en testserver med selvsigneret certifikat."""
    db = DatabaseIndstillinger(server="s", database="KONV", brugernavn="u", kodeord="p")
    assert "TrustServerCertificate=yes" in db.odbc_connection_string
    assert "Encrypt=yes" in db.odbc_connection_string


def test_certifikatkontrol_kan_slaas_til():
    db = DatabaseIndstillinger(server="s", database="KONV", brugernavn="u", kodeord="p",
                               trust_server_certificate=False)
    assert "TrustServerCertificate=no" in db.odbc_connection_string


def test_standard_databasenavn(monkeypatch):
    """Glemmer nogen KONV_DB_DATABASE, skal den ramme den rigtige base —
    ikke en der ikke findes."""
    for navn, vaerdi in {
        "KONV_DB_SERVER": "s",
        "KONV_DB_BRUGER": "u",
        "KONV_DB_KODEORD": "p",
        "SBSYS_BASE_URL": "https://x",
        "KONV_FIL_ROD": "/tmp",
    }.items():
        monkeypatch.setenv(navn, vaerdi)
    monkeypatch.delenv("KONV_DB_DATABASE", raising=False)

    assert Settings.fra_environment().db.database == "SBSYS_KONV"


# --- SBSYS-autentifikation ------------------------------------------
#
# Uden legitimationsoplysninger svarer SBSYS 401 på hvert eneste kald. Det
# skal opdages inden kørslen går i gang, ikke efter de første tusinde sager.

def _env(monkeypatch, **ekstra):
    basis = {
        "KONV_DB_SERVER": "s",
        "KONV_DB_BRUGER": "u",
        "KONV_DB_KODEORD": "p",
        "SBSYS_BASE_URL": "https://sbsys",
        "KONV_FIL_ROD": "/tmp",
    }
    for navn in ("SBSYS_TOKEN", "SBSYS_TOKEN_URL", "SBSYS_CLIENT_ID",
                 "SBSYS_CLIENT_SECRET", "SBSYS_USERNAME", "SBSYS_PASSWORD"):
        monkeypatch.delenv(navn, raising=False)
    for navn, vaerdi in {**basis, **ekstra}.items():
        monkeypatch.setenv(navn, vaerdi)


def test_bruger_og_kodeord_laeses_fra_env(monkeypatch):
    _env(monkeypatch, SBSYS_USERNAME="svc_konvertering", SBSYS_PASSWORD="hem")
    sbsys = Settings.fra_environment().sbsys
    assert sbsys.brugernavn == "svc_konvertering"
    assert sbsys.kodeord == "hem"


def test_dry_run_kan_koere_uden_sbsys_adgang(monkeypatch):
    """Payloads skal kunne bygges og gennemses før kontoen overhovedet findes."""
    _env(monkeypatch)
    assert Settings.fra_environment().dry_run is True


def test_live_uden_legitimation_afvises(monkeypatch):
    from dataclasses import replace as _replace

    _env(monkeypatch, SBSYS_TOKEN_URL="https://sbsys/token", SBSYS_CLIENT_ID="k")
    live = _replace(Settings.fra_environment(), dry_run=False)

    with pytest.raises(KonfigurationsFejl) as e:
        live.valider()
    assert "SBSYS_USERNAME" in str(e.value)


def test_live_med_bruger_accepteres(monkeypatch):
    from dataclasses import replace as _replace

    _env(monkeypatch, SBSYS_TOKEN_URL="https://sbsys/token", SBSYS_CLIENT_ID="k",
         SBSYS_USERNAME="svc", SBSYS_PASSWORD="hem")
    _replace(Settings.fra_environment(), dry_run=False).valider()


# --- databasens certifikat i prod (20-11-2026) ------------------------------
# Serveren har et selvsigneret certifikat. Blind tillid er tilladt i prod,
# men kun som et bevidst tilvalg — og med en advarsel ved hver kørsel.

def _prod(monkeypatch, **ekstra):
    for navn in ("KONV_DB_TRUST_CERT", "KONV_DB_TRUST_CERT_PROD"):
        monkeypatch.delenv(navn, raising=False)
    _env(monkeypatch, KONV_MILJOE="prod", **ekstra)


def test_prod_med_blind_tillid_afvises_uden_tilvalg(monkeypatch):
    _prod(monkeypatch)
    with pytest.raises(KonfigurationsFejl) as e:
        Settings.fra_environment().valider()
    assert "KONV_DB_TRUST_CERT_PROD=1" in str(e.value)


def test_prod_med_bevidst_blind_tillid_koerer_med_advarsel(monkeypatch, caplog):
    _prod(monkeypatch, KONV_DB_TRUST_CERT_PROD="1")
    s = Settings.fra_environment()
    s.valider()
    assert s.db.trust_server_certificate is True
    assert "verificeres IKKE" in caplog.text


def test_prod_med_verificeret_certifikat_behoever_intet_tilvalg(monkeypatch, caplog):
    _prod(monkeypatch, KONV_DB_TRUST_CERT="0")
    Settings.fra_environment().valider()
    assert "verificeres IKKE" not in caplog.text


def test_tilvalget_aendrer_intet_i_test(monkeypatch):
    _env(monkeypatch, KONV_MILJOE="test")
    monkeypatch.delenv("KONV_DB_TRUST_CERT_PROD", raising=False)
    Settings.fra_environment().valider()
