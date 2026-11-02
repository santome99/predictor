# Guía de despliegue y operación — de cero a dataset histórico

Guía completa para montar el sistema en una LXC (unprivileged, 4 GB RAM / 3 CPU)
y construir el histórico de La Liga **en tandas de ~500 créditos con claves API
distintas**, sin recomprar nunca un partido.

> Resumen de lo que hay montado:
> - `ingestion/` → descarga de StrikerAPI (consume créditos) a DuckDB + JSON crudo.
> - `features/` → matriz de entrenamiento, modelos, backtest y recomendación (0 créditos).
> - `curated/striker.db` + `raw/` → el activo que cuesta dinero; se respalda.
> - `artifacts/` → derivados regenerables gratis.

---

# PARTE A — Puesta en marcha

## A1. Crear la LXC (Proxmox)

```
Template:     debian-12-standard
Unprivileged: sí
RAM:          4096 MB (con 2048 va sobrado)
Swap:         512 MB
CPU:          3
Disco:        16 GB
Red:          salida a Internet
```

Entra por consola/SSH: `ssh root@IP_LXC`.

## A2. Paquetes del sistema

```bash
apt update
apt install -y python3 python3-venv python3-pip git rsync screen nano
python3 --version          # esperado >= 3.10
```

## A3. Traer el código

Si lo tienes en un remoto git:

```bash
git clone TU_REMOTO /opt/predictor
cd /opt/predictor
```

Si no hay remoto, desde tu máquina de desarrollo:

```bash
rsync -av --exclude '.git' --exclude 'artifacts' --exclude 'curated' \
  --exclude 'raw' --exclude '.venv' --exclude '.env' \
  predictor/ root@IP_LXC:/opt/predictor/
```

Comprueba que están las carpetas clave:

```bash
ls        # ingestion features schema tests requirements.txt .env.example
```

## A4. Entorno Python

```bash
cd /opt/predictor
python3 -m venv .venv
. .venv/bin/activate
pip install -U pip
pip install -r requirements.txt          # 2-5 min
python -c "import duckdb,pandas,numpy,scipy,sklearn,pyarrow,requests,dotenv; print('deps OK')"
```

> Cada vez que abras una shell nueva: `. /opt/predictor/.venv/bin/activate`

## A5. Crear el `.env`

```bash
cd /opt/predictor
cp .env.example .env
chmod 600 .env            # la clave no debe ser legible por otros
nano .env
```

Contenido recomendado para tu caso (tandas de 500 créditos, claves distintas):

```ini
# --- credencial de la tanda ACTUAL (se cambia en cada tanda) ---
STRIKER_API_KEY=clave_de_esta_tanda
STRIKER_CREDENTIAL_LABEL=tanda1

# --- fijos ---
STRIKER_COMPETITION=laliga
STRIKER_DB_PATH=curated/striker.db
STRIKER_RAW_DIR=raw

# --- presupuesto ---
STRIKER_MONTHLY_BUDGET=10000
STRIKER_CREDITS_REMAINING=500
STRIKER_RESERVE_CREDITS=50

# --- red (valores documentados por StrikerAPI) ---
STRIKER_RATE_PER_MINUTE=60
STRIKER_LOW_BALANCE_RATE=10
```

Qué significa cada uno de los nuevos:

| Variable | Significado |
|---|---|
| `STRIKER_CREDENTIAL_LABEL` | nombre de la tanda. Separa la contabilidad de créditos **por clave**. Si lo dejas vacío se usa un hash corto de la key. |
| `STRIKER_CREDITS_REMAINING` | saldo real de **esta** clave. El tope de gasto es `min(10000, 500)` = 500. |
| `STRIKER_RESERVE_CREDITS` | colchón que no se toca (errores/reintentos). Disponible = 500 − 50 = **450**. |

> **Importante:** la contabilidad de gasto va por credencial. Al cambiar de key
> (tanda nueva) el contador arranca de cero automáticamente para esa key; por eso
> puedes hacer tandas de 500 sin que la anterior te bloquee.

---

# PARTE B — Verificaciones SIN gastar créditos

## B1. Comprobar configuración

```bash
cd /opt/predictor && . .venv/bin/activate
python -m ingestion budget
```

Esperado:

```
Credential (tanda)    : tanda1
Credits remaining now : 500
Total cap             : 500
Reserve credits       : 50
Effective cap         : 450
Spent this month      : 0
Available (to reserve): 450
```

```bash
python -m ingestion status
```

Esperado: todo a 0 (crea `curated/striker.db`), con
`Pending games: 0` y `Pending cost estimate: ~0 cr`.

## B2. Smoke test del stack ML (dataset demo, 0 créditos)

```bash
python -m features demo
export STRIKER_DB_PATH=artifacts/demo.db STRIKER_COMPETITION=demo
python -m features build
python -m features backtest
python -m features predict
unset STRIKER_DB_PATH STRIKER_COMPETITION
rm -rf artifacts
```

Esperado: `Matrix: 528 filas × 99 columnas`, backtest con `"ok": true`,
tabla de 12 predicciones. Si esto falla, **no sigas**: es entorno, no API.

---

# PARTE C — INGESTA POR TANDAS (la parte importante)

## C1. El modelo de costes (leer una vez, aplica siempre)

| Operación | Coste | Cuándo se paga |
|---|---|---|
| `list_games` (descubrir partidos) | 2 cr/página, ~8 páginas por temporada ≈ **16 cr** | al lanzar `run --season X` (una vez por temporada) |
| `get_game` (stats + cuotas de un partido) | **5 cr/partido** | por cada partido nuevo |
| `get_ratings` (Elo) | 2 cr por fecha única ≈ **~76 cr/temporada** | al lanzar `elo` |
| `get_schedule` (fixtures futuros) | 2 cr | al lanzar `features schedule` |

Dos comandos, dos comportamientos distintos:

- **`run --season X`** = descubre la temporada (16 cr) **y** procesa partidos.
- **`resume`** = **no descubre nada** (0 cr fijos), solo procesa checkpoints
  PENDING a 5 cr/partido. **Este es el comando de las tandas 2..N.**

Con 450 disponibles por tanda:

| Tanda | Qué haces | Partidos nuevos | Coste |
|---|---|---|---|
| 1 | `run --season 2025 --limit 3` (incluye discovery) | 3 | 16 + 15 = **31** |
| 2..N | `resume --limit 89` | 89 | **445** |
| elo | `elo --season 2025` (una vez por temporada) | — | ~**76** |

Una temporada completa (380 partidos) = ~16 + 1900 + 76 ≈ **1.992 cr ≈ 4-5 tandas**.

## C2. Qué es un checkpoint y por qué no se recompra nada

Cada partido tiene una fila en `ingestion_checkpoints` con estado:

`PENDING → PROCESSING → COMPLETED` (o `FAILED` tras errores de datos).

- Si un partido está **COMPLETED**, `run` y `resume` lo **saltan**: no se vuelve a pagar.
- Si la tanda se corta a medias, el partido afectado queda **PENDING** (no FAILED,
  no consume intento) y la siguiente tanda lo retoma.
- Al terminar una tanda, **todos** los partidos del lote ya tienen su checkpoint
  (se pre-crean al empezar), por lo que `status` te dice exactamente qué falta.
- Además, el JSON crudo de cada partido se guarda en `raw/`: aunque cambies el
  parser o el esquema, no hace falta volver a comprarlo.

## C3. TANDA 1 — descubrir temporada + primeros partidos

```bash
cd /opt/predictor && . .venv/bin/activate
python -m ingestion run --season 2025 --limit 3
```

Esperado:

```
[season 2025] descubriendo partidos finales...
[season 2025] 380 finales, 380 pendientes
[season 2025] hechos 3
[limit] alcanzado 3. Paro.
OK: 3 partidos completados
```

Verifica (esto es tu ritual tras **cada** tanda):

```bash
python -m ingestion budget
python -m ingestion status
```

Esperado: `Spent this month: 31`, `Games (total): 3`, `COMPLETED: 3`,
`Pending games: 377`, `Pending cost estimate: ~1885 cr`.

> Nota: el límite cuenta partidos **procesados** (nuevos), no los saltados.

## C4. TANDAS 2..N — cambiar de clave y continuar

Procedimiento exacto (repetir por cada tanda):

**Paso 1 — editar `.env` con la clave nueva:**

```bash
nano /opt/predictor/.env
```

```ini
STRIKER_API_KEY=clave_de_la_tanda_2
STRIKER_CREDENTIAL_LABEL=tanda2
STRIKER_CREDITS_REMAINING=500
```

**Paso 2 — comprobar que la credencial cambió y el contador está limpio:**

```bash
cd /opt/predictor && . .venv/bin/activate
python -m ingestion budget
```

Esperado: `Credential (tanda): tanda2`, `Spent this month: 0`, `Available: 450`.
Si `Spent` no es 0, es que la label/key no cambió.

**Paso 3 — lanzar la tanda (sin discovery):**

```bash
python -m ingestion resume --limit 89
```

Esperado: procesa hasta 89 partidos nuevos y termina con
`OK: N reanudados`. Si llega al tope antes, verás
`[budget] creditos agotados... Ingesta pausada.` y terminará limpiamente (código 0).

**Paso 4 — verificar:**

```bash
python -m ingestion status
python -m ingestion budget
```

`Pending games` debe haber bajado en los partidos completados. `Available` de la
tanda actual ≈ 5–20 cr restantes.

**Paso 5 — registrar la tanda** (tabla de control, C6).

> ⚠️ **No cambies** `STRIKER_DB_PATH` ni `STRIKER_COMPETITION` entre tandas. Si los
> cambias, apuntas a otra BD y pierdes la continuidad (y el dinero).

## C5. Cuántos partidos meter por tanda

Fórmula: `partidos = floor((disponible) / 5)`.

- Tanda con discovery incluido: `(450 − 16) / 5 = 86` partidos.
- Tanda solo `resume`: `450 / 5 = 90` partidos.
- Si además vas a ejecutar `elo` de una temporada completa (~76 cr) en la misma
  tanda: `(450 − 76) / 5 = 74` partidos.

Recomendación: deja 5–10 partidos de margen por si hay reintentos de errores 5xx.

## C6. Tabla de control de tandas (rellénala)

| # | Fecha | label | key (últimos 4) | Disponible | Comando | Partidos | Gastado | Pendientes |
|---|-------|-------|-----------------|-----------|---------|----------|---------|------------|
| 1 | | tanda1 | ... | 450 | run --season 2025 --limit 3 | 3 | 31 | 377 |
| 2 | | tanda2 | ... | 450 | resume --limit 89 | | | |
| 3 | | tanda3 | ... | 450 | resume --limit 89 | | | |
| 4 | | tanda4 | ... | 450 | resume --limit 89 | | | |
| 5 | | tanda5 | ... | 450 | resume --limit 74 + elo | | | |

Puedes volcar el estado real en cualquier momento con:

```bash
python -m ingestion report     # incluye presupuesto por credencial + cobertura
```

## C7. Interrupciones: qué pasa y cómo se retoma

| Situación | Qué ocurre | Qué haces |
|---|---|---|
| `Ctrl+C` / cae el SSH | el proceso muere; el partido en curso queda `PROCESSING` (nunca `COMPLETED`) | relanzar `resume`: los `PROCESSING` se re-procesan (ese partido se recompra, ~5 cr) |
| Presupuesto agotado (detección local) | imprime `[budget] creditos agotados`, deja el partido en `PENDING`, sale con 0 | nueva tanda con otra clave |
| **HTTP 402** (saldo API real a 0) | igual que arriba: `PENDING`, sin quemar intento | nueva tanda |
| **HTTP 429** (rate limit) | el cliente espera y reintenta con backoff | nada |
| **HTTP 500** | reintenta con `x-idempotency-key` (seguro, no duplica); créditos reembolsados por el servidor | nada |
| **HTTP 400/401** | error permanente → partido `FAILED` con el mensaje guardado | revisar el mensaje en `status`; `retry` tras corregir |
| Partido aplazado/sin cuotas | se guarda igual; `odds_books` puede quedar vacío para ese partido | no es error |

Reanudar tras días parado:

```bash
cd /opt/predictor && . .venv/bin/activate
python -m ingestion status          # ver cuántos PENDING quedan
python -m ingestion budget          # ver saldo de la credencial configurada
python -m ingestion resume --limit 89
```

Si quedaron `FAILED` recuperables:

```bash
python -m ingestion retry           # los reencola a PENDING
```

## C8. Cambiar de temporada

Cuando la temporada en curso no tenga pendientes:

```bash
# descubre la siguiente (una vez): paga ~16 cr de discovery
python -m ingestion run --season 2024 --limit 3
# y continúa con resume como siempre
python -m ingestion resume --limit 89
```

Orden recomendado: 2026 → 2025 → 2024 → … → 2017 (de lo más reciente a lo más
antiguo, que es como el modelo aprovecha antes la forma actual).

## C9. Verificaciones de integridad por tanda

```bash
# 1) estado y coste de lo que falta
python -m ingestion status

# 2) contabilidad por credencial (no debe mezclar tandas)
python -m ingestion budget

# 3) muestra real de datos guardados
python - <<'EOF'
import duckdb
c = duckdb.connect("curated/striker.db", read_only=True)
print(c.execute("select count(*) partidos, count(distinct season) temps from games").df())
print(c.execute("select count(*) filas, count(distinct game_id) partidos, "
                "count(distinct provider) casas from odds_books").df())
print(c.execute("select count(*) from team_match_stats").df())
print(c.execute("select count(*) from raw_api_responses").df())
EOF
```

Señales de alarma:

- `odds_books` con `casas = 0` → el partido no tenía cuotas (normal en partidos
  antiguos muy tempranos) o hay un problema de parseo (avísame con un ejemplo).
- `partidos` mucho menor que `COMPLETED` → estarías mezclando otra BD (revisa `STRIKER_DB_PATH`).
- `raw_api_responses` a 0 → el guardado de JSON crudo no está activo.

---

# PARTE D — Features, modelos y recomendación (0 créditos)

## D1. Cuándo ejecutarlo

Con ~1 temporada ya hay matrice usable (el modelo será flojo, pero el pipeline
funciona). Con 3+ temporadas el backtest es informativo.

```bash
cd /opt/predictor && . .venv/bin/activate
python -m features build        # matriz congelada (as-of, anti-fuga)
python -m features backtest     # walk-forward por temporada + ROI vs cierre
python -m features show         # inspección de columnas y NaN
```

## D2. Fixtures y predicción

```bash
python -m features schedule --days 7     # 2 cr: guarda próximos partidos
python -m features predict               # probabilidades
```

## D3. Recomendación con cuotas de tu casa

```bash
cat > odds.json <<'EOF'
{"bookmaker":"winamax","matches":[
 {"home":"Real Madrid","away":"Sevilla","markets":{
   "1x2":{"home":1.55,"draw":4.40,"away":6.80},
   "over_under":{"line":2.5,"over":1.90,"under":1.95},
   "btts":{"yes":1.85,"no":1.95}}}]}
EOF
python -m features recommend odds.json --target-odds 2.5 --criterion value
```

Interpretación: `p_model` = probabilidad del modelo; `p_breakeven = 1/cuota` =
lo que exige la casa; `ev = p_model*cuota − 1` (>0 = valor).
`--criterion safety` ordena por probabilidad en vez de por EV.

> No se raspan webs de casas (son SPAs con JS privado). El JSON se copia a mano:
> 3 números por mercado. Ver `DECISIONES_FEATURES.md` (D25-D30).

---

# PARTE E — Automatización y mantenimiento

## E1. Cron diario (cuando ya no estés en modo "tandas manuales")

```bash
mkdir -p /opt/predictor/logs
crontab -e
```

```
30 6 * * * flock -n /tmp/ingest.lock bash -c "cd /opt/predictor && . .venv/bin/activate && python -m ingestion resume --limit 89" >> /opt/predictor/logs/ingesta.log 2>&1
0 8 * * * flock -n /tmp/feat.lock bash -c "cd /opt/predictor && . .venv/bin/activate && python -m features build && python -m features predict" >> /opt/predictor/logs/features.log 2>&1
0 9 * * 1 flock -n /tmp/feat.lock bash -c "cd /opt/predictor && . .venv/bin/activate && python -m features backtest" >> /opt/predictor/logs/backtest.log 2>&1
```

`resume` se autofrena cuando no queden créditos. Cuando cambies de key, edita
`.env`: el cron de la mañana siguiente usará la nueva.

## E2. Backup (obligatorio)

```bash
rsync -av --delete /opt/predictor/curated/ /opt/predictor/raw/ \
  usuario@NAS:/backup/predictor/
```

`artifacts/` no hace falta (se regenera). `curated/` + `raw/` sí: son créditos.

---

# PARTE F — Problemas frecuentes

| Síntoma | Causa probable | Solución |
|---|---|---|
| `402 Insufficient credits` | saldo real a 0 | cambiar de key/tanda |
| `Available: 0` en la tanda nueva | no cambiaste `STRIKER_CREDENTIAL_LABEL`/key | revisar `.env`; `budget` debe mostrar la nueva label |
| `No hay fixtures programados` | parón de liga o no lanzaste `schedule` | `features schedule --days 10` |
| `Ningún partido coincide con equipos conocidos` | nombres no casan con `teams` | usar nombre/abreviatura exacta (ej. `RMA`) |
| `run` dice "presupuesto insuficiente" y no empieza | 450 < 1916 (temporada completa) | es correcto: usa `run --limit 3` y luego `resume` |
| `status` con COMPLETED bajo y `games` alto | cambiaste `STRIKER_DB_PATH` | volver a la BD original |
| Backtest vacío | < 2 temporadas | seguir ingiriendo |
| `402` a mitad y aparece "PENDING" | correcto | nueva tanda con `resume` |

---

# PARTE G — Checklist final

- [ ] LXC creada (4 GB / 3 CPU / 16 GB), `python3 -m venv` OK, deps importan.
- [ ] `.env` con `chmod 600`, key + label de la tanda, `CREDITS_REMAINING=500`, reserva 50.
- [ ] `ingestion budget` muestra la credencial correcta y 450 disponibles.
- [ ] `features demo` + `build` + `backtest` OK (0 créditos).
- [ ] Tanda 1: `run --season 2025 --limit 3` → 31 cr, 3 COMPLETED.
- [ ] Tandas 2..N: cambiar key + label → `resume --limit 89` → ~445 cr.
- [ ] `elo --season 2025` una vez por temporada (~76 cr).
- [ ] Tras cada tanda: `status` + `budget` + tabla de control C6.
- [ ] Backup de `curated/` + `raw/` a otro host.
- [ ] `features build` cuando haya ≥1 temporada; `backtest` semanal.
