"""Værn om extract-laget (sql/100_extract_skabelon.sql).

Extract kan ikke køres uden GOStaging, men to ting i det er bekræftet mod
testdumpet 16-11-2026, og de må ikke glide tilbage:

* Foranstaltningsdata.Fra/Til er tekst i formatet dd-mm-aaaa, og serveren
  er us_english (mdy). Uden en eksplicit stil bliver 05-03-2021 lydløst
  til 3. maj — en forkert periode på en indsats, ikke en fejl.
* Filstien er <SagsId>/<DokumentID>.<endelse>. Mappestruktur er tom for
  alle dokumenter, og ScpFolder har '/GO_Files/' foran, som ville give
  ...\\GO_Files\\GO_Files\\... med roden sat til sharet.
"""

from __future__ import annotations

import re
from pathlib import Path

EXTRACT = Path(__file__).resolve().parents[1] / "sql" / "100_extract_skabelon.sql"


def _kode() -> str:
    """Extract uden kommentarer — det er koden, ikke forklaringen, der tæller."""
    tekst = EXTRACT.read_text(encoding="utf-8")
    tekst = re.sub(r"/\*.*?\*/", "", tekst, flags=re.DOTALL)
    return re.sub(r"--[^\n]*", "", tekst)


def test_fra_og_til_konverteres_med_dansk_datoformat():
    # (?:[^()]|\(\d+\)) tillader DATETIME2(3) inde i kaldet. Uden det så
    # testen kun tre af de fem konverteringer og overså periodekolonnerne.
    konverteringer = re.findall(
        r"TRY_CONVERT\((?:[^()]|\(\d+\))*?\[(?:Fra|Til)\](?:[^()]|\(\d+\))*?\)", _kode())
    assert len(konverteringer) >= 5, (
        f"fandt kun {len(konverteringer)} konverteringer af Fra/Til — er mønsteret ændret?")
    uden_stil = [k for k in konverteringer if not re.search(r",\s*105\s*\)$", k)]
    assert not uden_stil, (
        f"Fra/Til konverteres uden stil 105 (dd-mm-aaaa): {uden_stil}. Serveren "
        f"er mdy, så dag og måned byttes lydløst om."
    )


def test_dokumentstien_bygges_af_sagsid_og_dokumentid():
    kode = _kode()
    assert "d.[SagsId] + N'/' + CONVERT(NVARCHAR(20), d.[DokumentID])" in kode
    assert "[Mappestruktur]" not in kode, "Mappestruktur er tom i dumpet og må ikke indgå i stien"


def _sammenstoedende_strenge(sql: str) -> list[str]:
    """Linjer der slutter med en streng, efterfulgt af en linje der starter
    med en. Python og C sætter dem sammen; T-SQL giver Msg 102."""
    uden = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group().count("\n"), sql, flags=re.DOTALL)
    linjer = [linje.split("--")[0] for linje in uden.splitlines()]
    return [
        a.strip() for a, b in zip(linjer, linjer[1:], strict=False)
        if re.search(r"'\s*$", a) and re.match(r"\s*N?'", b)
    ]


def test_ingen_sammenstoedende_strenge_i_sql():
    """Extract fejlede med Msg 102 i SSMS 16-11-2026: en RAISERROR-besked
    var delt over tre linjer som tre strenge uden +."""
    fund = {
        p.name: s
        for p in sorted(EXTRACT.parent.glob("*.sql"))
        if (s := _sammenstoedende_strenge(p.read_text(encoding="utf-8")))
    }
    assert not fund, f"strenge uden + imellem: {fund}"


def test_sammenstoed_genkendes():
    """Holder tjekket ovenfor i live."""
    assert _sammenstoedende_strenge("RAISERROR(\n  'a '\n  'b', 16, 1);")
    assert not _sammenstoedende_strenge("SET @x = N'a '\n  + N'b';")


def test_journalark_og_workflows_bruger_ikke_scpfolder():
    kode = _kode()
    assert "n.[SagsId] + N'/' + n.[DokumentID]" in kode
    assert "w.[SagsId] + N'/' + w.[DokumentID]" in kode
    assert "[ScpFolder]" not in kode
