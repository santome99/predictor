# Despliegue en homelab (LXC unprivileged, 4 GB RAM / 3 CPU)

Guía paso a paso para ejecutar el sistema completo (ingesta + features +
recomendación) en un contenedor LXC de Proxmox. Carga de trabajo: ligera
(DuckDB embebido, pandas, sklearn sobre ~10-40k partidos).
Requisitos reales observados: <1 GB RAM, ~1 CPU, ~16 GB disco.

---

## 1. Crear el contenedor en Proxmox

```
ID:        (libre)
Hostname:  predictor
Template:  Debian 12
Unprivileged: sí (por defecto)
RAM:       4096 MB (con 2048 sobra)
Swap:      512 MB
CPU:       3
Disk:      16 GB   (curated/ + raw/ + artifacts/ + .venv)
```

Red con salida a Internet (para pip y la API de StrikerAPI).

## 2. Preparar el sistema base

```bash
apt update && apt install -y python3 python3-pip python3-venv git rsync screen
```

## 3. Copiar el código al contenedor

Opción A — rSync desde tu máquina de desarrollo (recomendado, sin git remoto):

```bash
# desde tu dev machine
rsync -av --exclude '.git' --exclude 'artifacts' --exclude 'curated' \
  --exclude 'raw' --exclude '.venv' --exclude '.env' \
  predictor/ usuario@IP_LXC:/opt/predictor/
```

Opción B — clonar desde tu git:

```bash
git clone TU_REMOTO /opt/predictor
```

> `.env` NUNCA se copia con el repositorio (contiene la API key).

## 4. Entorno Python

```bash
cd /opt/predictor
python3 -m venv .venv
. .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

## 5. Configuración (.env)

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

Mínimo indispensable:

```
STRIKER_API_KEY=<tu clave>
STRIKER_COMPETITION=laliga
STRIKER_MONTHLY_BUDGET=10000
STRIKER_RESERVE_CREDITS=500
STRIKER_CREDITS_REMAINING=0
```

El resto (DB path, rate limits 60/10 req-min, timeouts) ya trae valores
correctos por defecto.

### 5.b Poco saldo (plan gratuito: ~500 créditos/mes)

El tope real de gasto es `min(STRIKER_MONTHLY_BUDGET, STRIKER_CREDITS_REMAINING)`
menos `STRIKER_RESERVE_CREDITS`. Con **498 créditos** y reserva 50:

```
STRIKER_MONTHLY_BUDGET=10000     # techo del plan (no tocar)
STRIKER_RESERVE_CREDITS=50       # colchón para reintentos/errores
STRIKER_CREDITS_REMAINING=498    # saldo ACTUAL
# => cap 498, disponible 448
```

`STRIKER_CREDITS_REMAINING` es el saldo al **inicio del periodo de contabilidad**
(cuando `api_usage` de este mes está a 0). Si recargas a mitad de mes, pon
`STRIKER_CREDITS_REMAINING = saldo_nuevo + "Spent this month"` que muestra
`python -m ingestion budget`, para que siga cuadrando.

Comprobación:

```bash
python -m ingestion budget
# Credits remaining now : 498
# Total cap             : 498
# Effective cap         : 448
# Available             : 448
```

**Qué NO gastar con 500 créditos:** una `run` de temporada completa son ~1.900 cr.
`dry-run` lo dice y `run` se detiene antes de empezar.

**Coste real del descubrimiento:** `run --season X --limit N` paga primero el
`list_games` de TODA la temporada (≈8 páginas × 2 = **16 cr**) y luego 5 cr por
partido. Para no repagar discovery en cada tanda, usa `resume` (no llama a
`list_games`, solo 5 cr/partido).

**Plan de 448 créditos disponibles (dos tandas):**

```bash
# 1) piloto + discovery (crea checkpoints PENDING de toda la temporada)
python -m ingestion run --season 2025 --limit 3      # ≈ 31 cr (16 + 3x5)

# 2) continuar barato (sin discovery) hasta agotar presupuesto
python -m ingestion resume --limit 80                # ≈ 400 cr (80x5)

python -m ingestion budget                           # ~17 cr de colchón
```

Total: ~83 partidos reales (~1/4 de temporada). Suficiente para validar el
pipeline con datos reales; **insuficiente para entrenar un modelo útil** (hacen
falta temporadas completas). El plan gratuito repone a 500 cr cada mes, así que a
~85 partidos/mes se completa una temporada en ~4-5 meses; con Starter ($19 = 10k cr)
el histórico entero cae en ~2 meses.

## 6. Smoke test SIN gastar créditos (dataset demo)

```bash
python -m features demo
export STRIKER_DB_PATH=artifacts/demo.db STRIKER_COMPETITION=demo
python -m features build
python -m features backtest
python -m features predict
```

Si el backtest termina con logloss y ROI razonables, el stack Python,
DuckDB, scipy y sklearn están sanos.

## 7. PRIMERA prueba real controlada (3 partidos, ~17 créditos)

```bash
unset STRIKER_DB_PATH STRIKER_COMPETITION      # volver a la config del .env
python -m ingestion run --season 2025 --limit 3
python -m ingestion status
python -m ingestion budget
```

Verifica: partidos guardados, checkpoints COMPLETED, gasto ≈ 17 créditos.

## 8. Ingesta histórica completa (se autofrena con el presupuesto)

```bash
screen -S ingesta                       # para que sobreviva al ssh
python -m ingestion run                 # 2026 → 2017 descendente
python -m ingestion elo                 # ratings por fecha (barato)
python -m ingestion report             # resumen al terminar
```

Coste aproximado a 10.000 créditos/mes:
- `get_game` = 5 cr × 380 partidos ≈ **1.900 cr/temporada** (stats + cuotas)
- elo ≈ 76 cr/temporada (38 fechas × 2)
- histórico completo de La Liga (2017-18 → hoy, ~10 temporadas) ≈ **2 meses**

Tiempo puro de red respetando 60 req/min: ~40 min por temporada
(1900 llamadas); el histórico completo unas 3-5 horas repartidas en los
dos meses.

## 9. Features y predicción tras cada lote de ingesta

```bash
python -m features schedule --days 7   # fixtures próximos (2 cr) — si hay key
python -m features build               # matriz congelada as-of
python -m features backtest            # ROI walk-forward (semanal)
python -m features predict             # probs para próximos partidos
python -m features recommend odds.json --target-odds 2.5
```

## 10. Automatizar con cron

```bash
mkdir -p /opt/predictor/logs
crontab -e
```

Dentro del editor (adaptar rutas y temporada actual):

```
# diario 06:30 — refrescar la temporada EN CURSO (barato) + reanudar pendientes
30 6 * * * flock -n /tmp/ingest.lock bash -c "cd /opt/predictor && . .venv/bin/activate && python -m ingestion run --season 2025 && python -m ingestion elo --season 2025" >> /opt/predictor/logs/ingesta.log 2>&1

# diario 06:45 — reanudar partidos pendientes/falidos de TODAS las temporadas
45 7 * * * flock -n /tmp/ingest.lock bash -c "cd /opt/predictor && . .venv/bin/activate && python -m ingestion resume" >> /opt/predictor/logs/ingesta.log 2>&1

# diario 08:00 — features tras la ingesta
0 8 * * * flock -n /tmp/feat.lock bash -c "cd /opt/predictor && . .venv/bin/activate && python -m features build && python -m features predict" >> /opt/predictor/logs/features.log 2>&1

# lunes 09:00 — scoreboard semanal (vigilar drift del ROI)
0 9 * * 1 flock -n /tmp/feat.lock bash -c "cd /opt/predictor && . .venv/bin/activate && python -m features backtest" >> /opt/predictor/logs/backtest.log 2>&1
```

Racional de coste: `run --season <actual>` solo repagina el año en curso
(~16 cr de discovery + 5 cr por partido NUEVO; los COMPLETED no se re-compran).
El histórico viejo se procesa una sola vez con el `run` completo del apartado 8,
y `resume` recoge cualquier partido que haya quedado pendiente. No pongas
`run` sin `--season` en cron: repagaría el discovery de las 10 temporadas cada día.

Los comandos leen `STRIKER_DB_PATH` del `.env` solo si se ejecutan desde
`/opt/predictor`; por eso el `cd`+activate. `flock` evita solapamientos si un
run se alarga.

## 11. Monitorización recurrente

```bash
python -m ingestion status    # checkpoints y tablas
python -m ingestion budget    # gasto del mes vs 10k
python -m ingestion report    # partidos con cuotas/open-close, avg books
python -m features show       # NaN ratio de la matriz
tail -n 50 logs/ingesta.log
```

## 12. Backup (IMPORTANTE: los datos cuestan créditos)

```bash
# copia semanal a otro host/NAS (no dependas de un solo disco)
rsync -av --delete /opt/predictor/curated/ /opt/predictor/raw/ \
  backup@NAS:/backup/predictor/
```

`curated/striker.db` + `raw/` son reconstruibles solo re-pagando créditos.
`artifacts/` es regenerable gratis desde la BD.

## 13. Notas específicas de esta LXC

- **Sin docker**: no hace falta; todo es Python + DuckDB embebido.
- **RAM**: con 2 GB corre; los 4 GB dan holgura para cargar matrices grandes
  en pandas en `features show`/análisis manual.
- **CPUs**: el GBM y el Poisson usan 1-2 cores; los 3 cores sobran.
- **Reloj**: asume UTC en todo el stack (la API es UTC y la BD trabaja naive-UTC).
- Si cambias de plan de créditos, ajusta `STRIKER_MONTHLY_BUDGET` en `.env`;
  `STRIKER_RESERVE_CREDITS=500` protege de quedarte a 0.