<img src="custom_components/battmind/brand/icon.png" alt="BattMind" width="128" align="right">

# BattMind🤵‍♂️🔋⚡ til Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories/)
[![GitHub Release](https://img.shields.io/github/v/release/dezito/BattMind-HACS)](https://github.com/dezito/BattMind-HACS/releases)
[![License](https://img.shields.io/github/license/dezito/BattMind-HACS)](LICENSE)

Planlægning af husbatteri ud fra elpriser, husets forbrug, solcelleprognose og vejret.

BattMind udregner hvornår husbatteriet skal lade fra nettet, hvornår det må aflade, og hvornår
det kan betale sig at sælge strøm. Resultatet udstilles som en handling
(`sensor.battmind_powerwall_action`), som du selv styrer din inverter med.

> [!NOTE]
> Dette er **integrationsudgaven** af BattMind, som installeres via HACS. Den erstatter
> [PyScript-udgaven](https://github.com/dezito/BattMind) og kan importere dens konfiguration og
> databaser. Se [Skift fra PyScript-udgaven](#skift-fra-pyscript-udgaven).

> [!IMPORTANT]
> Første HACS-udgave. Opret gerne et [issue](https://github.com/dezito/BattMind-HACS/issues), hvis noget ikke virker.

---

## Funktioner

- **Ladeplan for flere dage** (1–7 dage) ud fra elpriser, prognoser og forbrugshistorik
- **Planlæggere, som slås til og fra hver for sig:**
  - **Billigste perioder:** lad i de billigste perioder (antal perioder og prisgrænser kan indstilles)
  - **Fyld op i billigste periode:** lad batteriet helt op i den billigste periode
  - **Dyreste perioder:** prioritér at have strøm på batteriet til de dyreste perioder
  - **Nødvendig før max niveau:** lad kun det, der er brug for, før solcellerne fylder batteriet
- **Afladning og salg**
  - Aflad kun, når det giver profit (min. profit pr. kWh og i alt)
  - Prioritér afladningstimer efter energiens kostpris
  - Sælg overskydende kWh til nettet i de dyreste timer (tvangsafladning)
  - Maksimer profit, og udeluk bestemte salgstimer
  - Tvangsafladning af "gamle" kWh, der har ligget for længe i batteriet
- **Solceller (valgfrit):** solcelleoverskud til opladning, optimeret solcelleopladning og
  prognose fra [Solcast PV Forecast](https://github.com/BJReplay/ha-solcast-solar) og/eller egen historik pr. skydække
- **Prisudbydere:** [Energi Data Service](https://github.com/MTrab/energidataservice) og/eller
  [Strømligning](https://github.com/MTrab/stromligning), inkl. salgspris, tariffer, salgsgebyr og refusion
- **Kostpris på batteriets strøm:** hvad energien på batteriet har kostet (net, sol, lade-/afladningstab og slitage)
- **Detaljeret ladehistorik og oversigt** som markdown og et færdigt dashboard i sidepanelet
- **Fail-safe:** mangler priserne, planlægges der ud fra den interne prisdatabase
- **Flere instanser:** hver instans har sit eget prefix (f.eks. ét pr. batteri)
- **Sprog:** dansk, engelsk, tysk, polsk og ukrainsk

### Kører ikke i Home Assistants hovedtråd

Alle beregninger kører i BattMinds egen worker-tråd. Home Assistant bliver altså ikke
blokeret, mens planen regnes ud, sådan som det skete med PyScript-udgaven. Attributterne
`worker_thread` og `calculation_seconds` på `sensor.battmind_current_charging_rule` viser,
hvor beregningen kørte, og hvor lang tid den tog.

---

## Sådan styrer du husbatteriet

BattMind sender **ingen kommandoer** til din inverter. Den udstiller hvad batteriet skal gøre lige nu:

| `sensor.battmind_powerwall_action` | Betydning |
|---|---|
| `grid_charging` | Lad batteriet fra nettet nu |
| `discharge_allowed` | Batteriet må aflade til huset |
| `force_discharge` | Aflad / sælg til nettet nu (de dyreste timer) |
| `force_solar_only_charging` | Lad kun fra solcellerne, ingen afladning |
| `blocked_solar_charge` | Lad ikke fra solcellerne (overskuddet sælges i stedet) |

Det nemmeste er den færdige [blueprint](#blueprint-styr-husbatteriet), der virker med alle
invertere. Vil du hellere skrive automationen selv, er eksemplet nedenfor en skabelon, hvor du
indsætter dine egne handlinger:

```yaml
alias: BattMind - styr husbatteri
triggers:
  - trigger: state
    entity_id: sensor.battmind_powerwall_action
actions:
  - choose:
      - conditions: "{{ trigger.to_state.state == 'grid_charging' }}"
        sequence: []  # f.eks. tving opladning fra nettet
      - conditions: "{{ trigger.to_state.state == 'discharge_allowed' }}"
        sequence: []  # f.eks. maksimer eget forbrug
      - conditions: "{{ trigger.to_state.state == 'force_discharge' }}"
        sequence: []  # f.eks. tving afladning til nettet
      - conditions: "{{ trigger.to_state.state in ['force_solar_only_charging', 'blocked_solar_charge'] }}"
        sequence: []  # f.eks. stop afladning
```

`sensor.battmind_current_charging_rule` viser hvorfor (f.eks. *Lader op til 💸*), og
`sensor.battmind_battery_level_needed` viser hvor meget batteri, der er brug for indtil næste ladning.

---

## Krav

- Home Assistant **2025.1** eller nyere med recorder (standard)
- En sensor for husbatteriets **batteriniveau** (%) og **effekt** (W)
- En sensor for **husets forbrug** (W)
- Prisintegration: [Energi Data Service](https://github.com/MTrab/energidataservice) og/eller [Strømligning](https://github.com/MTrab/stromligning)
- Valgfrit:
  - solcelleproduktion (W)
  - [Solcast PV Forecast](https://github.com/BJReplay/ha-solcast-solar)
  - en vejr-integration, der understøtter `weather.get_forecasts` med skydække, f.eks.
    [Meteorologisk institutt](https://www.home-assistant.io/integrations/met/),
    [AccuWeather](https://www.home-assistant.io/integrations/accuweather/) eller
    [OpenWeather](https://www.home-assistant.io/integrations/openweathermap/)
- Til dashboardet (HACS frontend): `apexcharts-card`, `mushroom`, `stack-in-card` og `config-template-card`

---

## Installation

### HACS (anbefalet)

[![Åbn dit Home Assistant og tilføj repoet i HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=dezito&repository=BattMind-HACS&category=integration)

1. **HACS** → ⋮ → **Custom repositories**: tilføj `https://github.com/dezito/BattMind-HACS` som **Integration**.
2. Søg efter **BattMind**, installer den, og genstart Home Assistant.
3. Tilføj integrationen:

   [![Åbn dit Home Assistant og tilføj BattMind.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=battmind)

   eller **Indstillinger → Enheder og tjenester → Tilføj integration → BattMind**.
4. Lav automationen, der styrer husbatteriet, f.eks. med [blueprinten](#blueprint-styr-husbatteriet).

### Manuelt

Kopiér `custom_components/battmind` til `config/custom_components/battmind`, og genstart Home Assistant.

---

## Opsætning

Opsætningen sker i brugerfladen, fordelt på små sider med hjælpetekster:

1. **Prefix og sprog:** prefixet bliver starten på alle entity id'er (standard `battmind`)
2. **Husbatteri:** batteriniveau, effekt (evt. omvendt fortegn), størrelse, min./maks. niveau,
   lade-/afladeeffekt, lade-/afladningstab og slitage pr. kWh
3. **Forbrug:** husets forbrug, og om det indeholder batteriets op-/afladning. Du kan også
   ignorere forbrugere, f.eks. elbilladeren.
4. **Prisudbydere:** Energi Data Service og/eller Strømligning (køb, salg og tariffer), refusion og salgsgebyr
5. **Solceller:** produktion, inverterens maks. effekt, fast salgspris eller timepris, og Solcast
6. **Vejr:** time- og dagsprognose (kun med solceller)
7. **Avanceret:** antal dage der planlægges, interval, dashboard, diagnostisk fil-log og testtilstand
8. **Database:** hvor meget historik der gemmes

Entitetsvælgerne viser kun relevante entiteter. Batteriniveau viser kun `battery`-sensorer,
effekt kun `power`-sensorer, priserne kun entiteter fra prisintegrationerne og prognosen kun Solcast.

Alt kan ændres bagefter under **Konfigurer** på integrationen. Integrationen genindlæses, når du gemmer.

---

## Entiteter

Entity id'erne starter med prefixet, her `battmind`.

### Indstillinger (ændres fra dashboardet)

| Type | Entiteter |
|---|---|
| `switch` | Deaktiver scriptet, solcelleoverskud til opladning, billigste perioder, fyld op i billigste periode, dyreste perioder, nødvendig før max niveau, kun afladning ved profit, prioritér afladning efter energikost, sælg overskydende kWh, brug midnat batteriniveau, maksimer profit, tvangsafladning af gamle kWh, optimer solcelleopladning |
| `number` | Billige prisperioder, stigning for billigste pris / billige perioder, min. profit pr. kWh, min. total profit, min. kWh til salg, maks. timer gamle kWh |
| `select` | Prognosetype (gennemsnit, EMA eller trend) |
| `text` | Ekskluder salgstimer (f.eks. `17,18,19`) |
| `button` | Gennemtving planlægning, genstart (genindlæser integrationen) |

Planen beregnes igen 5 sekunder efter en indstilling er ændret. Værdierne huskes ved genstart.

### Resultater

| Entitet | Indhold |
|---|---|
| `sensor.battmind_powerwall_action` | Hvad batteriet skal gøre nu (se [ovenfor](#sådan-styrer-du-husbatteriet)) |
| `sensor.battmind_current_charging_rule` | Nuværende regel som tekst |
| `sensor.battmind_battery_level_needed` | Nødvendigt batteriniveau indtil næste ladning (%) |
| `sensor.battmind_kwh_charged_by_solar` | kWh ladet af solcellerne (kun med solceller) |
| `sensor.battmind_overview` | Oversigt og plan (markdown i attributten `overview`) |
| `sensor.battmind_charging_history` | Ladehistorik (markdown i attributten `history`) |
| `sensor.battmind_emoji_description` | Forklaring på emojis (markdown i attributten `description`) |

Store attributter (markdown, planer) gemmes ikke i recorder-databasen.

---

## Dashboard

Med **Dashboard i sidepanelet** slået til (standard) får hver instans sit eget dashboard,
f.eks. `/battmind-dashboard`. Det bygges ved hver opstart ud fra din opsætning. Kort for
prisudbydere og solprognose, du ikke bruger, bliver fjernet.

Det genererede dashboard kan ikke redigeres i brugerfladen. Vil du tilpasse det, så kopiér
indholdet over i et almindeligt dashboard (**Rediger → Raw configuration editor**), og slå
**Dashboard i sidepanelet** fra.

---

## Blueprint: styr husbatteriet

BattMind bestemmer *hvad* batteriet skal gøre (`sensor.<prefix>_powerwall_action`), men styrer
ikke selv inverteren. Det klarer blueprinten – den virker med alle invertere, fordi du selv
vælger hvad der skal ske med Home Assistants almindelige handlings-editor.

Blueprinten installeres automatisk sammen med integrationen: ved opstart lægges den i
`config/blueprints/automation/battmind/battery_control.yaml` og findes derefter under
*Indstillinger → Automatiseringer og scener → Blueprints* som **BattMind - styr husbatteriet**.
Kommer der en ny version med en HACS-opdatering, erstattes filen kun, hvis du ikke selv har
rettet i den. Kilden er [`battery_control.yaml`](custom_components/battmind/blueprints/battery_control.yaml).

1. Klik på blueprinten → **Opret automatisering** og vælg BattMind-sensoren.
2. Under **Handlinger** tilføjer du for hver BattMind handling det, din inverter skal gøre –
   typisk *Tal: Indstil værdi* på batteriets maks. lade-/afladeeffekt, *Kontakt: Tænd/Sluk* eller
   en service fra inverter-integrationen. Felter du lader stå tomme, gør ingenting.
3. Har du en elbil, så vælg laderens effektsensor under **Elbil** og hvad der skal ske, mens
   bilen lader (f.eks. afladeeffekt 0, så batteriet ikke tømmes ind i bilen).
4. Slå **Gentag hver halve time** til, hvis inverterens tvungne op-/afladning selv stopper
   efter et stykke tid (f.eks. Huawei Solar efter 2 timer).

<details>
<summary><b>Eksempel: Huawei Solar (LUNA2000, 5000 W)</b></summary>

Vælg *Rediger i YAML* i hvert handlingsfelt og indsæt. Udskift `DIN_BATTERI_DEVICE_ID` med
batteri-enhedens id (ses i adresselinjen på enhedens side).

**Lad fra elnettet**
```yaml
- action: number.set_value
  target:
    entity_id:
      - number.batteries_maximum_charging_power
      - number.batteries_grid_charge_maximum_power
  data:
    value: 5000
- action: switch.turn_on
  target:
    entity_id: switch.batteries_charge_from_grid
- action: huawei_solar.forcible_charge
  data:
    device_id: DIN_BATTERI_DEVICE_ID
    duration: 120
    power: 5000
```

**Tvungen afladning**
```yaml
- action: number.set_value
  target:
    entity_id:
      - number.batteries_maximum_charging_power
      - number.batteries_grid_charge_maximum_power
      - number.batteries_maximum_discharging_power
  data:
    value: 5000
- action: huawei_solar.forcible_discharge
  data:
    device_id: DIN_BATTERI_DEVICE_ID
    duration: 120
    power: 5000
```

**Afladning tilladt**
```yaml
- action: huawei_solar.stop_forcible_charge
  data:
    device_id: DIN_BATTERI_DEVICE_ID
- action: number.set_value
  target:
    entity_id:
      - number.batteries_maximum_charging_power
      - number.batteries_grid_charge_maximum_power
      - number.batteries_maximum_discharging_power
  data:
    value: 5000
```

**Kun solcelleopladning**
```yaml
- action: huawei_solar.stop_forcible_charge
  data:
    device_id: DIN_BATTERI_DEVICE_ID
- action: number.set_value
  target:
    entity_id:
      - number.batteries_grid_charge_maximum_power
      - number.batteries_maximum_discharging_power
  data:
    value: 0
- action: number.set_value
  target:
    entity_id: number.batteries_maximum_charging_power
  data:
    value: 5000
```

**Solcelleopladning blokeret**
```yaml
- action: huawei_solar.stop_forcible_charge
  data:
    device_id: DIN_BATTERI_DEVICE_ID
- action: number.set_value
  target:
    entity_id:
      - number.batteries_maximum_charging_power
      - number.batteries_grid_charge_maximum_power
  data:
    value: 0
- action: number.set_value
  target:
    entity_id: number.batteries_maximum_discharging_power
  data:
    value: 5000
```

**Når bilen lader**
```yaml
- action: huawei_solar.stop_forcible_charge
  data:
    device_id: DIN_BATTERI_DEVICE_ID
- action: number.set_value
  target:
    entity_id:
      - number.batteries_maximum_charging_power
      - number.batteries_grid_charge_maximum_power
  data:
    value: 5000
- action: number.set_value
  target:
    entity_id: number.batteries_maximum_discharging_power
  data:
    value: 0
```

Slå **Gentag hver halve time** til.

</details>

## Services

| Service | Beskrivelse |
|---|---|
| `battmind.enforce_planning` | Beregn planen igen nu |
| `battmind.recalc_charging_history_today` | Genberegn dagens ladehistorik |
| `battmind.debug_info` | Returnerer intern status til fejlfinding |

Alle services tager valgfrit `config_entry_id`, så de kun rammer én instans.

---

## Skift fra PyScript-udgaven

BattMind læser PyScript-filerne, men ændrer dem aldrig. Integrationens egne data gemmes i `.storage`.

1. Tilføj integrationen. Findes `battmind_config.yaml` i konfigurationsmappen, spørger
   opsætningen om den skal importeres:
   - værdierne fra `battmind_config.yaml` udfyldes på siderne
   - `battmind_*_db.yaml` (ladehistorik, effektværdier, kWh-priser, solproduktion) importeres ved første start
   - findes de gamle `input_boolean` / `input_number` / … `battmind_*` stadig, kopieres deres værdier til de nye entiteter
2. Vil du køre de to udgaver side om side, så vælg et andet prefix, f.eks. `battmind_ha`.
   Opsætningen afviser et prefix, hvis PyScript-udgaven allerede har entiteter med det navn.
3. Når du er tilfreds, så fjern `pyscript/battmind.py` og `packages/battmind.yaml`, og genstart.

| PyScript | Integration |
|---|---|
| `battmind_config.yaml` | Opsætning / **Konfigurer** i brugerfladen |
| `input_boolean.battmind_*` | `switch.battmind_*` |
| `input_number.battmind_*` | `number.battmind_*` (fast solcelle-salgspris ligger nu i opsætningen) |
| `input_select.battmind_forecast_type` | `select.battmind_forecast_type` |
| `input_text.battmind_exclude_sell_hours` | `text.battmind_exclude_sell_hours` |
| `input_button.battmind_*` | `button.battmind_*` |
| `pyscript.battmind_debug_info` | `battmind.debug_info` |
| Opdateringsscripts og `shell_command` | HACS |

### Rettelser i forhold til PyScript-udgaven

- Forbrugshistorik gemmes også uden solceller og solprognose, så forbrugsprognosen ikke er tom.
- "Billigste timer" planlægger ikke længere i timer, der allerede er gået.
- "Sælg overskydende kWh" tæller ikke længere den samme salgsperiode med flere gange.
- En fejl i én regel stopper ikke længere hele planlægningen.
- Historikken læses fra en buffer i hukommelsen i stedet for 70+ recorder-opslag pr. planlægning.

---

## Fejlfinding

- Slå **Diagnostisk fil-log** til under **Konfigurer → Avanceret**. Loggen skrives til
  `battmind_<prefix>.log` i konfigurationsmappen (2 MB, 3 roterede filer).
- Eller slå debug-log til i `configuration.yaml`:
  ```yaml
  logger:
    logs:
      custom_components.battmind: debug
  ```
- Kald `battmind.debug_info` under **Udviklerværktøjer → Handlinger**, og vedhæft svaret, når du opretter et issue.

---

## Licens

GPL-3.0, se [LICENSE](LICENSE).
