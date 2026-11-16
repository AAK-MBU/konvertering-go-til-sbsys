/*
  100_extract_skabelon.sql — GOStaging  ->  stg.

  Skrevet 11-11-2026 mod den faktiske struktur i GOStaging (syv tabeller,
  kolonner og typer aflaest med sql/101_go_struktur.sql).

  Kør: uv run konv db koer sql/100_extract_skabelon.sql
  Forudsætning: GOStaging ligger paa samme instans og er READ_ONLY.

  Tre regler for dette lag:
   1. Ingen forretningslogik. Kun feltudtraek og typekonvertering.
   2. kilde_noegle skal vaere stabil mellem to leverancer.
      ALDRIG et loebenummer. Se noeglevalgene nedenfor.
   3. Koer altid fuldt: DELETE + INSERT. Staging er en projektion, ikke
      en journal.

  ---------------------------------------------------------------------
  NOEGLEVALG — den vigtigste beslutning i hele filen

  Cases.ID, Contacts.ID og Foranstaltningsdata.ID er IDENTITY-kolonner:
  stagingdatabasens egne loebenumre. De aendrer sig ved naeste indlaesning
  og maa ALDRIG bruges som kilde_noegle.

    stg.sag       Cases.GOSagsID            nvarchar(36), GO's sagsnummer
                                            (fx PPR-2015-123456-001). Stabil
                                            (bekraeftet af NC 11-11-2026), og
                                            OGSAA mappenavnet paa fileshare'et
                                            (bekraeftet 16-11-2026)
    stg.dokument  Documents.DokumentID      GO's eget dokument-id
    stg.notat     JournalNotes.DokumentID   GO's eget id
    stg.part      CPR eller CVR             Contacts har INGEN stabil egen
                                            noegle — se afsnit 3
    stg.indsats   sammensat                 Foranstaltningsdata har heller
                                            ingen — se afsnit 5

  ---------------------------------------------------------------------
  TRE FUND DER AENDRER ANTAGELSER

  1) JOURNALNOTATER ER FILER OG KONVERTERES SOM DOKUMENTER.
     JournalNotes har ingen tekstkolonne — DokumentID er et FILNAVN, og
     stien dannes af ScpFolder + DokumentID. Besluttet 11-11-2026 (A20):
     de lander i stg.dokument med praefikset 'JN:' paa noeglen og
     kategorien 'Journalnotat'. stg.notat fyldes ikke laengere.

  2) GOWorkflows ER OGSAA DOKUMENTER.
     Samme form som JournalNotes, og det er .txt-filer der skal paa
     sagen som dokumenter (B12, besvaret 11-11-2026). Praefiks 'WF:'.

  3) CHECKSUMMER FINDES ALLEREDE.
     Documents.SHACheckSum, JournalNotes.SHACheckSum og DocumentSHA.
     Vi skal ikke beregne dem selv over 7 GB — og en checksum fra KILDEN
     beviser mere end en vi selv laver. Se B13.
*/

SET NOCOUNT ON;
SET XACT_ABORT ON;

/* ================================================================== */
/* 1) Sager                                                            */
/* ================================================================== */
BEGIN TRANSACTION;

DELETE FROM stg.sag;

INSERT INTO stg.sag (
    kilde_noegle, go_sagsnummer, titel,
    kle_go, facet_go, sagstype_go, status_go,
    ansvarlig_go, afdeling_go, organisering_go,
    oprettet_dato, afsluttet_dato,
    primaer_part_noegle, raa_json
)
SELECT
    c.[GOSagsID],                     -- sagsnummeret, stabilt. IKKE c.ID
    c.[GOSagsID],                     -- GO har intet separat sagsnummer
    LEFT(NULLIF(LTRIM(RTRIM(c.[Sagsnavn])), N''), 500),
    c.[KLE],
    c.[Facet],
    c.[Sagsprofil],                   -- -> sagsskabelon via kodelisten
    c.[Sagsstatus],
    c.[Sagsbehandler],
    c.[Afdeling],
    c.[Organisering],                 -- PPR | ELM | BOR | EMN
    TRY_CONVERT(DATETIME2(3), c.[StartDato]),
    TRY_CONVERT(DATETIME2(3), c.[LukketDato]),
    /* Den primaere part staar paa SAGEN, ikke i Contacts: Cases baerer
       CPR og PartNavn direkte. Noeglen er CPR'et selv — se afsnit 3. */
    NULLIF(LTRIM(RTRIM(c.[CPR])), N''),
    (SELECT c.* FOR JSON PATH, WITHOUT_ARRAY_WRAPPER)
FROM   GOStaging.dbo.[Cases] AS c;

COMMIT;

/* ================================================================== */
/* 2) Boelgetildeling                                                  */
/* ================================================================== */
/*
  B9 besvaret 11-11-2026: Cases.Organisering afgoer OMRAADET.

    PPR = PPR          ELM = Digitale mapper
    BOR = Borgersager  EMN = Emnesager

  Tre af dem er én boelge hver. ELM er seks: skole uge 1-2, dagtilbud
  uge 3-5 og klub/special/SDT uge 6. Omraadet alene kan derfor ikke
  afgoere boelgen for ELM — der skal enheden med.

  Derfor to trin, begge gennem map.kodeliste, saa forretningen kan rette
  dem uden en kodeaendring.
*/
UPDATE s
SET    boelge = map.fn_slaa_op('boelge', s.organisering_go)
FROM   stg.sag AS s;

/* Trin 2: enheden kan overskrive omraadet. ELM's seks boelger adskilles
   paa skole/dagtilbud/klub, og Boerneterapien er en selvstaendig boelge
   uden en egen Organisering-vaerdi — samme problem, derfor én liste.

   Enheden for en ELM-sag staar i Skolenavn, DagtilbudNavn eller
   Institutionsnavn, alt efter hvad sagen handler om. Foerste ikke-tomme
   vinder.

   Kun raekker der FINDES i listen overskriver. En enhed vi ikke kender,
   bliver NULL for ELM og dukker op i stg.v_boelge_mangler. Det er med
   vilje: en skole vi ikke kender, maa ikke stiltiende havne i uge 1. */
UPDATE s
SET    boelge = map.fn_slaa_op('boelge_enhed', k.enhed)
FROM   stg.sag AS s
CROSS  APPLY (
    SELECT enhed = COALESCE(
        NULLIF(LTRIM(RTRIM(JSON_VALUE(s.raa_json, '$.Skolenavn'))), N''),
        NULLIF(LTRIM(RTRIM(JSON_VALUE(s.raa_json, '$.DagtilbudNavn'))), N''),
        NULLIF(LTRIM(RTRIM(JSON_VALUE(s.raa_json, '$.Institutionsnavn'))), N''),
        s.afdeling_go)
) AS k
WHERE  map.fn_slaa_op('boelge_enhed', k.enhed) IS NOT NULL
   OR  s.organisering_go = N'ELM';

/* Fail-fast: en boelgekode der ikke findes i konv.boelge er en stavefejl
   i kodelisten. Uden det her ville den give nul raekker, nul fejl og en
   groen rapport. */
IF EXISTS (
    SELECT 1 FROM stg.sag s
    WHERE s.boelge IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM konv.boelge b WHERE b.boelge = s.boelge)
)
BEGIN
    DECLARE @ukendte NVARCHAR(500) = (
        SELECT STRING_AGG(x.boelge, ', ') FROM (
            SELECT DISTINCT s.boelge FROM stg.sag s
            WHERE s.boelge IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM konv.boelge b WHERE b.boelge = s.boelge)
        ) AS x);
    RAISERROR('Ukendte boelgekoder i mapping: %s', 16, 1, @ukendte);
END

/* ================================================================== */
/* 3) Parter                                                           */
/* ================================================================== */
/*
  Contacts.ID er IDENTITY og dermed ubrugelig som noegle. Men en part
  BEHOEVER heller ikke vores egen noegle: SBSYS slaar parten op paa
  CPR/CVR, saa nummeret ER identiteten. To raekker med samme CPR er den
  samme person, ogsaa paa tvaers af sager.

  Foelgen: parten deduplikeres paa CPR/CVR, og relationen sag<->part
  baerer rollen. En part uden baade CPR og CVR kan ikke slaas op i SBSYS
  og springes over her — den ville alligevel blive afvist senere.
*/
DELETE FROM stg.sag_part;
DELETE FROM stg.part;

INSERT INTO stg.part (kilde_noegle, part_type, cpr, cvr, navn, adresse, raa_json)
SELECT
    k.noegle,
    CASE WHEN k.cpr IS NOT NULL THEN 'person' ELSE 'virksomhed' END,
    k.cpr,
    k.cvr,
    MAX(k.navn),
    MAX(k.adresse),
    NULL
FROM (
    /* Den primaere part: staar paa sagen selv */
    SELECT noegle  = NULLIF(LTRIM(RTRIM(c.[CPR])), N''),
           cpr     = NULLIF(LTRIM(RTRIM(c.[CPR])), N''),
           cvr     = CONVERT(CHAR(8), NULL),
           navn    = c.[PartNavn],
           adresse = c.[PartAdresse]
    FROM   GOStaging.dbo.[Cases] AS c
    WHERE  NULLIF(LTRIM(RTRIM(c.[CPR])), N'') IS NOT NULL

    UNION ALL

    /* Oevrige parter: foraeldre og andre, fra Contacts */
    SELECT noegle  = COALESCE(NULLIF(LTRIM(RTRIM(k.[CPR])), N''),
                              NULLIF(LTRIM(RTRIM(k.[CVR])), N'')),
           cpr     = NULLIF(LTRIM(RTRIM(k.[CPR])), N''),
           cvr     = NULLIF(LTRIM(RTRIM(k.[CVR])), N''),
           navn    = LTRIM(RTRIM(COALESCE(k.[Fornavn], N'') + N' '
                                 + COALESCE(k.[Efternavn], N''))),
           adresse = k.[Adresse]
    FROM   GOStaging.dbo.[Contacts] AS k
    WHERE  COALESCE(NULLIF(LTRIM(RTRIM(k.[CPR])), N''),
                    NULLIF(LTRIM(RTRIM(k.[CVR])), N'')) IS NOT NULL
) AS k
GROUP  BY k.noegle, k.cpr, k.cvr;

/* Relationen. Noeglen er sag + part, saa den er stabil og idempotent. */
INSERT INTO stg.sag_part (kilde_noegle, sag_noegle, part_noegle, rolle_go, primaer)
SELECT DISTINCT
    r.sag_noegle + '|' + r.part_noegle,
    r.sag_noegle,
    r.part_noegle,
    r.rolle,
    r.primaer
FROM (
    SELECT sag_noegle  = c.[GOSagsID],
           part_noegle = NULLIF(LTRIM(RTRIM(c.[CPR])), N''),
           rolle       = N'Primær part',
           primaer     = CONVERT(BIT, 1)
    FROM   GOStaging.dbo.[Cases] AS c
    WHERE  NULLIF(LTRIM(RTRIM(c.[CPR])), N'') IS NOT NULL

    UNION ALL

    SELECT sag_noegle  = k.[SagsId],
           part_noegle = COALESCE(NULLIF(LTRIM(RTRIM(k.[CPR])), N''),
                                  NULLIF(LTRIM(RTRIM(k.[CVR])), N'')),
           rolle       = k.[Rolle],
           primaer     = CONVERT(BIT, 0)
    FROM   GOStaging.dbo.[Contacts] AS k
    WHERE  COALESCE(NULLIF(LTRIM(RTRIM(k.[CPR])), N''),
                    NULLIF(LTRIM(RTRIM(k.[CVR])), N'')) IS NOT NULL
) AS r
WHERE  EXISTS (SELECT 1 FROM stg.part p WHERE p.kilde_noegle = r.part_noegle)
  AND  EXISTS (SELECT 1 FROM stg.sag s WHERE s.kilde_noegle = r.sag_noegle);

/* ================================================================== */
/* 4) Dokumenter                                                       */
/* ================================================================== */
DELETE FROM stg.dokument;

INSERT INTO stg.dokument (
    kilde_noegle, sag_noegle, titel, kategori_go, dokument_dato,
    registreret, forfatter_go, retning, version, er_hoved_dok, er_kladde,
    fil_relativ_sti, fil_navn, fil_stoerrelse, fil_sha256, raa_json
)
SELECT
    CONVERT(VARCHAR(200), d.[DokumentID]),
    d.[SagsId],
    LEFT(NULLIF(LTRIM(RTRIM(d.[DokumentTitel])), N''), 500),
    d.[DokumentKategori],
    TRY_CONVERT(DATETIME2(3), d.[Dato]),
    TRY_CONVERT(DATETIME2(3), d.[Oprettet]),
    COALESCE(d.[DokumentAnsvarlig], d.[OprettetAf]),
    /* A21 besvaret 11-11-2026: GO har ikke et ind-/udgaaende-begreb.
       Forskellen paa et dokument i SBSYS er journaliseret vs. kladde,
       og den baeres af er_kladde nedenfor. */
    'internt',
    1,
    1,
    /* A22 besvaret 11-11-2026: Journaliseret 0 = kladde, 1 = dokument.
       BEGGE former findes paa en sag i SBSYS, saa begge skal konverteres.
       Et dokument der er kladde i GO, skal vaere kladde i SBSYS — ikke
       journaliseret, og ikke udeladt. */
    CASE WHEN d.[Journaliseret] = 1 THEN 0 ELSE 1 END,
    /* Sti paa fileshare'et, relativ til KONV_FIL_ROD (\\<server>\GO_Files):
           <SagsId>/<DokumentID>.<endelse>
       Bekraeftet 16-11-2026 mod testdumpet: mappen hedder sagens GOSagsID
       (fx PPR-2015-123456-001), og filen hedder dokument-id'et med
       endelsen fra GO's oprindelige filnavn. Mappestruktur er tom for alle
       dokumenter og bruges ikke.

       Overloebsmapper (<SagsId>_1, _2 ved over 1000 filer) og endelsens
       store/smaa bogstaver haandteres af FilOpslag, ikke her: hvilken
       mappe filen ligger i, kan kun filsystemet svare paa. Har Filnavn
       ingen endelse, staar dokument-id'et alene, og FilOpslag leder efter
       <DokumentID>.* i stedet. */
    d.[SagsId] + N'/' + CONVERT(NVARCHAR(20), d.[DokumentID])
        + CASE WHEN CHARINDEX(N'.', REVERSE(d.[Filnavn])) > 1
               THEN RIGHT(d.[Filnavn], CHARINDEX(N'.', REVERSE(d.[Filnavn])))
               ELSE N'' END,
    /* GO's oprindelige filnavn — det sagsbehandleren ser i SBSYS. Det er
       IKKE navnet paa disken. */
    d.[Filnavn],
    d.[Filstoerrelse],
    /* Checksum FRA KILDEN. `konv filer scan` skal ikke beregne den igen;
       den skal sammenligne med den. Se B13. */
    NULLIF(LTRIM(RTRIM(d.[SHACheckSum])), N''),
    (SELECT d.* FOR JSON PATH, WITHOUT_ARRAY_WRAPPER)
FROM   GOStaging.dbo.[Documents] AS d
WHERE  EXISTS (SELECT 1 FROM stg.sag s WHERE s.kilde_noegle = d.[SagsId]);

/* Checksummer der staar i DocumentSHA frem for paa dokumentet selv. */
UPDATE d
SET    fil_sha256 = NULLIF(LTRIM(RTRIM(s.[SHACheckSum])), N'')
FROM   stg.dokument AS d
JOIN   GOStaging.dbo.[DocumentSHA] AS s
       ON CONVERT(VARCHAR(200), s.[DocumentId]) = d.kilde_noegle
WHERE  d.fil_sha256 IS NULL;

/* ================================================================== */
/* 5) Journalnotater — konverteres SOM DOKUMENTER                      */
/* ================================================================== */
/*
  A20 besvaret 11-11-2026: journalnotater skal konverteres som
  dokumenter, ikke som noter i SBSYS' journalark.

  Det passer med dataene. JournalNotes har ingen tekstkolonne — den har
  DokumentID (et FILNAVN), DokumentTitel, Dato, SHACheckSum og ScpFolder.
  Et notat i GO ER en fil paa et drev, og stien dannes af ScpFolder +
  DokumentID.

  Derfor lander de her i stg.dokument sammen med de oevrige dokumenter.
  Samme loader, samme journalisering, samme filopslag.

  NOEGLEN FAAR ET PRAEFIKS. Documents.DokumentID er et heltal,
  JournalNotes.DokumentID er et filnavn — de kan i princippet kollidere,
  og en kollision ville lade det ene objekt overskrive det andet.
  'JN:' foran er billigt nu og umuligt at rette senere: noeglen staar i
  konv.id_map, og aendres den, mister vi koblingen til alt hvad der
  allerede er oprettet.

  stg.notat fyldes IKKE laengere. Tabellen og `loaders/notat.py` bliver
  staaende — SBSYS' journalark findes stadig, og skal der senere komme
  rigtige noter fra en anden kilde, er vejen der. Men fra GO kommer der
  ingen.
*/
DELETE FROM stg.notat;

INSERT INTO stg.dokument (
    kilde_noegle, sag_noegle, titel, kategori_go, dokument_dato,
    retning, version, er_hoved_dok, er_kladde,
    fil_relativ_sti, fil_navn, fil_sha256, raa_json
)
SELECT
    'JN:' + CONVERT(VARCHAR(196), n.[DokumentID]),
    n.[SagsId],
    LEFT(COALESCE(NULLIF(LTRIM(RTRIM(n.[DokumentTitel])), N''),
                  n.[DokumentID]), 500),
    /* Egen kategori, saa de kan skelnes i SBSYS og i afstemningen.
       Vaerdien skal mappes i mapping/dokumentkategori.csv som alle
       andre — den foeres med i afsnit 8. */
    N'Journalnotat',
    TRY_CONVERT(DATETIME2(3), n.[Dato]),
    'internt',
    1,
    1,
    0,
    /* <SagsId>/<DokumentID>, som for de oevrige dokumenter. Filnavnet staar
       i id-kolonnen. ScpFolder er '/GO_Files/<SagsId>' (bekraeftet 16/16
       mod testdumpet 16-11-2026) — men med '/GO_Files/' foran ville stien
       blive ...\GO_Files\GO_Files\..., fordi KONV_FIL_ROD allerede er
       GO_Files. Derfor bygges stien af SagsId og ikke af ScpFolder. */
    n.[SagsId] + N'/' + n.[DokumentID],
    n.[DokumentID],
    NULLIF(LTRIM(RTRIM(n.[SHACheckSum])), N''),
    (SELECT n.* FOR JSON PATH, WITHOUT_ARRAY_WRAPPER)
FROM   GOStaging.dbo.[JournalNotes] AS n
WHERE  EXISTS (SELECT 1 FROM stg.sag s WHERE s.kilde_noegle = n.[SagsId]);

/* ================================================================== */
/* 5b) Workflows — ogsaa dokumenter                                    */
/* ================================================================== */
/*
  B12 besvaret 11-11-2026: GOWorkflows er .txt-filer der skal paa sagen
  som dokumenter, praecis som journalnotaterne. Samme form i kilden
  (DokumentID = filnavn, ScpFolder = mappe), samme behandling her.

  Praefikset er 'WF:'. Tre navnerum i samme tabel — heltal fra Documents,
  'JN:' fra journalnotater, 'WF:' herfra — og ingen af dem kan kollidere.
*/
INSERT INTO stg.dokument (
    kilde_noegle, sag_noegle, titel, kategori_go, dokument_dato,
    retning, version, er_hoved_dok, er_kladde,
    fil_relativ_sti, fil_navn, fil_sha256, raa_json
)
SELECT
    'WF:' + CONVERT(VARCHAR(196), w.[DokumentID]),
    w.[SagsId],
    LEFT(COALESCE(NULLIF(LTRIM(RTRIM(w.[DokumentTitel])), N''),
                  w.[DokumentID]), 500),
    N'Workflow',
    TRY_CONVERT(DATETIME2(3), w.[Dato]),
    'internt',
    1,
    1,
    0,
    /* Samme form som journalnotaterne — se dem for hvorfor ikke ScpFolder */
    w.[SagsId] + N'/' + w.[DokumentID],
    w.[DokumentID],
    NULLIF(LTRIM(RTRIM(w.[SHACheckSum])), N''),
    (SELECT w.* FOR JSON PATH, WITHOUT_ARRAY_WRAPPER)
FROM   GOStaging.dbo.[GOWorkflows] AS w
WHERE  EXISTS (SELECT 1 FROM stg.sag s WHERE s.kilde_noegle = w.[SagsId]);

/* ================================================================== */
/* 6) Indsatser                                                        */
/* ================================================================== */
/*
  Foranstaltningsdata.ID er IDENTITY og ubrugelig som noegle. Der er
  ingen anden kandidat i tabellen, saa noeglen SAMMENSAETTES af de felter
  der goer raekken unik: sag + indholdstype + ydelse + periodestart.

  Det er svagere end en GUID. To indsatser paa samme sag med samme ydelse
  og samme startdato ville kollidere — og en kollision her betyder at den
  ene indsats forsvinder lydloest. Derfor taelles dubletter FOER
  indlaesningen, og kørslen stopper hvis der er nogen. Se B14.

  Datoerne Fra og Til er nvarchar(256), ikke datoer, og de staar som
  dd-mm-aaaa (bekraeftet 16-11-2026: 437 af 1777 Fra-vaerdier havde en dag
  over 12 i foerste felt). Formatet SKAL angives (stil 105): uden det
  laeser SQL Server efter serverens sprog, og med us_english/mdy bliver
  05-03-2021 lydloest til 3. maj. Det er en forkert dato, ikke en fejl —
  den ville ende i SBSYS som en indsats med forkert periode.

  TRY_CONVERT giver NULL paa det der stadig ikke kan tolkes, og de raekker
  fanges af ValideringsFejl 'mangler_periode' i loaderen frem for at blive
  gaettet.
*/
DELETE FROM stg.indsats;

;WITH kandidater AS (
    SELECT
        noegle = CONVERT(VARCHAR(200),
                     f.[SagsId] + '|' + COALESCE(f.[Indholdstype], N'')
                               + '|' + COALESCE(f.[Ydelse], N'')
                               + '|' + COALESCE(CONVERT(VARCHAR(10),
                                          TRY_CONVERT(DATE, f.[Fra], 105), 23), N'')),
        f.*
    FROM GOStaging.dbo.[Foranstaltningsdata] AS f
)
INSERT INTO stg.indsats (
    kilde_noegle, sag_noegle, ydelse_go, bevillingstype_go,
    periode_start, periode_slut, enhedstype_go, udslagsgivende_go, raa_json
)
SELECT
    k.noegle,
    k.[SagsId],
    k.[Ydelse],
    k.[Bevillingstype],
    TRY_CONVERT(DATETIME2(3), k.[Fra], 105),
    TRY_CONVERT(DATETIME2(3), k.[Til], 105),
    /* Timer er fritekst ("4 timer ugentligt"). Antal og enhed kan ikke
       udledes forsvarligt, og et gaet her bliver til en bevilling.
       Lades tom indtil E2b er afklaret; vaerdien er bevaret i raa_json. */
    NULL,
    COALESCE(k.[HenvisningsAarsag], k.[Aarsag]),
    (SELECT k.* FOR JSON PATH, WITHOUT_ARRAY_WRAPPER)
FROM   kandidater AS k
WHERE  EXISTS (SELECT 1 FROM stg.sag s WHERE s.kilde_noegle = k.[SagsId])
  AND  k.noegle IN (
        SELECT noegle FROM kandidater GROUP BY noegle HAVING COUNT(*) = 1
  );

/* Kollisioner: raekker der IKKE kom med, fordi den sammensatte noegle
   ramte mere end én. De er ikke tabt — de er udeladt med vilje, og de
   skal ses af et menneske. */
IF EXISTS (
    SELECT 1 FROM GOStaging.dbo.[Foranstaltningsdata] f
    GROUP BY f.[SagsId], f.[Indholdstype], f.[Ydelse], TRY_CONVERT(DATE, f.[Fra], 105)
    HAVING COUNT(*) > 1
)
BEGIN
    DECLARE @dubletter INT = (
        SELECT COUNT(*) FROM (
            SELECT 1 AS x FROM GOStaging.dbo.[Foranstaltningsdata] f
            GROUP BY f.[SagsId], f.[Indholdstype], f.[Ydelse], TRY_CONVERT(DATE, f.[Fra], 105)
            HAVING COUNT(*) > 1
        ) AS d);
    /* Beskeden bygges i en variabel. T-SQL saetter ikke to strenge sammen
       fordi de staar ved siden af hinanden (det gav Msg 102), og RAISERROR
       tager kun en literal eller en variabel — ikke et udtryk med +. */
    DECLARE @kollisionsbesked NVARCHAR(400) =
        N'Foranstaltningsdata: %d noeglekollisioner. Den sammensatte noegle '
        + N'(sag+indholdstype+ydelse+fra) er ikke unik. De kolliderende '
        + N'raekker er IKKE lagt i stg.indsats. Se B14 i docs/04-aabne-spoergsmaal.md.';
    RAISERROR(@kollisionsbesked, 16, 1, @dubletter);
END

/* ================================================================== */
/* 7) Boelgen arves nedad                                              */
/* ================================================================== */
/* Denormaliseret med vilje: det goer boelgefiltret til et indeksopslag
   i stedet for et join paa millioner af raekker. */
UPDATE d SET boelge = s.boelge
FROM   stg.dokument AS d JOIN stg.sag AS s ON s.kilde_noegle = d.sag_noegle;

UPDATE n SET boelge = s.boelge
FROM   stg.notat AS n JOIN stg.sag AS s ON s.kilde_noegle = n.sag_noegle;

UPDATE i SET boelge = s.boelge
FROM   stg.indsats AS i JOIN stg.sag AS s ON s.kilde_noegle = i.sag_noegle;

/* ================================================================== */
/* 8) Kodelisterne faar de vaerdier der faktisk optraeder              */
/* ================================================================== */
/* Nye vaerdier lander med sbsys_vaerdi = NULL og dukker straks op i
   map.v_mangler. Ingen ny vaerdi kan smutte ubemaerket igennem. */
MERGE map.kodeliste AS t
USING (
    SELECT DISTINCT 'kle' AS liste, kle_go AS go_vaerdi FROM stg.sag WHERE kle_go IS NOT NULL
    UNION SELECT DISTINCT 'facet',           facet_go      FROM stg.sag      WHERE facet_go IS NOT NULL
    UNION SELECT DISTINCT 'sagsskabelon',    sagstype_go   FROM stg.sag      WHERE sagstype_go IS NOT NULL
    UNION SELECT DISTINCT 'sagsstatus',      status_go     FROM stg.sag      WHERE status_go IS NOT NULL
    UNION SELECT DISTINCT 'bruger',          ansvarlig_go  FROM stg.sag      WHERE ansvarlig_go IS NOT NULL
    UNION SELECT DISTINCT 'afdeling',        afdeling_go   FROM stg.sag      WHERE afdeling_go IS NOT NULL
    UNION SELECT DISTINCT 'dokumentkategori', kategori_go  FROM stg.dokument WHERE kategori_go IS NOT NULL
    UNION SELECT DISTINCT 'paragraf',        bevillingstype_go FROM stg.indsats WHERE bevillingstype_go IS NOT NULL
    UNION SELECT DISTINCT 'foranstaltning',  ydelse_go     FROM stg.indsats  WHERE ydelse_go IS NOT NULL
    UNION SELECT DISTINCT 'tilbud',          ydelse_go     FROM stg.indsats  WHERE ydelse_go IS NOT NULL
    UNION SELECT DISTINCT 'udslagsgivende_forhold', udslagsgivende_go FROM stg.indsats WHERE udslagsgivende_go IS NOT NULL
    /* Boelgetildelingen: omraadet, og enheden for ELM's seks */
    UNION SELECT DISTINCT 'boelge',          organisering_go FROM stg.sag    WHERE organisering_go IS NOT NULL
    UNION SELECT DISTINCT 'boelge_enhed',
        COALESCE(NULLIF(LTRIM(RTRIM(JSON_VALUE(raa_json, '$.Skolenavn'))), N''),
                 NULLIF(LTRIM(RTRIM(JSON_VALUE(raa_json, '$.DagtilbudNavn'))), N''),
                 NULLIF(LTRIM(RTRIM(JSON_VALUE(raa_json, '$.Institutionsnavn'))), N''),
                 afdeling_go)
        FROM stg.sag WHERE organisering_go = N'ELM'
) AS s
ON  t.liste = s.liste AND t.go_vaerdi = s.go_vaerdi
WHEN NOT MATCHED BY TARGET AND s.go_vaerdi IS NOT NULL THEN
    INSERT (liste, go_vaerdi, handling) VALUES (s.liste, s.go_vaerdi, 'map');

/* ================================================================== */
/* 9) Hvad kom der ind?                                                */
/* ================================================================== */
SELECT 'Indlaest' AS afsnit, 'sag' AS objekt, COUNT(*) AS antal FROM stg.sag
UNION ALL SELECT 'Indlaest', 'part',     COUNT(*) FROM stg.part
UNION ALL SELECT 'Indlaest', 'sag_part', COUNT(*) FROM stg.sag_part
UNION ALL SELECT 'Indlaest', 'dokument', COUNT(*) FROM stg.dokument
UNION ALL SELECT 'Indlaest', 'heraf journalnotat',
       COUNT(*) FROM stg.dokument WHERE kilde_noegle LIKE 'JN:%'
UNION ALL SELECT 'Indlaest', 'heraf workflow',
       COUNT(*) FROM stg.dokument WHERE kilde_noegle LIKE 'WF:%'
UNION ALL SELECT 'Indlaest', 'heraf kladde',
       COUNT(*) FROM stg.dokument WHERE er_kladde = 1
UNION ALL SELECT 'Indlaest', 'notat (forventet 0)', COUNT(*) FROM stg.notat
UNION ALL SELECT 'Indlaest', 'indsats',  COUNT(*) FROM stg.indsats;

/* Sager uden boelge: de koerer aldrig med. Skal vaere tom. */
SELECT 'Uden boelge' AS afsnit, organisering_go, afdeling_go, COUNT(*) AS antal
FROM   stg.sag
WHERE  boelge IS NULL
GROUP  BY organisering_go, afdeling_go
ORDER  BY COUNT(*) DESC;

/* Rækker der blev valgt fra, fordi sagen ikke fandtes. */
SELECT 'Forældreloese' AS afsnit, 'dokument' AS kilde, COUNT(*) AS antal
FROM   GOStaging.dbo.[Documents] d
WHERE  NOT EXISTS (SELECT 1 FROM stg.sag s WHERE s.kilde_noegle = d.[SagsId])
UNION ALL
SELECT 'Forældreloese', 'journalnotat', COUNT(*)
FROM   GOStaging.dbo.[JournalNotes] n
WHERE  NOT EXISTS (SELECT 1 FROM stg.sag s WHERE s.kilde_noegle = n.[SagsId])
UNION ALL
SELECT 'Forældreloese', 'workflow', COUNT(*)
FROM   GOStaging.dbo.[GOWorkflows] w
WHERE  NOT EXISTS (SELECT 1 FROM stg.sag s WHERE s.kilde_noegle = w.[SagsId]);

/* Journalnotater uden en sti: de kan ikke findes paa fileshare. */
SELECT 'Journalnotat uden sti' AS afsnit, COUNT(*) AS antal
FROM   stg.dokument
WHERE  kilde_noegle LIKE 'JN:%'
  AND (fil_relativ_sti IS NULL OR fil_navn IS NULL);
