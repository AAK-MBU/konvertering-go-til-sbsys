/*
  010_konv_schema.sql — styringstabeller for konverteringen.
  Database: SBSYS_KONV (adskilt fra GOStaging og fra SBSYS' egen database).
  Dialekt: T-SQL / SQL Server 2019+.

  Princip: ALT hvad der styrer en kørsel ligger her. Kildedatabasen (GOStaging)
  er read-only og røres aldrig. SBSYS ændres kun via API.
*/

IF SCHEMA_ID('konv') IS NULL EXEC('CREATE SCHEMA konv');
GO

/* ------------------------------------------------------------------ */
/* Bølger — konverteringen leveres i ni portioner, hver med sit eget    */
/* datadump fra Netcompany og sin egen undervisning bagefter.           */
/* Alle bølger er sager; bølgen er derfor et filter, ikke en objekttype. */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('konv.boelge') IS NULL
CREATE TABLE konv.boelge (
    boelge          VARCHAR(40)  NOT NULL PRIMARY KEY,
    navn            NVARCHAR(200) NOT NULL,
    raekkefoelge    INT          NOT NULL,
    planlagt_uge    VARCHAR(20)  NULL,       -- som i konverteringsplanen
    status          VARCHAR(20)  NOT NULL DEFAULT 'planlagt',
                    -- planlagt | aktiv | indlaest | afstemt | godkendt
    afstemt_af      NVARCHAR(100) NULL,
    afstemt_dato    DATETIME2(3) NULL,
    note            NVARCHAR(1000) NULL
);
GO

/* Seedet fra konverteringsplanen af 18-09-2026. Ugerne er MBU's
   konverteringsblok, ikke NC's dump eller undervisningen. */
MERGE konv.boelge AS t
USING (VALUES
    ('ppr',                 N'PPR-sag',                                 10, '51-53'),
    ('boerneterapien',      N'Børneterapien',                           20, '51-53'),
    ('skole-1',             N'Skole digitale mapper (uge 1)',           30, '51-53'),
    ('skole-2',             N'Skole digitale mapper (uge 2)',           40, '52-01'),
    ('dagtilbud-3',         N'Dagtilbud digitale mapper (uge 3)',       50, '02'),
    ('dagtilbud-4',         N'Dagtilbud digitale mapper (uge 4)',       60, '03'),
    ('dagtilbud-5',         N'Dagtilbud digitale mapper (uge 5)',       70, '04'),
    ('klub-special-sdt-6',  N'Klubber, special, SDT digitale mapper (uge 6)', 80, '05'),
    /* Borgersager og emnesager deler vindue, men er to omraader i GO
       (Cases.Organisering = BOR og EMN). To boelger, saa foelgesedlen
       fra NC og vores egen afstemning kan laegges op ad hinanden
       omraade for omraade. */
    ('borgersager',         N'Borgersager',                             90, '05-06'),
    ('emnesager',           N'Emnesager',                               95, '05-06')
) AS s (boelge, navn, raekkefoelge, planlagt_uge)
ON t.boelge = s.boelge
WHEN NOT MATCHED THEN
    INSERT (boelge, navn, raekkefoelge, planlagt_uge)
    VALUES (s.boelge, s.navn, s.raekkefoelge, s.planlagt_uge);
GO

/* Den gamle samlede boelge 'borger-emnesager' blev delt 11-11-2026.
   Databaser der allerede har den, skal ikke staa med en boelge ingen
   laengere tildeler. Den fjernes kun hvis INTET peger paa den — ellers
   ville en sletning tage en indlaest sags spor med sig. Er der rækker,
   siges det hoejt i stedet, saa de kan flyttes bevidst. */
IF EXISTS (SELECT 1 FROM konv.boelge WHERE boelge = 'borger-emnesager')
BEGIN
    /* Kun konv.id_map tjekkes: stg.sag oprettes foerst i 030, og en
       reference til en tabel der ikke findes, faar hele scriptet til at
       fejle. Staar der raekker i stg.sag, faar de ingen boelge ved
       naeste extract og dukker op i stg.v_boelge_mangler. */
    IF EXISTS (SELECT 1 FROM konv.id_map WHERE boelge = 'borger-emnesager')
        PRINT 'BEMAERK: boelgen borger-emnesager er delt i borgersager og '
            + 'emnesager, men der findes stadig raekker paa den gamle. '
            + 'Flyt dem bevidst, fx: UPDATE stg.sag SET boelge = '
            + 'CASE organisering_go WHEN ''BOR'' THEN ''borgersager'' '
            + 'ELSE ''emnesager'' END WHERE boelge = ''borger-emnesager'';';
    ELSE
        DELETE FROM konv.boelge WHERE boelge = 'borger-emnesager';
END;
GO

/* ------------------------------------------------------------------ */
/* Kørsler — én række pr. eksekvering (batch), så alt kan spores tilbage */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('konv.run') IS NULL
CREATE TABLE konv.run (
    run_id            BIGINT IDENTITY(1,1) PRIMARY KEY,
    run_key           UNIQUEIDENTIFIER NOT NULL DEFAULT NEWID(),
    fase              VARCHAR(30)  NOT NULL,   -- extract | plan | load | verify
    objekt_type       VARCHAR(30)  NULL,       -- sag | part | dokument | notat
    boelge            VARCHAR(40)  NULL REFERENCES konv.boelge(boelge),
    miljoe            VARCHAR(20)  NOT NULL,   -- test | preprod | prod
    dry_run           BIT          NOT NULL DEFAULT 1,
    kodeversion       VARCHAR(60)  NULL,       -- git sha, så en kørsel kan reproduceres
    as_session_id     VARCHAR(60)  NULL,       -- Automation Server session
    startet           DATETIME2(3) NOT NULL DEFAULT SYSUTCDATETIME(),
    afsluttet         DATETIME2(3) NULL,
    status            VARCHAR(20)  NOT NULL DEFAULT 'running',
    note              NVARCHAR(1000) NULL
);
GO

/* Hvilket SBSYS kørslen skrev til. Uden den kan konv.id_map ikke skelne
   mellem "sagen ligger i SBSYS' testmiljø" og "sagen ligger i attrappen
   på localhost" — og i begge tilfælde springes den over for evigt.
   Tilføjes separat, fordi tabellen ovenfor kun oprettes én gang og
   eksisterende databaser ellers aldrig ville få kolonnen. */
IF COL_LENGTH('konv.run', 'maal') IS NULL
    ALTER TABLE konv.run ADD maal VARCHAR(300) NULL;
GO

/* ------------------------------------------------------------------ */
/* ID-map — hjertet i idempotensen.                                    */
/* En kilde-nøgle må kun oprettes én gang i SBSYS, uanset hvor mange   */
/* gange vi kører. Unik constraint håndhæver det i databasen, ikke kun */
/* i koden.                                                            */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('konv.id_map') IS NULL
CREATE TABLE konv.id_map (
    id_map_id       BIGINT IDENTITY(1,1) PRIMARY KEY,
    objekt_type     VARCHAR(30)   NOT NULL,     -- sag | part | dokument | notat
    kilde_system    VARCHAR(20)   NOT NULL DEFAULT 'GO',
    kilde_noegle    VARCHAR(200)  NOT NULL,     -- stabil, naturlig nøgle fra GO
    /* Bølgen er rapporteringsdimension, ikke en del af identiteten:
       den indgår bevidst IKKE i unik-constrainten. Flyttes en sag mellem
       to bølger undervejs, må den stadig kun oprettes én gang i SBSYS. */
    boelge          VARCHAR(40)   NULL,
    sbsys_id        VARCHAR(100)  NULL,         -- id returneret af SBSYS API
    sbsys_nummer    VARCHAR(100)  NULL,         -- fx sagsnummer, til menneskelig afstemning
    payload_hash    CHAR(64)      NULL,         -- SHA-256 af den kanoniske payload
    status          VARCHAR(20)   NOT NULL DEFAULT 'pending',
                    -- pending | loaded | skipped | failed | manual
    forsoeg         INT           NOT NULL DEFAULT 0,
    sidste_fejl     NVARCHAR(2000) NULL,
    run_id          BIGINT        NULL REFERENCES konv.run(run_id),
    oprettet        DATETIME2(3)  NOT NULL DEFAULT SYSUTCDATETIME(),
    opdateret       DATETIME2(3)  NOT NULL DEFAULT SYSUTCDATETIME(),
    CONSTRAINT uq_id_map UNIQUE (kilde_system, objekt_type, kilde_noegle)
);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'ix_id_map_status' AND object_id = OBJECT_ID('konv.id_map'))
CREATE INDEX ix_id_map_status ON konv.id_map (objekt_type, status) INCLUDE (kilde_noegle, sbsys_id);
IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'ix_id_map_boelge' AND object_id = OBJECT_ID('konv.id_map'))
CREATE INDEX ix_id_map_boelge ON konv.id_map (boelge, objekt_type, status);
GO

/* ------------------------------------------------------------------ */
/* Kaldelog — én række pr. API-kald. Revisionsspor og fejlsøgning.     */
/* OBS: gem ALDRIG personhenførbar payload her. Kun hash + felt-navne. */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('konv.api_kald') IS NULL
CREATE TABLE konv.api_kald (
    api_kald_id     BIGINT IDENTITY(1,1) PRIMARY KEY,
    run_id          BIGINT        NULL REFERENCES konv.run(run_id),
    korrelations_id VARCHAR(60)   NOT NULL,     -- sendes med som header til SBSYS
    objekt_type     VARCHAR(30)   NULL,
    kilde_noegle    VARCHAR(200)  NULL,
    operation       VARCHAR(60)   NOT NULL,     -- fx opret_sag
    metode          VARCHAR(10)   NOT NULL,
    sti             NVARCHAR(400) NOT NULL,
    http_status     INT           NULL,
    varighed_ms     INT           NULL,
    forsoeg_nr      INT           NOT NULL DEFAULT 1,
    fejl_kode       VARCHAR(60)   NULL,
    fejl_tekst      NVARCHAR(2000) NULL,
    tidspunkt       DATETIME2(3)  NOT NULL DEFAULT SYSUTCDATETIME()
);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'ix_api_kald_korrelation' AND object_id = OBJECT_ID('konv.api_kald'))
CREATE INDEX ix_api_kald_korrelation ON konv.api_kald (korrelations_id);
IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'ix_api_kald_fejl' AND object_id = OBJECT_ID('konv.api_kald'))
CREATE INDEX ix_api_kald_fejl ON konv.api_kald (http_status, tidspunkt) WHERE http_status >= 400;
GO

/* ------------------------------------------------------------------ */
/* Afviste rækker — data der ikke kan konverteres, med årsag.          */
/* Dette er leverancen til forretningen, ikke en logfil.               */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('konv.afvist') IS NULL
CREATE TABLE konv.afvist (
    afvist_id       BIGINT IDENTITY(1,1) PRIMARY KEY,
    run_id          BIGINT        NULL REFERENCES konv.run(run_id),
    objekt_type     VARCHAR(30)   NOT NULL,
    boelge          VARCHAR(40)   NULL,
    kilde_noegle    VARCHAR(200)  NOT NULL,
    regel           VARCHAR(80)   NOT NULL,     -- fx manglende_kle, fil_ikke_fundet
    alvorlighed     VARCHAR(10)   NOT NULL,     -- blok | advarsel
    beskrivelse     NVARCHAR(1000) NULL,
    felt            VARCHAR(80)   NULL,
    vaerdi          NVARCHAR(400) NULL,
    tidspunkt       DATETIME2(3)  NOT NULL DEFAULT SYSUTCDATETIME()
);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'ix_afvist_regel' AND object_id = OBJECT_ID('konv.afvist'))
CREATE INDEX ix_afvist_regel ON konv.afvist (objekt_type, regel, alvorlighed);
GO

/* ------------------------------------------------------------------ */
/* Afstemning — talgrundlag pr. kørsel, gemt så det kan sammenlignes   */
/* over tid og bruges som dokumentation ved godkendelse.               */
/* ------------------------------------------------------------------ */
IF OBJECT_ID('konv.afstemning') IS NULL
CREATE TABLE konv.afstemning (
    afstemning_id   BIGINT IDENTITY(1,1) PRIMARY KEY,
    run_id          BIGINT        NULL REFERENCES konv.run(run_id),
    boelge          VARCHAR(40)   NULL,
    maal            VARCHAR(80)   NOT NULL,     -- fx antal_sager_pr_aar
    dimension       NVARCHAR(200) NULL,         -- fx '2019' eller 'Skole/PPR'
    antal_kilde     BIGINT        NULL,
    antal_maal      BIGINT        NULL,
    afvigelse       AS (ISNULL(antal_maal,0) - ISNULL(antal_kilde,0)) PERSISTED,
    tidspunkt       DATETIME2(3)  NOT NULL DEFAULT SYSUTCDATETIME()
);
GO
