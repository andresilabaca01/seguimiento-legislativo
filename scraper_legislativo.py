#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
====================================================================
  SCRAPER DE SEGUIMIENTO LEGISLATIVO BANCARIO
  Consulta el estado de tramitación de cada boletín en el Senado
  (API/HTML de tramitacion.senado.cl) y genera 'proyectos.json',
  que la plataforma HTML consume para actualizarse automáticamente.
====================================================================

USO:
    python scraper_legislativo.py

REQUISITOS:
    pip install requests beautifulsoup4

SALIDA:
    proyectos.json  ->  se publica (GitHub Pages / Drive / servidor)
                        y la plataforma lo lee vía FUENTE_REMOTA.

NOTA IMPORTANTE SOBRE EL SCRAPING:
    Las webs de la Cámara y el Senado cambian su HTML con frecuencia
    y a veces bloquean peticiones automáticas. Este script está
    construido de forma DEFENSIVA: si no logra leer un boletín,
    conserva el dato previo del proyectos.json anterior en lugar de
    borrarlo. Revisa el log al final de cada corrida.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, date

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Faltan dependencias. Ejecuta:  pip install requests beautifulsoup4")
    sys.exit(1)

# --------------------------------------------------------------------
# CONFIGURACIÓN
# --------------------------------------------------------------------
ARCHIVO_BASE   = "proyectos.json"        # se lee el anterior y se sobrescribe
ARCHIVO_SEMILLA = "proyectos_semilla.json"  # base inicial (los 105 proyectos)
TIMEOUT        = 25
PAUSA_ENTRE    = 1.5                      # segundos entre peticiones (cortesía)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; SeguimientoLegislativoABIF/1.0)",
    "Accept": "text/html,application/xhtml+xml,application/json",
    "Accept-Language": "es-CL,es;q=0.9",
}

# Endpoint de tramitación del Senado (acepta el boletín sin guion final largo).
# Devuelve HTML con la ficha del proyecto y su historial de trámites.
URL_SENADO_FICHA = ("https://tramitacion.senado.cl/appsenado/templates/"
                    "tramitacion/index.php?boletin_ini={bol}")

# Página de proyectos ingresados recientemente (se ajusta según disponibilidad).
# El scraper intenta leer los últimos boletines y filtra por palabras clave.
URL_SENADO_RECIENTES = ("https://tramitacion.senado.cl/appsenado/templates/"
                        "tramitacion/proyectos.php")

# --------------------------------------------------------------------
# PALABRAS CLAVE PARA DETECTAR RELEVANCIA BANCARIA (lista estándar editable)
# Si el título o la materia de un proyecto NUEVO contiene alguno de estos
# términos, se agrega a la BANDEJA de la plataforma como candidato por revisar.
# Ajusta esta lista libremente: agrega o quita términos según tu criterio.
# --------------------------------------------------------------------
PALABRAS_CLAVE = [
    "banco", "bancaria", "bancario", "banca",
    "crédito", "credito", "deudor", "deuda", "sobreendeudamiento",
    "secreto bancario", "reserva bancaria",
    "tasa de interés", "tasa de interes", "interés máximo", "tasa máxima convencional", "anatocismo",
    "unidad de fomento", " uf ", "reajustabilidad",
    "hipotecar", "hipoteca", "dividendo",
    "consumidor", "sernac", "ley 19.496", "19496",
    "datos personales", "datos de carácter", "ley 19.628", "19628", "buró", "buro de crédito",
    "cobranza", "operación de crédito", "operacion de credito",
    "tarjeta de crédito", "tarjeta de credito", "medios de pago", "fraude",
    "ley general de bancos", "cmf", "comisión para el mercado financiero",
    "uaf", "lavado de activos", "financiamiento del terrorismo", "crimen organizado",
    "firma electrónica", "firma electronica", "documento electrónico", "pagaré", "pagare", "letra de cambio",
    "cuenta corriente", "factoring", "leasing", "seguro de desgravamen",
    "inembargab", "garantía", "garantia", "prenda", "remate", "subasta",
    "efectivo", "billete", "cajero", "fogape", "afide",
    "negociación colectiva", "negociacion colectiva", "jornada laboral", "sala cuna", "gratificación",
    "educación financiera", "educacion financiera", "cae", "deuda educativa",
]


# --------------------------------------------------------------------
# UTILIDADES
# --------------------------------------------------------------------
def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}")


def boletin_numero(boletin):
    """ '15.975-25' -> '15975-25' ; toma el primero si hay refundidos. """
    primero = boletin.split("/")[0].strip()
    return primero.replace(".", "")


def cargar_json(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("proyectos", data) if isinstance(data, dict) else data
    return None


def normaliza_fecha(texto):
    """ Intenta convertir '09-06-2026' o '09/06/2026' a ISO '2026-06-09'. """
    if not texto:
        return None
    texto = texto.strip()
    for sep in ("-", "/"):
        partes = texto.split(sep)
        if len(partes) == 3 and len(partes[0]) <= 2:
            d, m, a = partes
            try:
                return f"{int(a):04d}-{int(m):02d}-{int(d):02d}"
            except ValueError:
                pass
    # ya viene ISO
    if re.match(r"\d{4}-\d{2}-\d{2}", texto):
        return texto[:10]
    return None


# --------------------------------------------------------------------
# SCRAPING DE UN BOLETÍN EN EL SENADO
# --------------------------------------------------------------------
def consultar_senado(boletin):
    """
    Devuelve un dict con lo que se pudo extraer:
      { 'etapa':..., 'camara':..., 'fecha':..., 'hist':[{f,t},...] }
    o None si no se pudo leer.
    """
    num = boletin_numero(boletin)
    url = URL_SENADO_FICHA.format(bol=num)
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        log(f"  ✗ {boletin}: error de red ({e})")
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    texto_pagina = soup.get_text(" ", strip=True)

    # Si la ficha no existe, el Senado suele responder con poco contenido
    if "Boletín" not in texto_pagina and "boletin" not in r.text.lower():
        log(f"  ? {boletin}: ficha sin datos reconocibles")
        return None

    resultado = {"etapa": None, "camara": None, "fecha": None, "hist": []}

    # --- Etapa / estado actual -------------------------------------
    # Heurística: buscar etiquetas típicas. Ajustable según el HTML real.
    for etiqueta in ["Etapa", "Estado", "Trámite", "Subetapa"]:
        celda = soup.find(string=re.compile(etiqueta, re.I))
        if celda and celda.find_next():
            valor = celda.find_next().get_text(strip=True)
            if valor and len(valor) < 120:
                resultado["etapa"] = valor
                break

    # --- Historial de tramitación (tabla de movimientos) -----------
    # Se buscan filas con fecha + descripción.
    filas = soup.find_all("tr")
    for fila in filas:
        celdas = [c.get_text(" ", strip=True) for c in fila.find_all(["td", "th"])]
        if len(celdas) >= 2:
            f_iso = normaliza_fecha(celdas[0])
            if f_iso:
                desc = " · ".join(celdas[1:])[:300]
                resultado["hist"].append({"f": f_iso, "t": desc})

    # fecha de último movimiento = la más reciente del historial
    if resultado["hist"]:
        resultado["hist"].sort(key=lambda h: h["f"])
        resultado["fecha"] = resultado["hist"][-1]["f"]

    encontrado = bool(resultado["etapa"] or resultado["hist"])
    log(f"  {'✓' if encontrado else '?'} {boletin}: "
        f"{len(resultado['hist'])} movimientos, etapa={resultado['etapa']}")
    return resultado if encontrado else None


# --------------------------------------------------------------------
# FUSIÓN: combina lo scrapeado con el proyecto previo (no destructivo)
# --------------------------------------------------------------------
def fusionar(proyecto, scraped):
    """
    Conserva relevancia, título y descripción curados a mano.
    Solo actualiza etapa, fecha e historial si el scraping trajo algo nuevo.
    """
    if not scraped:
        return proyecto, False

    cambio = False

    # Historial: agregar movimientos nuevos (por fecha+texto) sin duplicar
    existentes = {(h["f"], h["t"][:60]) for h in proyecto.get("hist", [])}
    for h in scraped.get("hist", []):
        clave = (h["f"], h["t"][:60])
        if clave not in existentes:
            proyecto.setdefault("hist", []).append(h)
            existentes.add(clave)
            cambio = True

    # Etapa
    if scraped.get("etapa") and scraped["etapa"] != proyecto.get("etapa"):
        proyecto["etapa"] = scraped["etapa"]
        cambio = True

    # Fecha de último movimiento (solo si es más reciente)
    if scraped.get("fecha"):
        if not proyecto.get("fecha") or scraped["fecha"] > proyecto["fecha"]:
            proyecto["fecha"] = scraped["fecha"]
            cambio = True

    # Reordenar historial por fecha
    if proyecto.get("hist"):
        proyecto["hist"].sort(key=lambda h: h["f"])

    return proyecto, cambio


# --------------------------------------------------------------------
# DETECCIÓN DE PROYECTOS NUEVOS CON RELEVANCIA BANCARIA (bandeja)
# --------------------------------------------------------------------
def texto_coincide(texto):
    """ Devuelve la lista de palabras clave encontradas en el texto. """
    t = " " + texto.lower() + " "
    return [kw.strip() for kw in PALABRAS_CLAVE if kw.lower() in t]


def detectar_candidatos(boletines_seguidos):
    """
    Intenta leer la lista de proyectos recién ingresados al Senado y devuelve
    los que coinciden con palabras clave bancarias y NO están ya en seguimiento.
    Devuelve lista de dicts: {boletin, titulo, fecha, camara, match[]}.
    Defensivo: si no logra leer la página, devuelve [] sin romper la corrida.
    """
    candidatos = []
    try:
        r = requests.get(URL_SENADO_RECIENTES, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        log(f"  (detección de nuevos) no se pudo leer recientes: {e}")
        return candidatos

    soup = BeautifulSoup(r.text, "html.parser")
    seguidos = set()
    for b in boletines_seguidos:
        for parte in b.replace(" ", "").split("/"):
            seguidos.add(parte.replace(".", ""))

    # Buscar enlaces o filas que contengan un número de boletín tipo NNNNN-NN
    patron_bol = re.compile(r"\b(\d{4,5}-\d{2})\b")
    filas = soup.find_all(["tr", "li", "a"])
    vistos = set()
    for fila in filas:
        txt = fila.get_text(" ", strip=True)
        m = patron_bol.search(txt)
        if not m:
            continue
        bol = m.group(1)
        if bol in vistos or bol.replace("-", "") in seguidos or bol in seguidos:
            continue
        matches = texto_coincide(txt)
        if matches:
            vistos.add(bol)
            # quitar el número del título para dejarlo más limpio
            titulo = patron_bol.sub("", txt).strip(" .-—|")
            candidatos.append({
                "boletin": bol,
                "titulo": titulo[:300],
                "fecha": None,
                "camara": "Por confirmar",
                "match": matches[:6],
            })
    log(f"  Candidatos nuevos detectados: {len(candidatos)}")
    return candidatos


# --------------------------------------------------------------------
# PRINCIPAL
# --------------------------------------------------------------------
def main():
    log("=== Iniciando scraping de seguimiento legislativo ===")

    # 1. Cargar base previa (o la semilla la primera vez)
    proyectos = cargar_json(ARCHIVO_BASE) or cargar_json(ARCHIVO_SEMILLA)
    if not proyectos:
        log(f"ERROR: no se encontró {ARCHIVO_BASE} ni {ARCHIVO_SEMILLA}.")
        log("Coloca el proyectos_semilla.json (exportado desde la plataforma) junto al script.")
        sys.exit(1)

    log(f"Base cargada: {len(proyectos)} proyectos.")

    # 2. Recorrer cada boletín y actualizar
    total_cambios = 0
    fallidos = []
    for p in proyectos:
        scraped = consultar_senado(p["boletin"])
        if scraped is None:
            fallidos.append(p["boletin"])
        else:
            _, cambio = fusionar(p, scraped)
            if cambio:
                total_cambios += 1
        time.sleep(PAUSA_ENTRE)

    # 2b. Detectar proyectos nuevos con relevancia bancaria (para la bandeja)
    candidatos = detectar_candidatos([p["boletin"] for p in proyectos])

    # 3. Empaquetar con metadatos de versión (la plataforma los usa)
    salida = {
        "version": datetime.now().strftime("%Y-%m-%d-%H%M"),
        "generado": datetime.now().isoformat(timespec="seconds"),
        "total": len(proyectos),
        "cambios_detectados": total_cambios,
        "no_leidos": fallidos,
        "candidatos": candidatos,
        "proyectos": proyectos,
    }

    with open(ARCHIVO_BASE, "w", encoding="utf-8") as f:
        json.dump(salida, f, ensure_ascii=False, indent=1)

    log("=== Resumen ===")
    log(f"  Proyectos totales : {len(proyectos)}")
    log(f"  Con cambios       : {total_cambios}")
    log(f"  No leídos         : {len(fallidos)}  {fallidos if fallidos else ''}")
    log(f"  Candidatos nuevos : {len(candidatos)}")
    log(f"  Archivo generado  : {ARCHIVO_BASE}  (versión {salida['version']})")
    log("Listo. Publica este archivo para que la plataforma lo lea.")


if __name__ == "__main__":
    main()
