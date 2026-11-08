# Direkte flerfarvet 3MF

Upload et uslicet `.3mf`-projekt (maks. 50 MiB) fra Bambu Studio eller OrcaSlicer med én plade. Projektets geometri og farvetildelinger sendes samlet til sliceren. Vælg en AMS-plads for hver projektfarve; hver plads skal indeholde PLA Basic, PLA Matte eller PLA Translucent. Printer, proces og filamentprofiler erstattes med platformens P2S-profiler. Upload og slicing starter ikke print.

Kontrollér printbanerne efter slicing og vælg AMS-plads for hvert anvendt filament ved godkendelsen. Alle pladser kontrolleres igen på Pi'en. Webapp, slicer og Pi-service skal opdateres sammen for flerfarvet print. Generiske 3MF-filer uden projektets filamentmetadata kan indlæses som én farve; vilkårlige teksturer og farveudvidelser understøttes ikke som printfarver. Projekter med flere plader skal først gemmes som et projekt med én plade.

CLI-brugen følger [Bambu Studios dokumentation](https://github.com/bambulab/BambuStudio/wiki/Command-Line-Usage): `--load-filaments` angiver en betroet profil for hver projektfarve.

# STL- og 3MF-slicing i Dokploy

STL slices med ét PLA-filament; 3MF-projekter kan bruge flere filamenter på én plade. Slicing bruger **Bambu Lab P2S med
0,4 mm dyse og Textured PEI Plate**. STL tolkes i millimeter. Modellen centreres
og arrangeres på pladen; orienteringen mod byggepladen (Z) ændres ikke automatisk,
men slicerens arrangement kan rotere i pladens plan. Automatisk støtte er slået fra.
Laghøjder: 0,12 / 0,20 / 0,28 mm. Infill: 10 / 15 / 20 / 40 / 100 %.

## To applikationer fra samme repo

Den eksisterende PrintPlatform-applikation bruger fortsat **Dockerfile** i repo-roden,
port 3000 og det eksisterende `/data`-volume. Bevar alle eksisterende miljøvariabler
og tilføj en ny tilfældig hemmelighed på mindst 32 tegn:

```dotenv
SLICER_TOKEN=YOUR_NEW_RANDOM_SLICER_TOKEN
```

Tokenet skal være forskelligt fra agentnøglen og webadgangskoden. Redeploy webappen.
Ingen ændringer eller installation på Pi'en er nødvendige for STL-funktionen.

Opret derefter en **ny Application** i samme Dokploy-projekt, fx `printplatform-slicer`:

- GitHub: `caspermartinsen/PrintPlatform`, branch `main`.
- Build type: **Dockerfile**.
- Dockerfile path: **`slicer/Dockerfile`**.
- Docker context path: **`.`** (repo-roden).
- Ingen offentlig port, intet domæne og intet permanent volume.
- Understøttet server: **Linux amd64**, mindst 2 CPU / 4 GiB RAM og ca. 3 GiB fri disk.
- Begræns containeren til én replica, ca. 2 CPU, 4 GiB RAM og højst 128 processer.
- Runtime-variabler:

```dotenv
PLATFORM_URL=https://print.martinsen.family
SLICER_TOKEN=THE_SAME_SLICER_TOKEN_AS_THE_WEBAPP
```

Workerens eneste eksterne forbindelse er udgående HTTPS til webappen. Den har
ingen printerkode eller Pi-agentnøgle, deler ikke datalager og kan ikke starte print.
Brug ikke Docker-socket eller privileged-mode. Adgang til privat internt HTTP er
valgfri via `SLICER_ALLOW_INTERNAL_HTTP=1`; brug kun dette på et privat Docker-netværk.

## Brug

1. Upload en `.stl` (maks. 50 MiB / 1.000.000 trekanter).
2. Drej og zoom 3D-modellen. Kontrollér dimensioner og orientering.
3. Vælg en fyldt AMS-plads med PLA Basic, PLA Matte eller PLA Translucent.
4. Vælg laghøjde og infill, og tryk **Slice model**.
5. Når slicing er klar, vises printbaner med lagvælger, anslået tid og gram.
6. Download eventuelt `.gcode.3mf`, eller vælg **Vælg filament og godkend** i køen.
7. Bekræft det slicede preview. Print kræver fortsat den eksisterende særskilte
   godkendelse, herunder tom byggeplade og `START PRINT` ved fysisk start.

**Upload og slicing godkender eller starter aldrig et print.** Den valgte AMS-plads
bestemmer slicerprofilen. Ved senere godkendelse kontrolleres AMS-indholdet igen.
Modellen kan slices igen, indtil jobbet godkendes. Godkendte/afsendte job kan ikke
om-slices. Et nyt slice-forsøg får et nyt id; et gammelt resultat kan ikke erstatte
et nyere. Ved afbrudt worker kan jobbet stå som *Slicer modellen*; genstart forsøget
med **Genstart slice-forsøg** efter kontrol af workerens log. En ældre kørsel kan
fortsætte indtil timeout, men resultatet ignoreres, og kun det nye forsøg accepteres.

## Teknik og begrænsninger

Sliceren bruger officiel **Bambu Studio 02.08.04.57**, downloadet fra Bambu Labs
GitHub-release og verificeret med SHA-256. Docker bygger fuldt opløste profiler
med inheritance og G-code-includes fra samme release. `machine_full` til P2S
tilføjes fra de medfølgende modeldata, så output har model-id **N7**. Filamentfarve,
laghøjde og infill er de eneste brugerbestemte profiloverskrivninger. Ingen
uploadede scripts, shell-kommandoer, G-code-skabeloner eller eksterne profil-URL'er
accepteres. En slicerproces har 5 minutters timeout og køres som ikke-root.

Output valideres igen på serveren: én plade, ét anvendt filament, P2S/N7,
0,4 mm dyse, Textured PEI og den forventede filamentprofil/farve. Resultatet bliver
kun et køjob, ikke et godkendt print. Ugyldige resultater bliver *Slicing fejlede*.

Preview viser ekstruderende G0/G1-segmenter efter første lagmarkør, ikke
opvarmning, purge eller rejsebevægelser. Nye profiler har arc fitting slået fra.
Uventede buer og segmenter over loftet på 200.000 udelades med en tydelig besked.
Preview er en kontrolvisning, ikke en kollisionssimulering eller garanti for et
vellykket print. Tid og gram er slicerens estimater. WebGL kræves til 3D-visningen.
Den genererede `.gcode.3mf` kan stadig downloades til kontrol i Bambu Studio.

Kun én worker understøttes i første version. Bevar webappens `/data`-volume;
workerens midlertidige filer slettes efter hver opgave. Forældreløse slicing-job
genforsøges manuelt. Gamle STL'er/resultater ryddes manuelt; samlet datakvote er
ca. 2 GiB / 1000 job. Slicing og preview til eksisterende `.gcode.3mf`-uploads
er ikke en del af denne tilføjelse; de følger fortsat det eksisterende forløb.

## Lokal test

```sh
python3 -m unittest cloud.test_slicing cloud.test_platform -v
cd frontend && npm ci && npm run build && node --test app.test.cjs
docker build -t printplatform-web .
docker build --platform linux/amd64 -f slicer/Dockerfile -t printplatform-slicer .
```

Tests bruger midlertidige lokale servere og sender ingen printerkommandoer.
Sliceren kan testes med `docker run --network none ...`, da selve slicing ikke
kræver netværk. Udviklingsverifikation skal udføres i simuleringstilstand.

Bambu Studio-kilde og licens (AGPL-3.0):
https://github.com/bambulab/BambuStudio/tree/v02.08.04.57
CLI-dokumentation: https://github.com/bambulab/BambuStudio/wiki/Command-Line-Usage
3D-preview bruger Three.js (MIT): https://github.com/mrdoob/three.js/tree/r180
