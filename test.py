--- /dev/fd/63	2026-10-02 11:35:57.665996675 +0000
+++ /dev/fd/62	2026-10-02 11:35:57.665996675 +0000
@@ -268,6 +268,9 @@
     v = (v or "").strip().upper()
     if not v:
         return ""
+    # Códigos IATA que arrancan con dígito: "2W2075" -> "2075" (pero "2075" queda igual)
+    if len(v) >= 3 and v[0].isdigit() and v[1].isalpha() and v[2].isdigit():
+        return v[2:]
     if v[0].isdigit():
         return v
     if len(v) >= 4 and v[:3].isalpha() and v[3].isdigit():
@@ -280,6 +283,19 @@
     return v[i:] or v
 
 
+def _numeros_equivalentes(a: str, b: str) -> bool:
+    """True si dos números de vuelo son el mismo (ej. FIDS publica "75" y
+    más tarde "2075" para el mismo World2Fly)."""
+    a, b = _normalizar_vuelo(a), _normalizar_vuelo(b)
+    if not a or not b:
+        return False
+    if a == b:
+        return True
+    corto, largo = sorted((a, b), key=len)
+    return (corto.isdigit() and largo.isdigit() and len(corto) >= 2
+            and 0 < len(largo) - len(corto) <= 2 and largo.endswith(corto))
+
+
 @dataclass
 class Vuelo:
     vuelo: str = ""
@@ -532,6 +548,7 @@
         ("FO", "B738", "Boeing 737-800",     189, "flybondi"),
         ("2W", "A332", "Airbus A330-200",    388, "world2fly"),
         ("2W", "A333", "Airbus A330-300",    388, "world2fly"),
+        ("2W", "A359", "Airbus A350-900",    432, "world2fly"),
         ("ZP", "CRJ2", "Bombardier CRJ-200",  50, "paranair"),
         ("O4", "B737", "Boeing 737-700",     149, "andes"),
     ]
@@ -852,6 +869,124 @@
                     "id_externo"]
 
 
+def _db_buscar_equivalente(con: sqlite3.Connection, v: Vuelo, vuelo_norm: str,
+                           direccion: str, fecha: str, ruta: str,
+                           aero: str) -> sqlite3.Row | None:
+    """Busca un registro del mismo vuelo guardado con otro número
+    (mismo id de FIDS, o mismo slot aerolínea+horario+ruta y número equivalente)."""
+    if v.id_externo:
+        f = con.execute(
+            "SELECT * FROM vuelos WHERE id_externo=? AND direccion=? AND fecha=?",
+            (v.id_externo, direccion, fecha)).fetchone()
+        if f:
+            return f
+    if not aero or not v.horario_local:
+        return None
+    for f in con.execute(
+            "SELECT * FROM vuelos WHERE direccion=? AND fecha=? "
+            "AND aerolinea_codigo=? AND horario_local=?",
+            (direccion, fecha, aero, v.horario_local)).fetchall():
+        if not ruta or not f["ruta"] or f["ruta"] == ruta:
+            return f
+    return None
+
+
+def db_deduplicar() -> int:
+    """Fusiona registros duplicados del mismo vuelo (mismo id de FIDS o mismo
+    slot aerolínea+horario+ruta con número equivalente). Devuelve cuántos borró."""
+    con = db_conectar()
+    borrados = 0
+    try:
+        filas = con.execute("SELECT rowid AS rid, * FROM vuelos").fetchall()
+        padre = {f["rid"]: f["rid"] for f in filas}
+
+        def raiz(x: int) -> int:
+            while padre[x] != x:
+                padre[x] = padre[padre[x]]
+                x = padre[x]
+            return x
+
+        def unir(a: int, b: int) -> None:
+            ra, rb = raiz(a), raiz(b)
+            if ra != rb:
+                padre[rb] = ra
+
+        por_id: dict[tuple, int] = {}
+        por_slot: dict[tuple, list] = {}
+        for f in filas:
+            if f["id_externo"]:
+                k = (f["direccion"], f["fecha"], f["id_externo"])
+                if k in por_id:
+                    unir(por_id[k], f["rid"])
+                else:
+                    por_id[k] = f["rid"]
+            ks = (f["direccion"], f["fecha"],
+                  _aero_canonica(f["aerolinea_codigo"] or ""),
+                  f["horario_local"], f["ruta"] or "")
+            por_slot.setdefault(ks, []).append(f)
+        for grupo in por_slot.values():
+            for g in grupo[1:]:
+                unir(grupo[0]["rid"], g["rid"])
+
+        grupos: dict[int, list] = {}
+        for f in filas:
+            grupos.setdefault(raiz(f["rid"]), []).append(f)
+
+        for miembros in grupos.values():
+            if len(miembros) < 2:
+                continue
+            ms = [dict(m) for m in miembros]
+            aeronave = sorted(
+                ms, key=lambda m: (m["confianza_num"] if m["confianza_num"]
+                                   is not None else -1, m["ultima_vez"] or ""),
+                reverse=True)
+            dinamico = sorted(
+                ms, key=lambda m: ("fids" in (m["fuentes"] or "").split("+"),
+                                   m["ultima_vez"] or ""),
+                reverse=True)[0]
+
+            canon = _normalizar_vuelo(dinamico["vuelo_norm"])
+            final = dict(aeronave[0])
+            for campo in ("horario_real", "horario_estimado", "estado",
+                          "puerta", "sector", "aerolinea_color", "id_externo",
+                          "origen_destino"):
+                if dinamico.get(campo):
+                    final[campo] = dinamico[campo]
+            for m in aeronave[1:]:
+                for campo, val in m.items():
+                    if campo in ("rid",):
+                        continue
+                    if not final.get(campo) and val:
+                        final[campo] = val
+            final["vuelo_norm"] = canon
+            final["vuelo"] = canon
+            final["fuentes"] = "+".join(sorted(
+                {x for m in ms for x in (m["fuentes"] or "").split("+") if x}))
+            final["primera_vez"] = min(m["primera_vez"] for m in ms
+                                       if m["primera_vez"])
+            final["ultima_vez"] = max(m["ultima_vez"] for m in ms
+                                      if m["ultima_vez"])
+            aero = _aero_canonica(final.get("aerolinea_codigo") or "")
+            if final.get("icao_type_code"):
+                a = calcular_asientos(con, aero, final["icao_type_code"])
+                if a:
+                    final["asientos"] = a
+            final.pop("rid", None)
+
+            for m in ms:
+                con.execute("DELETE FROM vuelos WHERE rowid=?", (m["rid"],))
+            cols = ", ".join(final.keys())
+            ph = ", ".join("?" * len(final))
+            con.execute(f"INSERT INTO vuelos ({cols}) VALUES ({ph})",
+                        list(final.values()))
+            borrados += len(ms) - 1
+        if borrados:
+            con.commit()
+    finally:
+        con.close()
+    return borrados
+
+
 def db_upsert(v: Vuelo) -> str:
     vuelo_norm, direccion, fecha = v.clave_db()
     if not vuelo_norm or not direccion or not fecha:
@@ -906,6 +1041,15 @@
             "SELECT * FROM vuelos WHERE vuelo_norm=? AND direccion=? AND fecha=?",
             (vuelo_norm, direccion, fecha)).fetchone()
 
+        equivalente = False
+        vuelo_norm_nuevo = vuelo_norm
+        if fila is None:
+            fila = _db_buscar_equivalente(con, v, vuelo_norm, direccion,
+                                          fecha, ruta, aero)
+            if fila is not None:
+                equivalente = True
+                vuelo_norm = fila["vuelo_norm"]
+
         if fila is None:
             datos = {
                 "vuelo_norm": vuelo_norm, "direccion": direccion, "fecha": fecha,
@@ -994,6 +1138,15 @@
         if v._nota and v._nota != (fila["nota"] or "") and mejora_conf:
             updates["nota"] = v._nota
 
+        if equivalente:
+            # Mismo vuelo con otro número: manda el número oficial (FIDS).
+            if "fids" in (v.fuentes or "").split("+") \
+                    and vuelo_norm_nuevo != vuelo_norm:
+                updates["vuelo_norm"] = vuelo_norm_nuevo
+                updates["vuelo"] = vuelo_norm_nuevo
+                accion = "update_parcial" if accion == "noop" else accion
+            else:
+                updates.pop("vuelo", None)
         updates["ultima_vez"] = ahora
 
         if len(updates) > 1 or accion != "noop":
@@ -1921,7 +2074,7 @@
             for j, v_fr24 in enumerate(fr24_vuelos):
                 if fr24_usados[j]:
                     continue
-                if _normalizar_vuelo(v_fr24.vuelo) != vuelo_norm:
+                if not _numeros_equivalentes(v_fr24.vuelo, vuelo_norm):
                     continue
                 if v_fr24.direccion != v_fids.direccion:
                     continue
@@ -1935,6 +2088,18 @@
                     if mejor_diff is None or diff < mejor_diff:
                         mejor_i = j
                         mejor_diff = diff
+            if mejor_i is None:
+                for j, v_fr24 in enumerate(fr24_vuelos):
+                    if (not fr24_usados[j]
+                            and v_fr24.direccion == v_fids.direccion
+                            and v_fr24.horario_local == v_fids.horario_local
+                            and v_fr24.aeropuerto_iata == v_fids.aeropuerto_iata
+                            and _aero_canonica(v_fr24.aerolinea_codigo
+                                               or _extraer_aerolinea(v_fr24.vuelo))
+                            == _aero_canonica(v_fids.aerolinea_codigo
+                                              or _extraer_aerolinea(v_fids.vuelo))):
+                        mejor_i = j
+                        break
             match_i = mejor_i
         if match_i is not None:
             fr24_usados[match_i] = True
@@ -2414,6 +2579,10 @@
 
     db_log_cambios(vuelos_antes, vuelos)
 
+    dups = db_deduplicar()
+    if dups:
+        print(f"[DB] Fusionados {dups} registros duplicados")
+
     corregidos_as = db_fix_consistencia_asientos()
     if corregidos_as:
         print(f"[DB] Corregidos {corregidos_as} asientos")
