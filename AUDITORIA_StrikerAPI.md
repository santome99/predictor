# Auditoría Completa de StrikerAPI
Dataset histórico para predicción de apuestas deportivas (fútbol) — La Liga + UEFA Champions League

**Fecha de auditoría:** 2026-10-31
**Fuentes (solo documentación oficial):** `https://strikerapi.com/docs`, `/docs/{tool}`, `/docs/timing-and-rights`, `/docs/find-the-right-call`, `/docs/atlas`, `/la-liga`, `/champions-league`, `/pricing`, `/data/soccer-odds-history`
**Estado:** Investigación y documentación únicamente. **No** se ha implementado código de ingesta ni se ha consumido ningún crédito. Todo lo aquí descrito proviene de la documentación pública; los campos marcados con ⚠️ requieren verificación empírica (con una prueba autorizada) antes de construir sobre ellos.

---

## 0. Resumen ejecutivo y conclusiones clave

StrikerAPI es una API de datos de fútbol que sirve 17 competiciones vía REST y MCP. Suministra partidos desde 2001-02, estadísticas y alineaciones (con box scores desde 2005-06), eventos con xG desde 2012-13, y líneas de apuestas (moneyline 1X2, hándicap y total) desde 2017-18, con cuotas de apertura/cierre fiables desde junio-2020 mediante el "sportsbook odds archive".

**Conclusiones críticas para el proyecto de ML (leer antes que todo):**

1. **Mercados disponibles (muy limitado respecto al objetivo).** La API solo ofrece **tres mercados por bookmaker**:
   - Moneyline 3 vías (1X2: local / empate / visitante).
   - Hándicap de goles (línea del local).
   - Total (over/under) de goles.
   - Más el mercado de jugador "anytime goalscorer" (`player_goal_scorer_anytime`).
   - **NO hay** BTTS, doble oportunidad, resultado al descanso, resultado a la HT/FT, over/under de córners, over/under de tarjetas, ni hándicap asiático completo. La doc de cuotas lo declara explícitamente: *"No corners or cards markets. The one player market is the anytime goalscorer price."*

2. **Sin cuotas BTTS ni mercados de descanso**: el dataset de cuotas se limita a 1X2 + hándicap + total. Si el modelo necesita BTTS o HT, **StrikerAPI no los cubre**; habría que obtenerlos de otra fuente o modelarlos.

3. **xG NO es "as-of" (peligro de fuga de datos).** La doc de *timing-and-rights* dice literalmente que el xG proviene de un modelo por liga ajustado sobre *todas* las temporadas (incluida la que se predice) y que un reajuste *recalcula* todos los tiros. Para validación estrictamente out-of-sample, **hay que ajustar un modelo propio** sobre las localizaciones de tiro.

4. **Sin historial de revisiones.** Cada registro se sobrescribe cuando la fuente corrige algo; no hay `revision_id` ni changelog por registro. La integridad del histórico depende de descargas repetidas en el momento correcto.

5. **Clasificaciones solo finales de temporada.** `get_standings` devuelve la tabla *publicada* (final). Para reconstruir la clasificación "justo antes de un partido" hay que derivarla de `list_games` (resultados acumulados).

6. **Elo útil para anti-leakage.** `get_ratings` admite `date` ("ratings as of this day"), por lo que se puede recuperar el Elo de un equipo en una fecha previa al partido sin contaminar la predicción.

7. **Zona temporal de fechas:** las fechas (`date`, `starts_at`) son UTC. `list_games` filtra por `from`/`to` en UTC.

---

## 1. Inventario completo de endpoints (19)

Todos los endpoints usan `POST /v1/{tool}` (REST) con body JSON, o el mismo `{tool}` vía MCP. Autenticación `x-api-key`. Para una liga concreta, cualquiera de estos se puede invocar como `/v1/laliga/{tool}` o `POST /v1/ucl/{tool}`, o pasando `"league": "laliga"` / `"league": "ucl"`. Los créditos se descuentan antes de ejecutar y se reembolsan en errores 500 derivados del servidor.

Tabla resumen por categoría y coste:

| Categoría | Endpoints | Créditos/llamada |
|---|---|---|
| Static reference | `list_teams`, `search_players` | 1 |
| Basic lookup | `list_games`, `get_schedule`, `get_standings`, `get_ratings`, `get_head_to_head` | 2 |
| Stats + aggregation | `get_game`, `get_lineups`, `get_player`, `get_player_games`, `get_leaders`, `get_team_stats`, `get_odds` | 5 |
| Multi-table | `get_events`, `get_shot_map`, `get_player_props`, `get_prop_history` | 10 |
| Time-series | `get_line_movement` | 10 |

De los endpoints especiales mencionados en el encargo, **StrikerAPI no tiene `get_line_movement`-igual, sí lo tiene; no existen `get_team_stats` por partido (es por temporada), ni `get_ratings` histórico por partido (es por fecha)**. Los que el encargo pedía se asignan así:

- `list_games` → Sí (existe).
- `get_game` → Sí (existe).
- `get_odds` → Sí (existe).
- `get_line_movement` → Sí (existe).
- `get_events` → Sí (existe).
- `get_lineups` → Sí (existe).
- `get_team_stats` → Existe, pero **por temporada** (medias por partido), no por partido.
- `get_ratings` → Existe, pero **por fecha/ranking**, no por partido.
- `get_standings` → Sí (existe).

### 1.1. Referencia estática (1 crédito)

#### `list_teams`
- **Descripción:** IDs, abreviaturas y colores de los clubes de una temporada.
- **Parámetros:** `season` (número|string, opcional; año de inicio, ej. 2025=2025-26; ligas de calendario usan año natural).
- **Respuesta:** `teams[]` con `team_id`, `abbrev`, `name`, `display_name`, `short_name`, `location`, `color`, `logo`; `count`.
- **Coste:** 1 cr. **Sin paginación**.

#### `search_players`
- **Descripción:** encuentra jugadores por nombre (acentos opcionales) para obtener `player_id`.
- **Parámetros:** `name` (string, **obligatorio**; total o parcial), `team` (opcional), `limit` (opcional).
- **Respuesta:** `players[]` con `player_id`, `name`, `short_name`, `position`, `jersey`, `team_id`, `team`, `last_season`, `appearances`; `count`.
- **Coste:** 1 cr.
- **Nota:** búsquedas de nombre parcial pueden devolver jugadores no deseados (el ejemplo "saka" devuelve a Wan-Bissaka). Verificar por equipo.

### 1.2. Búsqueda básica (2 créditos)

#### `list_games`
- **Descripción:** partidos y resultados por temporada, fase, fecha, rango de fechas o club. Nuevo primero.
- **Parámetros:**
  - `season` (número|string, opcional; año de inicio).
  - `stage` (enum `league`|`knockout`, opcional; `league`=liga/grupos/league phase; `knockout`=clasificatorias/eliminatorias/playoffs).
  - `team` (string, opcional; id, abreviatura o nombre).
  - `date`, `from`, `to` (string `YYYY-MM-DD`, opcional, **UTC**).
  - `status` (enum `scheduled`|`final`, opcional).
  - `limit` (integer, opcional, máx 50 por defecto), `offset` (integer, opcional; usar `next_offset`).
- **Respuesta:** `games[]` (ver §2) ; `count`, `next_offset`.
- **Coste:** 2 cr.
- **Valor:** es la puerta de entrada: identifica `game_id`, local/visitante, resultados y fechas.

#### `get_schedule`
- **Descripción:** próximos partidos con la línea 1X2 **mediana** entre casas (para apps de partidos/inputs de modelo). No es histórico.
- **Parámetros:** `days` (integer, opcional, por defecto 7), `team` (opcional).
- **Respuesta:** `games[]` con info de partido + `line` (`books`, `home_ml`, `draw_ml`, `away_ml`, `total`, `home_handicap`, `implied`).
- **Coste:** 2 cr. No histórico (partidos futuros).

#### `get_standings`
- **Descripción:** la tabla de la liga tal como se publica, con puntos (incl. deducciones), diferencia de goles, local/visitante y forma.
- **Parámetros:** `season` (número|string, opcional; por defecto la más reciente).
- **Respuesta:** `season`, `source` (`published`), `standings[]` con `group_name`, `rank`, `team_id`, `abbrev`, `display_name`, `played`, `wins`, `draws`, `losses`, `goals_for`, `goals_against`, `goal_diff`, `points`, `deductions`, `note`, `home` (W-D-L), `away` (W-D-L), `form` (última primero); `count`, `note`.
- **Coste:** 2 cr. **Cobertura:** 2002-03 → actual.
- **⚠️ Importante:** es la tabla **final** de temporada, no por jornada. La nota la documenta: `home`, `away` y `form` se derivan de los resultados que tiene StrikerAPI.

#### `get_ratings`
- **Descripción:** clasificación de clubes por Elo propio, más fuerte primero, **hoy o "as of" una fecha**. Es el endpoint clave contra el leakage.
- **Parámetros:** `date` (string `YYYY-MM-DD`, opcional; ratings en esa fecha; por defecto el más reciente), `limit` (integer, opcional, máx 50).
- **Respuesta:** `as_of`, `ratings[]` (`rank`, `team_id`, `abbrev`, `name`, `elo`, `last_game`), `count`, `method`.
- **Coste:** 2 cr. **Cobertura:** 2001-02 → actual, por competición (cada competición tiene su propio rating).
- **Método documentado:** *"Elo from every final match in this competition: K 20, home advantage 60 points (none at neutral venues), scaled by goal difference, a third of the way back to 1500 between seasons. 400 points = 10-to-1 odds. A rating moves only on this competition's matches."* Es una **variante Elo propia por competición** (K=20, ventaja de local 60, escala por diferencia de goles, regresión a 1500).

#### `get_head_to_head`
- **Descripción:** historial entre dos clubes (resumen + partidos, nuevas primero).
- **Parámetros:** `team` (**obligatorio**), `opponent` (**obligatorio**), `limit` (opcional, máx 20 por defecto; el resumen cuenta todos).
- **Respuesta:** `teams[]`, `summary` (`meetings`, `team_wins`, `draws`, `opponent_wins`, `team_goals`, `opponent_goals`, `first_meeting`), `games[]`, `count`.
- **Coste:** 2 cr.

### 1.3. Estadísticas y agregación (5 créditos)

#### `get_game`
- **Descripción:** un partido completo en una llamada: resultado, ambos box scores (con xG), línea de cada jugador, eventos clave y línea de cada casa.
- **Parámetros:** `game_id` (**obligatorio**).
- **Respuesta:** `game` (datos del partido) + `team_stats[]` + `players[]` + `key_events[]` + `odds[]`. (Detallado en §3 y §4).
- **Coste:** 5 cr.

#### `get_lineups`
- **Descripción:** formaciones, XI inicial, banquillo y sustituciones con minutos para un partido.
- **Parámetros:** `game_id` (**obligatorio**).
- **Respuesta:** `game` + `lineups[]` (`formation`, `starting_xi[]`, `substitutes_used[]`, `unused_substitutes[]`).
- **Coste:** 5 cr. **Cobertura:** lineups desde 2001-02 (La Liga 2002-03; ver §8).

#### `get_player`
- **Descripción:** perfil de jugador, totales por temporada, ratios per-90 y últimos 10 partidos.
- **Parámetros:** `player_id` (**obligatorio**).
- **Respuesta:** `player` + `seasons[]` (totales y per90: goles, asistencias, tiros, SOT, xG, tarjetas, minutos, etc.) + `recent_games[]`.
- **Coste:** 5 cr.

#### `get_player_games`
- **Descripción:** historial completo de partidos de un jugador (suele ser la vía para datos por jugador a nivel de partido).
- **Parámetros:** `player_id` (**obligatorio**), `season` (opcional), `limit` (opcional), `offset` (opcional).
- **Respuesta:** `player_id`, `games[]` (por partido: minutos, titular, goles, asistencias, tiros, SOT, xG, faltas, fuera de juego, tarjetas, goles en contra, etc.), `count`, `next_offset`.
- **Coste:** 5 cr.

#### `get_leaders`
- **Descripción:** líderes de una estadística en una temporada (total o per90).
- **Parámetros:** `stat` (enum `goals`|`assists`|`goal_contributions`|`shots`|`shots_on_target`|`xg`|`yellow_cards`|`red_cards`|`saves`|`clean_sheets`, opcional, por defecto `goals`), `season`, `per90` (boolean), `min_games`, `limit`.
- **Respuesta:** `stat`, `label`, `season`, `min_games`, `leaders[]`, `count`, `note`.
- **Coste:** 5 cr.

#### `get_team_stats`
- **Descripción:** medias por partido de cada club en una temporada (a favor y en contra): goles, xG, tiros, posesión, pases, disciplina. **Atención: es por temporada, no por partido.**
- **Parámetros:** `season` (opcional), `team` (opcional).
- **Respuesta:** `season`, `teams[]` (`goals_for`, `goals_against`, `xg_for`, `xg_against`, `shots_for`, `shots_against`, `shots_on_target_for/against`, `possession_pct`, `pass_accuracy_pct`, `passes`, `corners`, `fouls`, `yellow_cards`, `red_cards_total`, `tackles`, `interceptions`, `clean_sheets`, `failed_to_score`), `count`, `method`.
- **Coste:** 5 cr. La `method` documenta: *"xG is ours, from located shots; null where a season has no located shots. A stat not charted for a match is null, never 0."*

#### `get_odds`
- **Descripción:** la moneyline 1X2 de cada casa, el hándicap de goles y el total para un partido (5 cr) o un día (10 cr), con probabilidades sin margen y el resultado. Con `season` se puede paginar una temporada (cobro por partido, ver §9).
- **Parámetros (modos):**
  - Por partido: `game_id` (opcional).
  - Por día: `date` (`YYYY-MM-DD` UTC, opcional, **cuesta 10 cr**).
  - Por temporada: `season` (+ `team`, `limit`, `offset`).
- **Respuesta:** `games[]` cada uno con `books[]` de cada casa y `outcome`.
- **Coste:** 5 cr (partido), 10 cr (día), variable por temporada (§9).

### 1.4. Multi-tabla (10 créditos)

#### `get_events`
- **Descripción:** eventos de un partido en orden: goles, tiros con localización y xG, tarjetas, sustituciones, saques de banda/córneres, faltas.
- **Parámetros:** `game_id` (**obligatorio**), `kind` (enum `goals`|`shots`|`cards`|`substitutions`|`set_pieces`|`fouls`|`all`, por defecto `all`), `limit` (por defecto 300), `offset`.
- **Respuesta:** `events[]` (cada evento con `seq`, `minute`, `added_minute`, `period`, `type`, `team_id`, `player_id`, `shot{...}` para tiros, `is_goal`, `own_goal`, `shootout`, `text`), `count`, `next_offset`.
- **Coste:** 10 cr. **Cobertura:** tiros/córneres/faltas desde 2012-13; antes solo goles, tarjetas y sustituciones.

#### `get_shot_map`
- **Descripción:** mapa de tiros de un club, jugador o defensa (o de un partido con `game_id`): localización de cada tiro con xG, más totales por zona.
- **Parámetros:** `team` / `player` (al menos uno), `against` (boolean, tiros recibidos), `season`, `game_id`, `include_shots` (boolean; `false`=solo totales/zonas).
- **Respuesta:** `zones[]` (por zona: tiros, goles, a puerta, cabezazos, xG, conversión, cuota), `totals`, `shots[]` (hasta 2.000, con `x/y`, `end_x/end_y`, `distance_m`, `outcome`, `body_part`, `xg`, `zone`).
- **Coste:** 10 cr. **Cobertura:** tiros localizados desde 2012-13.

#### `get_player_props`
- **Descripción:** precios "anytime goalscorer" (goleador en cualquier momento) por casa, un precio pre-partido por jugador y casa. Único mercado de jugador.
- **Parámetros:** `game_id` (opcional; props de todos los jugadores del partido), `player_id` (opcional; con `from_date`/`to_date` para rango), `market` (enum `player_goal_scorer_anytime`), `bookmaker`, `snapshot_type` (enum `opening`|`closing`, por defecto `closing`), `estimate_only` (boolean).
- **Respuesta:** `snapshot_type`, `games`, `props[]` (`player_name`, `player_id`, `market`, `bookmaker`, `line`, `over_odds`, `under_odds`, `captured_at`, `source`), `count`.
- **Coste:** ⚠️ **10 cr antes del inicio / 20 cr después de jugado** (para cuentas creadas después de 2026-10-30; cuentas anteriores: 10 cr siempre). **Cobertura:** La Liga desde agosto-2024.
- **Nota:** un capture por jugador/casa → **no hay apertura, ni movimiento**.

#### `get_prop_history`
- **Descripción:** el precio de cierre "anytime goalscorer" de un jugador contra si marcó, partido a partido. Útil para evaluar el mercado.
- **Parámetros:** `player_id` (**obligatorio**), `market` (**obligatorio**, `player_goal_scorer_anytime`), `from_date`, `to_date`, `bookmaker`, `limit` (por defecto 50, máx 200). Paginación por `next_from_date`.
- **Respuesta:** `games[]` (`line`, `open_line`, `over_odds`, `under_odds`, `books`, `actual`, `result`, `margin`), `summary`, `truncated`, `next_from_date`.
- **Coste:** 10 cr.

### 1.5. Serie temporal (10 créditos)

#### `get_line_movement`
- **Descripción:** snapshots de la línea de cada casa **antes del inicio**, para reconstruir el movimiento de precios.
- **Parámetros:** `game_id` (**obligatorio**), `provider` (opcional; una casa), `all_snapshots` (boolean; por defecto se omiten capturas idénticas a la anterior, conservando siempre la última).
- **Respuesta:** `game` + `movement` (diccionario por `provider`, cada uno con array de snapshots: `captured_at`, `home_ml`, `draw_ml`, `away_ml`, `home_handicap`, `total`) + `snapshots` (número total).
- **Coste:** 10 cr.

---

## 2. Información de partidos (histórico)

### 2.1. Cómo consultar resultados históricos

El endpoint para histórico de partidos es **`list_games`** (2 cr). Ejemplos de filtrado:

- **Por competición:** se invoca con `league` (vía `/v1/laliga/{tool}` o `league:"laliga"`). No hay otro parámetro de competición.
- **Por temporada:** `season` (año de inicio, ej. `2017` = 2017-18). 
- **Por rango de fechas:** `from` + `to` (`YYYY-MM-DD`, UTC).
- **Por una fecha:** `date`.
- **Por club:** `team` (id/abreviatura/nombre).
- **Por fase:** `stage = league|knockout` (para UCL separa fase de liga de eliminatorias).
- **Todos los partidos de una temporada:** paginar con `limit` (50 por defecto) y `offset`/`next_offset`.

### 2.2. Estructura de un partido

`game` / `games[]` contienen:

| Campo | Tipo | Notas |
|---|---|---|
| `game_id` | string | **Identificador único.** Ej. `704345`. Se obtiene aquí y se usa en el resto de endpoints. |
| `season` | number | Año de inicio. |
| `stage` | string | `Regular Season`, `League Phase`, `Round of 16`, `Final` (y eliminatorias/clasificatorias). |
| `date` | string | `YYYY-MM-DD` (UTC). |
| `starts_at` | string | ISO-8601 UTC, e.g. `2026-04-19T15:30:00.000Z`. |
| `status` | string | `scheduled` / `final`. |
| `status_detail` | string | `FT` (final), descripciones de texto para programados. |
| `home` | object | `team_id`, `abbrev`, `name`, `score`. **Local.** |
| `away` | object | idem. **Visitante.** |
| `result` | string | `H` (local), `A` (visitante), `D` (empate). |
| `neutral_site` | null | no se documenta su uso práctico. |
| `venue`, `city` | string | estadio y ciudad. |
| `attendance` | number/null | asistencia (puede ser `null`). |

- **Local/visitante:** `home`/`away`.
- **Resultado final:** `home.score` / `away.score`, y `result`.
- **Resultado al descanso:** **NO está disponible** en ningún documento. No existe campo de HT en `list_games`, `get_game` ni `get_events`. ⚠️ Requiere confirmación empírica, pero por documentación no se ofrece.
- **Suspendidos/cancelados:** la doc solo documenta `status = scheduled|final`. No se documentan códigos explícitos de suspendido/aplazado/descartado. Un partido suspendido podría aparecer como `scheduled` hasta reanudarse o con marcador `final`. ⚠️ Filtrar partidos anómalos: `score` nulo en `final`, o `attendance` nula.
- **Prórroga y tanda de penaltis:** `get_events` expone eventos con `shootout: true/false` y `own_goal`, `penalty`; la cobertura de "fixtures and results" cita *"extra time and shootout"* como parte del dataset. No se documenta un marcador de penaltis separado; el resultado en `result` y `score` es el que determina el desenlace final. ⚠️ Verificar cómo se representa un partido decidido por penaltis en `score`/`result`/`status_detail`.

### 2.3. Cobertura histórica de La Liga (y UCL)

- **La Liga:** todos los partidos desde **2001-02** (9.636 partidos, 26 temporadas). Hay registros por temporada (2017-18: 417 partidos; 2018-19: 385; 2019-20 a 2023-24: ≈380; 2022-23: 375, etc.).
- **UCL:** desde **2001-02** (4.915 partidos, 26 temporadas), todas las fases. Las rondas clasificatorias están "floja": lineups y goles, "mostly without box scores or shot locations". **Desde 2020-21 solo se listan fase de grupos/league phase y eliminatorias.**
- `get_head_to_head` también da resultados históricos entre dos equipos.

---

## 3. Estadísticas de los partidos

### 3.1. Box score por equipo (`get_game.team_stats`, 5 cr/partido)

Para los dos equipos (identificados por `home: true/false`), por partido:

`goals`, `xg`, `possession_pct`, `shots`, `shots_on_target`, `blocked_shots`, `corners`, `fouls`, `yellow_cards`, `red_cards`, `offsides`, `saves`, `passes`, `accurate_passes`, `crosses`, `accurate_crosses`, `long_balls`, `accurate_long_balls`, `tackles`, `effective_tackles`, `interceptions`, `clearances`, `penalty_goals`, `penalty_attempts`.

- Es **dato agregado por equipo**, no por jugador.
- La regla general de la plataforma: *"A stat not charted for a match is null, never 0."*

### 3.2. Medias por temporada (`get_team_stats`, 5 cr)

Por club y temporada, a favor y en contra: `goals_for/against`, `xg_for/against`, `shots_for/against`, `shots_on_target_for/against`, `possession_pct`, `pass_accuracy_pct`, `passes`, `corners`, `fouls`, `yellow_cards`, `red_cards_total`, `tackles`, `interceptions`, `clean_sheets`, `failed_to_score`. Utile para construir "forma" y promedios rodantes por temporada (con la precaución de §11).

### 3.3. Eventos por partido (`get_events`, 10 cr) — tiros, xG, faltas, córneres

Con `kind=shots`/`all` se obtiene cada tiro: `outcome`, `body_part`, `penalty`, `xg`, `zone` (`six_yard_box`, `penalty_area`, `outside_box`), `x/y`, `end_x/end_y`, `distance_m`, `is_goal`. También `fouls`, `corners` (via `set_pieces`), `cards`, `substitutions`, `goals`. 

- Permite calcular por partido: tiros totales, a puerta, dentro/fuera del área, grandes ocasiones (proxy de xG), xG por equipo, córneres, faltas, tarjetas. **No hay un campo "big chances" / "big chances missed" nativo.**
- **Cobertura:** córneres/faltas/fueras de juego y tiros localizados desde **2012-13**. Antes solo goles, tarjetas y sustituciones.

### 3.4. Mapa de tiros (`get_shot_map`, 10 cr)

Por equipo/jugador/defensa o partido (`game_id`). Da totales por zona (área de 6, área penal, fuera del área) con xG y conversión, más cada tiro localizado (hasta 2.000).

### 3.5. Estadísticas por jugador

`get_player` y `get_player_games` (5 cr) por partido: `starter`, `position`, `minute_on`, `minute_off`, `minutes`, `goals`, `assists`, `shots`, `shots_on_target`, `xg`, `fouls_committed`, `fouls_suffered`, `offsides`, `yellow_cards`, `red_cards`, `own_goals`, `saves`, `goals_conceded`, `shots_faced`.

### 3.6. Mapa frente al encargo (estadísticas)

| Estadística | ¿Disponible? | Fuente | Desde |
|---|---|---|---|
| Goles | Sí (ambos equipos, agregado y por jugador) | `get_game`, `get_events`, `get_player*` | 2001-02 (objetivo) |
| xG | Sí (de tiros localizados) | `get_game.team_stats.xg`, `get_events`, `get_shot_map` | 2012-13 |
| Tiros totales | Sí | `get_game`, `get_team_stats` | 2005-06 (box) |
| Tiros a puerta | Sí | idem | 2005-06 |
| Tiros fuera | Derivable (`shots - on_target - blocked`) | idem | 2005-06 |
| Tiros bloqueados | Sí (`blocked_shots`) | `get_game` | 2005-06 |
| Tiros dentro/fuera del área | Parcial: en `get_events` cada tiro tiene `zone` y `x/y`; se agrega por partido | `get_events`, `get_shot_map.zones` | 2012-13 |
| Posesión | Sí (`possession_pct`) | `get_game`, `get_team_stats` | 2005-06 |
| Córneres | Sí | `get_game.team_stats`, `get_events` | 2005-06 (box) / 2012-13 (eventos) |
| Faltas | Sí | idem | 2005-06 / 2012-13 |
| Tarjetas amarillas | Sí | idem | 2005-06 (box) / 2011-12 (eventos) |
| Tarjetas rojas | Sí | idem | idem |
| Paradas (saves) | Sí | `get_game`, `get_player` | 2005-06 |
| Grandes ocasiones | **No** (no nativo; proxy con xG/tiros) | — | — |
| Grandes ocasiones falladas | **No** | — | — |
| Pases | Sí (`passes`, `accurate_passes`) | `get_game` | 2005-06 |
| Precisión de pases | Sí (`accurate_passes/passes`; `pass_accuracy_pct` en `get_team_stats`) | `get_game`, `get_team_stats` | 2005-06 |
| Fueras de juego (offsides) | Sí | `get_game`, `get_events` | 2005-06 / 2012-13 |
| Duelos ganados | **No** documentado | — | — |
| Centros (crosses) | Sí (`crosses`, `accurate_crosses`) | `get_game` | 2005-06 |
| Entradas (tackles) | Sí (`tackles`, `effective_tackles`) | `get_game`, `get_team_stats` | 2005-06 |
| Recuperaciones | Parcial (`interceptions`, `clearances`, `long_balls`) | `get_game` | 2005-06 |

→ La mayor carencia: **xG y tiros localizados solo desde 2012-13**; sin "big chances" ni "duelos ganados".

---

## 4. Cuotas de apuestas (prioridad máxima)

### 4.1. Mercados disponibles

Solo existen **tres mercados de partido** por bookmaker, más **un mercado de jugador**:

| Mercado | ¿Existe? | Cómo aparece en los datos |
|---|---|---|
| 1X2 (moneyline 3 vías) | **Sí** | `home_ml` / `draw_ml` / `away_ml` (+ `_open`/`_close`) |
| Hándicap de goles (línea del local) | **Sí** | `home_handicap` (+ `_open`/`_close`), a veces con `handicap_home_odds`/`handicap_away_odds` |
| Total goles Over/Under | **Sí** | `total` (+`_open`/`_close`), `over_odds`, `under_odds` |
| BTTS (ambos marcan) | **No** | — |
| Doble oportunidad | **No** | — |
| Resultado al descanso | **No** | — |
| HT/FT | **No** | — |
| Over/Under córners | **No** | — |
| Over/Under tarjetas | **No** | — |
| Goles por equipo (team totals) | **No** (solo total global) | — |
| Mercado de jugador | Goleador anytime únicamente | `player_goal_scorer_anytime` (`over_odds`, `under_odds`) |

La doc lo condensa: *"No corners or cards markets. The one player market is the anytime goalscorer price (get_player_props)."*

**Formato de las cuotas:** **American odds** (ej. `105`, `-118`, `250`, `310`). Valores positivos/negativos en ambos sentidos. `get_odds` además devuelve `close_prob` = probabilidades sin margen, y `outcome` con `median_total`, `median_home_handicap`, `over`, `home_covered` (todas las cuotas de partido finalizados).

### 4.2. Bookmakers (casas de apuestas)

La cobertura varía por temporada y por fuente. Dos orígenes distintos:

**A) "Daily feed"** (el feed operativo actual):
- **2017-18 / 2018-19:** 2 casas.
- **2019-20 / 2020-21:** 6–8 casas.
- **2021-22 → 2023-24:** ~10 casas (entre ellas DraftKings, Caesars, BetMGM, Bet365, PointsBet, Unibet, Betfair Exchange).
- **Desde 2024-25:** 1–2 casas (Bet365 con ESPN BET o DraftKings).

**B) "Sportsbook odds archive"** (el archivo profundo, **desde junio-2020** en la mayoría de competiciones):
- Casas: **William Hill, DraftKings, Pinnacle, Marathonbet, 888sport, FanDuel** (desde jun-2020); **Unibet** (oct-2022); **Betfair Exchange** (abr-2023).
- Mediana de casas por partido en La Liga (archivo): 6 a 2021-22, 7 en 2022-23, 8 en 2023-24, 7 en 2024-25, 6 en 2025-26.
- UCL: mediana 4 casas hasta 2022-23, 8 en 2023-24/2024-25, 7 en 2025-26.

**Diferencias feed vs archivo:** el archivo es la **fuente más profunda y antigua** (apertura+cierre por casa desde jun-2020); el feed es actual/corto (1-2 casas desde 2024-25 y solo líneas de cierre antes de 2024). **Para histórico de cuotas conviene el archivo.**

### 4.3. Cuotas de apertura y cierre

- **Opening odds:** `home_ml_open`, `draw_ml_open`, `away_ml_open`, `total_open`, `home_handicap_open`.
- **Closing odds:** `home_ml_close`, etc.
- **Endpoint:** `get_odds` (con `game_id`, `date` o `season`). También `get_game` incluye `odds[]` por partido con `provider`, `source` (`archive`|`final`) y cuotas open/close. |y `get_line_movement` para el detalle temporal.
- **Reglas documentadas (importante / anti-fuga):**
  - `opening`: la **primera línea que conservamos** de una casa para el partido; *"which isn't always the book's very first posting"*.
  - `closing`: la **última línea antes del inicio**. En ligas top, ~10 min antes del inicio.
  - **Solo pre-partido**: se descarta todo lo capturado en/al inicio o después → no hay precios in-play.
  - **Antes de 2024** el feed solo guarda la línea final pre-partido (guardada como *close*). El archivo lo complementa con opening+closing desde **jun-2020**.
  - **Rangos temporales de apertura:** en las ligas top la apertura suele quedar 1–2 semanas antes del inicio.
- **Individual por partido:** sí, `get_odds` con `game_id`.
- **¿Cuotas anteriores a 2020?** No hay opening/close organizadas por casa antes de jun-2020; solo la línea final pre-partido (close) por casa, desde 2017-18 (y antes de 2017-18 **no hay líneas**).
- **Cobertura fiable por temporada:** ver §8. Sin líneas antes de 2017-18; 2017-18 solo ~52% (dato documentado para la **Premier**; para La Liga la doc afirma "lines from 2017-18" sin % exacto ⚠️).

### 4.4. Movimientos de cuotas (`get_line_movement`)

- **Qué devuelve:** por cada `provider`, un array de snapshots `{captured_at, home_ml, draw_ml, away_ml, home_handicap, total}`. Incluye `snapshots` (recuento).
- **Timestamps exactos:** sí, `captured_at` ISO-8601 UTC.
- **Histórico completo o snapshots:** snapshots. Por defecto se omiten capturas idénticas a la anterior (conservando siempre la última), lo que deja "cada movimiento y su hora"; con `all_snapshots=true` se obtienen todas las capturas horarias.
- **Frecuencia:** antes de 2026-10, **14–22 snapshots por partido** (desde 1–2 semanas antes hasta ~10 min antes del inicio). **Desde octubre-2026 cada línea se captura cada hora** para partidos de los próximos 4 días.
- **Por bookmaker:** `provider` (filtra una casa); el cuerpo ya está agrupado por casa.
- **Por mercado:** cada snapshot trae los tres mercados a la vez (ML, hándicap, total); no hay separación por mercado.
- **Reconstruir evolución temporal:** sí, ordenando por `captured_at` por casa se reconstruye local→cierre de cada cuota.
- **¿Cuotas anteriores al inicio?** Todas las snapshot son pre-inicio (se descartan in-play).
- **¿Distinguir apertura/medios/cierre?** Sí, por timestamps: el primer `captured_at`=apertura, el último antes de `starts_at`=cierre.
- **Profundidad histórica:** el archivo (desde jun-2020) y las capturas diarias desde el lanzamiento; antes de 2024 solo queda la línea final (close). `get_line_movement` está pensado para los partidos con snapshots (feed 2024+ / archivo 2020+).

---

## 5. Información de equipos y jugadores

### 5.1. Equipos

| Dato | Disponible | Endpoint |
|---|---|---|
| ID, abreviatura, nombre | Sí | `list_teams`, presente en todo listado de partidos |
| País, colores, logo | Parcial (colores/logo sí; país no se documenta) | `list_teams` |
| Competición | Implícita por endpoint de liga (`laliga`/`ucl`) | — |
| Temporada | Sí | `list_teams` |
| Clasificación histórica | ⚠️ Solo tabla final de temporada | `get_standings` |
| Victorias/empates/derrotas | Sí (final temporada) | `get_standings` |
| GF/GC | Sí | `get_standings`, `get_team_stats` |
| Local vs visitante | Sí (`home`/`away` en el partido; W-D-L local/visitante en `get_standings`; tabla no separada por local/visitante) | `get_standings`, `list_games` |
| Forma reciente | Sí (`form` string en `get_standings`; derivable de `list_games`) | `get_standings`, `list_games` |
| Ratings Elo | Sí (histórico por fecha) | `get_ratings` |

### 5.2. Jugadores

| Dato | Disponible | Endpoint |
|---|---|---|
| ID, posición, dorsal | Sí | `search_players`, `get_player` |
| Edad | **No se documenta** | — |
| Minutos jugados | Sí | `get_player`, `get_player_games` |
| Goles, asistencias | Sí | idem |
| xG, xA | xG sí; **xA no se documenta** | idem |
| Tiros / SOT | Sí | idem |
| Tarjetas | Sí | idem |
| Lesiones/ausencias | **No se documenta** | — |
| Alineaciones titulares / suplencias / minutos | Sí (por partido) | `get_lineups` (XI, banquillo, sustituciones con minutos), `get_player_games` (`starter`, `minute_on/off`, `minutes`) |
| Histórico | Datos por partido desde 2005-06 (box) / 2001-02 (alineaciones/goles) | `get_player_games`, `get_lineups` |

- **Tiempo real vs histórico:** lineups y box scores son históricos desde 2005-06; los eventos/xG desde 2012-13. No se documenta nada exclusivo de tiempo real salvo `get_schedule` (futuro) y la captura de líneas del feed.

---

## 6. Sistema Elo y ratings

- **Endpoint:** `get_ratings` (2 cr).
- **Tipo:** Elo **propio por competición** (cada competición tiene su propio rating; no es un Elo global cruzado entre ligas). Parámetros documentados: K=20, ventaja de local 60 puntos (nula en canchas neutrales), escalado por diferencia de goles, regresión de 1/3 hacia 1500 entre temporadas, 400 puntos = odds 10-a-1.
- **Histórico:** sí, desde 2001-02.
- **Antes de un partido:** `date` permite "ratings as of this day" → se puede obtener el rating previo a un partido concreto.
- **Local vs visitante:** el endpoint no separa por local/visitante (el método suma ventaja de local 60 al calcular, pero la salida es un rating por club).
- **Por competición:** sí, por endpoint de liga.
- **Evolución temporal:** la lista por `date`; se puede muestrear la evolución consultando varias fechas. Para reconstruir el rating en una fecha exacta, llamar con `date` = día previo al partido.
- **Anti-fuga:** viable, porque la llamada devuelve el rating tal como era en la fecha indicada. ⚠️ Confirmar que `date` corresponde al rating *antes* de los partidos de ese día (la respuesta incluye `as_of` y `last_game`, que marcan el último partido contabilizado).

---

## 7. Clasificaciones y contexto competitivo

- **Endpoint:** `get_standings` (2 cr / temporada). Cobertura 2002-03 → actual.
- **Campos:** `rank`, `played`, `wins`, `draws`, `losses`, `goals_for`, `goals_against`, `goal_diff`, `points`, `deductions`, `note` (clasif. europea), `home` (W-D-L), `away` (W-D-L), `form`.
- **⚠️ Clasificaciones por jornada:** **no existe**. `get_standings` devuelve la tabla **final publicada** de la temporada (`source: "published"`). No documenta un parámetro de jornada/fecha para "tabla a mitad de temporada".
- **Reconstruir la clasificación previa a un partido:** hay que **derivarla** acumulando los resultados de `list_games` (que sí devuelve todos los partidos con fecha y resultado) hasta la fecha previa del partido objetivo. Es trabajo de ingesta, no algo que devuelva la API.
- La UCL tiene "fase de grupos/league phase" y eliminatorias; `get_standings` aplica a la phase de liga. Para eliminatorias no hay tabla (contexto bilateral vía `get_head_to_head`).

---

## 8. Matriz de cobertura histórica — La Liga

**Fuente:** páginas `/la-liga`, `/docs` (Data coverage), `/docs/atlas`, `/champions-league`, `/data/soccer-odds-history`. Todas las afirmaciones sin año son "no documentado".

| Información | Temporada inicial | Temporada final | Cobertura | Endpoint |
|---|---|---|---|---|
| Partidos | 2001-02 | actual | Disponible | `list_games`, `get_schedule`, `get_head_to_head` |
| Resultados | 2001-02 | actual | Disponible | `list_games`, `get_game` |
| Estadísticas (box score por equipo) | 2005-06 | actual | Disponible (parcial antes: solo goles/tarjetas/sust. desde 2001-02) | `get_game`, `get_team_stats` |
| xG (tiros localizados) | 2012-13 | actual | Disponible | `get_events`, `get_shot_map`, `get_game`, `get_leaders`, `get_team_stats` |
| Cuotas 1X2 | 2017-18 | actual | Parcial (sin líneas <2017-18; cierre fiable; 2017-18 aprox. mitad ⚠️) | `get_odds`, `get_game`, `get_line_movement` |
| Over/Under total | 2017-18 | actual | Parcial (idem; `total`/`over_odds`/`under_odds`) | `get_odds` |
| BTTS | — | — | **No disponible** | — |
| Cuota de apertura | jun-2020 (archivo) / 2024 (feed) | actual | Parcial (antes de jun-2020 solo línea final pre-partido) | `get_odds` |
| Cuota de cierre | 2017-18 | actual | Disponible (pre-inicio) | `get_odds`, `get_game` |
| Movimiento de cuotas | 2021-22 (archivo) / 2024 (feed horario; oct-2026) | actual | Parcial (snapshots por archivo desde jun-2020; 14–22 por partido; horario desde oct-2026) | `get_line_movement` |
| Alineaciones (lineups) | 2002-03 (La Liga) | actual | Disponible | `get_lineups`, `get_game` |
| Elo (ratings) | 2001-02 | actual | Disponible | `get_ratings` |
| Jugadores | 2001-02 (perfil/alineaciones y goles) / 2005-06 (box) | actual | Disponible (parcial) | `search_players`, `get_player`, `get_player_games`, `get_leaders` |
| Clasificaciones | 2002-03 | actual | Disponible (solo tabla final de temporada) | `get_standings` |

**Leyenda:** Disponible / Parcial (disponible solo parte del rango o incompleto) / No disponible / Solo tiempo real (ninguno de los datos de partido es solo en tiempo real; `get_schedule` es solo futuro; la captura de líneas del feed es de tiempo real pero hay histórico de archivo).

**Rango "útil" para entrenar modelos (La Liga):** resultados desde 2001-02; box scores desde 2005-06; xG y eventos desde 2012-13; cuotas con apertura+cierre fiables desde 2020-21 (archivo), y solo cierre desde 2017-18. El cruce óptimo "todo junto" (resultado + box + xG + cuotas 1X2/total con apertura y cierre) es, en la práctica, **desde 2020-21** si se quiere apertura, o **desde 2012-13/2017-18** si se acepta sin apertura.

**UCL (para el presupuesto):** partidos desde 2001-02; xG/eventos desde 2012-13 (solo phase de liga+eliminatorias; clasificatorias sin box/shots); cuotas: "some matches from 2017-18, every match from 2020-21"; archivo desde ago-2020; Elo propio desde 2001-02.

---

## 9. Costes y límites de la API

### 9.1. Sistema de créditos

- **Plan gratuito:** 500 créditos/mes, sin tarjeta. Se rellenan mensualmente (no acumulan; se vuelven a "arriba" a 500 si el saldo es menor, ~3 días tras el aniversario).
- **Planes de pago:** Starter 10.000 cr/$19 (≈$0.0019/cr); Pro 30.000 cr/$49 (≈$0.0016/cr); Scale 125.000 cr/$149 (≈$0.0012/cr).
- **Wallet (recargas):** 400 cr por dólar ($5→2.000, $20→8.000, $50→20.000, $100→40.000, $200→80.000, $500→200.000). El wallet no expira mientras la cuenta esté abierta y se usa tras el mensual.
- Se descontó antes de ejecutar; **errores 500 = reembolso automático**; respuestas vacías = no cobran; misma llamada repetida en 2 min = cobra una vez.
- **No se documenta el saldo de una cuenta específica** (depende de cada cuenta). No puedo saber cuántos créditos tiene la cuenta del usuario.

### 9.2. Rate limits

- ≥100 créditos: **60 req/min**.
- <100 créditos: **10 req/min**.
- Mismos límites en todos los planes (protección de ráfaga).

### 9.3. Coste por endpoint

| Endpoint | Créditos/llamada |
|---|---|
| `list_teams`, `search_players` | 1 |
| `list_games`, `get_schedule`, `get_standings`, `get_ratings`, `get_head_to_head` | 2 |
| `get_game`, `get_lineups`, `get_player`, `get_player_games`, `get_leaders`, `get_team_stats`, `get_odds` | 5 |
| `get_events`, `get_shot_map`, `get_player_props` (10 pre-partido / **20 post** ⚠️ cuentas nuevas), `get_prop_history` | 10 |
| `get_line_movement` | 10 |

**Nota `get_odds` por temporada:** la doc de pricing y de `/data/soccer-odds-history` indica que **una temporada completa** (380 partidos) sale en ~4 llamadas paginadas y **1.900 créditos** (≈5 cr por partido; no 5 cr por llamada). Implica cobro **por partido** al usar `season`. ⚠️ Conviene verificar el cobro exacto con `estimate_only`/una llamada, pero es el dato oficial publicado.

### 9.4. Estimación de coste de descarga inicial

**Volumen de La Liga 2017-18 → 2025-26 (9 temporadas completas):** 417+385+(380×7)−5 = 3.452 partidos aprox. (2017-18:417, 2018-19:385, 2019-20..2025-26: 380 idem salvo 2022-23:375 → uso ~3.450).
**UCL 2017-18 → 2025-26:** 2017-18:219, 2018-19:216, 2019-20:210, 2020-21:125, 2021-22:125, 2022-23:125, 2023-24:125, 2024-25:189, 2025-26:189 = **1.523 partidos**.

Estimaciones (créditos) para los 6 bloques pedidos:

1. **Todas las jornadas de La Liga (id de partidos):** `list_games` paginado por temporada (~8 páginas de 50/temporada → ~70 llamadas) ≈ **140 cr**.
2. **Champions desde 2017-18:** ≈ **60 cr** (≈30 llamadas × 2).
3. **Estadísticas completas por partido** (`get_game`, 5 cr/partido): La Liga ~17.250 cr + UCL ~7.600 cr = **≈24.900 cr**. Alternativa barata: `get_odds`+`get_game` en doble barrido, o usar `get_team_stats` por temporada para features no por partido (mucho más barato).
4. **Cuotas históricas de todos los partidos** (`get_odds` por temporada, ~5 cr/partido): La Liga ~17.250 + UCL ~7.600 = **≈24.900 cr**. Si se limita a partir de 2020-21 (con apertura), se reduce ~1/3.
5. **Movimientos de cuotas** (`get_line_movement`, 10 cr/partido): La Liga ~34.500 + UCL ~15.200 = **≈49.700 cr** (solo donde existan snapshots; en la práctica menos, pues el archivo empieza en 2020/2021 y hay ~15 snapshots por partido dentro de una sola llamada).
6. **Alineaciones y ratings:**
   - `get_lineups` (5 cr/partido): La Liga ~17.250 + UCL ~7.600 = **≈24.900 cr**.
   - `get_ratings` (2 cr/llamada): una llamada por fecha/jornada devuelve el ranking completo de la liga. Para La Liga ~38 jornadas × 9 temporadas ≈ 342 llamadas ≈ **≈700 cr** + UCL (rondas). Muy barato frente a lo demás.

**Total inicial aprox. (full build):** sumando 1-6 ≈ **125.000–150.000 créditos**, dominado por `get_game` + `get_line_movement` + `get_odds` por partido. Con el plan **Scale (125.000 cr/mes, $149)** cabría en ~1 mes; con Starter (10.000/mes) exigiría ~12–15 meses o recargas de wallet.

**Estrategias para reducir coste:**
- Para features predictivos por partido, `get_game` ya trae box score + eventos clave + odds en **una sola llamada de 5 cr** (aunque `get_events` de 10 cr es más rico para tiros/xG).
- No pedir `get_events` + `get_shot_map` + `get_game` para el mismo partido si `get_game`/`get_events` bastan.
- Limitar el rango a partir de 2020-21 si se necesita apertura de cuotas (evita el rango 2017-2020 sin apertura).
- `get_ratings` y `get_standings` son muy baratos: aprovecharlos para features sin llamadas por partido.
- Reutilizar `get_prop_history` (10 cr) vs `get_player_props` (10/20 cr por partido) para evaluar mercado de goleador.

**Coste de mantenimiento diario (operativo):**
- Partidos recientes (p. ej. ~5–10 partidos/día en La Liga): `get_game`/`get_odds(date)`/`get_events`/`get_lineups`/`get_line_movement` ≈ 5+10+10+5+10 = 40 cr/partido → ~**200–400 cr/día** para La Liga (≈6.000–12.000 cr/mes). Con captura horaria vía `get_line_movement` (oct-2026) conviene programar snapshots para los próximos 4 días.
- El `get_odds` **por día** cuesta 10 cr (frente a 5 por partido): si se ingiere día a día, calcular bien el punto de equilibrio vs por partido.
- el archivado de props tras jugarse pasa a 20 cr con las cuentas nuevas.

---

## 10. Calidad e integridad de los datos

Riesgos detectados (documentados) y mitigaciones propuestas:

| Problema | Evidencia en la doc | Mitigación |
|---|---|---|
| Sin historial de revisiones | *"No. We keep the current version of each record... the record is overwritten... we don't keep the earlier version, a change log per record, or a revision id."* | Descargar en el momento correcto (antes de que la fuente corrija); comparar el partido días después; guardar `captured_at`/fecha de ingesta propia. |
| xG no as-of (revalorizado al refitear) | *"A refit revalues every shot. For strict out-of-sample work, fit your own model on the shot locations."* | Para features entrenados, usar localizaciones de tiro y xG **calculada por el propio proyecto** (o fijar la versión del modelo usado). No asumir xG estable en el tiempo. |
| Cuotas inexistentes / cobertura irregular de casas | Nº de casas varía por temporada (2 → ~10 → 1–2); archivo desde 2020 | Medir la mediana/el nº de casas por partido; registrar `books`; tratar `total`/`handicap` nulos o ausentes. Un precio ausente = falta de posting, no un error. |
| Partidos sin xG | xG solo desde tiros localizados (2012-13); antes `null`; *"A stat not charted is null, never 0"* | No imputar 0; usar `null` y modelar la ausencia. |
| Resultados vs estadísticas | Box scores desde 2005-06; antes solo goles/tarjetas/sust. contadas de eventos | Cruce: verificar que `games[].home.score/away.score` coincida con la suma de goles de los eventos. |
| Partidos aplazados/suspendidos/cancelados | Solo `status=scheduled|final` documentado; sin código explícito | Filtrar partidos con `status=final` pero `score` nulo o `attendance` nula; revision manual de outliers en la ingesta. |
| Partidos duplicados / cambio de IDs | `game_id` es el ID; no se documenta estabilidad entre refetch | Dedicar sobre `game_id` (clave primaria); guardar el mapping `game_id` inicial. |
| Zonas horarias | `date`/`starts_at` en **UTC** | Normalizar todo a UTC; para reconstruir "antes del partido" comparar `captured_at` con `starts_at`. |
| Cambio de nombres de equipo | `list_teams` por temporada da nombres/IDs por temporada | Mapear `team_id` (estable) a nombre en cada temporada; usar `team_id` como clave, no el nombre. |
| Cambio de competición/reestructuración | La UCL 2024-25 cambia a formato suizo/league-phase; fases varían | Usar `stage` y `season`; documentar el cambio de formato en el dataset. |
| Cuotas de cierre ≠ última consulta | *"closing is the last capture before the start"*; para filas `source=final`, `captured_at` es cuándo se obtuvieron, no cuándo se fijo el precio | Tratar `source=archive` (snapshot real) de forma distinta a `source=final` (riesgo de fuga/timing). |
| Antes de jun-2020 sin apertura | *"Before June 2020 there is no opening line, only the final pre-match line"* | No usar `_open` en ese rango; usar solo `_close`. |
| Clasificación solo final | `get_standings` = tabla publicada final | Derivar la tabla acumulada desde `list_games` para fechas intermedias. |

---

## 11. Prevención de data leakage

El proyecto debe simular predicciones "antes del inicio". Los endpoints que permiten obtener datos **con timestamp** son los de cuotas y lineups; para el resto hay que fijar la "fecha de conocimiento".

**Datos que se pueden conocer realmente antes del partido (§ endpoints):**

| Feature | ¿Previo al partido? | Endpoint / método | Notas anti-fuga |
|---|---|---|---|
| Cuotas de apertura/cierre por casa | **Sí** (todas pre-inicio) | `get_odds`, `get_game.odds[]`, `get_line_movement` | `captured_at` < `starts_at`; distinguir `source=archive` vs `final`. |
| Serie temporal de cuotas | **Sí** (snapshots pre-inicio) | `get_line_movement` | primera=opening, última=closing; todo < `starts_at`. |
| Goleador anytime (props) | **Sí** (una captura pre-inicio) | `get_player_props`, `get_prop_history` | sin apertura ni movimiento; ≈9 min antes del inicio (o 15:00/20:00 UTC desde oct-2026). |
| Elo previo | **Sí** | `get_ratings` con `date` = día anterior | la API devuelve `as_of` y `last_game`. |
| Alineaciones confirmadas | **⚠️ Marginal** | `get_lineups` | la doc dice que antes del inicio lee la alineación anunciada "usually about an hour before". Para histórico pre-2010 el anuncio pudo ser más tarde; tratar como riesgo. |
| Resultados/estadísticas previos del equipo | **Sí** (solo partidos ya terminados) | `list_games`, `get_game`, `get_team_stats`, `get_player_games` | filtrar por `starts_at` < partido objetivo. |
| Forma / tabla previa | **Derivable** | `get_standings` + `list_games` | `get_standings` es final; derivar acumulado. |
| Posesión/tiros/xG previos | **Sí**, si de partidos anteriores | `get_game`, `get_team_stats` | solo partidos ya jugados. |

**Datos GENERADOS durante o después (NO usar antes del inicio como feature):**
- `team_stats.xg`, `shots`, `possession`, etc., del propio partido objetivo.
- `get_events` del propio partido objetivo.
- `get_shot_map` del propio partido objetivo (aunque el xG esté guardado, ver §10 xG no as-of).
- Resultado/marcador del propio partido.

**Reglas de oro:**
1. **Cortar por `starts_at`:** ninguna feature con `captured_at`/fecha ≥ `starts_at` del partido.
2. **xG:** no usar el xG almacenado por StrikerAPI como feature as-of (es revalorizable); si se usa, recalcular con modelo del proyecto o acotar la versión.
3. **Doble fuente de cuotas:** archivo vs feed (`source`), porque en `source=final` el `captured_at` no es fiable para timing.
4. **Clasificación:** derivarla de `list_games` acumulado hasta la fecha previa; no usar `get_standings` (final de temporada).
5. **Elo:** usar `get_ratings(date)` del día previo; verificar `last_game` para que el rating no incluya el partido objetivo.

---

## 12. Recomendaciones para la futura arquitectura de ingesta

1. **Estratificación temática:** separar la ingesta en (a) partidos/resultados, (b) box scores/eventos, (c) cuotas (1X2/total/hándicap + movement), (d) lineups, (e) elo/clasificación, (f) props. Permite pagos parciales e independencias de fallo.
2. **Claves primarias estables:** `game_id`, `team_id`, `player_id` como llaves; `(game_id, provider)` para cuotas; `(game_id, player_id, provider)` para props. Evitar el uso de nombres.
3. **Normalizar a UTC** y guardar siempre `starts_at` y `captured_at` origen.
4. **Versionado de la ingesta:** guardar `ingested_at` local; re-descargar partidos recientes 2–3 días después (la doc indica que StrikerAPI re-obtiene eventos ~1,5 días tras el final) y detectar divergencias (ya que no hay revisiones).
5. **No usar el xG oficial como feature predicho as-of:** descargar localizaciones de tiro (`get_events`/`get_shot_map`) y ajustar xG propio si se necesita estricta validez out-of-sample.
6. **Cuotas:** priorizar el **sportsbook odds archive** (apertura+cierre por casa desde jun-2020 en La Liga; desde ago-2020 en UCL). Para 2017-2020 usar solo cierre. Registrar `source` y nº de casas por partido.
7. **Ingesta diaria estándar:** capturar `get_odds(date)` + `get_line_movement` de los próximos ~4 días (captura horaria desde oct-2026) para no depender del archivo para partidos nuevos; bajar `get_game`/`get_events`/`get_lineups` tras el partido.
8. **Control de costes:** optimizar el orden de llamadas (evitar `get_events` + `get_game` + `get_shot_map` superpuestos); usar `get_team_stats` y `get_ratings`/`get_standings` como features baratas; empezar el build en 2020-21 si se quiere cuotas con apertura.
9. **Gestión de errores:** reintentar con idempotency key (`x-idempotency-key`/`x-request-id`) en 500; respetar 60 req/min (bajar a 10 si saldo <100); tratar 402 como pausa de recarga.
10. **Presupuesto:** separar el coste de **backfill histórico** (grande, puntual; extremos = `get_line_movement`+`get_game`) del **mantenimiento diario** (moderado; ver §9.4) y planificar el plan/pago de créditos acorde.

---

## Anexo A. Referencias a páginas oficiales

- Recomendación/primera llamada, endpoints, rate limits, errores, cobertura, formatos: `https://strikerapi.com/docs`
- Cobertura por dataset y cuotas snapshot (opening/closing/books): `https://strikerapi.com/docs#data-coverage`
- `list_teams`, `search_players`: `/docs/list-teams`, `/docs/search-players`
- `list_games`, `get_schedule`, `get_standings`, `get_ratings`, `get_head_to_head`: `/docs/list-games`, `/docs/get-schedule`, `/docs/get-standings`, `/docs/get-ratings`, `/docs/get-head-to-head`
- `get_game`, `get_lineups`, `get_player`, `get_player_games`, `get_leaders`, `get_team_stats`, `get_odds`: `/docs/get-game`, `/docs/get-lineups`, `/docs/get-player`, `/docs/get-player-games`, `/docs/get-leaders`, `/docs/get-team-stats`, `/docs/get-odds`
- `get_events`, `get_shot_map`, `get_player_props`, `get_prop_history`: `/docs/get-events`, `/docs/get-shot-map`, `/docs/get-player-props`, `/docs/get-prop-history`
- `get_line_movement`: `/docs/get-line-movement`
- Tiempos y derechos (timestamps, apertura/cierre, xG, revisiones, uso de datos): `/docs/timing-and-rights`
- "Find the right call" (coste por llamada): `/docs/find-the-right-call`
- Atlas de datos (cobertura por dataset, bookmakers): `/docs/atlas`
- La Liga (cobertura por temporada, nº partidos, qué no tiene): `/la-liga`
- UCL (cobertura, clasificatorias, cuotas): `/champions-league`
- Precios y créditos (planes, wallet, coste por endpoint, ejemplo "Historical odds 2025-26 = 4 calls / 1.900 cr"): `/pricing`
- Cuotas históricas (cobertura por temporada, casas por temporada, qué no hace): `/data/soccer-odds-history`