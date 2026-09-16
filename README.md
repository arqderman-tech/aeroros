# aeroros

Tablero de vuelos del Aeropuerto de Rosario (ROS) con acumulación de datos
históricos y reportes de asientos por aerolínea y ruta.

## Fuentes

- **FIDS oficial** (aeropuertorosario.com) — fuente de verdad de horarios y estados
- **FlightRadar24** (tablero + histórico) — tipo de avión y matrícula
- **OpenSky** (base de aeronaves) — validación de matrícula → tipo
- **Correcciones manuales** — tabla en DB para casos donde FR24 está mal

## Cómo corre

Vía GitHub Actions, dos veces por día:
- 00:00 ART (03:00 UTC)
- 12:00 ART (15:00 UTC)

Cada corrida:
1. Lee FIDS + FR24 (tablero actual)
2. Enriquece con OpenSky, cache local y FR24 histórico
3. Aplica correcciones manuales por matrícula
4. Actualiza la base SQLite (`vuelos_ros.db`)
5. Regenera los reportes CSV
6. Commitea los cambios al repo

## Ejecución manual

    python test.py

Opciones útiles:

    python test.py --dump-raw                    # guarda respuestas crudas en ./dumps/
    python test.py --debug-hist LA2432           # volcar crudo de un vuelo
    python test.py --sin-fr24                    # sin FR24 tablero
    python test.py --sin-reportes                # sin reportes de asientos
    python test.py --sin-migracion               # sin migración de DB

## Archivos de estado (van al repo)

- `vuelos_ros.db` — base SQLite con todo el histórico acumulado
- `aircraft_cache.json` — cache de aeronaves por aerolínea+vuelo
- `fleet_cache.json` — flota inferida por aerolínea
- `fr24_history_cache.json` — respuestas del histórico FR24
- `opensky_aircraft_db.csv` — base de OpenSky (66 MB, se baja una vez cada 3-4 meses)
- `reporte_asientos_*.csv` — reportes mensuales
- `vuelos_ros.csv` — ventana actual
- `vuelos_ros_historico.csv` — toda la DB

## Agregar correcciones manuales

Cuando detectes una matrícula con tipo mal en FR24, agregala a la DB:

    INSERT OR REPLACE INTO correcciones_matricula
    VALUES ('HP-9820CMP', 'B38M', 'Boeing 737 MAX 8', 'Boeing', 'manual', 'FR24 tenía 738 por error');

La próxima corrida la aplica automáticamente.

## Actualizar la base de OpenSky

Cada 3-4 meses:

1. Borrar `opensky_aircraft_db.csv`
2. Cambiar la constante `OPENSKY_DB_URL` en `test.py` a un mes reciente
3. Correr el workflow manualmente

## Estructura del proyecto

    aeroros/
    ├── test.py
    ├── requirements.txt
    ├── .gitignore
    ├── README.md
    ├── .github/
    │   └── workflows/
    │       └── corrida.yml
    ├── vuelos_ros.db            (generado)
    ├── *.json                    (generados)
    └── reporte_*.csv            (generados)
