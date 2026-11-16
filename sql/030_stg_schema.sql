/*
  030_stg_schema.sql — staging: den kanoniske model i databasen.

  stg.* er den eneste kontrakt mellem "hvad GO indeholder" og "hvad vi
  indlæser i SBSYS". Ændrer GO-leverandøren sin bacpac-struktur, rettes
  KUN extract-laget (sql/100_extract_*.sql) — stg og Python er uberørt.

  Alle tabeller har kilde_noegle som naturlig nøgle. Den skal være stabil
  på tværs af to forskellige bacpac-leverancer, ellers mister vi idempotens.
*/

IF SCHEMA_ID('stg') IS NULL EXEC('CREATE SCHEMA stg');
GO

IF OBJECT_ID('stg.sag') IS NULL
CREATE TABLE stg.sag (
    kilde_noegle        VARCHAR(200)  NOT NULL PRIMARY KEY,
    /* Hvilken bølge sagen hører til. Sættes i extract-laget via
       map.kodeliste liste='boelge' — se sql/100_extract_skabelon.sql.
       NULL = ikke tildelt en bølge; de fanges af stg.v_boelge_mangler. */
    boelge              VARCHAR(40)   NULL,
    go_sagsnummer       NVARCHAR(100) NULL,
    titel               NVARCHAR(500) NOT NULL,
    beskrivelse         NVARCHAR(MAX) NULL,
    kle_go              NVARCHAR(50)  NULL,
    facet_go            NVARCHAR(50)  NULL,
    sagstype_go         NVARCHAR(100) NULL,
    status_go           NVARCHAR(100) NULL,
    ansvarlig_go        NVARCHAR(200) NULL,   -- login/UPN som det står i GO
    afdeling_go         NVARCHAR(200) NULL,
    /* GOStaging.dbo.Cases.Organisering: PPR, ELM (digitale mapper),
       BOR (borgersager), EMN (emnesager). Det felt der afgoer OMRAADET,
       og dermed udgangspunktet for boelgetildelingen (B9). ELM daekker
       seks boelger, saa omraadet alene er ikke nok — se 100_extract. */
    organisering_go     NVARCHAR(50)  NULL,
    oprettet_dato       DATETIME2(3)  NULL,
    afsluttet_dato      DATETIME2(3)  NULL,
    sidst_aendret       DATETIME2(3)  NULL,
    fortrolighed_go     NVARCHAR(50)  NULL,
    bevaring_go         NVARCHAR(50)  NULL,
    primaer_part_noegle VARCHAR(200)  NULL,
    raa_json            NVARCHAR(MAX) NULL,   -- resten af GO-felterne, til sporbarhed
    indlaest            DATETIME2(3)  NOT NULL DEFAULT SYSUTCDATETIME()
);
GO

/* Kolonner tilfoejet efter at tabellerne blev oprettet foerste gang.
   CREATE TABLE ovenfor koerer kun naar tabellen ikke findes, saa en
   eksisterende database ville ellers aldrig faa dem. */
IF COL_LENGTH('stg.sag', 'organisering_go') IS NULL
    ALTER TABLE stg.sag ADD organisering_go NVARCHAR(50) NULL;
GO

/* stg.dokument.er_kladde tilfoejes EFTER CREATE TABLE stg.dokument laengere
   nede. Stod den her, fejlede `konv db init` paa en tom database: COL_LENGTH
   er NULL naar tabellen ikke findes, og saa forsoegte den at aendre en
   tabel der endnu ikke var oprettet. */


IF OBJECT_ID('stg.part') IS NULL
CREATE TABLE stg.part (
    kilde_noegle    VARCHAR(200)  NOT NULL PRIMARY KEY,
    part_type       VARCHAR(20)   NOT NULL,   -- person | virksomhed | fri
    cpr             CHAR(10)      NULL,       -- pseudonymiseres i ikke-prod
    cvr             CHAR(8)       NULL,
    navn            NVARCHAR(300) NULL,
    adresse         NVARCHAR(400) NULL,
    postnr          NVARCHAR(10)  NULL,
    bynavn          NVARCHAR(100) NULL,   -- 'by' alene er et reserveret T-SQL-ord
    email           NVARCHAR(200) NULL,
    telefon         NVARCHAR(50)  NULL,
    raa_json        NVARCHAR(MAX) NULL,
    indlaest        DATETIME2(3)  NOT NULL DEFAULT SYSUTCDATETIME()
);
GO

IF OBJECT_ID('stg.sag_part') IS NULL
CREATE TABLE stg.sag_part (
    kilde_noegle    VARCHAR(200) NOT NULL PRIMARY KEY,
    sag_noegle      VARCHAR(200) NOT NULL,
    part_noegle     VARCHAR(200) NOT NULL,
    rolle_go        NVARCHAR(100) NULL,
    primaer         BIT          NOT NULL DEFAULT 0
);
GO

IF OBJECT_ID('stg.dokument') IS NULL
CREATE TABLE stg.dokument (
    kilde_noegle    VARCHAR(200)  NOT NULL PRIMARY KEY,
    sag_noegle      VARCHAR(200)  NOT NULL,
    boelge          VARCHAR(40)   NULL,   -- arves fra sagen, denormaliseret for filtrering
    titel           NVARCHAR(500) NOT NULL,
    kategori_go     NVARCHAR(100) NULL,
    dokument_dato   DATETIME2(3)  NULL,
    registreret     DATETIME2(3)  NULL,
    forfatter_go    NVARCHAR(200) NULL,
    retning         VARCHAR(20)   NULL,       -- indgaaende | udgaaende | internt
    version         INT           NULL,
    er_hoved_dok    BIT           NOT NULL DEFAULT 1,
    /* Documents.Journaliseret: 1 = journaliseret dokument, 0 = kladde.
       Begge former findes paa en sag i SBSYS, saa begge skal med. */
    er_kladde       BIT           NOT NULL DEFAULT 0,
    /* Filreference — filerne ligger på fileshare, ikke i bacpac'en */
    fil_relativ_sti NVARCHAR(1000) NULL,
    fil_navn        NVARCHAR(400) NULL,
    fil_stoerrelse  BIGINT        NULL,
    fil_sha256      CHAR(64)      NULL,       -- udfyldes af `konv filer scan`
    fil_status      VARCHAR(20)   NULL,       -- ok | mangler | tvetydig | tom | for_stor
    mime            NVARCHAR(150) NULL,
    raa_json        NVARCHAR(MAX) NULL,
    indlaest        DATETIME2(3)  NOT NULL DEFAULT SYSUTCDATETIME()
);
GO

/* Til databaser hvor stg.dokument blev oprettet foer er_kladde fandtes.
   Staar EFTER CREATE TABLE, saa den aldrig rammer en tabel der ikke findes. */
IF COL_LENGTH('stg.dokument', 'er_kladde') IS NULL
    ALTER TABLE stg.dokument ADD er_kladde BIT NOT NULL DEFAULT 0;
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'ix_stg_dokument_sag' AND object_id = OBJECT_ID('stg.dokument'))
CREATE INDEX ix_stg_dokument_sag ON stg.dokument (sag_noegle);
IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'ix_stg_dokument_filstatus' AND object_id = OBJECT_ID('stg.dokument'))
CREATE INDEX ix_stg_dokument_filstatus ON stg.dokument (fil_status);
IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'ix_stg_dokument_boelge' AND object_id = OBJECT_ID('stg.dokument'))
CREATE INDEX ix_stg_dokument_boelge ON stg.dokument (boelge);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'ix_stg_sag_boelge' AND object_id = OBJECT_ID('stg.sag'))
CREATE INDEX ix_stg_sag_boelge ON stg.sag (boelge);
GO

IF OBJECT_ID('stg.notat') IS NULL
CREATE TABLE stg.notat (
    kilde_noegle    VARCHAR(200)  NOT NULL PRIMARY KEY,
    sag_noegle      VARCHAR(200)  NOT NULL,
    boelge          VARCHAR(40)   NULL,
    titel           NVARCHAR(500) NULL,
    tekst           NVARCHAR(MAX) NULL,
    forfatter_go    NVARCHAR(200) NULL,
    dato            DATETIME2(3)  NULL,
    notat_type_go   NVARCHAR(100) NULL,
    raa_json        NVARCHAR(MAX) NULL,
    indlaest        DATETIME2(3)  NOT NULL DEFAULT SYSUTCDATETIME()
);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'ix_stg_notat_sag' AND object_id = OBJECT_ID('stg.notat'))
CREATE INDEX ix_stg_notat_sag ON stg.notat (sag_noegle);
GO

/*
  Arbejdsliste pr. objekttype: hvad mangler at blive indlæst?
  Loaderen læser herfra — aldrig direkte fra GOStaging.
  LEFT JOIN på id_map gør genkørsel gratis: allerede indlæste falder fra.
*/
CREATE OR ALTER VIEW stg.v_sager_til_indlaesning AS
SELECT s.*
FROM   stg.sag s
LEFT   JOIN konv.id_map m
       ON  m.objekt_type = 'sag'
       AND m.kilde_system = 'GO'
       AND m.kilde_noegle = s.kilde_noegle
WHERE  m.id_map_id IS NULL
   OR  m.status IN ('pending', 'failed');
GO

CREATE OR ALTER VIEW stg.v_dokumenter_til_indlaesning AS
SELECT d.*, sm.sbsys_id AS sbsys_sag_id
FROM   stg.dokument d
JOIN   konv.id_map sm                       -- sagen SKAL være indlæst først
       ON  sm.objekt_type = 'sag'
       AND sm.kilde_noegle = d.sag_noegle
       AND sm.status = 'loaded'
LEFT   JOIN konv.id_map dm
       ON  dm.objekt_type = 'dokument'
       AND dm.kilde_noegle = d.kilde_noegle
WHERE  (dm.id_map_id IS NULL OR dm.status IN ('pending', 'failed'))
  AND  ISNULL(d.fil_status, 'ok') = 'ok';
GO

/*
  Sager der ikke er tildelt en bølge. Skal være tom før en bølgekørsel —
  ellers findes der sager som ingen kørsel nogensinde samler op, og de
  opdages først når nogen leder efter dem i SBSYS efter GO LIVE.
*/
IF OBJECT_ID('stg.indsats') IS NULL
CREATE TABLE stg.indsats (
    kilde_noegle        VARCHAR(200)  NOT NULL PRIMARY KEY,
    sag_noegle          VARCHAR(200)  NOT NULL,
    boelge              VARCHAR(40)   NULL,
    /* GO's Ydelse og Bevillingstype — oversaettes til SBSYS' tripel
       (paragraf, foranstaltning, tilbud) via kodelisterne. */
    ydelse_go           NVARCHAR(200) NULL,
    bevillingstype_go   NVARCHAR(200) NULL,
    periode_start       DATETIME2(3)  NULL,
    periode_slut        DATETIME2(3)  NULL,
    antal_enheder       INT           NULL,
    enhedstype_go       NVARCHAR(50)  NULL,   -- Time | Doegn | Uge | Maaned | Aar | Stk
    udslagsgivende_go   NVARCHAR(200) NULL,
    /* Resten af de ~45 kolonner ligger her, indtil E2b er afklaret. Intet
       tabes: raa_json er fuldt spor tilbage til GO. */
    raa_json            NVARCHAR(MAX) NULL,
    indlaest            DATETIME2(3)  NOT NULL DEFAULT SYSUTCDATETIME()
);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'ix_stg_indsats_sag' AND object_id = OBJECT_ID('stg.indsats'))
CREATE INDEX ix_stg_indsats_sag ON stg.indsats (sag_noegle);
GO

CREATE OR ALTER VIEW stg.v_indsatser_til_indlaesning AS
SELECT i.*, sm.sbsys_id AS sbsys_sag_id
FROM   stg.indsats i
JOIN   konv.id_map sm                       -- sagen SKAL vaere indlaest foerst
       ON  sm.objekt_type  = 'sag'
       AND sm.kilde_noegle = i.sag_noegle
       AND sm.status       = 'loaded'
LEFT   JOIN konv.id_map im
       ON  im.objekt_type  = 'indsats'
       AND im.kilde_noegle = i.kilde_noegle
WHERE  im.id_map_id IS NULL
   OR  im.status IN ('pending', 'failed');
GO

CREATE OR ALTER VIEW stg.v_notater_til_indlaesning AS
SELECT n.*, sm.sbsys_id AS sbsys_sag_id
FROM   stg.notat n
JOIN   konv.id_map sm                       -- sagen SKAL vaere indlaest foerst
       ON  sm.objekt_type  = 'sag'
       AND sm.kilde_noegle = n.sag_noegle
       AND sm.status       = 'loaded'
LEFT   JOIN konv.id_map nm
       ON  nm.objekt_type  = 'notat'
       AND nm.kilde_noegle = n.kilde_noegle
WHERE  nm.id_map_id IS NULL
   OR  nm.status IN ('pending', 'failed');
GO

CREATE OR ALTER VIEW stg.v_boelge_mangler AS
SELECT kilde_noegle, go_sagsnummer, titel, afdeling_go, sagstype_go
FROM   stg.sag
WHERE  boelge IS NULL;
GO

/* Hvor langt er hver bølge? Ét opslag til statusmødet. */
CREATE OR ALTER VIEW stg.v_boelge_fremdrift AS
SELECT b.boelge, b.navn, b.raekkefoelge, b.status AS boelge_status,
       s.antal_sager,
       ISNULL(m.indlaeste_sager, 0) AS indlaeste_sager,
       d.antal_dokumenter,
       ISNULL(dm.indlaeste_dokumenter, 0) AS indlaeste_dokumenter
FROM   konv.boelge b
LEFT   JOIN (SELECT boelge, COUNT(*) AS antal_sager FROM stg.sag GROUP BY boelge) s
       ON s.boelge = b.boelge
LEFT   JOIN (SELECT boelge, COUNT(*) AS antal_dokumenter FROM stg.dokument GROUP BY boelge) d
       ON d.boelge = b.boelge
LEFT   JOIN (SELECT boelge, COUNT(*) AS indlaeste_sager FROM konv.id_map
             WHERE objekt_type = 'sag' AND status = 'loaded' GROUP BY boelge) m
       ON m.boelge = b.boelge
LEFT   JOIN (SELECT boelge, COUNT(*) AS indlaeste_dokumenter FROM konv.id_map
             WHERE objekt_type = 'dokument' AND status = 'loaded' GROUP BY boelge) dm
       ON dm.boelge = b.boelge;
GO
