# Planificación de datos y presupuesto de créditos — StrikerAPI

Complementa a `AUDITORIA_StrikerAPI.md`. Este documento decide y argumenta (1) la elección de esquema/almacenamiento y (2) la previsión de créditos por temporada bajo el límite gratuito de 500 créditos/mes.

---

## 1. Esquema de datos: decisión y argumentación

### Decisión: modelo de capas — raw JSON inmutable + tablas normalizadas en **Parquet** consultadas con **DuckDB**

```
predictor/
├── raw/<competition>/<tool>/           # JSON crudo inmutable de CADA llamada (fuente de verdad)
├── staging/                             # Parquet por tabla parseada desde raw (reflect en DuckDB)
├── curated/striker.db                   # DuckDB: tablas normalizadas del schema SQL (+ vistas de features)
└── features/                            # matrices feature/target materializadas (una fila por partido)
```

El esquema SQL entregado (`schema/strikerapi_schema.sql`) sigue un modelo **relacional normalizado** y es **compatible DuckDB** (funciona también en Postgres/SQLite con ajustes mínimos).

### ¿Por qué DuckDB + Parquet y no PostgreSQL o SQLite?

| Criterio | **DuckDB (elegida)** | PostgreSQL | SQLite |
|---|---|---|---|
| Modelo relacional, SQL, FKs | ✅ | ✅ | ✅ |
| Sin servidor que operar | ✅ (embebido, un archivo) | ❌ infraestructura + INSERT | ✅ |
| Volumen analítico (millones de filas de eventos) | ✅ motor columnar | o, con tuning | ❌ |
| Integración con pandas/PyTorch | ✅ nativo (lectura directa de DF) | medio | medio |
| Escala el pipeline de features | ✅ | ✅ | ⚠ |
| Coste de operación | €0 | servidor | €0 |

**Razones concretas:**
1. **Es un pipeline batch por temporada, no OLTP.** No hay usuarios concurrentes escribiendo; son 26 temporadas × ~380 partidos cargados en bloque y luego lectura analítica masiva. Un motor analítico embebido es la opción adecuada.
2. **Evita infraestructura.** No quieres administrar un Postgres para un dataset de ML. DuckDB abre/consulta un simple archivo y se conecta directo a pandas (`.df()`).
3. **Columnar + compresión** sobre decenas de millones de eventos/tiros es 10-100× más eficiente que SQLite en fila.
4. **La capa `raw/` inmutable es crítica:** StrikerAPI **no guarda historial de revisiones** (sobreeescribe puntuaciones/estadísticas y no conserva versiones previas). El JSON crudo que compras hoy es la *única* evidencia del snapshot "as-of" de cuotas de cierre. Guardarlo significa **no volver a pagar créditos** por un partido si corriges un parseo o el esquema cambia, y es tu seguro contra correcciones de la fuente.

### ¿Por qué normalizado y no una única tabla enorme deduplicada?
Procede de que **los datos llegan por partido y entidad** (box scores, eventos, cuotas por casa, props). Un modelo normalizado garantiza que cada partido existe una vez (PK `(competition, game_id)`), que stats/odds/eventos referencian ese partido por FK y que no se repite el marcador en cada fila (evita inconsistencias). Las vistas `match_targets`, `half_time_targets` y `feature_one_row_per_game` desnormalizan *al final* solo lo que el modelo necesita, sin duplicación en origen.

### Notas anti-fuga ya incorporadas en el schema
- Las cuotas son **pre-partido por definición** (la API descarta capturas post-inicio); `captured_at` queda registrado para auditar.
- Elo/standings/team_stats se referencian con `as_of`/`captured_on`/`season` ≤ fecha del partido en la vista de features.
- **xG de StrikerAPI no es "as-of"** (se refita incluyendo temporadas futuras); se guarda crudo en `match_shots.xg` como referencia, pero para backtesting estricto conviene recalcular xG propio sobre `x`/`y`/`distance_m`/`body_part` ya guardados.
- `status` permite filtrar `final`; `shootout` y `period` aíslan prórrogas/penaltis y reconstruyen el resultado al descanso.

---

## 2. Coste de créditos por llamada (base de cálculo)

| Endpoint | Créditos/llamada |
|---|---|
| `list_teams`, `search_players` | 1 |
| `list_games`, `get_schedule`, `get_standings`, `get_ratings`, `get_head_to_head` | 2 |
| `get_game`, `get_lineups`, `get_player`, `get_player_games`, `get_leaders`, `get_team_stats`, `get_odds` (partido) | 5 |
| `get_odds` (día completo) | 10 |
| `get_events`, `get_shot_map`, `get_player_props` (pre-juego), `get_prop_history`, `get_line_movement` | 10 |
| `get_player_props` (partido **ya jugado**) | 20 |

Contar partidos por temporada: **La Liga ≈ 380** · **UCL ≈ 189–225** (uso 200 como tipo). Histórico total: **La Liga 9.635** partidos, **UCL 4.915** partidos (26 temporadas cada una).

---

## 3. Previsión de créditos por temporada

### Escenario A — Mínimo (resultados + cuotas 1X2/total/hándicap, sin box scores)
`list_games` + `get_odds` (por día, 10 créditos/jornada).

| Concepto | La Liga | UCL |
|---|---|---|
| `list_games` (8-10 páginas × 2) | ~20 | ~10 |
| `get_odds` por jornada (~38 / ~15 días × 10) | ~380 | ~150 |
| **TOTAL / temporada** | **~400** | **~160** |

→ Con 500 créditos/mes: una temporada de La Liga ≈ en **1 mes**.

### Escenario B — Core recomendado (resultados + cuotas por casa open/close + box scores + xG agregado)
`list_games` + **`get_game`** (5/partido). **`get_game` ya incluye el bloque de cuotas por casa** (1X2, hándicap, total, y open/close cuando existe archivo), así que **no hace falta `get_odds` aparte**.

| Concepto | La Liga (380) | UCL (200) |
|---|---|---|
| `list_games` | ~20 | ~10 |
| `get_game` (380/200 × 5) | 1.900 | 1.000 |
| `get_team_stats` (1 × 5) | 5 | 5 |
| `get_standings` (1 × 2) | 2 | 2 |
| `get_ratings` (2-3 × 2) | ~6 | ~6 |
| **TOTAL / temporada** | **~1.930** | **~1.020** |

→ Con 500 créditos/mes: **La Liga ≈ 3,9‑4 meses por temporada** · **UCL ≈ 2 meses por temporada**. Impráctico en plan gratuito para el histórico.

### Escenario C — Core + eventos y tiros con xG
`B` + `get_events` (10/partido) **o** la **ruta optimizada con `get_shot_map` por club** (10 créditos por club-temporada, devuelve hasta 2.000 tiros).

**C1 — `get_events` por partido:**

| Concepto | La Liga (380) | UCL (200) |
|---|---|---|
| Escenario B | ~1.930 | ~1.020 |
| `get_events` (380/200 × 10) | 3.800 | 2.000 |
| **TOTAL / temporada** | **~5.730** | **~3.020** |

→ La Liga ≈ 11,5 meses/temporada al ritmo gratis. **Desaconsejado en masa.**

**C2 — `get_shot_map` por club (optimizada):** para toda la liga, 20 clubes × (a favor + en contra) = 40 llamadas × 10 = **~400 créditos/temporada**, obteniendo **todo** el xG/tirro por tiro de la liga, frente a las 3.800 de `get_events`.

| Concepto | La Liga | UCL |
|---|---|---|
| Escenario B | ~1.930 | ~1.020 |
| `get_shot_map` por club (for/against) | ~400 | ~800* |
| **TOTAL / temporada** | **~2.330** | **~1.820** |

*UCL: más clubes por temporada (hasta ~40 con fase de grupos+KO desde 2012-13); las clasificatorias son finas en datos.

### Escenario D — Completo (B + eventos + lineups + movement de muestra)
`B` + `get_events` + `get_lineups` (5/partido) + `get_line_movement` (10/partido, solo ~10% muestra):

| Concepto | La Liga | UCL |
|---|---|---|
| Escenario B | ~1.930 | ~1.020 |
| `get_events` | 3.800 | 2.000 |
| `get_lineups` | 1.900 | 1.000 |
| `get_line_movement` (10% ≈ 38/20 × 10) | 380 | 200 |
| **TOTAL / temporada** | **~8.010** | **~4.220** |

### Goleadores (props) — aparte y caros
- `get_player_props` por partido **jugado** = **20 créditos/partido** → 7.600/temporada (La Liga). **No masivo.**
- **Alternativa eficiente:** `get_prop_history` por jugador (10 créditos, hasta 200 partidos del histórico del jugador en una llamada). Para ~200 jugadores clave de una temporada ≈ **2.000 créditos** cubriendo todo su histórico. Cobertura real: La Liga desde ago-2024, UCL desde mar-2025.

---

## 4. Histórico completo (26 temporadas) — LN + UCL

| Alcance | La Liga (9.635) | UCL (4.915) | **Conjunto** |
|---|---|---|---|
| **Core B** (~5-7/partido) | ~48.600 | ~24.900 | **~73.500 créditos** |
| **Core + eventos C1** (~15/partido) | ~145.000 | ~74.000 | **~219.000 créditos** |
| **Core + shot_map C2** (optimizado) | ~60.600 | ~49.400* | ~110.000 créditos |

*Estimación para UCL con muchos clubes; verificar con una prueba sobre 1 temporada.

**Al ritmo del plan gratuito (500 cr/mes):**
- Core B conjunto: 73.500 / 500 ≈ **147 meses (~12 años)** → inviable.
- Incluso el Escenario A mínimo (solo odds por día) sobre el histórico sería del orden de miles de créditos → meses/años.

→ **Conclusión:** el plan gratuito sirve para pilotar y validar el pipeline sobre 1-3 temporadas; para el dataset histórico completo se requiere plan de pago.

---

## 5. Tiempo esperado según plan (Escenario B Core, conjunto 73.500 cr)

| Plan | Créditos/mes | Histórico core B conjunto | 1 temporada La Liga (1.930) |
|---|---|---|---|
| Free $0 | 500 | ~147 meses | ~4 meses |
| Starter $19 | 10.000 | ~7,5 meses | ~1 semana (2 jornadas) |
| Pro $49 | 30.000 | ~2,5 meses | **~2 días** |
| Scale $149 | 125.000 | **~18 días** | <1 día |

Con eventos (C1, 219.000): Starter ~22 meses · Pro ~7,3 meses · Scale ~1,8 meses.

---

## 6. Recomendaciones de coste (top tips)

1. **Guarda `raw/` siempre.** Un partido cuesta 5-10 créditos; una corrección de parseo o de esquema no debe obligarte a re-comprarlo. Guardar el JSON evita doble gasto (idempotencia).
2. **`get_game` ya trae las cuotas por casa.** No pagues `get_odds` por partido si usas `get_game` (ahorro 5 cr/partido → ~1.900/temporada de La Liga). Solo usa `get_odds` si necesitas el detalle `close_prob` sin margen (para EV+) o el barrido por día.
3. **xG: usa `get_shot_map` por club** (~400 cr/temporada La Liga) en lugar de `get_events` por partido (3.800) si lo que necesitas es el xG por tiro.
4. **Props: usa `get_prop_history` por jugador** (10 cr / ≤200 partidos) en lugar de `get_player_props` por partido jugado (20 cr/partido).
5. **Costes por día/escaneo aún por verificar:** `get_odds` con `season` pague el coste por página (probablemente 5-10) y no por partido individual → si es así, barrer una temporada de cuotas cuesta ~40-80 créditos en vez de 1.900. **Pilotar 1 temporada real** (la misma llamada repetida en 2 min se cobra una única vez) antes de lanzar la ingesta masiva.
6. **`estimate_only`** en `get_player_props` permite consultar el coste sin cobrar; úsalo en la fase de validación del pipeline.
7. **Free tier → plan Starter/Pro para el histórico.** Presupuesto razonable: ~73.500 cr (core B) ≈ **plan Scale ~18 días** o **~2,5 meses en Pro** (~$123 con Pro). Si añades xG por tiro (C2), ~110.000 cr → Pro ~3,7 meses / Scale ~29 días.