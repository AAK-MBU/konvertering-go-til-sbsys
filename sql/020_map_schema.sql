/*
  020_map_schema.sql — mapping-tabeller (kodelister).

  Regel: ingen mapping hardkodes i Python. Alt der oversætter en GO-værdi
  til en SBSYS-værdi ligger her, ejes af forretningen, versioneres som CSV
  i mapping/ og indlæses med `konv mapping load`.

  Det gør at en fagperson kan rette en KLE-mapping uden at nogen rører kode,
  og at vi altid kan svare på "hvorfor blev denne sag lagt her".
*/

IF SCHEMA_ID('map') IS NULL EXEC('CREATE SCHEMA map');
GO

/* Generisk kodeliste-mapping. Én tabel, mange kodelister. */
IF OBJECT_ID('map.kodeliste') IS NULL
CREATE TABLE map.kodeliste (
    kodeliste_id    INT IDENTITY(1,1) PRIMARY KEY,
    liste           VARCHAR(50)   NOT NULL,   -- kle | sagsstatus | sagstype | dokumentkategori | bruger | afdeling
    go_vaerdi       NVARCHAR(200) NOT NULL,
    go_tekst        NVARCHAR(400) NULL,       -- til menneskelig genkendelse
    sbsys_vaerdi    NVARCHAR(200) NULL,       -- NULL = ikke afklaret endnu
    sbsys_tekst     NVARCHAR(400) NULL,
    handling        VARCHAR(20)   NOT NULL DEFAULT 'map',
                    -- map | ignorer | bloker | standard
    kommentar       NVARCHAR(1000) NULL,
    godkendt_af     NVARCHAR(100) NULL,
    godkendt_dato   DATE          NULL,
    gyldig          BIT           NOT NULL DEFAULT 1,
    CONSTRAINT uq_kodeliste UNIQUE (liste, go_vaerdi)
);
GO

/*
  Hvilke værdier mangler mapping? Denne view er det daglige arbejdsredskab
  for fagpersonerne — den skal være tom før produktionskørsel.
*/
CREATE OR ALTER VIEW map.v_mangler AS
SELECT liste, go_vaerdi, go_tekst, kommentar
FROM   map.kodeliste
WHERE  gyldig = 1
  AND  handling = 'map'
  AND  (sbsys_vaerdi IS NULL OR LTRIM(RTRIM(sbsys_vaerdi)) = '');
GO

/*
  Opslagsfunktion. Returnerer NULL hvis værdien ikke er mappet — kaldende
  kode skal selv beslutte om det er blokerende. Vi gætter aldrig.
*/
CREATE OR ALTER FUNCTION map.fn_slaa_op (@liste VARCHAR(50), @go_vaerdi NVARCHAR(200))
RETURNS NVARCHAR(200)
AS
BEGIN
    DECLARE @r NVARCHAR(200);
    SELECT @r = sbsys_vaerdi
    FROM   map.kodeliste
    WHERE  liste = @liste AND go_vaerdi = @go_vaerdi AND gyldig = 1 AND handling = 'map';
    RETURN @r;
END;
GO

/*
  Beregnede værdilistemuligheder (konverterings-PDF'en trin 5, note 3).

  Én række pr. GO-feltnavn og GO-feltværdi med den VaerdiMulighedExternalKey
  værdien skal have i SBSYS. Fyldes af `konv vaerdier mapning ... --gem-db`,
  som ERSTATTER hele indholdet i én transaktion — tabellen er altid
  resultatet af den seneste kørsel, aldrig en blanding.

  status: klar             findes i SBSYS
          mangler_i_sbsys  LIS skal oprette den (også alle konvertering_)
          uafklaret        kræver en afgørelse i gennemsynet

  go_feltvaerdi er binært unik: GO's stavemåder kan afvige alene på store og
  små bogstaver, og hver af dem er sin egen række.
*/
IF OBJECT_ID('map.vaerdilistemulighed') IS NULL
CREATE TABLE map.vaerdilistemulighed (
    vaerdilistemulighed_id       INT IDENTITY(1,1) PRIMARY KEY,
    liste                        VARCHAR(50)    NOT NULL,   -- bevillingssted | institution | kommune | klassetrin
    go_feltnavn                  NVARCHAR(100)  NOT NULL,
    go_feltvaerdi                NVARCHAR(400)  COLLATE Latin1_General_100_BIN2 NOT NULL,
    vaerdiliste_external_key     VARCHAR(100)   NOT NULL,
    vaerdimulighed_external_key  VARCHAR(100)   NOT NULL,
    vaerdimulighed_navn          NVARCHAR(400)  NOT NULL,
    noegletype                   VARCHAR(20)    NOT NULL,   -- lisid | instnr | kommunekode | konvertering
    status                       VARCHAR(20)    NOT NULL,
    kommentar                    NVARCHAR(1000) NULL,
    kilde                        NVARCHAR(400)  NULL,       -- gennemsynsfilen
    beregnet                     DATETIME2(0)   NOT NULL DEFAULT SYSDATETIME(),
    CONSTRAINT uq_vaerdilistemulighed UNIQUE (vaerdiliste_external_key, go_feltnavn, go_feltvaerdi),
    CONSTRAINT ck_vaerdilistemulighed_status
        CHECK (status IN ('klar', 'mangler_i_sbsys', 'uafklaret'))
);
GO

/*
  Det LIS skal oprette: én række pr. valgmulighed (nøgle), uanset hvor mange
  GO-stavemåder der peger på den. Uafklarede er med, så de ikke glemmes,
  men skal afgøres før listen afleveres.
*/
CREATE OR ALTER VIEW map.v_vaerdilistemuligheder_til_lis AS
SELECT vaerdiliste_external_key,
       vaerdimulighed_external_key,
       MIN(vaerdimulighed_navn) AS vaerdimulighed_navn,
       MIN(noegletype)          AS noegletype,
       MAX(status)              AS status,      -- 'uafklaret' > 'mangler_i_sbsys'
       COUNT(*)                 AS go_stavemaader
FROM   map.vaerdilistemulighed
WHERE  status <> 'klar'
GROUP BY vaerdiliste_external_key, vaerdimulighed_external_key;
GO
