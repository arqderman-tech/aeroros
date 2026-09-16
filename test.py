# test.py
# ---------------------------------------------------------------------------
# Tablero ROS: FIDS + FR24 tablero + FR24 histórico + OpenSky + turnaround
# + base de datos acumulativa SQLite + reportes de asientos
# + correcciones manuales por matrícula + export JSON para dashboard HTML.
# ---------------------------------------------------------------------------
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import requests

try:
    csv.field_size_limit(sys.maxsize)
except OverflowError:
    csv.field_size_limit(2**31 - 1)

_FR24_DISPONIBLE = False
_FR24_ERROR = ""
try:
    from FlightRadarAPI import FlightRadar24API  # type: ignore
    _FR24_DISPONIBLE = True
except Exception as exc:
    _FR24_ERROR = f"{type(exc).__name__}: {exc}"

try:
    from bs4 import BeautifulSoup
    _BS4_DISPONIBLE = True
except ImportError:
    _BS4_DISPONIBLE = False

try:
    from curl_cffi import requests as cf_requests
    _CF_DISPONIBLE = True
except ImportError:
    _CF_DISPONIBLE = False


# ---------------------------------------------------------------------------
# CONFIGURACIÓN
# ---------------------------------------------------------------------------
AEROPUERTO = "ROS"
TZ_LOCAL = ZoneInfo("America/Argentina/Buenos_Aires")

FIDS_URL = "https://aeropuertorosario.com/wp-json/fids/v1/vuelos"
FIDS_TIMEOUT = 20

OPENSKY_DB_URL = (
    "https://s3.opensky-network.org/data-samples/metadata/"
    "aircraft-database-complete-2025-08.csv"
)
OPENSKY_CACHE = "opensky_aircraft_db.csv"
OPENSKY_TIMEOUT = 180

AIRCRAFT_CACHE = "aircraft_cache.json"
FLEET_CACHE = "fleet_cache.json"
HIST_CACHE = "fr24_history_cache.json"
DB_PATH = "vuelos_ros.db"
DUMP_DIR = "dumps"
DASHBOARD_JSON = "docs/datos.json"

FR24_PAUSA = 1.5
FR24_HIST_PAUSA = 1.2
FR24_HIST_MAX_POR_CORRIDA = 40
TOLERANCIA_MIN = 60

VENTANA_PASADO_H = 30
VENTANA_FUTURO_H = 30

_DUMP_RAW = False

CONF_NUM = {
    "":             -1,
    "suposicion":    0,
    "cache_modelo":  1,
    "cache":         2,
    "historico":     3,
    "real":          4,
}


# ---------------------------------------------------------------------------
# NOMBRES DE AEROLÍNEAS (fallback cuando FR24 no da nombre)
# ---------------------------------------------------------------------------
NOMBRES_AEROLINEAS: dict[str, str] = {
    "CM": "Copa Airlines",
    "AR": "Aerolíneas Argentinas",
    "LA": "LATAM Airlines",
    "G3": "GOL Linhas Aéreas",
    "DM": "Arajet",
    "FO": "Flybondi",
    "2W": "World2Fly",
    "ZP": "Paranair",
    "O4": "Andes Líneas Aéreas",
    "AJ": "American Jet",
}


# ---------------------------------------------------------------------------
# ALIAS DE AEROLÍNEAS
# ---------------------------------------------------------------------------
AEROLINEA_ALIAS: dict[str, str] = {
    "W1": "DM",
    "W7": "DM",
    "8D": "DM",
}


def _aero_canonica(codigo: str) -> str:
    c = (codigo or "").strip().upper()
    return AEROLINEA_ALIAS.get(c, c)


# ---------------------------------------------------------------------------
# NORMALIZACIÓN DE TIPOS
# ---------------------------------------------------------------------------
IATA_A_ICAO: dict[str, str] = {
    "737": "B737", "738": "B738", "739": "B739", "73G": "B737",
    "73H": "B738", "73W": "B737",
    "7M7": "B37M", "7M8": "B38M", "7M9": "B39M", "7MJ": "B3XM",
    "318": "A318", "319": "A319", "320": "A320", "321": "A321",
    "32N": "A20N", "32Q": "A21N", "32A": "A320", "32B": "A321",
    "32S": "A20N",
    "330": "A330", "332": "A332", "333": "A333", "339": "A339",
    "340": "A340", "343": "A343", "346": "A346",
    "350": "A350", "351": "A351", "359": "A359", "35K": "A35K",
    "380": "A388",
    "744": "B744", "747": "B744", "748": "B748",
    "757": "B752", "75W": "B752",
    "763": "B763", "764": "B764", "767": "B763",
    "772": "B772", "773": "B773", "77L": "B77L", "77W": "B77W",
    "787": "B787", "788": "B788", "789": "B789", "78X": "B78X",
    "E70": "E170", "E75": "E175", "E90": "E190", "E95": "E195",
    "E170": "E170", "E175": "E175", "E190": "E190", "E195": "E195",
    "E75L": "E75L", "E75S": "E75S",
    "CR1": "CRJ1", "CR2": "CRJ2", "CR7": "CRJ7", "CR9": "CRJ9",
    "CRK": "CRJX", "CRJ2": "CRJ2", "CRJ7": "CRJ7", "CRJ9": "CRJ9",
    "CRJX": "CRJX",
    "AT4": "AT44", "AT5": "AT45", "AT7": "AT72",
    "AT44": "AT44", "AT45": "AT45", "AT72": "AT72",
    "F70": "F70", "F100": "F100", "M82": "MD82", "M83": "MD83",
    "MD82": "MD82", "MD83": "MD83",
}


def _normalizar_tipo(tipo: str) -> str:
    if not tipo:
        return ""
    t = tipo.strip().upper()
    return IATA_A_ICAO.get(t, t)


TYPE_CODE_A_MODELO: dict[str, tuple[str, str]] = {
    "B737": ("Boeing 737-700", "Boeing"), "B738": ("Boeing 737-800", "Boeing"),
    "B739": ("Boeing 737-900", "Boeing"),
    "B37M": ("Boeing 737 MAX 7", "Boeing"), "B38M": ("Boeing 737 MAX 8", "Boeing"),
    "B39M": ("Boeing 737 MAX 9", "Boeing"),
    "A318": ("Airbus A318", "Airbus"), "A319": ("Airbus A319", "Airbus"),
    "A320": ("Airbus A320", "Airbus"), "A321": ("Airbus A321", "Airbus"),
    "A20N": ("Airbus A320neo", "Airbus"), "A21N": ("Airbus A321neo", "Airbus"),
    "E170": ("Embraer E170", "Embraer"), "E175": ("Embraer E175", "Embraer"),
    "E190": ("Embraer E190", "Embraer"), "E195": ("Embraer E195", "Embraer"),
    "E75L": ("Embraer E175", "Embraer"), "E75S": ("Embraer E175", "Embraer"),
    "A332": ("Airbus A330-200", "Airbus"), "A333": ("Airbus A330-300", "Airbus"),
    "A339": ("Airbus A330-900neo", "Airbus"),
    "A343": ("Airbus A340-300", "Airbus"), "A346": ("Airbus A340-600", "Airbus"),
    "A359": ("Airbus A350-900", "Airbus"), "A35K": ("Airbus A350-1000", "Airbus"),
    "A388": ("Airbus A380-800", "Airbus"),
    "B744": ("Boeing 747-400", "Boeing"), "B748": ("Boeing 747-8", "Boeing"),
    "B752": ("Boeing 757-200", "Boeing"),
    "B763": ("Boeing 767-300", "Boeing"), "B764": ("Boeing 767-400", "Boeing"),
    "B772": ("Boeing 777-200", "Boeing"), "B773": ("Boeing 777-300", "Boeing"),
    "B77L": ("Boeing 777-200LR", "Boeing"), "B77W": ("Boeing 777-300ER", "Boeing"),
    "B787": ("Boeing 787 Dreamliner", "Boeing"),
    "B788": ("Boeing 787-8 Dreamliner", "Boeing"),
    "B789": ("Boeing 787-9 Dreamliner", "Boeing"),
    "B78X": ("Boeing 787-10 Dreamliner", "Boeing"),
    "CRJ1": ("Bombardier CRJ-100", "Bombardier"),
    "CRJ2": ("Bombardier CRJ-200", "Bombardier"),
    "CRJ7": ("Bombardier CRJ-700", "Bombardier"),
    "CRJ9": ("Bombardier CRJ-900", "Bombardier"),
    "CRJX": ("Bombardier CRJ-1000", "Bombardier"),
    "AT44": ("ATR 42-400", "ATR"), "AT45": ("ATR 42-500", "ATR"),
    "AT72": ("ATR 72", "ATR"),
    "F70": ("Fokker 70", "Fokker"), "F100": ("Fokker 100", "Fokker"),
    "MD82": ("McDonnell Douglas MD-82", "McDonnell Douglas"),
    "MD83": ("McDonnell Douglas MD-83", "McDonnell Douglas"),
}


def _modelo_desde_tipo(tipo: str) -> tuple[str, str] | None:
    if not tipo:
        return None
    return TYPE_CODE_A_MODELO.get(_normalizar_tipo(tipo))


# ---------------------------------------------------------------------------
# DUMP RAW
# ---------------------------------------------------------------------------
def _dump_json(name: str, data: Any) -> None:
    if not _DUMP_RAW:
        return
    os.makedirs(DUMP_DIR, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    path = os.path.join(DUMP_DIR, f"{safe}.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# MODELO Y NORMALIZACIÓN DE VUELO
# ---------------------------------------------------------------------------
def _extraer_aerolinea(num: str) -> str:
    """Extrae código IATA (2 alfanum con al menos 1 letra) o ICAO (3 letras).
    Aplica alias (W1 → DM).
    """
    num = (num or "").strip().upper()
    if not num:
        return ""
    if len(num) >= 4 and num[:3].isalpha() and num[3].isdigit():
        return _aero_canonica(num[:3])
    if len(num) >= 3 and any(c.isalpha() for c in num[:2]) and num[2].isdigit():
        return _aero_canonica(num[:2])
    i = 0
    while i < len(num) and num[i].isalpha():
        i += 1
    return _aero_canonica(num[:i])


def _normalizar_vuelo(v: str) -> str:
    """Devuelve solo el número, sin prefijo de aerolínea.
    'G37722' → '7722', 'W14256' → '4256', 'CM882' → '882', '7722' → '7722'.
    Usa el prefijo RAW (sin aplicar alias) para que W1→DM no rompa el strip.
    """
    v = (v or "").strip().upper()
    if not v:
        return ""
    # Si empieza con dígito, no tiene prefijo
    if v[0].isdigit():
        return v
    # ICAO: 3 letras + dígito (AUA123)
    if len(v) >= 4 and v[:3].isalpha() and v[3].isdigit():
        return v[3:]
    # IATA: 2 alfanum (al menos 1 letra) + dígito (G3, 2W, W1, CM)
    if len(v) >= 3 and any(c.isalpha() for c in v[:2]) and v[2].isdigit():
        return v[2:]
    # Fallback: solo letras iniciales
    i = 0
    while i < len(v) and v[i].isalpha():
        i += 1
    return v[i:] or v


@dataclass
class Vuelo:
    vuelo: str = ""
    direccion: str = ""
    horario_local: str = ""
    horario_real: str = ""
    horario_estimado: str = ""
    estado: str = ""
    origen_destino: str = ""
    aeropuerto_iata: str = ""
    ruta: str = ""
    aerolinea_codigo: str = ""
    aerolinea_nombre: str = ""
    aerolinea_color: str = ""
    puerta: str = ""
    sector: str = ""
    matricula: str = ""
    modelo_avion: str = ""
    fabricante: str = ""
    icao_type_code: str = ""
    icao24_hex: str = ""
    operador: str = ""
    fuentes: str = ""
    id_externo: str = ""
    confianza: str = ""
    _nota: str = ""
    asientos: int = 0

    def clave(self) -> tuple[str, str, str]:
        return (_normalizar_vuelo(self.vuelo), self.direccion, self.horario_local)

    def clave_db(self) -> tuple[str, str, str]:
        return (_normalizar_vuelo(self.vuelo), self.direccion, self.fecha_iso())

    def clave_cache(self) -> str:
        aero = (self.aerolinea_codigo or "").strip().upper() or _extraer_aerolinea(self.vuelo)
        return f"{aero}|{_normalizar_vuelo(self.vuelo)}|{self.direccion}"

    def fecha_iso(self) -> str:
        return self.horario_local[:10] if len(self.horario_local) >= 10 else ""


# ---------------------------------------------------------------------------
# UTILIDADES
# ---------------------------------------------------------------------------
def _t(v: Any, default: str = "") -> str:
    return v if isinstance(v, str) and v.strip() else default


def _iso_a_local(iso_str: str | None) -> str:
    if not iso_str or not isinstance(iso_str, str):
        return ""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ_LOCAL)
        return dt.astimezone(TZ_LOCAL).strftime("%Y-%m-%dT%H:%M")
    except (ValueError, TypeError):
        return ""


def _fecha(horario_local: str) -> str:
    return horario_local[:10] if len(horario_local) >= 10 else ""


def _parse_horario(s: str) -> datetime | None:
    if not s or len(s) < 16:
        return None
    try:
        return datetime.strptime(s[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=TZ_LOCAL)
    except ValueError:
        return None


def _hhmm_a_min(horario_local: str) -> int:
    if len(horario_local) < 16:
        return -1
    try:
        return int(horario_local[11:13]) * 60 + int(horario_local[14:16])
    except (ValueError, IndexError):
        return -1


def _variantes_registro(reg: str) -> set[str]:
    reg = (reg or "").strip().upper()
    if not reg:
        return set()
    return {reg, reg.replace("-", ""), reg.replace(" ", ""),
            reg.replace("-", "").replace(" ", "")}


def _limpiar_celda(s: str) -> str:
    if not isinstance(s, str):
        return ""
    return s.strip().strip("'\"").lstrip("\ufeff").strip()


def _buscar_col(row: dict, *nombres: str) -> str:
    for n in nombres:
        n_clean = _limpiar_celda(n).lower()
        for k in row.keys():
            if k is None:
                continue
            if _limpiar_celda(k).lower() == n_clean:
                v = row.get(k)
                if isinstance(v, str):
                    v = _limpiar_celda(v)
                if v:
                    return v
    return ""


def _en_ventana(horario_local: str, desde: datetime, hasta: datetime) -> bool:
    dt = _parse_horario(horario_local)
    if dt is None:
        return False
    return desde <= dt <= hasta


def _calcular_ruta(v: Vuelo) -> str:
    iata = (v.aeropuerto_iata or "").strip().upper()
    if not iata:
        return ""
    return f"{iata} → {AEROPUERTO}" if v.direccion == "Arrival" else f"{AEROPUERTO} → {iata}"


# ---------------------------------------------------------------------------
# BASE DE DATOS
# ---------------------------------------------------------------------------
def db_conectar() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def db_init() -> None:
    con = db_conectar()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS vuelos (
                vuelo_norm       TEXT NOT NULL,
                direccion        TEXT NOT NULL,
                fecha            TEXT NOT NULL,
                vuelo            TEXT,
                horario_local    TEXT,
                horario_real     TEXT,
                horario_estimado TEXT,
                estado           TEXT,
                origen_destino   TEXT,
                aeropuerto_iata  TEXT,
                ruta             TEXT DEFAULT '',
                aerolinea_codigo TEXT,
                aerolinea_nombre TEXT,
                aerolinea_color  TEXT,
                puerta           TEXT,
                sector           TEXT,
                matricula        TEXT,
                modelo_avion     TEXT,
                fabricante       TEXT,
                icao_type_code   TEXT,
                icao24_hex       TEXT,
                operador         TEXT,
                fuentes          TEXT,
                id_externo       TEXT,
                confianza        TEXT,
                confianza_num    INTEGER,
                nota             TEXT,
                asientos         INTEGER DEFAULT 0,
                primera_vez      TEXT,
                ultima_vez       TEXT,
                PRIMARY KEY (vuelo_norm, direccion, fecha)
            )
        """)
        for col, tipo in [("ruta", "TEXT DEFAULT ''"),
                          ("asientos", "INTEGER DEFAULT 0")]:
            try:
                con.execute(f"ALTER TABLE vuelos ADD COLUMN {col} {tipo}")
            except sqlite3.OperationalError:
                pass

        con.execute("CREATE INDEX IF NOT EXISTS idx_fecha ON vuelos(fecha)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_ultima_vez ON vuelos(ultima_vez)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_aero_fecha ON vuelos(aerolinea_codigo, fecha)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_ruta_fecha ON vuelos(ruta, fecha)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_matricula ON vuelos(matricula)")

        con.execute("""
            CREATE TABLE IF NOT EXISTS capacidades_aeronaves (
                aerolinea_codigo TEXT NOT NULL,
                tipo_code        TEXT NOT NULL,
                modelo           TEXT,
                asientos         INTEGER NOT NULL,
                fuente           TEXT,
                notas            TEXT,
                PRIMARY KEY (aerolinea_codigo, tipo_code)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS capacidades_genericas (
                tipo_code TEXT PRIMARY KEY,
                modelo    TEXT,
                asientos  INTEGER NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS correcciones_matricula (
                matricula  TEXT PRIMARY KEY,
                tipo_code  TEXT NOT NULL,
                modelo     TEXT,
                fabricante TEXT,
                fuente     TEXT,
                notas      TEXT
            )
        """)
        con.execute("""
            INSERT OR IGNORE INTO correcciones_matricula
            VALUES ('HP-9820CMP', 'B38M', 'Boeing 737 MAX 8', 'Boeing', 'manual',
                    'FR24 histórico tenía 738 por error')
        """)

        if con.execute("SELECT COUNT(*) FROM capacidades_aeronaves").fetchone()[0] == 0:
            _seed_capacidades(con)
        else:
            _seed_capacidades_parcial(con)

        con.commit()
    finally:
        con.close()


def _catalogo_capacidades_aero() -> list[tuple]:
    return [
        ("CM", "B738", "Boeing 737-800",     160, "seatguru"),
        ("CM", "B38M", "Boeing 737 MAX 8",   166, "seatguru"),
        ("CM", "B39M", "Boeing 737 MAX 9",   166, "seatguru"),
        ("AR", "B737", "Boeing 737-700",     128, "aerolineas"),
        ("AR", "B738", "Boeing 737-800",     168, "aerolineas"),
        ("AR", "B38M", "Boeing 737 MAX 8",   170, "aerolineas"),
        ("AR", "E190", "Embraer E190AR",      96, "aerolineas"),
        ("AR", "E195", "Embraer E195AR",      96, "aerolineas"),
        ("AR", "A332", "Airbus A330-200",    265, "aerolineas"),
        ("LA", "A319", "Airbus A319",        144, "latam"),
        ("LA", "A320", "Airbus A320",        174, "latam"),
        ("LA", "A20N", "Airbus A320neo",     174, "latam"),
        ("LA", "A321", "Airbus A321",        220, "latam"),
        ("LA", "A21N", "Airbus A321neo",     220, "latam"),
        ("LA", "B789", "Boeing 787-9",       300, "latam"),
        ("G3", "B737", "Boeing 737-700",     138, "gol"),
        ("G3", "B738", "Boeing 737-800",     186, "gol"),
        ("G3", "B38M", "Boeing 737 MAX 8",   186, "gol"),
        ("DM", "B38M", "Boeing 737 MAX 8",   185, "arajet"),
        ("FO", "B738", "Boeing 737-800",     189, "flybondi"),
        ("2W", "A332", "Airbus A330-200",    388, "world2fly"),
        ("2W", "A333", "Airbus A330-300",    388, "world2fly"),
        ("ZP", "CRJ2", "Bombardier CRJ-200",  50, "paranair"),
    ]


def _catalogo_capacidades_genericas() -> list[tuple]:
    return [
        ("B737", "Boeing 737-700",      149),
        ("B738", "Boeing 737-800",      186),
        ("B739", "Boeing 737-900",      180),
        ("B37M", "Boeing 737 MAX 7",    153),
        ("B38M", "Boeing 737 MAX 8",    178),
        ("B39M", "Boeing 737 MAX 9",    178),
        ("A318", "Airbus A318",         107),
        ("A319", "Airbus A319",         144),
        ("A320", "Airbus A320",         180),
        ("A321", "Airbus A321",         220),
        ("A20N", "Airbus A320neo",      180),
        ("A21N", "Airbus A321neo",      220),
        ("E170", "Embraer E170",         76),
        ("E175", "Embraer E175",         88),
        ("E190", "Embraer E190",        100),
        ("E195", "Embraer E195",        118),
        ("A332", "Airbus A330-200",     290),
        ("A333", "Airbus A330-300",     300),
        ("A339", "Airbus A330-900neo",  287),
        ("B763", "Boeing 767-300",      269),
        ("B772", "Boeing 777-200",      375),
        ("B773", "Boeing 777-300",      368),
        ("B77W", "Boeing 777-300ER",    396),
        ("B788", "Boeing 787-8",        242),
        ("B789", "Boeing 787-9",        296),
        ("CRJ2", "Bombardier CRJ-200",   50),
        ("CRJ7", "Bombardier CRJ-700",   70),
        ("CRJ9", "Bombardier CRJ-900",   90),
        ("AT72", "ATR 72",               70),
        ("F100", "Fokker 100",          100),
        ("MD82", "McDonnell Douglas MD-82", 172),
        ("MD83", "McDonnell Douglas MD-83", 172),
    ]


def _seed_capacidades(con: sqlite3.Connection) -> None:
    por_aero = _catalogo_capacidades_aero()
    con.executemany(
        "INSERT OR IGNORE INTO capacidades_aeronaves "
        "(aerolinea_codigo, tipo_code, modelo, asientos, fuente) "
        "VALUES (?,?,?,?,?)", por_aero)
    genericas = _catalogo_capacidades_genericas()
    con.executemany(
        "INSERT OR IGNORE INTO capacidades_genericas (tipo_code, modelo, asientos) "
        "VALUES (?,?,?)", genericas)
    print(f"[DB] Capacidades iniciales: {len(por_aero)} por aerolínea, "
          f"{len(genericas)} genéricas")


def _seed_capacidades_parcial(con: sqlite3.Connection) -> None:
    con.executemany(
        "INSERT OR IGNORE INTO capacidades_aeronaves "
        "(aerolinea_codigo, tipo_code, modelo, asientos, fuente) "
        "VALUES (?,?,?,?,?)", _catalogo_capacidades_aero())
    con.executemany(
        "INSERT OR IGNORE INTO capacidades_genericas (tipo_code, modelo, asientos) "
        "VALUES (?,?,?)", _catalogo_capacidades_genericas())


def calcular_asientos(con: sqlite3.Connection, aero: str, tipo: str) -> int:
    if not tipo:
        return 0
    tipo = _normalizar_tipo(tipo)
    aero = _aero_canonica(aero)
    if aero:
        r = con.execute(
            "SELECT asientos FROM capacidades_aeronaves "
            "WHERE aerolinea_codigo=? AND tipo_code=?",
            (aero, tipo)).fetchone()
        if r:
            return r["asientos"]
    r = con.execute(
        "SELECT asientos FROM capacidades_genericas WHERE tipo_code=?",
        (tipo,)).fetchone()
    return r["asientos"] if r else 0


def _cargar_correcciones(con: sqlite3.Connection) -> dict[str, dict]:
    rows = con.execute("SELECT * FROM correcciones_matricula").fetchall()
    return {r["matricula"].upper(): dict(r) for r in rows}


def db_migrar_todo() -> dict[str, int]:
    con = db_conectar()
    stats = {"aero": 0, "alias": 0, "tipo": 0, "matricula": 0,
             "asientos": 0, "huerfanas": 0, "nombres": 0}
    try:
        # 1+2: aerolíneas
        filas = con.execute(
            "SELECT rowid, vuelo, aerolinea_codigo FROM vuelos").fetchall()
        for f in filas:
            actual = (f["aerolinea_codigo"] or "").strip().upper()
            esperado = _extraer_aerolinea(f["vuelo"] or "")
            if not esperado:
                continue
            esperado_canonico = _aero_canonica(esperado)
            actual_canonico = _aero_canonica(actual)
            necesita_fix = False
            if not actual:
                necesita_fix = True
            elif len(actual) < len(esperado):
                necesita_fix = True
            elif actual != actual_canonico:
                necesita_fix = True
            elif actual_canonico != esperado_canonico:
                necesita_fix = True
            if necesita_fix and esperado_canonico:
                if actual != esperado_canonico:
                    con.execute(
                        "UPDATE vuelos SET aerolinea_codigo=? WHERE rowid=?",
                        (esperado_canonico, f["rowid"]))
                    if _aero_canonica(actual) != actual:
                        stats["alias"] += 1
                    else:
                        stats["aero"] += 1
        if stats["aero"] or stats["alias"]:
            con.commit()

        # 3: tipos IATA → ICAO
        filas = con.execute(
            "SELECT rowid, icao_type_code FROM vuelos").fetchall()
        for f in filas:
            actual = (f["icao_type_code"] or "").strip().upper()
            if not actual:
                continue
            normalizado = _normalizar_tipo(actual)
            if normalizado != actual:
                con.execute(
                    "UPDATE vuelos SET icao_type_code=? WHERE rowid=?",
                    (normalizado, f["rowid"]))
                stats["tipo"] += 1
        if stats["tipo"]:
            con.commit()

        # 3.5: correcciones manuales por matrícula
        correcciones = _cargar_correcciones(con)
        if correcciones:
            for mat, corr in correcciones.items():
                m = _modelo_desde_tipo(corr["tipo_code"])
                modelo = corr.get("modelo") or (m[0] if m else "")
                fab = corr.get("fabricante") or (m[1] if m else "")
                cur = con.execute(
                    "UPDATE vuelos SET icao_type_code=?, modelo_avion=?, fabricante=? "
                    "WHERE UPPER(matricula)=? AND icao_type_code != ?",
                    (corr["tipo_code"], modelo, fab, mat, corr["tipo_code"]))
                if cur.rowcount > 0:
                    stats["matricula"] += cur.rowcount
            con.commit()

        # 4: mismo avión → mismo tipo
        filas = con.execute("""
            SELECT matricula, icao_type_code, COUNT(*) as n
            FROM vuelos
            WHERE matricula != '' AND icao_type_code != ''
            GROUP BY matricula, icao_type_code
            ORDER BY matricula, n DESC
        """).fetchall()
        por_matricula: dict[str, str] = {}
        for f in filas:
            mat = f["matricula"]
            if mat not in por_matricula:
                por_matricula[mat] = f["icao_type_code"]

        for mat, tipo_dominante in por_matricula.items():
            m = _modelo_desde_tipo(tipo_dominante)
            modelo_dom = m[0] if m else ""
            fab_dom = m[1] if m else ""
            cur = con.execute(
                "UPDATE vuelos SET icao_type_code=?, "
                "modelo_avion=?, fabricante=? "
                "WHERE matricula=? AND icao_type_code != ?",
                (tipo_dominante, modelo_dom, fab_dom, mat, tipo_dominante))
            if cur.rowcount > 0:
                stats["matricula"] += cur.rowcount
        if stats["matricula"]:
            con.commit()

        # 5: recalcular asientos
        filas = con.execute(
            "SELECT rowid, aerolinea_codigo, icao_type_code, asientos "
            "FROM vuelos").fetchall()
        for f in filas:
            aero = _aero_canonica(f["aerolinea_codigo"] or "")
            tipo = _normalizar_tipo(f["icao_type_code"] or "")
            if not tipo:
                continue
            nuevo = calcular_asientos(con, aero, tipo)
            if nuevo and nuevo != (f["asientos"] or 0):
                con.execute(
                    "UPDATE vuelos SET asientos=? WHERE rowid=?",
                    (nuevo, f["rowid"]))
                stats["asientos"] += 1
        if stats["asientos"]:
            con.commit()

        # 6: huérfanas por vuelo_norm viejo
        filas = con.execute(
            "SELECT rowid, vuelo, vuelo_norm, direccion, fecha FROM vuelos"
        ).fetchall()
        for f in filas:
            esperado = _normalizar_vuelo(f["vuelo"] or "")
            if esperado and esperado != (f["vuelo_norm"] or ""):
                existe = con.execute(
                    "SELECT 1 FROM vuelos WHERE vuelo_norm=? AND direccion=? AND fecha=?",
                    (esperado, f["direccion"], f["fecha"])).fetchone()
                if existe:
                    con.execute("DELETE FROM vuelos WHERE rowid=?", (f["rowid"],))
                    stats["huerfanas"] += 1
        if stats["huerfanas"]:
            con.commit()

        # 7: rellenar aerolinea_nombre desde código
        filas = con.execute(
            "SELECT rowid, aerolinea_codigo, aerolinea_nombre FROM vuelos "
            "WHERE aerolinea_nombre = '' OR aerolinea_nombre IS NULL"
        ).fetchall()
        for f in filas:
            aero = _aero_canonica(f["aerolinea_codigo"] or "")
            nombre = NOMBRES_AEROLINEAS.get(aero, "")
            if nombre:
                con.execute(
                    "UPDATE vuelos SET aerolinea_nombre=? WHERE rowid=?",
                    (nombre, f["rowid"]))
                stats["nombres"] += 1
        if stats["nombres"]:
            con.commit()

        return stats
    finally:
        con.close()


CAMPOS_DINAMICOS = [
    "horario_local", "horario_real", "horario_estimado",
    "estado", "puerta", "sector",
]

CAMPOS_AERONAVE = [
    "matricula", "modelo_avion", "fabricante",
    "icao_type_code", "icao24_hex", "operador",
]

CAMPOS_GENERALES = [
    "vuelo", "origen_destino", "aeropuerto_iata",
    "aerolinea_codigo", "aerolinea_nombre", "aerolinea_color",
    "id_externo",
]


def db_upsert(v: Vuelo) -> str:
    vuelo_norm, direccion, fecha = v.clave_db()
    if not vuelo_norm or not direccion or not fecha:
        return "noop"

    v.icao_type_code = _normalizar_tipo(v.icao_type_code)
    v.aerolinea_codigo = _aero_canonica(v.aerolinea_codigo)

    ahora = datetime.now(TZ_LOCAL).isoformat(timespec="minutes")
    nuevo_conf = CONF_NUM.get(v.confianza, -1)
    ruta = _calcular_ruta(v)

    con = db_conectar()
    try:
        aero = _aero_canonica(v.aerolinea_codigo or _extraer_aerolinea(v.vuelo))
        # Fallback: si no hay nombre, inferirlo del código
        if not v.aerolinea_nombre and aero:
            v.aerolinea_nombre = NOMBRES_AEROLINEAS.get(aero, "")
        asientos = calcular_asientos(con, aero, v.icao_type_code)

        fila = con.execute(
            "SELECT * FROM vuelos WHERE vuelo_norm=? AND direccion=? AND fecha=?",
            (vuelo_norm, direccion, fecha)).fetchone()

        if fila is None:
            datos = {
                "vuelo_norm": vuelo_norm, "direccion": direccion, "fecha": fecha,
                "vuelo": v.vuelo,
                "horario_local": v.horario_local,
                "horario_real": v.horario_real,
                "horario_estimado": v.horario_estimado,
                "estado": v.estado,
                "origen_destino": v.origen_destino,
                "aeropuerto_iata": v.aeropuerto_iata,
                "ruta": ruta,
                "aerolinea_codigo": v.aerolinea_codigo,
                "aerolinea_nombre": v.aerolinea_nombre,
                "aerolinea_color": v.aerolinea_color,
                "puerta": v.puerta, "sector": v.sector,
                "matricula": v.matricula, "modelo_avion": v.modelo_avion,
                "fabricante": v.fabricante,
                "icao_type_code": v.icao_type_code,
                "icao24_hex": v.icao24_hex, "operador": v.operador,
                "fuentes": v.fuentes, "id_externo": v.id_externo,
                "confianza": v.confianza, "confianza_num": nuevo_conf,
                "nota": v._nota,
                "asientos": asientos,
                "primera_vez": ahora, "ultima_vez": ahora,
            }
            cols = ", ".join(datos.keys())
            placeholders = ", ".join("?" * len(datos))
            con.execute(f"INSERT INTO vuelos ({cols}) VALUES ({placeholders})",
                        list(datos.values()))
            con.commit()
            return "insert"

        conf_vieja = fila["confianza_num"] if fila["confianza_num"] is not None else -1
        updates: dict[str, Any] = {}
        accion = "noop"

        for campo in CAMPOS_DINAMICOS:
            nuevo = getattr(v, campo, "") or ""
            viejo = fila[campo] or ""
            if nuevo and nuevo != viejo:
                updates[campo] = nuevo
                if accion == "noop":
                    accion = "update_parcial"

        # Actualizar CAMPOS_GENERALES si el valor nuevo difiere del viejo
        for campo in CAMPOS_GENERALES:
            nuevo = getattr(v, campo, "") or ""
            viejo = fila[campo] or ""
            if nuevo and nuevo != viejo:
                updates[campo] = nuevo
                if accion == "noop":
                    accion = "update_parcial"

        if ruta and ruta != (fila["ruta"] or ""):
            updates["ruta"] = ruta
            if accion == "noop":
                accion = "update_parcial"
        if asientos and asientos != (fila["asientos"] or 0):
            updates["asientos"] = asientos
            if accion == "noop":
                accion = "update_parcial"

        mejora_conf = nuevo_conf > conf_vieja
        for campo in CAMPOS_AERONAVE:
            nuevo = getattr(v, campo, "") or ""
            viejo = fila[campo] or ""
            if not nuevo:
                continue
            if not viejo:
                updates[campo] = nuevo
                if accion == "noop":
                    accion = "update_parcial"
            elif mejora_conf and nuevo != viejo:
                updates[campo] = nuevo
                accion = "update_aeronave"

        fuentes_viejas = set((fila["fuentes"] or "").split("+"))
        fuentes_nuevas = set((v.fuentes or "").split("+"))
        fuentes_union = "+".join(sorted(fuentes_viejas | fuentes_nuevas - {""}))
        if fuentes_union and fuentes_union != (fila["fuentes"] or ""):
            updates["fuentes"] = fuentes_union

        if mejora_conf and v.confianza != (fila["confianza"] or ""):
            updates["confianza"] = v.confianza
            updates["confianza_num"] = nuevo_conf
        if v._nota and v._nota != (fila["nota"] or "") and mejora_conf:
            updates["nota"] = v._nota

        updates["ultima_vez"] = ahora

        if len(updates) > 1 or accion != "noop":
            set_clause = ", ".join(f"{k}=?" for k in updates.keys())
            con.execute(
                f"UPDATE vuelos SET {set_clause} "
                f"WHERE vuelo_norm=? AND direccion=? AND fecha=?",
                list(updates.values()) + [vuelo_norm, direccion, fecha])
            con.commit()

        return accion
    finally:
        con.close()


def db_upsert_many(vuelos: list[Vuelo]) -> dict[str, int]:
    res = {"insert": 0, "update_aeronave": 0, "update_parcial": 0, "noop": 0}
    for v in vuelos:
        accion = db_upsert(v)
        res[accion] = res.get(accion, 0) + 1
    return res


def _fila_a_vuelo(f: sqlite3.Row) -> Vuelo:
    keys = f.keys()
    return Vuelo(
        vuelo=f["vuelo"] or "", direccion=f["direccion"] or "",
        horario_local=f["horario_local"] or "",
        horario_real=f["horario_real"] or "",
        horario_estimado=f["horario_estimado"] or "",
        estado=f["estado"] or "",
        origen_destino=f["origen_destino"] or "",
        aeropuerto_iata=f["aeropuerto_iata"] or "",
        ruta=(f["ruta"] if "ruta" in keys else "") or "",
        aerolinea_codigo=f["aerolinea_codigo"] or "",
        aerolinea_nombre=f["aerolinea_nombre"] or "",
        aerolinea_color=f["aerolinea_color"] or "",
        puerta=f["puerta"] or "", sector=f["sector"] or "",
        matricula=f["matricula"] or "",
        modelo_avion=f["modelo_avion"] or "",
        fabricante=f["fabricante"] or "",
        icao_type_code=f["icao_type_code"] or "",
        icao24_hex=f["icao24_hex"] or "",
        operador=f["operador"] or "",
        fuentes=f["fuentes"] or "",
        id_externo=f["id_externo"] or "",
        confianza=f["confianza"] or "",
        _nota=f["nota"] or "",
        asientos=(f["asientos"] if "asientos" in keys else 0) or 0,
    )


def db_query_ventana(desde: datetime, hasta: datetime) -> list[Vuelo]:
    desde_s = desde.strftime("%Y-%m-%dT%H:%M")
    hasta_s = hasta.strftime("%Y-%m-%dT%H:%M")
    con = db_conectar()
    try:
        filas = con.execute(
            "SELECT * FROM vuelos WHERE horario_local >= ? AND horario_local <= ? "
            "ORDER BY horario_local ASC, direccion ASC, vuelo ASC",
            (desde_s, hasta_s)).fetchall()
    finally:
        con.close()
    return [_fila_a_vuelo(f) for f in filas]


def db_query_full() -> list[Vuelo]:
    con = db_conectar()
    try:
        filas = con.execute(
            "SELECT * FROM vuelos ORDER BY horario_local ASC").fetchall()
    finally:
        con.close()
    return [_fila_a_vuelo(f) for f in filas]


def db_stats() -> dict[str, Any]:
    con = db_conectar()
    try:
        total = con.execute("SELECT COUNT(*) FROM vuelos").fetchone()[0]
        con_modelo = con.execute(
            "SELECT COUNT(*) FROM vuelos WHERE modelo_avion != ''").fetchone()[0]
        con_mat = con.execute(
            "SELECT COUNT(*) FROM vuelos WHERE matricula != ''").fetchone()[0]
        con_asientos = con.execute(
            "SELECT COUNT(*) FROM vuelos WHERE asientos > 0").fetchone()[0]
        sin_aero = con.execute(
            "SELECT COUNT(*) FROM vuelos WHERE aerolinea_nombre = '' "
            "OR aerolinea_nombre IS NULL").fetchone()[0]
        por_conf = con.execute(
            "SELECT confianza, COUNT(*) FROM vuelos GROUP BY confianza").fetchall()
        primera = con.execute("SELECT MIN(fecha) FROM vuelos").fetchone()[0]
        ultima = con.execute("SELECT MAX(fecha) FROM vuelos").fetchone()[0]
    finally:
        con.close()
    return {
        "total": total, "con_modelo": con_modelo,
        "con_matricula": con_mat, "con_asientos": con_asientos,
        "sin_aero_nombre": sin_aero,
        "por_confianza": {r[0] or "sin_datos": r[1] for r in por_conf},
        "primera_fecha": primera or "", "ultima_fecha": ultima or "",
    }


# ---------------------------------------------------------------------------
# CORRECCIONES POR MATRÍCULA
# ---------------------------------------------------------------------------
def aplicar_correcciones_matricula(vuelos: list[Vuelo]) -> None:
    con = db_conectar()
    try:
        correcciones = _cargar_correcciones(con)
    finally:
        con.close()
    if not correcciones:
        return
    n = 0
    for v in vuelos:
        if not v.matricula:
            continue
        corr = correcciones.get(v.matricula.upper())
        if not corr:
            continue
        if v.icao_type_code == corr["tipo_code"]:
            continue
        v.icao_type_code = corr["tipo_code"]
        if corr.get("modelo"):
            v.modelo_avion = corr["modelo"]
        if corr.get("fabricante"):
            v.fabricante = corr["fabricante"]
        v._nota = f"corregido por matrícula ({corr.get('fuente') or 'manual'})"
        n += 1
    if n:
        print(f"[Matrícula] {n} vuelos corregidos por tabla manual")


# ---------------------------------------------------------------------------
# CACHE JSON
# ---------------------------------------------------------------------------
def _cargar_json(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        print(f"[Cache] Error leyendo {path}: {exc}")
        return default


def _guardar_json(path: str, data: Any) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        print(f"[Cache] Error guardando {path}: {exc}")


# ---------------------------------------------------------------------------
# FIDS
# ---------------------------------------------------------------------------
def fids_obtener() -> list[Vuelo]:
    print(f"[FIDS] Consultando {FIDS_URL}")
    try:
        r = requests.get(FIDS_URL, timeout=FIDS_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        _dump_json("fids_raw", data)
    except Exception as exc:
        print(f"  [FIDS] Error: {type(exc).__name__}: {exc}")
        return []
    if not isinstance(data, dict):
        return []

    registros: list[Vuelo] = []
    for item in data.get("arribos") or []:
        if not isinstance(item, dict):
            continue
        v = _t(item.get("vuelo"))
        if not v:
            continue
        ciudad = _t(item.get("origen"), "N/A")
        apt = _t(item.get("aeropuerto_codigo"))
        registros.append(Vuelo(
            vuelo=v, direccion="Arrival",
            horario_local=_iso_a_local(item.get("hora_programada")) or "N/A",
            horario_real=_iso_a_local(item.get("hora_real")),
            horario_estimado=_iso_a_local(item.get("hora_estimada")),
            estado=_t(item.get("estado"), "N/A"),
            origen_destino=f"{ciudad} ({apt})" if apt else ciudad,
            aeropuerto_iata=apt,
            aerolinea_codigo=_t(item.get("aerolinea_codigo")),
            aerolinea_nombre=_t(item.get("aerolinea_nombre")),
            aerolinea_color=_t(item.get("aerolinea_color")),
            puerta=_t(item.get("puerta")), sector=_t(item.get("sector")),
            fuentes="fids", id_externo=_t(item.get("id")),
        ))
    for item in data.get("partidas") or []:
        if not isinstance(item, dict):
            continue
        v = _t(item.get("vuelo"))
        if not v:
            continue
        ciudad = _t(item.get("destino"), "N/A")
        apt = _t(item.get("aeropuerto_codigo"))
        registros.append(Vuelo(
            vuelo=v, direccion="Departure",
            horario_local=_iso_a_local(item.get("hora_programada")) or "N/A",
            horario_real=_iso_a_local(item.get("hora_real")),
            horario_estimado=_iso_a_local(item.get("hora_estimada")),
            estado=_t(item.get("estado"), "N/A"),
            origen_destino=f"{ciudad} ({apt})" if apt else ciudad,
            aeropuerto_iata=apt,
            aerolinea_codigo=_t(item.get("aerolinea_codigo")),
            aerolinea_nombre=_t(item.get("aerolinea_nombre")),
            aerolinea_color=_t(item.get("aerolinea_color")),
            puerta=_t(item.get("puerta")), sector=_t(item.get("sector")),
            fuentes="fids", id_externo=_t(item.get("id")),
        ))
    print(f"  [FIDS] {len(registros)} vuelos")
    return registros


# ---------------------------------------------------------------------------
# OPENSKY
# ---------------------------------------------------------------------------
def opensky_cargar_db() -> dict[str, dict[str, dict[str, str]]]:
    if os.path.exists(OPENSKY_CACHE):
        size_mb = os.path.getsize(OPENSKY_CACHE) / (1024 * 1024)
        print(f"[OpenSky] Usando caché local ({size_mb:.1f} MB)")
        path = OPENSKY_CACHE
    else:
        print(f"[OpenSky] Descargando base de datos (~50 MB)...")
        try:
            with requests.get(OPENSKY_DB_URL, stream=True,
                              timeout=OPENSKY_TIMEOUT) as r:
                r.raise_for_status()
                with open(OPENSKY_CACHE, "wb") as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        if chunk:
                            f.write(chunk)
            path = OPENSKY_CACHE
        except Exception as exc:
            print(f"  [OpenSky] Error descargando: {type(exc).__name__}: {exc}")
            return {"hex": {}, "reg": {}}

    idx_hex: dict[str, dict[str, str]] = {}
    idx_reg: dict[str, dict[str, str]] = {}
    leidas = 0
    errores = 0
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f, delimiter=",", quotechar='"')
            for row in reader:
                leidas += 1
                try:
                    reg = _buscar_col(row, "registration", "reg")
                    modelo = _buscar_col(row, "model")
                    fab = _buscar_col(row, "manufacturername",
                                      "manufacturerName", "manufacturericao")
                    tipo = _buscar_col(row, "typecode", "icaoaircrafttype")
                    op = _buscar_col(row, "operator")
                    hex_id = _buscar_col(row, "icao24", "mode_s", "hex").lower()
                    info = {"registration": reg, "model": modelo,
                            "manufacturer": fab,
                            "typecode": _normalizar_tipo(tipo),
                            "operator": op}
                    if hex_id:
                        idx_hex[hex_id] = info
                    for var in _variantes_registro(reg):
                        idx_reg[var] = info
                except Exception:
                    errores += 1
                    continue
    except Exception as exc:
        print(f"  [OpenSky] Error leyendo CSV: {type(exc).__name__}: {exc}")

    print(f"[OpenSky] {len(idx_hex)} por hex, {len(idx_reg)} por matrícula "
          f"({leidas} filas, {errores} errores)")
    return {"hex": idx_hex, "reg": idx_reg}


def enriquecer_con_opensky(vuelos: list[Vuelo],
                           db: dict[str, dict[str, dict[str, str]]]) -> None:
    idx_hex = db.get("hex") or {}
    idx_reg = db.get("reg") or {}
    if not idx_hex and not idx_reg:
        return
    hits = 0
    corregidos = 0
    for v in vuelos:
        info = None
        if v.icao24_hex:
            info = idx_hex.get(v.icao24_hex.lower())
        if info is None and v.matricula:
            for var in _variantes_registro(v.matricula):
                info = idx_reg.get(var)
                if info:
                    break
        if not info:
            continue
        hits += 1

        tipo_opensky = _normalizar_tipo(info.get("typecode", ""))
        if tipo_opensky and tipo_opensky != _normalizar_tipo(v.icao_type_code):
            if v.icao_type_code:
                corregidos += 1
            v.icao_type_code = tipo_opensky
            m = _modelo_desde_tipo(tipo_opensky)
            if m:
                v.modelo_avion, v.fabricante = m

        if not v.modelo_avion and info["model"]:
            v.modelo_avion = info["model"]
            if not v.fabricante:
                v.fabricante = info["manufacturer"]
            if not v.confianza:
                v.confianza = "real"
        if not v.fabricante:
            v.fabricante = info["manufacturer"]
        if not v.operador:
            v.operador = info["operator"]

    if hits:
        print(f"[OpenSky] {hits} verificados, {corregidos} tipos corregidos")


# ---------------------------------------------------------------------------
# FR24 TABLERO
# ---------------------------------------------------------------------------
def fr24_obtener(codigo: str) -> list[Vuelo]:
    if not _FR24_DISPONIBLE:
        print(f"[FR24] Tablero omitido. Motivo: {_FR24_ERROR}")
        return []
    print(f"[FR24] Consultando tablero de {codigo}")
    try:
        api = FlightRadar24API()
    except Exception as exc:
        print(f"  [FR24] Error: {type(exc).__name__}: {exc}")
        return []
    resultado: list[Vuelo] = []
    for direccion, clave in (("Arrival", "arrivals"), ("Departure", "departures")):
        try:
            detalle = api.get_airport_details(codigo, flight_limit=100)
        except Exception as exc:
            print(f"  [FR24] {direccion}: {type(exc).__name__}: {exc}")
            continue
        if not isinstance(detalle, dict):
            continue
        _dump_json(f"fr24_tablero_{direccion.lower()}", detalle)
        schedule = ((detalle.get("airport") or {}).get("pluginData") or {}).get("schedule") or {}
        items = ((schedule.get(clave) or {}).get("data")) or []
        for item in items:
            if not isinstance(item, dict):
                continue
            fd = item.get("flight") or item
            ident = fd.get("identification") or {}
            num = _t((ident.get("number") or {}).get("default")) or _t(ident.get("callsign"), "N/A")
            ac = fd.get("aircraft") or {}
            modelo_obj = ac.get("model") or {}
            modelo = _t(modelo_obj.get("text"))
            iata_code = _t(modelo_obj.get("code"))
            icao_code = _normalizar_tipo(iata_code)
            matr = _t(ac.get("registration"))
            hex_id = _t(ac.get("hex")).lower()
            apt_info = fd.get("airport") or {}
            otro = (apt_info.get("origin") if direccion == "Arrival"
                    else apt_info.get("destination")) or {}
            apt_nombre = _t(otro.get("name"))
            apt_iata = _t((otro.get("code") or {}).get("iata"))
            tiempo = fd.get("time") or {}
            campo = "arrival" if direccion == "Arrival" else "departure"
            prog = (tiempo.get("scheduled") or {}).get(campo)
            real = ((tiempo.get("real") or {}).get(campo)
                    or (tiempo.get("estimated") or {}).get(campo))
            horario = ""
            if isinstance(prog, (int, float)) and prog > 0:
                horario = datetime.fromtimestamp(prog, tz=TZ_LOCAL).strftime("%Y-%m-%dT%H:%M")
            horario_r = ""
            if isinstance(real, (int, float)) and real > 0:
                horario_r = datetime.fromtimestamp(real, tz=TZ_LOCAL).strftime("%Y-%m-%dT%H:%M")
            if num in ("", "N/A") and not horario:
                continue
            aero_cod = _extraer_aerolinea(num)
            v = Vuelo(
                vuelo=num, direccion=direccion,
                horario_local=horario or "N/A", horario_real=horario_r,
                estado=_t((fd.get("status") or {}).get("text"), "N/A"),
                origen_destino=f"{apt_nombre} ({apt_iata})" if apt_iata else apt_nombre,
                aeropuerto_iata=apt_iata,
                matricula=matr, modelo_avion=modelo, icao_type_code=icao_code,
                icao24_hex=hex_id,
                aerolinea_codigo=aero_cod,
                aerolinea_nombre=NOMBRES_AEROLINEAS.get(aero_cod, ""),
                fuentes="fr24",
            )
            if modelo:
                v.confianza = "real"
            resultado.append(v)
        time.sleep(FR24_PAUSA)
    print(f"  [FR24] {len(resultado)} vuelos")
    return resultado


# ---------------------------------------------------------------------------
# FR24 HISTÓRICO
# ---------------------------------------------------------------------------
def _fr24_api_flight_history(aero_cod: str, vuelo_norm: str) -> list[dict[str, Any]]:
    query = f"{aero_cod.upper()}{vuelo_norm}"
    url = "https://api.flightradar24.com/common/v1/flight/list.json"
    params = {"query": query, "fetchBy": "flight", "page": 1, "limit": 100}
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36"),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.flightradar24.com",
        "Referer": f"https://www.flightradar24.com/data/flights/{query.lower()}",
    }
    data = None
    if _CF_DISPONIBLE:
        try:
            r = cf_requests.get(url, params=params, headers=headers,
                                impersonate="chrome120", timeout=25)
            if r.status_code == 200:
                data = r.json()
            else:
                print(f"    [FR24-api] HTTP {r.status_code}")
        except Exception as exc:
            print(f"    [FR24-api] curl_cffi falló: {type(exc).__name__}: {exc}")
    if data is None:
        try:
            r = requests.get(url, params=params, headers=headers, timeout=25)
            if r.status_code == 200:
                data = r.json()
        except Exception as exc:
            print(f"    [FR24-api] requests falló: {type(exc).__name__}: {exc}")
    if data is None:
        return []
    _dump_json(f"fr24_api_{query}", data)
    return _extraer_data_api(data)


def _extraer_data_api(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    result = data.get("result") or {}
    response = result.get("response") or {}
    items = response.get("data")
    if not items:
        items = (response.get("flight") or {}).get("data") or []
    return items if isinstance(items, list) else []


def _parsear_item_api(item: dict[str, Any]) -> dict[str, str]:
    fecha_iso = ""
    t = item.get("time") or {}
    sched = t.get("scheduled") or {}
    dep_ts = sched.get("departure")
    if isinstance(dep_ts, (int, float)) and dep_ts > 0:
        fecha_iso = datetime.fromtimestamp(dep_ts, tz=TZ_LOCAL).strftime("%Y-%m-%d")
    ac = item.get("aircraft") or {}
    matricula = _t(ac.get("registration"))
    hex_id = _t(ac.get("hex")).lower()
    modelo_obj = ac.get("model") or {}
    tipo_raw = _t(modelo_obj.get("code"))
    tipo = _normalizar_tipo(tipo_raw)
    modelo_txt = _t(modelo_obj.get("text"))
    return {"fecha": fecha_iso, "tipo": tipo, "modelo": modelo_txt,
            "matricula": matricula, "hex": hex_id}


def _aplicar_historial(v: Vuelo, tipo: str, matricula: str,
                       modelo_txt: str = "", hex_id: str = "",
                       match_exacto: bool = False) -> bool:
    tipo = _normalizar_tipo(tipo)
    cambios = False
    tipo_cambio = False

    if tipo:
        if not v.icao_type_code:
            v.icao_type_code = tipo
            cambios = True
            tipo_cambio = True
        elif match_exacto and tipo != _normalizar_tipo(v.icao_type_code):
            v.icao_type_code = tipo
            cambios = True
            tipo_cambio = True

    if matricula and not v.matricula:
        v.matricula = matricula
        cambios = True
    if hex_id and not v.icao24_hex:
        v.icao24_hex = hex_id

    if tipo_cambio or not v.modelo_avion:
        if modelo_txt:
            v.modelo_avion = modelo_txt
        else:
            m = _modelo_desde_tipo(tipo)
            if m:
                v.modelo_avion, v.fabricante = m
            else:
                v.modelo_avion = f"({tipo})"
        cambios = True
    elif modelo_txt and modelo_txt != v.modelo_avion and match_exacto:
        v.modelo_avion = modelo_txt
        cambios = True

    if cambios and CONF_NUM.get(v.confianza, -1) < CONF_NUM["historico"]:
        v.confianza = "historico"
        v._nota = "histórico FR24"

    return cambios


def enriquecer_con_fr24_historico(
    vuelos: list[Vuelo],
    hist_cache: dict[str, dict[str, str]],
    max_scrapes: int = FR24_HIST_MAX_POR_CORRIDA,
    forzar_vuelo: str | None = None,
) -> dict[str, dict[str, str]]:
    hits_cache = 0
    pendientes: list[Vuelo] = []
    for v in vuelos:
        if v.confianza == "real":
            continue
        if v.modelo_avion and v.matricula and v.confianza == "historico":
            continue
        if v.modelo_avion and not v.matricula:
            try:
                f_v = datetime.strptime(v.fecha_iso(), "%Y-%m-%d").date()
                hoy = datetime.now(TZ_LOCAL).date()
                if (f_v - hoy).days > 2:
                    continue
            except ValueError:
                continue

        aero = (v.aerolinea_codigo or "").strip().upper() or _extraer_aerolinea(v.vuelo)
        vuelo_norm = _normalizar_vuelo(v.vuelo)
        fecha = v.fecha_iso()
        if not aero or not vuelo_norm or not fecha:
            continue
        if forzar_vuelo and vuelo_norm != _normalizar_vuelo(forzar_vuelo):
            continue
        key = f"{aero}|{vuelo_norm}|{fecha}"
        cached = hist_cache.get(key)
        if cached:
            hits_cache += 1
            _aplicar_historial(v, cached.get("tipo", ""),
                              cached.get("matricula", ""),
                              cached.get("modelo", ""),
                              cached.get("hex", ""),
                              match_exacto=cached.get("match_exacto", True))
        else:
            pendientes.append(v)

    if hits_cache:
        print(f"[FR24-hist] {hits_cache} vuelos desde cache local")
    if not pendientes:
        return hist_cache

    grupos: dict[tuple[str, str], list[Vuelo]] = defaultdict(list)
    for v in pendientes:
        aero = (v.aerolinea_codigo or "").strip().upper() or _extraer_aerolinea(v.vuelo)
        vuelo_norm = _normalizar_vuelo(v.vuelo)
        if aero and vuelo_norm:
            grupos[(aero, vuelo_norm)].append(v)

    print(f"[FR24-hist] {len(pendientes)} vuelos sin datos, "
          f"{len(grupos)} páginas a consultar (máx {max_scrapes})")

    consultadas = 0
    nuevos = 0
    for (aero, vuelo_norm), lista in grupos.items():
        if consultadas >= max_scrapes:
            print(f"  [FR24-hist] Tope alcanzado")
            break
        consultadas += 1
        print(f"  [FR24-hist] {aero}{vuelo_norm} ({len(lista)} vuelo(s))")
        items_api = _fr24_api_flight_history(aero, vuelo_norm)
        filas: list[dict[str, str]] = []
        if items_api:
            for it in items_api:
                info = _parsear_item_api(it)
                if info["fecha"]:
                    filas.append(info)
            print(f"    → API: {len(filas)} filas")
        if not filas:
            print(f"    → sin datos")
            time.sleep(FR24_HIST_PAUSA)
            continue

        por_fecha = {f["fecha"]: f for f in filas if f["fecha"]}
        for v in lista:
            fecha = v.fecha_iso()
            info = por_fecha.get(fecha)
            match_exacto = info is not None
            if info is None:
                try:
                    objetivo = datetime.strptime(fecha, "%Y-%m-%d").date()
                except ValueError:
                    continue
                for iso, f in por_fecha.items():
                    try:
                        d = datetime.strptime(iso, "%Y-%m-%d").date()
                    except ValueError:
                        continue
                    if abs((d - objetivo).days) <= 2:
                        info = f
                        break
            if info is None:
                continue
            matricula_a_usar = info.get("matricula", "") if match_exacto else ""
            hex_a_usar = info.get("hex", "") if match_exacto else ""
            if _aplicar_historial(v, info.get("tipo", ""),
                                  matricula_a_usar,
                                  info.get("modelo", ""),
                                  hex_a_usar,
                                  match_exacto=match_exacto):
                key = f"{aero}|{vuelo_norm}|{fecha}"
                hist_cache[key] = {
                    "tipo": info.get("tipo", ""),
                    "matricula": matricula_a_usar,
                    "modelo": info.get("modelo", ""),
                    "hex": hex_a_usar,
                    "match_exacto": match_exacto,
                }
                nuevos += 1
        time.sleep(FR24_HIST_PAUSA)

    if nuevos:
        print(f"[FR24-hist] {nuevos} vuelos enriquecidos desde web")
    return hist_cache


# ---------------------------------------------------------------------------
# CONSOLIDACIÓN
# ---------------------------------------------------------------------------
CAMPOS_ENRIQUECIBLES = [
    "matricula", "modelo_avion", "fabricante", "icao_type_code",
    "icao24_hex", "operador",
    "estado", "puerta", "sector", "aerolinea_nombre", "aerolinea_codigo",
    "aerolinea_color", "horario_real", "horario_estimado", "id_externo",
]


def _fusionar(principal: Vuelo, otro: Vuelo) -> None:
    for campo in CAMPOS_ENRIQUECIBLES:
        if not getattr(principal, campo) and getattr(otro, campo):
            setattr(principal, campo, getattr(otro, campo))
    if CONF_NUM.get(otro.confianza, -1) > CONF_NUM.get(principal.confianza, -1):
        principal.confianza = otro.confianza
        if otro._nota:
            principal._nota = otro._nota
    principal.fuentes = "+".join(sorted(set(
        (principal.fuentes or "").split("+") + (otro.fuentes or "").split("+")
    ) - {""}))


def consolidar(fuentes: dict[str, list[Vuelo]]) -> list[Vuelo]:
    fids_vuelos = fuentes.get("fids") or []
    fr24_vuelos = fuentes.get("fr24") or []
    indice_fr24: dict[tuple, int] = {}
    for i, v in enumerate(fr24_vuelos):
        indice_fr24[v.clave()] = i
    fr24_usados = [False] * len(fr24_vuelos)
    consolidado: list[Vuelo] = []
    for v_fids in fids_vuelos:
        match_i: int | None = None
        i = indice_fr24.get(v_fids.clave())
        if i is not None and not fr24_usados[i]:
            match_i = i
        if match_i is None:
            fecha_fids = _fecha(v_fids.horario_local)
            min_fids = _hhmm_a_min(v_fids.horario_local)
            vuelo_norm = _normalizar_vuelo(v_fids.vuelo)
            mejor_i = None
            mejor_diff = None
            for j, v_fr24 in enumerate(fr24_vuelos):
                if fr24_usados[j]:
                    continue
                if _normalizar_vuelo(v_fr24.vuelo) != vuelo_norm:
                    continue
                if v_fr24.direccion != v_fids.direccion:
                    continue
                if _fecha(v_fr24.horario_local) != fecha_fids:
                    continue
                min_fr24 = _hhmm_a_min(v_fr24.horario_local)
                if min_fids < 0 or min_fr24 < 0:
                    continue
                diff = abs(min_fr24 - min_fids)
                if diff <= TOLERANCIA_MIN:
                    if mejor_diff is None or diff < mejor_diff:
                        mejor_i = j
                        mejor_diff = diff
            match_i = mejor_i
        if match_i is not None:
            fr24_usados[match_i] = True
            _fusionar(v_fids, fr24_vuelos[match_i])
        consolidado.append(v_fids)
    for j, v_fr24 in enumerate(fr24_vuelos):
        if not fr24_usados[j]:
            consolidado.append(v_fr24)
    consolidado.sort(key=lambda r: (r.horario_local, r.direccion, r.vuelo))
    return consolidado


# ---------------------------------------------------------------------------
# CACHES
# ---------------------------------------------------------------------------
def aplicar_cache(vuelos: list[Vuelo], cache: dict[str, dict[str, str]]) -> None:
    if not cache:
        return
    hits_modelo = 0
    hits_mat = 0
    for v in vuelos:
        info = cache.get(v.clave_cache())
        if not info:
            continue
        if v.confianza in ("real", "historico"):
            continue
        fecha_vuelo = v.fecha_iso()
        fecha_cache = (info.get("ultima_vez") or "")[:10]
        misma_fecha = (fecha_cache == fecha_vuelo)

        if not v.modelo_avion and info.get("modelo_avion"):
            v.modelo_avion = info["modelo_avion"]
            if not v.fabricante:
                v.fabricante = info.get("fabricante", "")
            if not v.icao_type_code:
                v.icao_type_code = _normalizar_tipo(info.get("icao_type_code", ""))
            hits_modelo += 1
            if CONF_NUM.get(v.confianza, -1) < CONF_NUM["cache_modelo"]:
                v.confianza = "cache_modelo"
                v._nota = "modelo del cache"

        if not v.matricula and misma_fecha and info.get("matricula"):
            v.matricula = info["matricula"]
            if not v.icao24_hex:
                v.icao24_hex = info.get("icao24_hex", "")
            if not v.operador:
                v.operador = info.get("operador", "")
            hits_mat += 1
            if CONF_NUM.get(v.confianza, -1) < CONF_NUM["cache"]:
                v.confianza = "cache"
                v._nota = "cache local"
    if hits_modelo or hits_mat:
        print(f"[Cache] {hits_modelo} modelos, {hits_mat} matrículas desde cache")


def actualizar_cache(vuelos: list[Vuelo],
                     cache: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    ahora = datetime.now(TZ_LOCAL).isoformat(timespec="minutes")
    nuevos = 0
    for v in vuelos:
        if not v.modelo_avion:
            continue
        k = v.clave_cache()
        anterior = cache.get(k) or {}
        prev_conf = CONF_NUM.get(anterior.get("confianza", ""), -1)
        curr_conf = CONF_NUM.get(v.confianza, -1)
        if prev_conf > curr_conf:
            continue
        matricula = v.matricula or anterior.get("matricula", "")
        hex_prev = v.icao24_hex or anterior.get("icao24_hex", "")
        operador = v.operador or anterior.get("operador", "")
        if k not in cache:
            nuevos += 1
        cache[k] = {
            "matricula": matricula, "modelo_avion": v.modelo_avion,
            "fabricante": v.fabricante,
            "icao_type_code": _normalizar_tipo(v.icao_type_code),
            "icao24_hex": hex_prev, "operador": operador,
            "confianza": v.confianza, "ultima_vez": ahora,
        }
    if nuevos:
        print(f"[Cache] {nuevos} aeronaves nuevas")
    return cache


def aplicar_fleet_cache(vuelos: list[Vuelo], fleet: dict[str, str]) -> None:
    if not fleet:
        return
    n = 0
    for v in vuelos:
        if v.modelo_avion:
            continue
        aero = (v.aerolinea_codigo or "").strip().upper() or _extraer_aerolinea(v.vuelo)
        if not aero:
            continue
        vuelo_norm = _normalizar_vuelo(v.vuelo)
        claves = [f"{aero}|{vuelo_norm}|{v.direccion}",
                  f"{aero}||{v.direccion}", f"{aero}||"]
        tipo = next((fleet[k] for k in claves if k in fleet), None)
        if not tipo:
            continue
        tipo = _normalizar_tipo(tipo)
        m = _modelo_desde_tipo(tipo)
        if not m:
            continue
        v.modelo_avion, v.fabricante = m
        if not v.icao_type_code:
            v.icao_type_code = tipo
        v.confianza = "suposicion"
        v._nota = "SUPUESTO por flota"
        n += 1
    if n:
        print(f"[Flota] {n} vuelos con tipo SUPUESTO por flota")


def actualizar_fleet_cache(vuelos: list[Vuelo], fleet: dict[str, str]) -> dict[str, str]:
    for v in vuelos:
        if not v.icao_type_code:
            continue
        if v.confianza in ("suposicion", ""):
            continue
        aero = (v.aerolinea_codigo or "").strip().upper() or _extraer_aerolinea(v.vuelo)
        if not aero:
            continue
        vuelo_norm = _normalizar_vuelo(v.vuelo)
        tipo = _normalizar_tipo(v.icao_type_code)
        fleet.setdefault(f"{aero}|{vuelo_norm}|{v.direccion}", tipo)
        fleet.setdefault(f"{aero}||{v.direccion}", tipo)
        fleet.setdefault(f"{aero}||", tipo)
    return fleet


# ---------------------------------------------------------------------------
# TURNAROUND (ARR → DEP)
# ---------------------------------------------------------------------------
def propagar_matricula_por_turnaround(vuelos: list[Vuelo]) -> None:
    T_MIN, T_MAX = 20, 300

    def _min(h: str) -> int | None:
        dt = _parse_horario(h)
        return int(dt.timestamp() // 60) if dt else None

    def _num(v: Vuelo) -> int | None:
        n = _normalizar_vuelo(v.vuelo)
        return int(n) if n.isdigit() else None

    def _aero(v: Vuelo) -> str:
        return (v.aerolinea_codigo or _extraer_aerolinea(v.vuelo)).upper()

    def _iata(v: Vuelo) -> str:
        return (v.aeropuerto_iata or "").upper()

    arribos = [v for v in vuelos if v.direccion == "Arrival"]
    partidas = [v for v in vuelos if v.direccion == "Departure"]
    propagados = 0
    pares_log: list[str] = []

    for arr in arribos:
        if not arr.matricula:
            continue
        n_arr = _num(arr)
        t_arr = _min(arr.horario_local)
        if n_arr is None or t_arr is None:
            continue
        aero_arr, iata_arr = _aero(arr), _iata(arr)
        if not aero_arr or not iata_arr:
            continue

        mejor_dep, mejor_dt = None, None
        for dep in partidas:
            if dep.matricula:
                continue
            n_dep = _num(dep)
            if n_dep is None or abs(n_dep - n_arr) != 1:
                continue
            if _aero(dep) != aero_arr or _iata(dep) != iata_arr:
                continue
            t_dep = _min(dep.horario_local)
            if t_dep is None:
                continue
            dt = t_dep - t_arr
            if not (T_MIN <= dt <= T_MAX):
                continue
            if mejor_dt is None or dt < mejor_dt:
                mejor_dep, mejor_dt = dep, dt

        if mejor_dep is None:
            continue

        mejor_dep.matricula = arr.matricula
        if not mejor_dep.icao24_hex:
            mejor_dep.icao24_hex = arr.icao24_hex
        if not mejor_dep.operador:
            mejor_dep.operador = arr.operador
        if not mejor_dep.modelo_avion:
            mejor_dep.modelo_avion = arr.modelo_avion
            mejor_dep.fabricante = arr.fabricante
        if not mejor_dep.icao_type_code:
            mejor_dep.icao_type_code = arr.icao_type_code
        conf_f = arr.confianza
        mejor_dep.confianza = "cache" if conf_f in ("real", "historico") else conf_f
        mejor_dep._nota = f"mismo avión que {arr.vuelo} (ARR)"
        pares_log.append(
            f"    {mejor_dep.vuelo} (DEP) ← "
            f"{arr.vuelo} (ARR)  {arr.matricula}")
        propagados += 1

    if propagados:
        print(f"[Turnaround] {propagados} matrículas propagadas ARR→DEP:")
        for l in pares_log:
            print(l)
    else:
        print("[Turnaround] sin pares ARR→DEP para propagar")


# ---------------------------------------------------------------------------
# REPORTES
# ---------------------------------------------------------------------------
def reporte_asientos_por_mes(con: sqlite3.Connection) -> list[dict]:
    sql = """
        SELECT
            substr(fecha, 1, 7) AS mes,
            aerolinea_codigo,
            aerolinea_nombre,
            ruta,
            COUNT(*) AS vuelos,
            SUM(asientos) AS asientos_totales,
            GROUP_CONCAT(DISTINCT modelo_avion) AS modelos,
            GROUP_CONCAT(DISTINCT icao_type_code) AS tipos
        FROM vuelos
        WHERE asientos > 0 AND direccion = 'Arrival'
        GROUP BY mes, aerolinea_codigo, ruta
        ORDER BY mes DESC, asientos_totales DESC
    """
    return [dict(r) for r in con.execute(sql).fetchall()]


def reporte_asientos_por_aerolinea(con: sqlite3.Connection) -> list[dict]:
    sql = """
        SELECT
            aerolinea_codigo, aerolinea_nombre,
            COUNT(*) AS vuelos,
            SUM(asientos) AS asientos_totales,
            COUNT(DISTINCT ruta) AS rutas,
            COUNT(DISTINCT substr(fecha, 1, 7)) AS meses
        FROM vuelos
        WHERE asientos > 0 AND direccion = 'Arrival'
        GROUP BY aerolinea_codigo
        ORDER BY asientos_totales DESC
    """
    return [dict(r) for r in con.execute(sql).fetchall()]


def reporte_asientos_por_ruta(con: sqlite3.Connection) -> list[dict]:
    sql = """
        SELECT
            ruta,
            COUNT(*) AS vuelos,
            SUM(asientos) AS asientos_totales,
            COUNT(DISTINCT aerolinea_codigo) AS aerolineas
        FROM vuelos
        WHERE asientos > 0 AND direccion = 'Arrival'
        GROUP BY ruta
        ORDER BY asientos_totales DESC
    """
    return [dict(r) for r in con.execute(sql).fetchall()]


def exportar_reportes() -> None:
    con = db_conectar()
    try:
        print("\n=== REPORTE POR MES / AEROLÍNEA / RUTA ===")
        rep = reporte_asientos_por_mes(con)
        if rep:
            df = pd.DataFrame(rep)
            print(df.to_string(index=False))
            df.to_csv("reporte_asientos_mes_aero_ruta.csv",
                      index=False, encoding="utf-8-sig")
            print("[CSV] reporte_asientos_mes_aero_ruta.csv")
        else:
            print("  (sin datos todavía)")

        print("\n=== REPORTE POR AEROLÍNEA ===")
        rep2 = reporte_asientos_por_aerolinea(con)
        if rep2:
            df2 = pd.DataFrame(rep2)
            print(df2.to_string(index=False))
            df2.to_csv("reporte_asientos_aerolinea.csv",
                       index=False, encoding="utf-8-sig")
            print("[CSV] reporte_asientos_aerolinea.csv")

        print("\n=== REPORTE POR RUTA ===")
        rep3 = reporte_asientos_por_ruta(con)
        if rep3:
            df3 = pd.DataFrame(rep3)
            print(df3.to_string(index=False))
            df3.to_csv("reporte_asientos_ruta.csv",
                       index=False, encoding="utf-8-sig")
            print("[CSV] reporte_asientos_ruta.csv")
    finally:
        con.close()


# ---------------------------------------------------------------------------
# EXPORT PARA DASHBOARD HTML
# ---------------------------------------------------------------------------
def exportar_json_dashboard(path: str = DASHBOARD_JSON) -> None:
    con = db_conectar()
    try:
        filas = con.execute(
            "SELECT * FROM vuelos ORDER BY horario_local ASC").fetchall()
    finally:
        con.close()

    vuelos = []
    for f in filas:
        vuelos.append({
            "vuelo": f["vuelo"] or "",
            "vuelo_norm": f["vuelo_norm"] or "",
            "fecha": f["fecha"] or "",
            "horario_local": f["horario_local"] or "",
            "horario_real": f["horario_real"] or "",
            "direccion": f["direccion"] or "",
            "estado": f["estado"] or "",
            "ruta": f["ruta"] or "",
            "origen_destino": f["origen_destino"] or "",
            "aeropuerto_iata": f["aeropuerto_iata"] or "",
            "aerolinea_codigo": f["aerolinea_codigo"] or "",
            "aerolinea_nombre": f["aerolinea_nombre"] or "",
            "aerolinea_color": f["aerolinea_color"] or "",
            "matricula": f["matricula"] or "",
            "modelo_avion": f["modelo_avion"] or "",
            "fabricante": f["fabricante"] or "",
            "icao_type_code": f["icao_type_code"] or "",
            "operador": f["operador"] or "",
            "asientos": f["asientos"] or 0,
            "confianza": f["confianza"] or "",
            "fuentes": f["fuentes"] or "",
        })

    payload = {
        "generado": datetime.now(TZ_LOCAL).isoformat(timespec="minutes"),
        "aeropuerto": AEROPUERTO,
        "total": len(vuelos),
        "vuelos": vuelos,
    }

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))

    size_kb = os.path.getsize(path) / 1024
    print(f"[Dashboard] {path} — {len(vuelos)} vuelos ({size_kb:.1f} KB)")


# ---------------------------------------------------------------------------
# SALIDA
# ---------------------------------------------------------------------------
def exportar_csv(vuelos: list[Vuelo], path: str) -> None:
    if not vuelos:
        print("Sin datos para exportar.")
        return
    df = pd.DataFrame([asdict(v) for v in vuelos])
    try:
        df.to_csv(path, index=False, encoding="utf-8-sig")
        print(f"[CSV] {path}  ({len(df)} filas)")
    except PermissionError:
        base, ext = os.path.splitext(path)
        alt = f"{base}_{datetime.now():%Y%m%d_%H%M%S}{ext}"
        df.to_csv(alt, index=False, encoding="utf-8-sig")
        print(f"[CSV] '{path}' bloqueado. Guardado: {alt}")


def mostrar_tabla(vuelos: list[Vuelo], max_filas: int = 300) -> None:
    if not vuelos:
        print("Sin datos para mostrar.")
        return
    df = pd.DataFrame([{
        "Fecha": (v.horario_local[8:10] + "/" + v.horario_local[5:7]
                  if len(v.horario_local) >= 10 else ""),
        "Hora": (v.horario_local[11:16] if len(v.horario_local) >= 16
                 else v.horario_local),
        "Vuelo": v.vuelo,
        "Dir": "ARR" if v.direccion == "Arrival" else "DEP",
        "Estado": v.estado[:14],
        "Ruta": v.ruta or v.origen_destino[:20],
        "Matrícula": v.matricula,
        "Modelo": v.modelo_avion[:22],
        "Tipo": v.icao_type_code,
        "Asient": v.asientos,
        "Conf": v.confianza[:12],
        "Nota": v._nota[:18],
    } for v in vuelos[:max_filas]])
    try:
        from tabulate import tabulate
        print(tabulate(df, headers="keys", tablefmt="simple", showindex=False))
    except ImportError:
        pd.set_option("display.max_colwidth", 24)
        pd.set_option("display.width", 340)
        print(df.to_string(index=False))


def mostrar_resumen(vuelos: list[Vuelo], titulo: str) -> None:
    total = len(vuelos)
    if not total:
        print(f"\n--- {titulo} ---\n  (sin vuelos)")
        return
    con_modelo = sum(1 for v in vuelos if v.modelo_avion)
    con_mat = sum(1 for v in vuelos if v.matricula)
    con_asientos = sum(1 for v in vuelos if v.asientos > 0)
    sin_aero = sum(1 for v in vuelos if not v.aerolinea_nombre)
    suma_asientos = sum(v.asientos for v in vuelos)
    por_conf = defaultdict(int)
    for v in vuelos:
        por_conf[v.confianza or "sin_datos"] += 1
    print(f"\n--- {titulo} ---")
    print(f"  Total vuelos:         {total}")
    print(f"  Con modelo de avión:  {con_modelo} ({100*con_modelo//total}%)")
    print(f"  Con matrícula:        {con_mat} ({100*con_mat//total}%)")
    print(f"  Con asientos:         {con_asientos}")
    print(f"  Sin aerolínea:        {sin_aero}")
    print(f"  Suma asientos:        {suma_asientos}")
    print(f"  Confianza:            {dict(por_conf)}")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main() -> int:
    global _DUMP_RAW
    ap = argparse.ArgumentParser(description="Tablero ROS con DB acumulativa")
    ap.add_argument("--ventana-pasado", type=int, default=VENTANA_PASADO_H)
    ap.add_argument("--ventana-futuro", type=int, default=VENTANA_FUTURO_H)
    ap.add_argument("--csv-window", default="vuelos_ros.csv")
    ap.add_argument("--csv-full", default="vuelos_ros_historico.csv")
    ap.add_argument("--sin-fr24", action="store_true")
    ap.add_argument("--sin-opensky", action="store_true")
    ap.add_argument("--sin-historico", action="store_true")
    ap.add_argument("--sin-cache", action="store_true")
    ap.add_argument("--sin-reportes", action="store_true")
    ap.add_argument("--sin-dashboard", action="store_true")
    ap.add_argument("--sin-migracion", action="store_true")
    ap.add_argument("--dump-raw", action="store_true")
    ap.add_argument("--debug-hist", metavar="VUELO", default=None)
    ap.add_argument("--max-filas", type=int, default=300)
    args = ap.parse_args()

    _DUMP_RAW = args.dump_raw or bool(args.debug_hist)
    if _DUMP_RAW:
        os.makedirs(DUMP_DIR, exist_ok=True)

    ahora = datetime.now(TZ_LOCAL)
    desde_mostrar = ahora - timedelta(hours=args.ventana_pasado)
    hasta_mostrar = ahora + timedelta(hours=args.ventana_futuro)

    print(f"\n=== Tablero ROS — {ahora:%Y-%m-%d %H:%M} ===")
    print(f"Ventana: {desde_mostrar:%Y-%m-%d %H:%M} → {hasta_mostrar:%Y-%m-%d %H:%M}")
    print(f"Enriquecimiento y persistencia: sobre TODOS los vuelos recibidos\n")

    if not _FR24_DISPONIBLE and not args.sin_fr24:
        print(f"⚠️  FR24 tablero: {_FR24_ERROR}\n")
    if not _BS4_DISPONIBLE and not args.sin_historico:
        print("⚠️  bs4 no instalado (pip install beautifulsoup4)\n")

    db_init()
    print(f"[DB] {DB_PATH}")

    if not args.sin_migracion:
        mig = db_migrar_todo()
        if any(mig.values()):
            print(f"[DB] Migración: {mig}")

    cache_aviones: dict[str, dict[str, str]] = {}
    fleet: dict[str, str] = {}
    hist_cache: dict[str, dict[str, str]] = {}
    if not args.sin_cache:
        cache_aviones = _cargar_json(AIRCRAFT_CACHE, {})
        if cache_aviones:
            print(f"[Cache] {len(cache_aviones)} aeronaves en cache")
        fleet = _cargar_json(FLEET_CACHE, {})
        if fleet:
            print(f"[Flota] {len(fleet)} entradas")
        hist_cache = _cargar_json(HIST_CACHE, {})
        if hist_cache:
            print(f"[FR24-hist] {len(hist_cache)} en cache histórico")

    # 1) FIDS
    fuentes: dict[str, list[Vuelo]] = {}
    fuentes["fids"] = fids_obtener()

    # 2) FR24 tablero
    if not args.sin_fr24:
        print()
        fuentes["fr24"] = fr24_obtener(AEROPUERTO)

    # 3) Consolidar
    vuelos = consolidar(fuentes)
    print(f"\n[Consolidado] {len(vuelos)} vuelos únicos")

    # Enriquecer TODOS
    if not args.sin_opensky:
        print()
        db_osk = opensky_cargar_db()
        enriquecer_con_opensky(vuelos, db_osk)

    if cache_aviones:
        print()
        aplicar_cache(vuelos, cache_aviones)

    if not args.sin_historico:
        print()
        hist_cache = enriquecer_con_fr24_historico(
            vuelos, hist_cache, forzar_vuelo=args.debug_hist)

    print()
    aplicar_correcciones_matricula(vuelos)

    if fleet:
        print()
        aplicar_fleet_cache(vuelos, fleet)

    print()
    propagar_matricula_por_turnaround(vuelos)

    # 4) Upsert
    print()
    res = db_upsert_many(vuelos)
    print(f"[DB] {res}")

    # 5) Guardar caches
    if not args.sin_cache:
        cache_aviones = actualizar_cache(vuelos, cache_aviones)
        _guardar_json(AIRCRAFT_CACHE, cache_aviones)
        fleet = actualizar_fleet_cache(vuelos, fleet)
        _guardar_json(FLEET_CACHE, fleet)
        _guardar_json(HIST_CACHE, hist_cache)
        print(f"[Cache] guardados: {len(cache_aviones)} aeronaves, "
              f"{len(fleet)} flota, {len(hist_cache)} histórico")

    # 6) Leer y mostrar
    vuelos_ventana = db_query_ventana(desde_mostrar, hasta_mostrar)
    print("\n" + "=" * 165)
    mostrar_tabla(vuelos_ventana, max_filas=args.max_filas)
    print("=" * 165)
    mostrar_resumen(vuelos_ventana, "RESUMEN VENTANA")

    # 7) Stats
    stats = db_stats()
    print("\n--- DB TOTAL ---")
    print(f"  Vuelos en DB:         {stats['total']}")
    print(f"  Con modelo:           {stats['con_modelo']}")
    print(f"  Con matrícula:        {stats['con_matricula']}")
    print(f"  Con asientos:         {stats['con_asientos']}")
    print(f"  Sin aerolínea:        {stats['sin_aero_nombre']}")
    print(f"  Rango:                {stats['primera_fecha']} → {stats['ultima_fecha']}")
    print(f"  Por confianza:        {stats['por_confianza']}")

    # 8) CSV
    print()
    exportar_csv(vuelos_ventana, args.csv_window)
    if args.csv_full:
        todos = db_query_full()
        exportar_csv(todos, args.csv_full)

    # 9) Reportes
    if not args.sin_reportes:
        exportar_reportes()

    # 10) Export JSON para dashboard HTML
    if not args.sin_dashboard:
        print()
        exportar_json_dashboard()

    return 0


if __name__ == "__main__":
    sys.exit(main())
