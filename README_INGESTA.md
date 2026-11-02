# StrikerAPI — Ingesta histórica para predicción de apuestas (La Liga)

Sistema Python reanudable, idempotente y consciente de presupuesto que descarga el
histórico de La Liga (y, opcionalmente, UCL) desde StrikerAPI hacia una base
[DuckDB](../) con capa raw JSON inmutable, y una **capa de features + modelos +
predicción** que consume esa base para producir probabilidades pre-partido y
backtests de valor (EV+).

**Etapas:**
1. `ingestion/` — crawler (clave API, créditos): `list_games` + `get_game` + `get_ratings`.
2. `features/` — consumo local: matriz train as-of, Poisson + GBM + blend,
   walk-forward backtest y predicciones para próximos fixtures.
   Decisiones de diseño razonadas: `DECISIONES_FEATURES.md`.

**Alcance inicial (FASE 1ª):** `list_games` + `get_game` + `get_ratings`.
Los endpoints `get_events`, `get_shot_map`, `get_lineups`, `get_player*`,
`get_player_props`, `get_prop_history`, `get_line_movement` **NO se utilizan en
esta fase** (enriquecimiento posterior). Ver `PLAN_INGESTA_Y_PRESUPUESTO.md`.

---

## Arquitectura

```
predictor/
├── ingestion/                  # paquete Python (python -m ingestion)
│   ├── cli.py                  # comandos CLI
│   ├── client.py               # cliente StrikerAPI (auth, retries, rate limit, créditos)
│   ├── credits.py              # control de presupuesto
│   ├── db.py                   # DuckDB: schema, upserts, ledger, checkpoints
│   ├── ingestor.py             # orquestación: descubrir/enriquecer/persistir
│   ├── normalize.py            # JSON -> filas normalizadas
│   ├── prices.py               # American odds -> decimal
│   └── report.py               # status / presupuesto / informes
├── features/                   # consumo de datos + modelos (python -m features)
│   ├── cli.py                  # build | backtest | predict | demo | show | schedule
│   ├── builder.py              # DuckDB -> matriz 1 fila/partido (as-of)
│   ├── windows.py              # rolling pre-partido (anti-fuga)
│   ├── context.py              # joins as-of (Elo)
│   ├── odds.py                 # margen/overround + probs implícitas
│   ├── models.py               # Poisson MLE + GBM histograma + blend
│   ├── backtest.py             # walk-forward por temporada + EV/ROI
│   ├── predict.py              # probabilidades para fixtures
│   └── demo.py                 # dataset sintético determinista
├── schema/
│   ├── duckdb_schema.sql       # runtime (aplicado por db.migrate)
│   └── strikerapi_schema.sql   # schema analítico completo (referencia/futuro)
├── raw/<competition>/<tool>/   # **JSON crudo inmutable de cada llamada**
├── curated/striker.db          # duckdb database (ingesta)
├── artifacts/                  # matriz train, walk-forward, predicciones, demo.db
├── tests/
├── DECISIONES_FEATURES.md      # decisiones de diseño de la capa de features
└── .env.example
```

### Flujo de una temporada

1. `list_games` con `season` paginado por `limit`/`offset`/`next_offset` (2 cr).
2. Se conservan solo `status=final`; se ordenan por `starts_at` desc.
3. Por cada partido pendiente: `get_game` (5 cr) → persiste partido, equipos,
   box score por equipo, líneas por jugador y cuotas por casa (original + decimal).
4. Actualiza checkpoint y registro de créditos; continúa.
5. `get_ratings` (2 cr) por fecha única para elo anti-fuga (opcional, comando `elo`).

### Anti-data-leakage (clave)

- Las cuotas se guardan **pre-partido** (`captured_at` < `starts_at` siempre;
  la API descarta capturas post-inicio). Apertura/cierre por casa.
- **xG de StrikerAPI NO es "as-of"** (se reajusta sobre temporadas completas):
  se guarda tal cual en `team_match_stats.xg` pero **no debe usarse como variable
  estrictamente histórica as-of** sin recalcular sobre localizaciones.
- `get_ratings` devuelve `as_of` y `last_game`: se registra el snapshot por fecha
  y el join de features debe filtrar `as_of` < fecha del partido.
- `get_standings` NO se usa aún (tabla final de temporada); la clasificación
  pre-partido se derivará de `list_games` acumulado.

---

## Configuración

```bash
pip install -r requirements.txt
cp .env.example .env
# editar .env: STRIKER_API_KEY=...
```

Variables relevantes (todas opcionales salvo la clave):

| Variable | Defecto | Descripción |
|---|---|---|
| `STRIKER_API_KEY` | — | clave API (obligatoria para `run`/`resume`/`elo`) |
| `STRIKER_COMPETITION` | `laliga` | competición (`laliga`, `ucl`) |
| `STRIKER_DB_PATH` | `curated/striker.db` | DB DuckDB |
| `STRIKER_RAW_DIR` | `raw` | JSON crudo por llamada |
| `STRIKER_MONTHLY_BUDGET` | `10000` | presupuesto mensual en créditos |
| `STRIKER_RESERVE_CREDITS` | `500` | reserva no programable para errores |
| `STRIKER_RATE_PER_MINUTE` | `60` | rate limit documentado (saldo ≥100 cr) |
| `STRIKER_LOW_BALANCE_RATE` | `10` | rate limit documentado (saldo <100 cr) |

Rate limits documentados por StrikerAPI: **60 req/min** cuando hay ≥100 créditos y
**10 req/min** bajo ese umbral. El cliente respeta ambos (el umbral se estima
localmente con el contador de gasto).

---

## Comandos

```bash
# ---- Ingesta (requiere API key) ----
python -m ingestion status            # estado de la ingesta
python -m ingestion budget            # presupuesto local y gasto por tool
python -m ingestion dry-run           # estimación de coste sin llamadas
python -m ingestion run               # temporadas 2026..2017 en orden desc
python -m ingestion run --season 2025 # una temporada
python -m ingestion run --season 2025 --limit 3   # prueba: solo 3 partidos
python -m ingestion resume            # reencola FAILED/RETRY_PENDING y continúa
python -m ingestion retry             # reencola fallidos recuperables
python -m ingestion elo               # get_ratings por fecha (para features Elo)
python -m ingestion report            # resumen completo + presupuesto

# ---- Features / modelos (local, sin red) ----
python -m features demo               # dataset sintético para aprender sin créditos
python -m features build              # matriz train congelada (as-of, anti-fuga)
python -m features backtest           # walk-forward por temporada + EV/ROI vs cierre
python -m features predict            # probabilidades 1X2/O-U/BTTS de fixtures
python -m features schedule --days 7  # refresca fixtures vía get_schedule (API key)
python -m features show               # inspección de la matriz guardada

# ---- Recommend: cuotas pegadas (JSON) + modelo -> selecciones ----
python -m features recommend odds.json --target-odds 2.5 --criterion value
python -m features recommend odds.json --target-odds 2.5 --tolerance 0.15 \
    --criterion safety --min-ev 0.0 --per-fixture
echo '<JSON con cuotas>' | python -m features recommend -   # leer de stdin

# Ejemplo end-to-end sin API key (demo):
STRIKER_DB_PATH=artifacts/demo.db STRIKER_COMPETITION=demo python -m features build
STRIKER_DB_PATH=artifacts/demo.db STRIKER_COMPETITION=demo python -m features backtest
STRIKER_DB_PATH=artifacts/demo.db STRIKER_COMPETITION=demo python -m features predict
```

### Formato del JSON de cuotas para `recommend`

```json
{
  "bookmaker": "winamax",
  "matches": [
    {"home": "Real Madrid", "away": "Sevilla",
     "markets": {
       "1x2": {"home": 1.55, "draw": 4.40, "away": 6.80},
       "over_under": {"line": 2.5, "over": 1.90, "under": 1.95},
       "btts": {"yes": 1.85, "no": 1.95}}
    }
  ]
}
```

Los nombres de equipos se resuelven contra la tabla `teams` (acentos/espacios
insensibles). Solo se recomienda sobre equipos con historia en la BD; el resto
se excluye con aviso. La salida muestra `p_model` (prob. modelo), `p_breakeven`
(=1/cuota) y `ev` (= p·cuota−1); con `--criterion value` ordena por EV, con
`--criterion safety` por probabilidad de acierto.

Con `--verbose` se amplía el logging. `run` y `resume` requieren API key.

**Cadencia recomendada en el homelab (cron):**
1. Diario (o tras jornadas): `python -m ingestion run && python -m ingestion elo`
2. Tras la ingesta: `python -m features schedule --days 7 && python -m features build`
3. Antes de apuestas: `python -m features predict`
4. Semanal: `python -m features backtest` (vigilar drift del ROI)

---

## Coste por endpoint (documentado)

| Endpoint | Créditos |
|---|---|
| `list_games`, `get_schedule`, `get_standings`, `get_ratings`, `get_head_to_head` | 2 |
| `get_game`, `get_odds` (match), `get_lineups`, `get_team_stats`, `get_player*`, `get_leaders` | 5 |
| `get_events`, `get_shot_map`, `get_line_movement`, `get_prop_history` | 10 |
| `get_player_props` (partido jugado) | 20 ⚠️ |
| `list_teams`, `search_players` | 1 |

La ingesta inicial consume únicamente `list_games` (2), `get_game` (5) y
`get_ratings` (2) por fecha.

**Estimación La Liga/temporada (core):** `get_game` 380×5 = 1.900 + `list_games`
~16 + `get_ratings` (38 fechas×2=76) ≈ **~2.000 créditos/temporada**.
Con presupuesto 10.000/mes y reserva 500 → **~4-5 temporadas/mes** en orden
descendente (2026, 2025, 2024, 2023, ...).

`--limit` permite validar sin consumir masivo: `run --season 2025 --limit 3`.

---

## Esquema de base de datos (DuckDB)

Tablas normalizadas `schema/duckdb_schema.sql`:

- `competitions`, `teams`, `team_seasons`
- `games` (PK `competition, game_id`)
- `team_match_stats` (PK `competition, game_id, team_id`)
- `player_match_stats` (PK `competition, game_id, player_id`)
- `odds_books` (PK `competition, game_id, provider, source`) — odds **original**
  (americano) y **decimal** derivado; `captured_at`; `close_prob_raw`.
- `elo_snapshots` (PK `competition, as_of, team_id`)
- `ingestion_jobs`, `ingestion_checkpoints` (reanudación), `api_usage` (gasto
  mensual por tool), `raw_api_responses` (índice de los JSON en disco).

El schema analítico extenso (`strikerapi_schema.sql`) se mantiene para futuras
fases (events, shots, lineups, props, movement) y vistas de features.

### Cuotas: conversión American → decimal

`ingestion/prices.py`:
- positiva: `1 + odds/100` (105 → 2.05)
- negativa: `1 + 100/|odds|` (-118 → 1.8475)
- `0`/`None`/vacío → `NULL` (nunca 0)

Se conservan siempre los valores originales (`*_orig`) junto a los decimales
(`*_dec`).

---

## Reanudación y robustez

- **Checkpoint por partido** (`ingestion_checkpoints`): PENDING → PROCESSING →
  COMPLETED; FAILED/RETRY_PENDING para reintentos. Si el proceso se interrumpe,
  `resume` reencola y continúa sin repetir COMPLETED.
- **Errores:** 400/401 → fallo permanente (no reintentable); 500/429 → reintentos
  con backoff e `x-idempotency-key`; **402 → agotamiento de créditos, se detiene**.
- **Idempotencia:** `INSERT OR REPLACE` sobre PKs; mismas llamadas repetidas (>2
  min) no duplican filas.
- **Presupuesto local:** `api_usage` acumula el gasto mensual real por endpoint
  (el cliente notifica cada gasto al `BudgetController`). Antes de cada lote se
  compara coste estimado vs disponible (= presupuesto − gastado − reserva).
- **Raw JSON inmutable** en `raw/` → el dataset es reconstruible sin volver a
  gastar créditos.

---

## Pruebas

```bash
python -m pytest tests -q
```

Cubren conversión American→decimal, normalización (NULL nunca 0), paginación por
`next_offset`, persistencia idempotente en DuckDB, reanudación sin repetir
COMPLETED, reintentos 5xx/429, 402 aborta, y pipeline completo con API simulada.

**Prueba real recomendada (3 partidos):**

```bash
export STRIKER_API_KEY=...   # o .env
python -m ingestion run --season 2025 --limit 3
python -m ingestion status
python -m ingestion report
```

Coste esperado: 1×`list_games`(2) + 3×`get_game`(5) = **~17 créditos**.

---

## Fases siguientes

- FASE 2ª+: enriquecer con `get_events`/`get_shot_map` (xG por tiro), `get_lineups`,
  `get_line_movement`, props (cuidado con el coste post-juego).
- Features anti-fuga: clasificación acumulada desde `list_games`, forma, medias
  rodantes de `get_game`, Elo `as_of` previo.