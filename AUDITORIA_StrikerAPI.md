# Auditoría Completa de StrikerAPI
Para el proyecto de dataset histórico de predicción de apuestas (La Liga + UEFA Champions League)

**Fecha de auditoría:** 2026-10-30
**Fuentes:** Documentación oficial en https://strikerapi.com/docs (incluidos `/docs/{tool}`, `/la-liga`, `/champions-league`, `/docs/timing-and-rights`)
**Estado:** Investigación y documentación únicamente. No se ha implementado código de ingesta.

---

## 0. Descripción general de la plataforma

StrikerAPI es una API de datos de fútbol que sirve 17 competiciones mediante REST y MCP. Para este proyecto son relevantes:

| Competición | Clave `league` | Cobertura de resultados | Cobertura de tiros/xG | Líneas de cuotas |
|---|---|---|---|---|
| **La Liga** | `laliga` | 2001-02 → | 2012-13 → | 2017-18 → (archivo completo de cuotas desde jun-2020) |
| **UEFA Champions League** | `ucl` | 2001-02 → (todas las fases) | 2012-13 → (solo fase de grupos/knockout) | 2017-18 → (todos desde 2020-21) |

### Datos relevantes por competición
- **La Liga**: 9.635 partidos, 619.373 eventos, 6.006 jugadores, 26 temporadas.
- **UCL**: 4.915 partidos, 227.569 eventos, 15.995 jugadores, 26 temporadas.

### Acceso
- **Base URL:** `https://mcp.strikerapi.com`
- **REST:** `POST /v1/{tool}` con body JSON. También `POST /v1/{league}/{tool}` (ej. `/v1/laliga/list_games`, `/v1/ucl/get_odds`).
- **MCP:** `POST /mcp` (Streamable HTTP). Correo: `https://mcp.strikerapi.com/mcp/laliga` y `/mcp/ucl`.
- **Catálogo público:** `GET /v1/tools`, especificación OpenAPI en `/openapi.json`.
- **Autenticación:** `x-api-key: <key>` o `Authorization: Bearer <key>` o `?key=` (MCP).

### Convenciones
- **Temporadas:** El año en que empieza la temporada (2025 = 2025-26). Para La Liga y UCL aplica esto. Las ligas de calendario (MLS, NWSL, Liga MX, Brasil, Argentina) usan el año natural.
- **Tipos de cambio de cuotas:** `season` por año de inicio. `stage`: `league` (liga/grupos/league phase) o `knockout` (clasificatorias, eliminatorias, playoffs).
- **Game ID:** ID de partido (ej. `704345`), obtenible con `list_games` o `get_schedule`.
- **Paginación:** Aunque la doc usa `limit`/`offset`, en la práctica los endpoints devuelven `next_offset` que se debe reenviar como `offset`. No hay cursor.

### Límites y errores
- **Rate limits:** 60 req/min si balance ≥ 100 créditos; 10 req/min si balance < 100 créditos.
- **Créditos:** Se descuentan antes de ejecutar; se reembolsan en errores de servidor (500). Las respuestas vacías no se cobran. La misma llamada repetida en 2 minutos se cobra una vez.
- **Códigos de error:**
  - `400` Parámetros inválidos
  - `401` API key inválida/faltante
  - `402` Créditos insuficientes
  - `429` Límite de peticiones excedido
  - `500` Error de servidor (reintentar con idempotency key)
- **Idempotencia:** Cabeceras `x-idempotency-key` o `x-request-id` para reintentos sin duplicados.
- **Respuestas REST:** Todo éxito envuelto en `{ "data": { ... } }`; errores `{ "error": ... , "message": ... }`. Cabeceras `X-Credits-Used` y `X-Credits-Remaining`.

### Holgura temporal / derechos (clave para backtesting)
- **Timestamp `captured_at` (cuotas):** Para filas de archivo (source "archive") es la hora del snapshot archivado. Las ligas top tienen una serie por casa, 14-22 snapshots por partido (desde 1-2 semanas antes hasta ~10 min antes del inicio). Para filas `source="final"` es cuando se obtuvieron, no cuando se fijó el precio.
- **Líneas solo pre-partido:** Todo lo capturado en el momento del inicio o después se descarta. No hay precios en vivo (in-play).
- **Apertura/cierre:** `opening` = primera captura que se tiene para un partido+casa (no siempre la primera publicación real); `closing` = última captura antes del inicio (normalmente ~10 min antes).
- **Props de goleadores:** capturas pre-partido (~9 min antes desde 2026; antes ~15:00 y 20:00 UTC día de partido).
- **Alineaciones:** pre-partido se lee live del anuncio oficial (~1 hora antes); tras el partido queda guardada con el resultado.
- **Eventos y box scores:** se actualizan 1 vez/día a las 10:00 UTC. El partido se re-consulta durante ~1,5 días tras terminar.
- **xG NO es as-of:** El modelo xG se ajusta sobre todas las temporadas (incluidas posteriores) y puede revalorar todos los tiros. Para backtesting estrictamente out-of-sample hay que ajustar el propio modelo sobre las localizaciones de tiro.
- **Sin historial de revisiones:** solo se guarda la versión actual de cada registro.

**Implicación critical para ML:** Para evitar fuga de datos, debe filtrare los partidos cuyo resultado se conoce y usar solo el snapshot `closing` (o `opening`) de cuotas capturado ANTES del inicio. El xG de la API es "as-of" y se debe tratar con cuidado (o recalcular).

---

## 1. Inventario completo de endpoints

Los **19 endpoints** se dividen en 5 categorías con coste fijo en créditos. Todos usan `POST /v1/{tool}`.

### Referencia estática (1 crédito)
| Endpoint | Descripción |
|---|---|
| `list_teams` | IDs de club, abreviaturas y colores para una temporada |
| `search_players` | Búsqueda de jugadores por nombre |

### Búsqueda básica (2 créditos)
| Endpoint | Descripción |
|---|---|
| `list_games` | **Fixtures y resultados** por temporada, etapa, fecha, rango o club |
| `get_schedule` | Próximos fixtures con la línea 1X2 mediana entre casas |
| `get_standings` | Clasificación (puntos, deducciones, GF/GC, casa/fuera, forma) |
| `get_ratings` | Ratings Elo de clubes |
| `get_head_to_head` | Cara a cara entre dos clubes |

### Estadísticas + agregación (5 créditos)
| Endpoint | Descripción |
|---|---|
| `get_game` | **Un partido**: marcador, ambos box scores, línea de cada jugador, eventos clave, línea de cada casa |
| `get_lineups` | Formaciones, XI inicial, banquillos y sustituciones con minutos |
| `get_player` | Perfil, totales por temporada y tasas por 90, últimos 10 partidos |
| `get_player_games` | Registro completo de partidos de un jugador |
| `get_leaders` | Líderes de temporada (goles, asistencias, tiros, xG, tarjetas, paradas) |
| `get_team_stats` | **Medias por partido** (goles, xG, tiros, posesión, pases, disciplina, a favor/en contra) |
| `get_odds` | **Cuotas 1X2, hándicap y total por casa** para un partido (5) o un día (10), con resultados |

### Multi-tabla (10 créditos)
| Endpoint | Descripción |
|---|---|
| `get_events` | **Eventos del partido**: goles, tarjetas, sustituciones, tiros con ubicación y xG, faltas, córners |
| `get_shot_map` | Mapa de tiros para club, jugador o defensa |
| `get_player_props` | Cuota de "anotar anytime" por casa |
| `get_prop_history` | Cuota de goleador vs si marcó, partido a partido |

### Serie temporal (10 créditos)
| Endpoint | Descripción |
|---|---|
| `get_line_movement` | Snapshots de la línea de cada casa antes del inicio (movimiento) |

---

## 2. Detalle de los endpoints prioritarios

### 2.1 `list_games` — 2 créditos
**Función:** Partidos y resultados por temporada, etapa, fecha, rango o club. **Es el punto de entrada principal del pipeline.**

**Parámetros (todos opcionales):**
- `season` (number|string): año de inicio. Default: última.
- `stage` (enum `league | knockout`): `league` = liga/fase grupos/league phase; `knockout` = clasificatorias, eliminatorias, playoffs.
- `team` (string): id, abreviatura o nombre del club.
- `date` (string): `YYYY-MM-DD` (UTC).
- `from` / `to` (string): rango de fechas `YYYY-MM-DD`.
- `status` (enum `scheduled | final`).
- `limit` (integer): max partidos (default 50).
- `offset` (integer): desplazamiento de página; usar `next_offset`.

**Campos de respuesta clave:** `game_id`, `season`, `stage`, `date`, `starts_at` (ISO 8601 UTC), `status`, `status_detail` (p.ej. "FT", "Mon, October 19th at 3:00 PM EDT"), `home/away` (`.team_id`, `.abbrev`, `.name`, `.score`), `result` (`H`/`A`/`D`), `neutral_site`, `venue`, `city`, `attendance`. Incluye `count` y `next_offset`.

**Local/visitante y resultado:** `home`/`away` identifican el local/visitante; `result` da el ganador (H/A/D). `neutral_site` marca finales u otros partidos en sede neutral (relevante en UCL).

**Prórroga, penaltis, suspendidos:** `list_games` solo expone el marcador final a tiempo reglamentario y `status_detail` ("FT"). Para detectar prórroga/tanda de penaltis hay que usar `get_game` (ver nota) o `get_events` donde los eventos llevan `shootout` (booleano) y `penalty`. La documentación indica que los partidos UCL tienen "extra time and shootouts" y que cada evento de gol tiene `shootout: false/true`. No se documenta un flag explícito de "partido suspendido/cancelado" más allá de `status` (scheduled/final): los suspendidos aparecerían como `scheduled` o sin resultado completo.

**Nota sobre resultados al descanso:** No existe un campo directo de "HT result" en `list_games`/`get_game`. El marcador al descanso se puede reconstruir filtrando los eventos/goles del primer tiempo (minuto ≤ 45) vía `get_events` (campo `period`). El campo `period: 1` identifica la primera parte.

**Dependencias:** devuelve `game_id` para `get_game`, `get_events`, `get_odds`, `get_line_movement`, `get_lineups`, `get_shot_map`; devuelve `team` para `get_team_stats`, `get_ratings`.

### 2.2 `get_game` — 5 créditos
**Función:** Todo un partido en una llamada: marcador, box scores de ambos equipos (con xG), línea de cada jugador, eventos clave y línea de cada casa de apuestas.

**Parámetro:** `game_id` (string, **obligatorio**).

**Campos clave:**
- `game`: marcador, local/visitante, result, venue, attendance.
- `team_stats[]`: por equipo → `goals`, `xg`, `possession_pct`, `shots`, `shots_on_target`, `blocked_shots`, `corners`, `fouls`, `yellow_cards`, `red_cards`, `offsides`, `saves`, `passes`, `accurate_passes`, `crosses`, `accurate_crosses`, `long_balls`, `accurate_long_balls`, `tackles`, `effective_tackles`, `interceptions`, `clearances`, `penalty_goals`, `penalty_attempts`.
- `players[]`: línea de cada jugador (`goals`, `assists`, `shots`, `shots_on_target`, `fouls_committed`, `fouls_suffered`, `offsides`, `yellow_cards`, `red_cards`, `own_goals`, `saves`, `goals_conceded`, `shots_faced`, posiciones, minutos).
- `key_events[]`: goles, tarjetas, con minuto, jugador, asistente, `own_goal`, `penalty`, `shootout`, outcome y texto.
- `odds[]`: cuotas por casa (ver sección 4).

**Nota:** Es el endpoint más rico por partido y de coste 5 créditos. Para un dataset masivo histórico, combinar `get_game` (box scores + cuotas) o alternativamente `get_events` + `get_odds`.

### 2.3 `get_odds` — 5 créditos (partido) / 10 créditos (día)
**Función:** Las cuotas 1X2, hándicap de goles y total de cada casa, con resultados y probabilidades sin margen.

**Parámetros (todos opcionales):**
- `game_id`: un partido.
- `date`: los partidos de un día (`YYYY-MM-DD` UTC).
- `season` (number|string): una temporada de partidos con cuotas.
- `team` (string): con `season`, los partidos de un club.
- `limit` / `offset`: con `season`, paginación (default limit 50).

**Campos por casa (`books[]`):** `provider`, `source` (`archive` | `final`), `home_ml_open`, `home_ml_close`, `draw_ml_open`, `draw_ml_close`, `away_ml_open`, `away_ml_close`, `close_prob` (probabilidades sin margen), `home_handicap_open`, `home_handicap_close`, `handicap_home_odds`, `handicap_away_odds`, `total_open`, `total_close`, `over_odds`, `under_odds`, `captured_at`.

**Campo `outcome`** (derivado, muy útil): `result` (H/A/D), `median_total`, `median_home_handicap`, `over` (bool: Over y/o total superado), `home_covered` (bool: el local cubrió el hándicap).

**Formato de cuotas:** Americano (`-118`, `+300`). Los moneylines 1X2 como enteros américanos; `over_odds/under_odds` también americanos; `total` en goles. `close_prob` son las probabilidades sin margen.

**Nota de coste:** por partido cuesta 5; por día 10. Para ingesta histórica por temporada, `get_odds` con `season`+`team` permite barrer partidos con cuotas de una vez.

### 2.4 `get_line_movement` — 10 créditos
**Función:** Snapshots del movimiento de línea de cada casa en los días previos al inicio.

**Parámetros:**
- `game_id` (**obligatorio**).
- `provider` (string): una sola casa (ej. `DraftKings`).
- `all_snapshots` (boolean): `true` = todas las capturas horarias; default omite capturas idénticas a la anterior (mantiene los movimientos y su hora).

**Respuesta:** objeto `movement` con una clave por casa; cada captura: `captured_at`, `home_ml`, `draw_ml`, `away_ml`, `home_handicap`, `total`. Campo final `snapshots` (total de capturas).

**Uso:** Para reconstruir la curva de evolución de cuotas pre-partido (útil para features tipo "steam move"/movimiento de línea). Es caro (10 créditos/partido): para dataset masivo solo conviene en una muestra.

### 2.5 `get_events` — 10 créditos
**Función:** Eventos del partido en orden cronológico: goles, tiros con ubicación/xG, tarjetas, sustituciones, faltas, córners.

**Parámetros:**
- `game_id` (**obligatorio**).
- `kind` (enum `goals | shots | cards | substitutions | set_pieces | fouls | all`). Default `all`.
- `limit` (default 300) y `offset`.

**Campos por evento (`events[]`):** `seq`, `minute`, `added_minute`, `period` (1/2), `type`, `team_id`, `team`, `player_id`, `player`, `player2_id`/`player2` (asistente), objeto `shot` (si aplica): `outcome`, `body_part`, `penalty`, `xg`, `zone`, `x`, `y`, `end_x`, `end_y`, `distance_m`, `metres`. Además `is_goal`, `own_goal`, `shootout`, `text`.

**Nota:** Es la fuente para reconstruir tiros, faltas, córners, y para detectar penaltis (`shot.penalty`, `key_events` de gol con `penalty`) y tandas de penaltis (`shootout`).

### 2.6 `get_lineups` — 5 créditos
**Función:** Formaciones, XI inicial, banquillos y sustituciones con minutos.

**Parámetro:** `game_id` (**obligatorio**).

**Campos:** por equipo: `team_id`, `abbrev`, `formation` (ej. `4-2-3-1`), `starting_xi[]` (player_id, name, jersey, position, formation_place, minute_on/off, goals, assists, cards), `substitutes_used[]`, `unused_substitutes[]`.

### 2.7 `get_team_stats` — 5 créditos
**Función:** Medias por partido de cada club en una temporada (a favor y en contra).

**Parámetros (opcionales):** `season`, `team`.

**Campos (`teams[]`):** `team_id`, `abbrev`, `display_name`, `games`, `goals_for`, `goals_against`, `xg_for`, `xg_against`, `shots_for`, `shots_against`, `shots_on_target_for`, `shots_on_target_against`, `possession_pct`, `pass_accuracy_pct`, `passes`, `corners`, `fouls`, `yellow_cards`, `red_cards_total`, `tackles`, `interceptions`, `clean_sheets`, `failed_to_score`.

**Nota:** Añade el campo `method` explicando que es la media sobre todos los partidos finales de la temporada (liga y copa), y que "a stat not charted for a match is null, never 0". La posesión es null para algunas ligas/temporadas.

### 2.8 `get_ratings` — 2 créditos
**Función:** Ratings Elo de clubes, ordenados, hoy o a una fecha.

**Parámetros (opcionales):** `date` (`YYYY-MM-DD`), `limit`.

**Campos:** `as_of`, `ratings[]` (rank, team_id, abbrev, name, elo, last_game), `method`.

**Nota sobre Elo de UCL:** "Ratings are rated from Champions League matches alone" para league `ucl`. El método: K=20, ventaja de casa 60 puntos (0 en sede neutral), escalado por diferencia de goles, retorno parcial a 1500 entre temporadas. **Útil como feature de fuerza de equipos pre-partido.**

### 2.9 `get_standings` — 2 créditos
**Función:** Tabla oficial de la competición (puntos, deducciones, GF/GC, casa/fuera, forma).

**Parámetro (opcional):** `season`.

**Campos (`standings[]`):** `group_name`, `rank`, `team_id`, `abbrev`, `display_name`, `played`, `wins`, `draws`, `losses`, `goals_for`, `goals_against`, `goal_diff`, `points`, `deductions`, `note` (p.ej. "Champions League"), `home` (W-D-L), `away` (W-D-L), `form` (últimos 5, latest first). Campos `source` y `note`.

**Disponibilidad:** 2002-03 → actual para la tabla publicada.

---

## 3. Estadísticas disponibles por partido

### 3.1 Box score de equipo (`get_game.team_stats`)
Disponible **por ambos equipos**, **agregado a nivel de equipo**. Desde 2005-06 en las ligas europeas (en Premier desde 2001-02); antes solo goles/asistencias/tarjetas contados de los eventos y el resto `null`. Funciona para partidos históricos.

| Estadística | Campo | Presente en respuesta de ejemplo |
|---|---|---|
| Goles | `goals` | Sí |
| Expected Goals (xG) | `xg` | Sí |
| Tiros totales | `shots` | Sí |
| Tiros a puerta | `shots_on_target` | Sí |
| **Tiros fuera** | NO expuesto directamente como "shots off target" | Se deduce: `shots - shots_on_target - blocked_shots` |
| Tiros bloqueados | `blocked_shots` | Sí |
| Tiros dentro/fuera del área | NO como recuento directo; sí por zona vía `get_events`/`get_shot_map` (zones `six_yard_box`, `penalty_area`, `outside_box`) | vía shot map |
| Posesión | `possession_pct` | Sí (null en algunas ligas/temporadas) |
| Córners | `corners` | Sí |
| Faltas | `fouls` | Sí |
| Tarjetas amarillas | `yellow_cards` | Sí |
| Tarjetas rojas | `red_cards` | Sí |
| Paradas | `saves` | Sí |
| Grandes ocasiones / creadas y falladas | NO disponible explícitamente | Se puede aproximar con xG o tiros de alta calidad desde `get_events` |
| Pases | `passes` | Sí |
| Precisión de pases | `accurate_passes` (y `pass_accuracy_pct` en `get_team_stats`) | Sí |
| Fueras de juego | `offsides` | Sí |
| Duelos ganados | NO expuesto como "duels"; disponible `tackles`/`effective_tackles` | Parcial |
| Centros | `crosses` y `accurate_crosses` | Sí |
| Entradas | `tackles` / `effective_tackles` | Sí |
| Recuperaciones | `interceptions` (+ `clearances`) | Sí |

### 3.2 Medias de temporada (`get_team_stats`) — agregado
Da medias por partido a favor y en contra: `goals_for`, `goals_against`, `xg_for`, `xg_against`, `shots_for`, `shots_against`, `shots_on_target_for`, `shots_on_target_against`, `possession_pct`, `pass_accuracy_pct`, `passes`, `corners`, `fouls`, `yellow_cards`, `red_cards_total`, `tackles`, `interceptions`, `clean_sheets`, `failed_to_score`. Disponible por club por temporada (suma de todos los partidos finales de la temporada, liga y copa; xG null donde no hay tiros ubicados).

### 3.3 Por jugador (`get_game.players`, `get_player_games`, `get_player`)
Líneas por jugador: goles, asistencias, tiros, tiros a puerta, faltas cometidas/sufridas, fueras de juego, tarjetas, autogoles, paradas, goles encajados, tiros recibidos, minutos. Y `get_leaders` para líderes de temporada (goles, asistencias, goal_contributions, tiros, shot on target, xG, tarjetas amarillas/rojas, paradas, clean sheets; opción `per90`).

### 3.4 Tiros y xG por evento (`get_events`, `get_shot_map`)
Cada tiro lleva: `outcome` (goal/missed/blocked/saved...), `body_part`, `penalty`, `xg`, `zone` (six_yard_box/penalty_area/outside_box), `x`/`y` (coordenadas normalizadas), `end_x`/`end_y`, `distance_m`. 
- **Disponibilidad:** xG/locación de tiros completo desde 2012-13 (La Liga, UCL fase de grupos y knockout). Antes de 2012-13 solo los goles llevan ubicación.
- `get_shot_map` agrega zonas (tiros, goles, on_target, headers, xG, conversión, share) y totales.

### 3.5 Cobertura temporal y por competición (resumen para ML)
| Dato | La Liga | UCL |
|---|---|---|
| Resultados/alineaciones/goles | 2001-02 → | 2001-02 → (todas las fases) |
| Box scores completos | 2005-06 → | box scores solo fase grupos/knockout desde 2012-13; clasificatorias finas (alineaciones y goles) |
| Tarjetas en eventos | 2011-12 → | 2011-12 → |
| Tiros ubicados + xG | 2012-13 → | 2012-13 → (solo grupos/knockout) |
| Tabla | 2002-03 → | fases de grupos/league phase |
| Elo | 2001-02 → | 2001-02 → (solo partidos UCL) |
| Cuotas | 2017-18 → | 2017-18 → (todos desde 2020-21) |

---

## 4. Cuotas de apuestas (PRIORIDAD MÁXIMA)

### 4.1 Qué mercados ofrece realmente la API
La cobertura real de mercados es **más limitada** de lo que a priori se pediría. Mercados confirmados disponibles:

| Mercado | Endpoint | Formato | Notas |
|---|---|---|---|
| **1X2 (moneyline 3 vías)** | `get_odds`, `get_game`, `get_line_movement`, `get_schedule` | Americana (home/draw/away) + `close_prob` sin margen | El mercado principal |
| **Hándicap de goles** (goal handicap, no "asiático" con +0.25) | `get_odds`, `get_line_movement` | `home_handicap_close` + `handicap_home_odds`/`handicap_away_odds` | Algunas casas solo 3 vías |
| **Over/Under total de goles** | `get_odds`, `get_line_movement` | `total` + `over_odds`/`under_odds` | Presente en Bet 365, Pinnacle, William Hill de los ejemplos |
| **Anytime goalscorer** (goleador) | `get_player_props`, `get_prop_history` | `over_odds` (Yes) y opcional `under_odds` (No) | Único mercado de jugador |

**Mercados que NO se ofrecen directamente:** hándicap asiático con líneas divididas (0.25/0.75), hándicap europeo como mercado separado, doble oportunidad, resultado al descanso, resultado final combinado, over/under de córners, over/under de tarjetas, goles por equipo, goles al descanso, mercados de jugadores distintos del goleador (asistencias, tiros a puerta, tarjetas, etc.). Estos se podrían **derivar** de las estadísticas (p.ej. total de córners = `corners` local + visitante; o resultado al descanso = goles del `period 1` en `get_events`), pero StrikerAPI **no publica su cuota**.

**Conclusión clave:** Para este proyecto las cuotas utilizables directamente son **1X2, over/under total y hándicap de goles**, más **anytime goalscorer** como mercado de jugador adicional.

### 4.2 Bookmakers (casas) disponibles
La cobertura varía según la competición, la temporada y la fuente:

**Feed diario (widget `get_odds`, `get_line_movement`, `get_schedule`):**
- Alrededor de **10 casas por partido de 2021-22 a 2023-24** (incluidos DraftKings, Caesars, BetMGM, Bet365, PointsBet, Unibet).
- Desde 2024-25: normalmente **1-2 casas por partido** (Bet365 con ESPN BET o DraftKings).
- De 2017-18 a 2018-19: solo 2 casas. 2019-20 a 2020-21: 6-8 casas. Sin líneas antes de 2017-18.

**Archivo de cuotas de casas (sportsbook odds archive) — más profundo:**
- Añade líneas **de apertura y cierre desde junio/agosto de 2020** para 16 competiciones (incluidas La Liga y UCL, todas las NWSL no).
- Casas en el archivo: **William Hill, DraftKings, Pinnacle, Marathonbet, 888sport, FanDuel** (y luego Unibet y Betfair Exchange).
- En La Liga: mediana de 6 casas/partido hasta 2021-22, 7 en 2022-23, 8 en 2023-24, 7 en 2024-25, 6 en 2025-26.
- En UCL: mediana de 4 casas/partido hasta 2022-23, 8 en 2023-24 y 2024-25, 7 en 2025-26.
- El archivo arranca en la Premier desde junio 2020 y en UCL **desde agosto 2020 (todos los partidos de 2020-21 en adelante)**.

**Fuentes `source` posibles:** `archive` (línea completa apertura+cierre del archivo de casas) o `final` (una línea pre-partido por casa, la línea final antes del cierre).

### 4.3 Cobertura temporal de cuotas (resumen para backtesting)
| Periodo | Cobertura |
|---|---|
| Antes de 2017-18 | Sin líneas |
| 2017-18 a 2018-19 | 2 casas por partido |
| 2019-20 a 2020-21 | 6-8 casas por partido |
| 2021-22 a 2023-24 | ~10 casas por partido (feed diario) |
| Desde 2024-25 | 1-2 casas por partido (feed diario) |
| Desde jun/ago 2020 | Archivo de casas: apertura + cierre por casa en La Liga y UCL (todas de 2020-21 UCL) |

### 4.4 Goleadores (anytime goalscorer) — `get_player_props` / `get_prop_history`
- **Cobertura:** Premier y MLS desde abr-2024; Ligue 1 desde may-2024; **La Liga, Serie A y Bundesliga desde ago-2024; UCL desde mar-2025**.
- **Casas:** DraftKings y FanDuel (FanDuel desde mar-2025 fuera de Premier/Ligue 1), luego Pinnacle desde abr-2026 y William Hill desde ago-2026. 2 casas hasta mediados 2026, hasta 4 ahora. Solo en partidos donde la casa publicó precio.
- **Una captura por jugador y casa** (sin apertura ni movimiento).
- **Mercado único:** `player_goal_scorer_anytime` (over = marca).
- `get_player_props`: precios por partido (`game_id`) o por jugador (`player_id` + `from_date`/`to_date`), filtro `bookmaker`, `snapshot_type` (`closing`/`opening`), `estimate_only`. Coste 10 créditos por partido antes de jugarse, **20 tras jugarse** (cuentas abiertas antes del 30-oct-2026 pagan 10 siempre).
- `get_prop_history`: para un `player_id`, cada partido con la línea de cierre (mediana entre casas) + `actual` + `result` (over/under) + resumen de acierto. Límite default 50, máx 200, paginación con `next_from_date`.

### 4.5 Formato y modelo de datos de cuotas
- **Formato:** Americano (enteros: positivos = beneficio por 100 apostados; negativos = a arriesgar por 100 de beneficio). `total` en goles (ej. 2.5).
- Cada fila de casa en `get_odds`: `home_ml_open/close`, `draw_ml_open/close`, `away_ml_open/close`, `close_prob` (probabilidades sin margen), `home_handicap_open/close` + `handicap_home_odds`/`handicap_away_odds`, `total_open/close` + `over_odds`/`under_odds`, `captured_at`.
- `get_line_movement` proporciona la serie temporal horaria de `home_ml`, `draw_ml`, `away_ml`, `home_handicap`, `total` por casa.
- **Cálculo EV+:** `close_prob` o convertir las cuotas americanas a decimales y eliminar el margen permite obtener probabilidades implícitas por casa para comparar contra el modelo y detectar EV+. La API ya entrega `close_prob` y el `outcome` real (result, over, home_covered), lo que facilita evaluar el valor esperado directamente.

---

## 5. Diseño de dataset recomendado (feature-engineering)

### 5.1 Pipeline de ingesta sugerido
1. `list_teams` (por temporada) y `list_games` (por temporada/`stage`) → maestro de partidos con `game_id`, `date`, `starts_at`, `home`/`away`, `result`. Para La Liga usar `league=laliga`; para UCL `league=ucl` con `stage` (league/knockout).
2. Para cada partido final: `get_odds` (o `get_game`) para cuotas 1X2/hándicap/total + resultados; `get_game` y/o `get_events` para stats y eventos; `get_team_stats` y `get_standings` para contexto de temporada; `get_ratings` (Elo) para fuerza de equipos.
3. Para goleadores: `get_player_props`/`get_prop_history` (cobertura desde ago-2024 La Liga / mar-2025 UCL).
4. Opcional (cara, paramétrico): `get_line_movement` para movimiento de línea en una muestra.

### 5.2 Consideraciones anti-fuga (critical)
- Usar solo snapshots **pre-partido** (`closing`/`opening`) de cuotas.
- xG de la API es "as-of" (no causal en sentido estricto): para features de entrenamiento valorar recalcular xG sobre tiros ubicados con modelo propio, o usar xG de la API solo como referencia (contamina si se reajusta con la temporada actual).
- `get_team_stats` y `get_standings` y `get_ratings` se pueden consultar con fecha anterior al partido para construir features temporales legítimas.

### 5.3 Variables de salida (targets) que se pueden derivar
- `result` (H/A/D) → target 1X2.
- Marcador local/visitante → total de goles → target Over/Under; y BTTS (ambos marcan) derivado de `home.score>0 && away.score>0`.
- `outcome.over` y `outcome.home_covered` → targets para Over/Total y hándicap.
- `actual`/`result` en `get_prop_history` → targets para goleador anytime.

### 5.4 Features disponibles
- Elo pre-partido (`get_ratings` as of antes del partido).
- Medias de temporada a favor/en contra (`get_team_stats`).
- Clasificación y forma (`get_standings`).
- Cara a cara (`get_head_to_head`).
- Stats por partido previas (acumulables) y xG.
- Movimiento de línea (open→close) por casa (`get_odds` open/close y `get_line_movement`).
- Probabilidades sin margen de cierre (`close_prob`) de cada casa.

---

## 6. Referencias de endpoints (resumen de coste y parámetros)

| Endpoint | CR | Parámetros oblig. |
|---|---|---|
| `list_teams` | 1 | — |
| `search_players` | 1 | `name` |
| `list_games` | 2 | — |
| `get_schedule` | 2 | — |
| `get_standings` | 2 | — |
| `get_ratings` | 2 | — |
| `get_head_to_head` | 2 | `team`, `opponent` |
| `get_game` | 5 | `game_id` |
| `get_lineups` | 5 | `game_id` |
| `get_player` | 5 | `player_id` |
| `get_player_games` | 5 | `player_id` |
| `get_leaders` | 5 | — |
| `get_team_stats` | 5 | — |
| `get_odds` | 5 (partido) / 10 (día) | — |
| `get_events` | 10 | `game_id` |
| `get_shot_map` | 10 | — |
| `get_player_props` | 10 (20 tras jugarse) | — |
| `get_prop_history` | 10 | `player_id`, `market` |
| `get_line_movement` | 10 | `game_id` |

**Presupuesto orientativo:** 500 créditos gratis/mes. Una temporada de La Liga ≈ 380 partidos. Histórico completo (26 temporadas) de solo partidos y resultados = ~26 × ~5 llamadas `list_games` paginadas = factible. Todo el detalle por partido (stats+cuotas/eventos/alineaciones) a 5-10 créditos/partido = miles de créditos por temporada. Se recomienda priorizar `get_odds` + `get_game` para el núcleo del dataset y usar `get_events`/`get_line_movement`/props de forma selectiva.