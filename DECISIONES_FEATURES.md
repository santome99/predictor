# Decisiones de diseño — Fase de Features y Predicción

Registro razonado de cada decisión de la fase de **consumo de datos** (`features/`).
Cada decisión tiene un ID (D1..D24) referenciado en el código, la **justificación**,
las **alternativas consideradas** y el **coste/riesgo** aceptado. Objetivo: aprender
por qué el pipeline es como es y poder cambiarlo con criterio.

---

## D1 — Configuración única por variables de entorno
**Qué:** `features/config.py` lee `.env` (mismas claves que `ingestion`).
**Por qué:** el homelab ejecuta por cron sin perfil interactivo; un único punto de
configuración evita divergencia entre ingesta y features (misma BD, misma competición).
**Alternativas:** YAML/TOML aparte (más bonito, pero un segundo formato sin ganancia real),
constantes en código (prohibido: credenciales/rutas por entorno).
**Riesgo:** ninguna; `dotenv` no sobreescribe variables ya exportadas.

## D2 — Artefactos en `artifacts/`, separados del código
**Qué:** matrices, modelos y reportes van a `artifacts/`; el paquete `features/` es puro código.
**Por qué:** los artefactos son derivados reconstruibles (DuckDB/Parquet); el código es
versionable. Separarlos hace trivial: borrar artefactos, regenerar, versionar solo código,
y evita que commits arrastren gigabytes.
**Alternativas:** guardar dentro del paquete (malo: confunde código/datos), guardar en `curated/`
(mezcla nivel analítico con nivel de modelo).
**Riesgo:** hay que documentar la ruta; se resuelve con `FEATURES_ARTIFACTS_DIR`.

## D3 — Quitar el margen (overround) de las cuotas antes de usarlas como feature
**Qué:** las cuotas brutas 1X2 suman probabilidades implícitas > 1 (el margen del book).
Normalizamos `p_i / Σp` para obtener probabilidades "justas" (sin margen).
**Por qué:** un modelo entrenado con probabilidades infladas aprende un sesgo sistemático;
además queremos comparar nuestro modelo contra la **creencia real** de la casa, no contra su
comisión. Fórmula: `overround = Σ(1/dec) − 1`.
**Alternativas:** usar las probabilidades brutas (sencillo pero sesgado), restar el margen
proporcionalmente (variante refinada; la normalización simple es el estándar y suficiente).
**Riesgo:** la normalización asume margen uniforme entre resultados; en mercados sesgados
(favoritos vs outsiders) la casa no reparte el margen uniformemente. **Aceptado** como
aproximación de primer orden; monitorizamos el overround como diagnóstico.

## D4 — Mediana entre casas, no media
**Qué:** el consenso de mercado de un partido es la **mediana** de las cuotas por casa.
**Por qué:** una sola casa desviada (error de tecleo, libro suspendido, "dutching" agresivo)
distorsiona la media; la mediana es el precio de consenso robusto a outliers.
**Alternativas:** media (sensible a outliers), Pinnacle-close alone (excelente pero no
siempre presente en el dataset), media recortada (comparable; la mediana es más simple).
**Riesgo:** con 1-2 casas la mediana es solo la única casa disponible; documentamos
`n_books` para saber cuándo ocurre.

## D5 — El mercado Over/Under se guarda como par (línea, precio)
**Qué:** features `total_line_close`, `p_over_close` (probabilidad sin margen del over en
la línea de mercado). No asumimos línea fija 2.5 universal.
**Por qué:** el over/under cotizado varía de partido en partido (2.5, 2.75, 3...); modelar
"over 2.5" cuando la casa cotizaba over 3 es comparar peras con manzanas.
**Alternativas:** binarizar todo a 2.5 (más simple, menos fiel), ignorar el mercado de
totales (pierde señal valiosa).
**Riesgo:** la API solo publica precios over/under sobre el total de **cierre**; el análisis
de apertura en totales se limita a la línea (`total_line_open`).

## D6 — Rolling as-of con `shift(1)`: NUNCA incluir el partido propio
**Qué:** toda feature de forma se calcula con ventanas que solo ven **partidos estrictamente
anteriores**. Implementación: `.rolling(W).mean().shift(1)` sobre el log por equipo ordenado
por `starts_at`.
**Por qué:** si la media de "últimos 5 goles" incluyera el propio partido, la feature
**sabría el marcador objetivo**. Es la regla anti-data-leakage número uno y el test
`test_rolling_only_uses_past_matches` la bloquea explícitamente.
**Alternativas:** excluir por índice (equivalente), usar `expanding()` (usa TODO el pasado:
menos reactivo a cambios de forma), usar partido-t en adelante (**prohibido**).
**Riesgo:** ninguno; es la implementación canónica. La inspección se puede hacer con
`features show` (NaN ratio por columna).

## D7 — Elo con join as-of estricto (`<`, no `<=`)
**Qué:** para un partido en fecha T, se usa la última foto de Elo con `as_of < T`
(`merge_asof(allow_exact_matches=False)`), y además se descarta si `last_game >= T`.
**Por qué:** la API expone el rating "as of" un día; una foto del mismo día puede ya incluir
el resultado del partido (si el match es el único del día, `last_game == T` lo delata).
Doble candado = cero posibilidad de fuga.
**Alternativas:** `as_of <= T` (fuga sutil en partidos únicos del día), usar solo el top-n
(no; necesitamos rating del par concreto).
**Riesgo:** perdemos información de partidos el mismo día; insignificante en la práctica de liga.

## D8 — Ventanas de forma 5 y 10 partidos
**Qué:** `windows = (5, 10)`.
**Por qué:** 5 captura estado de forma reciente (lesiones, dinámicas); 10 suaviza ruido.
Dos horizontes dan al modelo la elección del peso vía el aprendizaje.
**Alternativas:** solo 5 (pierde contexto), 1/3/5/10/38 (más columnas, riesgo de
sobreajuste sin ganancia medida), ventanas por goles esperados (más sofisticado; fase 2).
**Riesgo:** con `min_periods=3` las primeras semanas de temporada producen NaN
→ el GBM los maneja nativamente (decisión D15).

## D9 — `min_periods=3`
**Qué:** una media rolling solo se emite con al menos 3 partidos previos observados; si no, NaN.
**Por qué:** medias de 1-2 partidos son ruido puro; marcar NaN (y no 0) es la forma honesta:
el modelo distingue "no sé" de "valor cero". Coherente con el principio del ingestor
("nunca convertir ausencia en cero").
**Alternativas:** min_periods=1 (ruido), valores de relleno (inventan información).
**Riesgo:** equipos nuevos/ascendidos empiezan a ciegas; el Elo (que sí tiene historia
desde 2001) cubre parcialmente esa ceguera.

## D10 — Matriz entrenamiento: UNA fila por partido
**Qué:** el ensamblador produce una fila por partido con todas las features y targets.
**Por qué:** es el formato universal de los modelos tabulares (XGBoost/GBM), encaja en
pandas/sklearn sin adaptadores, y es fácil de congelar en Parquet para reproducibilidad.
**Alternativas:** diseño largo (una fila por partido-equipo, más fiel para modelos de
equipo pero obliga a pivots y a duplicar joins), star-schema puro (mejor para SQL incremental,
peor para ML directo).
**Riesgo:** las features del equipo local y visitante conviven en columnas prefijadas
`h_*`/`a_*`; es solo convención de nombres, el modelo no pierde información.

## D11 — Matriz de entrenamiento congelada (`freeze_at`)
**Qué:** el builder filtra `starts_at < freeze_at` y escribe el corte. Re-ejecutar es idempotente.
**Por qué:** el corte en el tiempo es lo que hace honestas las versiones: puedes reconstruir
exactamente la matriz de una fecha pasada. Sin corte temporal, al añadir partidos nuevos las
features del pasado no cambian, pero el alcance de la matriz sí; el freeze lo hace explícito.
**Alternativas:** matriz siempre "todo lo que hay" (indistinguible de un corte al delete),
echar snapshots a un VCS (overkill aquí).
**Riesgo:** ninguno estructural; hay que acordarse de re-ejecutar `build` antes de `backtest`
(u `predict`) cuando llegan datos nuevos.

## D12 — Timestamps tz-naive consistentes (UTC) en la matriz
**Qué:** DuckDB devuelve timestamps naive; la freeze y todas las comparaciones se normalizan
a naive-UTC.
**Por qué:** mezclar tz-aware con tz-naive en pandas lanza excepciones en las comparaciones;
todo el ecosistema (API Striker = UTC, DB naive) es UTC. El partido se conoce por su instante
en UTC y no importa la zona local.
**Alternativas:** forzar tz-aware en todo el pipeline (más trabajo, cero ganancia aquí).
**Riesgo:** al mostrar predicciones recordar que es UTC (+2h en verano peninsular).

## D13 — Poisson generativo (goles) + GBM discriminativo (1X2)
**Qué:** dos familias de modelo:
1. **Poisson bivariado** por equipos (ataque/defensa + ventaja local), ajustado por máxima
   verosimilitud. Genera la **matriz de marcadores** → todos los mercados derivados
   (1X2, over/under cualquier línea, BTTS).
2. **GBM tabular** (histogram-binned) para 1X2 sobre features contextuales.
3. **Blend** pesado (por defecto 0.5/0.5) para 1X2; el Poisson es la única fuente de
   marcadores (líneas, BTTS).
**Por qué:** el Poisson es el modelo natural de goles (eventos contables raros), con pocos
parámetros y robusto a datos escasos → espina dorsal. El GBM captura interacciones no lineales
(form, descanso, mercado) que el Poisson ignora. Mezclar ambos suele batir a cualquiera por
separado en 1X2 (diversificación de sesgos).
**Alternativas:** solo Poisson (simple, peor en 1X2), solo GBM (pierde el espacio de
marcadores), Dixon-Coles completo con ρ de correlación de bajas marcadas (mejora matizada;
fase 2 si el dataset lo pide).
**Riesgo:** el blend es un hiperparámetro más (`poisson_weight`); se calibra con el backtest.

## D14 — Truncado de la matriz de marcadores a 8 goles
**Qué:** `max_goals=8`; las filas se renomalizan.
**Por qué:** la Poisson tiene soporte infinito; 8 goles cubre prácticamente todas las
marcas de ligas reales (P(X≥8) ≈ 0.2-0.5% por equipo) y hace finitas las sumas.
**Alternativas:** integración continua (complejidad sin beneficio), truncar a 5 (perdería
masa en partidos goleadores).
**Riesgo:** en el demo sintético las lambdas llegan a ~4 y truncar a 8 distorsionó las
cuotas demo — **lección registrada**: el truncado debe ir alineado con el rango de lambdas
del dataset (en demo usamos 12).

## D15 — `HistGradientBoostingClassifier` de sklearn en vez de XGBoost/LightGBM
**Qué:** el GBM es el equivalente integrado de LightGBM (binning por histogramas) de scikit-learn.
**Por qué:** (a) no añade dependencias pesadas al homelab (sin wheels de lightgbm/xgboost);
(b) **soporta NaN como feature nativa** (crucial: partidos sin cuotas o sin Elo siguen
entrenando); (c) la API sklearn es estable y bien documentada. Cambiar luego a LightGBM
real es reemplazar el estimador, no el pipeline.
**Alternativas:** XGBoost (maduro, histórico, pero NaN hay que imputarlo), LightGBM (más
rápido en datos grandes; nuestras decenas de miles de filas son triviales para ambos),
CatBoost (excelente con categóricas; aquí no hay).
**Riesgo:** límite de performance última respecto a LightGBM en datos enormes; irrelevante
para ~10-40k filas de partidos.

## D16 — Backtest walk-forward, NUNCA CV aleatorio
**Qué:** siempre entrenar con partidos estrictamente anteriores y evaluar sobre bloques
futuros.
**Por qué:** el fútbol es serie temporal: un split aleatorio deja al modelo "ver" ambos
lados de cada semana de la misma temporada (fuga temporal) e infla artificialmente las
métricas. El walk-forward imita la producción (apuestas solo con lo conocido hasta el
viernes).
**Alternativas:** k-fold temporal aislado (variante válida), CV aleatorio (**prohibido**).
**Riesgo:** ninguna; es más lento, y es el coste de la honestidad.

## D17 — Ventana expansiva por temporada
**Qué:** para evaluar la temporada T se entrena con todas las temporadas < T.
**Por qué:** replica el despliegue real (todo lo histórico disponible hasta hoy) y da un
punto de evaluación natural (fin de temporada) con bloques de ~380 partidos, suficiente
potencia estadística.
**Alternativas:** ventana deslizante de N partidos (más reactiva a cambios de "régimen",
pero pierde memoria larga útil para Elo/estilos), bloques mensuales (ruidosos).
**Riesgo:** la meta-optimización sobre el conjunto de test final siempre es un peligro;
mitigación: no tunes hiperparámetros sobre el último bloque sin re-validar.

## D18 — EV/ROI medidos contra el CIERRE; apertura aparte
**Qué:** las apuestas simuladas se liquidan contra la cuota medial de **cierre**; la
apertura se reporta por separado.
**Por qué:** el cierre es la mejor estimación del mercado en el instante previo al partido;
batirlo es el test más duro y honesto de que hay habilidad. Batir la apertura es más fácil
(su precio aún absorbe poca información) y se reporta porque un apostador real puede
"apostar temprano".
**Alternativas:** medir contra la apertura (optimista), contra la cuota media del día
(intermedio; no la guardamos por partido en la fase 1).
**Riesgo:** con 1-2 casas por partido, la "cuota de cierre" es la de una casa; `n_books`
queda disponible para filtrar.

## D19 — Umbral de apuesta EV > 0 (configurable)
**Qué:** se apuesta 1 unidad si `p_modelo × cuota − 1 > ev_threshold` (defecto 0).
**Por qué:** el EV positivo es exactamente el criterio de "value"; el umbral configurable
permite exigir más margen para filtrar oportunidades marginales ruidosas. Liquidación
estándar: gana → `+(cuota−1)`, pierde → `−1`; ROI = P/L acumulado / stake total.
**Alternativas:** apostar siempre al pick (sin criterio de valor → pérdidas garantizadas
ante el margen), fracción de Kelly (óptima a largo plazo pero volátil y sensible al error
de estimación; fase 2).
**Riesgo:** estimamos valor con probabilidades mal calibradas → backtest lo revela; el
siguiente paso natural es calibrar las probabilidades del blend.

## D20 — Mismo pipeline para predecir fixtures (ningún atajo)
**Qué:** `predict` reutiliza exactamente el flujo de features del builder, con cortes
as-of AHORA.
**Por qué:** garantiza coherencia train/serve (el "training-serving skew" es la causa nº1
de modelos rotos en producción). El archivo `feature_columns.json` actúa de contrato.
**Alternativas:** código predict separado tapando huecos (reintroduce skew).
**Riesgo:** ninguna real; obliga a recapacitar si feature de training no existe en serving.

## D21 — Features ausentes en inferencia → NaN, no imputación
**Qué:** si un fixture aún no tiene cuotas (o un Elo), la columna llega NaN y el GBM la
trata como su propia dirección de rama.
**Por qué:** respecto a la imputación (media/cero), el NaN es información honesta
("todavía no observado") y el histogram-GBM la explota correctamente. El Poisson no
necesita esas columnas.
**Alternativas:** imputar mediana (borra la señal de ausencia), excluir esos partidos
(no predecir hasta el cierre = inútil).
**Riesgo:** en mercados sin cuotas el blend pierde la señal de mercado; lo compensamos con
forma+Elo y documentamos.

## D22 — CLI `features` separado de `ingestion`
**Qué:** `python -m features build|backtest|predict|show|demo|schedule`.
**Por qué:** dominios de fallo distintos: ingesta = red+créditos+crawling (corre en
intervalos), features = cómputo local sin red (se lanza tras la ingesta). Separarlos
permite cron/cadencia independientes y pruebas aisladas.
**Alternativas:** un solo CLI monolítico (simple al inicio, acoplado después).
**Riesgo:** hay que documentar la cadencia (README); asumido.

## D23 — Fixtures vía `get_schedule` refresh separado (2 créditos)
**Qué:** `python -m features schedule --days N` trae próximos fixtures y los guarda en
`games` como `scheduled`.
**Por qué:** así `predict` funciona 100% offline después; el coste de refresco es
marginal (2 cr/llamada) y el dato queda persistido.
**Alternativas:** llamar a get_schedule en cada predict (dependencia de red/créditos),
leer RSS/webs (licencias inciertas).
**Riesgo:** requiere API key; si no hay, `predict` informa que no hay fixtures.

## D24 — Dataset demo sintético determinista separado de datos reales
**Qué:** `python -m features demo` genera una BD DuckDB completa (12 equipos, 4 temporadas,
cuotas con margen, Elo) con semilla fija, en otra ruta.
**Por qué:** permite aprender y validar el pipeline end-to-end SIN clave ni créditos, y
con un mecanismo generador CONOCIDO (se puede verificar que el modelo recupera las fuerzas
escondidas). Jamás mezclado con datos reales (competition="demo").
**Lecciones reales que ya ha dado el demo:** (1) el bug del factorial en la pmf Poisson
distorsionó las cuotas demo y el backtest lo detectó como ROI absurdo — excelejemplo de
por qué simulaciones y tests importan; (2) el truncado de la matriz de marcadores debe
seguir el rango de lambdas (D14).
**Alternativas:** probar con datos reales directamente (gasta créditos y no permite saber
la "verdad"), fixtures a mano (tedioso).
**Riesgo:** falsa confianza si el demo diverge del real; mitigada porque el generador
replica los mercados y las tablas 1:1.

## D25 — Cuotas del usuario por JSON pegado, NO scraping de casas
**Qué:** `python -m features recommend <archivo|JSON|stdin>`. El usuario copia los mercados
que ve en la página de su casa (Winamax, etc.) a un JSON pequeño y nosotros los corregimos
con el modelo. Se descarta raspar SPAs: comprobado en vivo que la página de Winamax sirve
"solo HTML con 'Cargando...'" y las cuotas llegan por JS privado.
**Por qué:** (a) raspado SPA es frágil (se rompe semanalmente, requiere headless browser o
API interna sin documentar); (b) términos de servicio (el scraping masivo lo prohíben);
(c) copiar 3 números por partido tarda menos que mantener un scraper. El JSON también es
la entrada natural si mañana se contrata un feed de cuotas con licencia (solo cambia el
origen, no el motor).
**Alternativas:** scraper SPA (frágil/ToS), APIs públicas de cuotas sin licencia (no
permiten uso comercial), apostar solo con el feed de StrikerAPI (cuotas medianas internas,
sin las casas finales).
**Riesgo:** error humano al copiar; se mitiga mostrando SIEMPRE la cuota interpretada y su
`p_breakeven` para que el usuario detecte errores de un vistazo.

## D26 — Recomendación = ranking CON restricciones, con criterio explícito
**Qué:** el motor filtra por ventana de cuota (`target ± tolerance`) y por `EV >= min_ev`,
y ordena por `criterion = value` (EV) o `safety` (prob. del modelo). La salida muestra
ambas columnas.
**Por qué:** "cuota 2.5 lo más segura" esconde una contradicción matemática: 2.5 exige
p ≥ 0.40 solo para el empate; "segura" empuja hacia probabilidades altas (cuotas bajas).
Al mostrar `p_model` y `p_breakeven` juntos, el usuario ve exactamente qué pide a cambio.
**Alternativas:** un único ranking por EV sin restricción de cuota (ignora la petición),
sin umbral de EV (recomienda picks perdedores), Kelly automático (fase 2).
**Riesgo:** si el usuario fija `min_ev=0`, picks con edge marginal pasan; se documenta y
es configurable.

## D27 — Mercados del recommend: 1X2, O/U en la línea cotizada, BTTS
**Qué:** el recommendar puntúa 1X2 (blend Poisson+GBM cuando hay features; si no, Poisson
puro), over/under **en la línea que la casa cotiza** (nunca una línea fija), y BTTS desde
el espacio de marcadores. Hándicaps: fase posterior (requiere la matriz de marcadores al
rango de la línea exacta).
**Por qué:** son los mercados que (a) la API de StrikerAPI cubre, (b) el modelo Poisson
deriva directamente de su matriz de scores, y (c) el usuario pedirá primero.
**Alternativas:** añadir hándicap ya (complejidad ahora sin demanda), añadir markets de
jugador (requieren modelo de props, inexistente).
**Riesgo:** ninguna permanente; el motor es extensible añadiendo branches en `score()`.

## D28 — Equipos desconocidos: se excluyen, no se adivina
**Qué:** si el nombre pegado no coincide con ningún equipo de la BD (`TeamResolver`), el
partido se saca de la recomendación con aviso.
**Por qué:** recomendar con un modelo que no tiene historia de esos equipos es emitir
ruido con autoridad. Mejor decir "no lo cubro" (la honestidad es parte del value betting:
el primer enemigo del apostador es apostar sin edge).
**Alternativas:** usar solo probabilidad implícita del mercado (sin modelo, sería decir
"el mercado dice X" — útil como feature pero no como recomendación de valor).
**Riesgo:** renunciar a partidos de copa/amistosos; compensado por exactitud.

## D29 — EV sobre cuotas SIN normalizar (el precio real apostable)
**Qué:** `EV = p_model × cuota_pegada − 1`, con la cuota tal cual la pegó el usuario
(incluido el margen de la casa).
**Por qué:** es el dinero real que puede ganar/apostar; normalizar primero (quitar margen)
sería comparar con un precio inexistente. La probabilidad implícita con margen se muestra
como `p_breakeven` (=1/cuota) para que se vea el "precio" que el modelo debe superar.
**Alternativas:** EV vs probabilidad sin margen (más optimista de lo que es apostable).
**Riesgo:** probabilidades del modelo aún sin calibrar (limitación conocida) → el EV del
backtest es la referencia de calibración; usar `--min-ev` conservador hasta calibrar.

## D30 — `p_breakeven` junto a `p_model` en toda salida
**Qué:** toda fila recomendada muestra además de la prob. del modelo la probabilidad que
exige la cuota (`p_breakeven = 1/cuota`).
**Por qué:** es la única cifra que conecta intuición ("¿es segura?") con matemática
("¿paga su riesgo?"). Una apuesta con cuota 42 puede parecer atractiva; ver p_breakeven
0.024 la sitúa.
**Alternativas:** mostrar solo EV (pierde contexto), mostrar solo p (oculta el precio).
**Riesgo:** ninguna; añade transparencia.

---

## Flujo de trabajo estándar

```bash
# 0) una vez: datos demo para aprender sin créditos
python -m features demo
STRIKER_DB_PATH=artifacts/demo.db STRIKER_COMPETITION=demo python -m features build
STRIKER_DB_PATH=artifacts/demo.db STRIKER_COMPETITION=demo python -m features backtest
STRIKER_DB_PATH=artifacts/demo.db STRIKER_COMPETITION=demo python -m features predict

# 1) con datos reales ingeridos (curated/striker.db)
python -m features build        # matriz congelada
python -m features backtest     # walk-forward + ROI vs cierre
python -m features schedule --days 7   # fixtures (con API key)
python -m features predict      # probabilidades por fixture
python -m features show         # inspección de la matriz
```

## Lectura del informe del backtest

| Métrica | Qué significa |
|---|---|
| `logloss_1x2` | error de las probs 1X2 (menor mejor; ~0.90-1.05 típico en fútbol) |
| `brier_home/draw/away` | error cuadrático por resultado (menor mejor) |
| `accuracy` | % de picks ganadores (limitada: no mide calibración) |
| `bets_*_close` | nº de apuestas donde EV>0 vs cierre |
| `pl_*_close` | beneficio acumulado simulado (1 u/apuesta) |
| `roi_*_close` | beneficio / staked total. **>0 sostenido = señal de edge** |
| `pl_*_open` | lo anterior pero contra cuotas de apertura (más fácil de batir) |

## Limitaciones conocidas de esta fase

1. **Sin calibración explícita de probabilidades** (isotonic/Platt) — siguiente paso
   natural si el ROI del backtest es positivo pero marginal.
2. **xG de StrikerAPI no es as-of** (la API lo refita sobre temporadas enteras): las
   features rolling que incluyen xG (`lag_xg`) son potencialmente revisables y deben
   tratarse con la cautela documentada en el audit; para trabajo estricto conviene
   recalcular xG propio desde localizaciones (fase events).
3. **Cuotas escasas en fixtures**: antes del cierre oficial el mercado aún no cotiza
   el partido; `predict` degrada elegantemente a Elo+forma (NaN → GBM).
4. **Un solo modelo por competición**: `laliga` y `ucl` son espacios de features
   separados por `competition` en la BD; el builder entrena por competición.
5. **Mercados cubiertos**: 1X2, over/under (línea de mercado), BTTS. Sin BTTS apostable
   directo en la API (ver auditoría), se obtiene vía Poisson como probabilidad.
6. **La "cuota de cierre" es la mediana entre las casas guardadas** (`n_books` permite
   filtrar partidos con cobertura escasa).