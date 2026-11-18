"""Simulering med en falsk GOStaging (sql/998_gofake_kilde.sql).

Den rigtige extract køres mod tabeller med GOStaging's form, men falske
data: `konv db koer sql/100_extract_skabelon.sql --kilde SBSYS_KONV.gofake`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from konvertering.cli import GO_KILDE, skift_kilde
from konvertering.config import KonfigurationsFejl

SQL = Path(__file__).resolve().parents[1] / "sql"

# Kolonnerne i GOStaging som de stod i SSMS 18-11-2026.
CASES = {
    "ID", "Organisering", "Sagsnavn", "Gruppe", "CPR", "PartNavn", "Hovedgruppe",
    "Institution", "GOSagsID", "PartAdresse", "Sagsstatus", "Sagsprofil",
    "Sagsbehandler", "SupplerendeSagsbehandlere", "Afdeling", "SupplerendeAfdelinger",
    "KLE", "Facet", "StartDato", "Samlesag", "LukketDato", "GenaabningsDato",
    "GenaabningsAarsag", "BarnetsAlder", "Bopaelsskoledistrikt", "Skolenavn",
    "Klassebetegnelse", "PersonligtKlassetrin", "Klasseart", "KlasseartStartDato",
    "Skolekode", "Institutionsnavn", "Institutionsnummer", "DagtilbudNavn",
    "DagtilbudNummer", "Distrikt", "GOSagsDBID",
}
FORANSTALTNINGSDATA = {
    "ID", "SagsId", "Indholdstype", "Visiteret", "Fra", "Til", "Sagsbehandler",
    "ABAGruppe", "Ydelse", "SkoleEllerInstitution", "Betalingssted", "Timer",
    "VisiteretTilTilbud", "Visitationsteam", "Specialundervisningstilbud",
    "TypeAfEnkelteIntegration", "Betalingskommune", "Skoleaar", "Faggruppe", "Indsats",
    "Institutionsnavn", "Aarsag", "HenvisningsAarsag", "Klassetrin", "Klassebetegnelse",
    "Skole", "NavnPaaSkole", "Bopaelsoplysning", "AndenBetalingskommune",
    "EkstraBevilling", "IndstilletAf", "Indstillingsaarsag", "Aktivitet",
    "BetegnelseSpecialklasse", "FritSkolevalg", "NavnPaaSpecialskole",
    "BetegnelseSpecialskole", "Bevillingstype", "Institutionstype", "Bevillingssted",
    "Bevillingsaarsag", "Emneliste",
}


# --- kildeskiftet ---------------------------------------------------------

def test_kilden_skiftes_kun_foran_tabelnavne():
    sql = "SELECT * FROM GOStaging.dbo.[Cases] JOIN GOStaging.dbo.Contacts k ON 1=1"
    ud = skift_kilde(sql, "SBSYS_KONV.gofake")
    assert ud == "SELECT * FROM SBSYS_KONV.gofake.[Cases] JOIN SBSYS_KONV.gofake.Contacts k ON 1=1"


def test_extract_kan_skifte_kilde():
    tekst = (SQL / "100_extract_skabelon.sql").read_text(encoding="utf-8")
    ud = skift_kilde(tekst, "SBSYS_KONV.gofake")
    assert f"{GO_KILDE}." not in ud
    assert "SBSYS_KONV.gofake.[Foranstaltningsdata]" in ud


@pytest.mark.parametrize("ondt", ["x; DROP TABLE y", "a.b.c", "", "gofake]", "1abc"])
def test_kildenavnet_valideres(ondt):
    """Navnet sættes direkte ind i SQL — det må kun være et navn."""
    with pytest.raises(KonfigurationsFejl):
        skift_kilde("SELECT 1 FROM GOStaging.dbo.Cases", ondt)


def test_script_uden_gostaging_afvises():
    with pytest.raises(KonfigurationsFejl):
        skift_kilde("SELECT 1", "gofake")


# --- de falske data -------------------------------------------------------

def _indsatte_kolonner(tabel: str) -> set[str]:
    tekst = (SQL / "998_gofake_kilde.sql").read_text(encoding="utf-8")
    fundne: set[str] = set()
    for liste in re.findall(rf"INSERT INTO gofake\.{tabel}\s*\(([^)]*)\)", tekst):
        fundne |= set(re.findall(r"\[(\w+)\]", liste))
    return fundne


def test_falske_sager_bruger_rigtige_kolonnenavne():
    assert _indsatte_kolonner("Cases") <= CASES


def test_falske_foranstaltninger_bruger_rigtige_kolonnenavne():
    assert _indsatte_kolonner("Foranstaltningsdata") <= FORANSTALTNINGSDATA


def test_alle_extracts_kildetabeller_oprettes():
    """Mangler én, fejler extract midtvejs mod gofake."""
    extract = (SQL / "100_extract_skabelon.sql").read_text(encoding="utf-8")
    kilde = (SQL / "998_gofake_kilde.sql").read_text(encoding="utf-8")
    brugte = set(re.findall(r"GOStaging\.dbo\.\[?(\w+)", extract))
    oprettede = set(re.findall(r"INTO gofake\.(\w+)\s+FROM GOStaging\.dbo\.\1", kilde))
    assert brugte <= oprettede, f"mangler: {sorted(brugte - oprettede)}"
