#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Scraper ABIF v8.0 — monitor legislativo ABIF definitivo Cámara + Senado.

Objetivos de esta versión
-------------------------
1. Mantener una sola fuente de verdad para Cámara y Senado.
2. Asociar cada movimiento por número de boletín exacto y evitar contaminación
   con proyectos vecinos de una tabla o página.
3. Separar HECHOS OCURRIDOS (hist) de AGENDA FUTURA (agenda).
4. Deduplicar proyectos y movimientos equivalentes.
5. Proponer en la bandeja SOLO proyectos ingresados desde 01-06-2026.
6. Reducir falsos positivos de palabras cortas/ambiguas (UF, POS, CAE, etc.).
7. Publicar calendario de semanas distritales para el informe semanal.
8. Conservar descripciones, relevancias, resúmenes y análisis ABIF curados a mano.

Uso GitHub Actions / local:
    python scraper_abif.py --input proyectos.json --output proyectos.json

Dependencias mínimas:
    requests beautifulsoup4

`curl_cffi` es opcional. Si está instalado se usa como segundo intento cuando
un sitio bloquea requests. Para páginas públicas también existe fallback Jina.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import difflib
import json
import os
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# CONFIGURACIÓN
# ---------------------------------------------------------------------------
TZ = dt.timezone(dt.timedelta(hours=-4))
HOY = dt.datetime.now(TZ).date()
CANDIDATOS_DESDE = dt.date(2026, 6, 1)
CAMARA_HISTORIAL_DESDE = dt.date(2026, 6, 1)
SENADO_HISTORIAL_DESDE = dt.date(2026, 6, 1)

CAMARA_BASE = "https://www.camara.cl/"
CAMARA_CITACIONES_TODAS = "https://www.camara.cl/legislacion/comisiones/citaciones_todas.aspx"
CAMARA_RESULTADOS_TODOS = "https://www.camara.cl/legislacion/comisiones/resultados_todos.aspx"
CAMARA_TABLA = "https://www.camara.cl/verDoc.aspx?prmId=0&prmTipo=TABLASEMANAL"
CAMARA_OPEN_LEG = "https://opendata.camara.cl/camaradiputados/WServices/WSLegislativo.asmx"
CAMARA_PROYECTOS = "https://www.camara.cl/legislacion/ProyectosDeLey/proyectos_ley.aspx"
CAMARA_COMISIONES_PERMANENTES = "https://www.camara.cl/legislacion/comisiones/comisiones_permanentes.aspx#marca"
CAMARA_SESIONES_SALA = "https://www.camara.cl/legislacion/sesiones_sala/sesiones_sala.aspx"
CAMARA_VOTACIONES = "https://www.camara.cl/legislacion/sala_sesiones/votaciones.aspx"

SENADO_FICHA = (
    "https://tramitacion.senado.cl/appsenado/templates/tramitacion/"
    "index.php?boletin_ini={bol}"
)
SENADO_CITACIONES = "https://www.senado.cl/actividad-legislativa/comisiones/citaciones"
SENADO_CITACIONES_ALT = (
    "https://tramitacion.senado.cl/appsenado/index.php?"
    "ac=citacionesComision&mo=comisiones&tipo_consulta=1"
)
SENADO_RESULTADOS = "https://www.senado.cl/actividad-legislativa/comisiones/resultados"
SENADO_TABLA = "https://www.senado.cl/actividad-legislativa/sala-de-sesiones/tabla-semanal"
SENADO_VOTACIONES = "https://www.senado.cl/actividad-legislativa/sala/votaciones"
SENADO_SESIONES_SALA = "https://www.senado.cl/actividad-legislativa/sala-de-sesiones/sesiones-de-sala"
SENADO_TRAMITACION = "https://tramitacion.senado.cl/appsenado/templates/tramitacion/"
SENADO_ULTIMOS = "https://tramitacion.senado.cl/appsenado/index.php?ac=ultimos_vistos&etc=&mo=tramitacion"
SENADO_SESIONES_COMISION = (
    "https://tramitacion.senado.cl/appsenado/index.php?"
    "ac=sesiones_celebradas&idcomision={idcomision}&mo=comisiones&t="
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ABIF-Monitor-Legislativo/8.0; +https://github.com/)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-CL,es;q=0.9,en;q=0.5",
}

# Fuentes que ABIF exige revisar en CADA ejecución. Las fuentes auxiliares
# (Datos Abiertos, endpoints legacy, Jina, etc.) se mantienen como respaldo,
# pero nunca sustituyen silenciosamente a estas trece fuentes oficiales.
FUENTES_OBLIGATORIAS: Dict[str, Dict[str, str]] = {
    "senado_resultados_comisiones": {"camara":"Senado","nombre":"Resultados de comisiones","url":SENADO_RESULTADOS,"modo":"navegador"},
    "senado_citaciones_comisiones": {"camara":"Senado","nombre":"Citaciones de comisiones","url":SENADO_CITACIONES,"modo":"navegador+detalle"},
    "senado_tabla_semanal": {"camara":"Senado","nombre":"Tabla semanal de Sala","url":SENADO_TABLA,"modo":"web"},
    "senado_votaciones_sala": {"camara":"Senado","nombre":"Votaciones de Sala","url":SENADO_VOTACIONES,"modo":"navegador+detalle"},
    "senado_sesiones_sala": {"camara":"Senado","nombre":"Sesiones de Sala","url":SENADO_SESIONES_SALA,"modo":"navegador+detalle"},
    "senado_fichas_tramitacion": {"camara":"Senado","nombre":"Fichas de tramitación por boletín","url":SENADO_TRAMITACION,"modo":"por-proyecto"},
    "camara_proyectos_ley": {"camara":"Cámara de Diputados","nombre":"Proyectos de ley","url":CAMARA_PROYECTOS,"modo":"web+datos-abiertos"},
    "camara_comisiones_permanentes": {"camara":"Cámara de Diputados","nombre":"Comisiones permanentes","url":CAMARA_COMISIONES_PERMANENTES,"modo":"web+directorio"},
    "camara_citaciones_comisiones": {"camara":"Cámara de Diputados","nombre":"Citaciones de comisiones","url":CAMARA_CITACIONES_TODAS,"modo":"web+semanas"},
    "camara_resultados_comisiones": {"camara":"Cámara de Diputados","nombre":"Resultados de comisiones","url":CAMARA_RESULTADOS_TODOS,"modo":"web+semanas"},
    "camara_tabla_semanal": {"camara":"Cámara de Diputados","nombre":"Tabla semanal de Sala","url":CAMARA_TABLA,"modo":"web"},
    "camara_sesiones_sala": {"camara":"Cámara de Diputados","nombre":"Sesiones de Sala","url":CAMARA_SESIONES_SALA,"modo":"web+detalle"},
    "camara_votaciones_sala": {"camara":"Cámara de Diputados","nombre":"Votaciones de Sala","url":CAMARA_VOTACIONES,"modo":"web+detalle"},
}


# Se conserva explícito porque el monitor necesita poder explicar semanas sin
# actividad ordinaria. Se puede ampliar sin cambiar el resto del código.
SEMANAS_DISTRITALES_2026 = [
    {"desde": "2026-09-14", "hasta": "2026-09-20", "etiqueta": "Semana distrital"},
    {"desde": "2026-10-12", "hasta": "2026-10-18", "etiqueta": "Semana distrital"},
]

# Reglas ponderadas. No se usan coincidencias crudas de 'uf', 'pos', 'cae',
# 'capital' o 'consumo', porque producían demasiados falsos positivos.
REGLAS_ABIF: List[Tuple[re.Pattern, int, str]] = [
    (re.compile(r"\bsecreto\s+bancario\b|\breserva\s+bancaria\b", re.I), 8, "Secreto y reserva bancaria"),
    (re.compile(r"\bley\s+general\s+de\s+bancos\b|\bbanc(?:o|os|aria|arias|ario|arios)\b", re.I), 6, "Regulación bancaria"),
    (re.compile(r"\bcomisi[oó]n\s+para\s+el\s+mercado\s+financiero\b|\bCMF\b", re.I), 5, "Regulación prudencial"),
    (re.compile(r"\bunidad\s+de\s+fomento\b|(?<![A-Za-z])UF(?![A-Za-z])", re.I), 5, "Crédito hipotecario"),
    (re.compile(r"\bcr[eé]dito(?:s)?\s+hipotecari[oa]s?\b|\bdividendos?\s+hipotecarios?\b", re.I), 6, "Crédito hipotecario"),
    (re.compile(r"\boperaci[oó]n(?:es)?\s+de\s+cr[eé]dito\b|\bcr[eé]dito(?:s)?\s+de\s+consumo\b", re.I), 4, "Crédito de consumo"),
    (re.compile(r"\btasa\s+m[aá]xima\s+convencional\b|\banatocismo\b|\bcl[aá]usula(?:s)?\s+de\s+aceleraci[oó]n\b", re.I), 5, "Crédito de consumo"),
    (re.compile(r"\bcobranza\s+extrajudicial\b|\bjuicio\s+ejecutivo\b|\binembargab", re.I), 5, "Cobranza y ejecución"),
    (re.compile(r"\bmedios?\s+de\s+pago\b|\btarjetas?\s+de\s+(?:cr[eé]dito|d[eé]bito)\b|\bpagar[eé]\s+electr[oó]nico\b", re.I), 5, "Pagos y transacciones"),
    (re.compile(r"\bfirma\s+electr[oó]nica\b|\bdocumento(?:s)?\s+electr[oó]nico", re.I), 4, "Pagos y transacciones"),
    (re.compile(r"\bley\s+n?[°º]?\s*20\.009\b|\bfraude(?:s)?\s+(?:bancario|financiero|de\s+medios\s+de\s+pago)", re.I), 5, "Fraudes / medios de pago"),
    (re.compile(r"\bdatos\s+personales\b|\bprotecci[oó]n\s+de\s+datos\b|\btratamiento\s+de\s+datos\b", re.I), 4, "Datos personales financieros"),
    (re.compile(r"\binteligencia\s+artificial\b.*\b(?:solvencia|creditici|scoring)\b|\bscoring\s+crediticio\b", re.I | re.S), 5, "Fintech / Open Finance / IA financiera"),
    (re.compile(r"\bUAF\b|\blavado\s+de\s+activos\b|\bfinanciamiento\s+del\s+terrorismo\b|\bbeneficiario\s+final\b", re.I), 5, "Lavado de activos / AML"),
    (re.compile(r"\bSERNAC\b|\bconsumidor(?:es)?\s+financier", re.I), 3, "Protección al consumidor financiero"),
    (re.compile(r"\bcall\s*center\b|\bteleoperador", re.I), 3, "Seguridad operacional"),
    (re.compile(r"\bciberseguridad\b|\bseguridad\s+operacional\b", re.I), 3, "Seguridad operacional"),
    (re.compile(r"\bnegociaci[oó]n\s+colectiva\b|\bsala\s+cuna\b", re.I), 2, "Laboral bancario"),
]

PROYECTOS_SEGUIMIENTO_OBLIGATORIO = [
    {
        "id": "p99",
        "boletin": "18.216-05",
        "titulo": "Para la reconstrucción nacional y el desarrollo económico y social",
        "desc": "Mensaje del Ejecutivo, ingresado a la Cámara de Diputados el 22/04/2026. Proyecto amplio de reconstrucción y reactivación económica. Para ABIF fue especialmente relevante por normas incorporadas durante la tramitación sobre anatocismo, derecho al olvido financiero y pago a proveedores. El Ejecutivo formuló vetos supresivos sobre esas tres materias; Cámara y Senado aprobaron las observaciones. Al 13/08/2026 la ficha de la Cámara registra trámite ante el Tribunal Constitucional.",
        "camara": "Tribunal Constitucional",
        "etapa": "Tramitación concluida en el Congreso · trámite ante Tribunal Constitucional",
        "urgencia": "Sin urgencia",
        "impacto": "Crédito · Anatocismo · Información financiera · Pago a proveedores",
        "relevancia": "alta",
        "fecha": "2026-08-13",
        "hist": [
            {
                "f": "2026-04-22",
                "t": "Ingreso del mensaje a la Cámara de Diputados.",
                "organo": "Cámara de Diputados",
                "tipo": "tramitacion",
                "fuente": "https://www.camara.cl/legislacion/proyectosdeley/tramitacion.aspx?prmBOLETIN=18216-05&prmID=18872"
            },
            {
                "f": "2026-07-22",
                "t": "La Comisión Mixta despachó su propuesta para resolver las divergencias entre ambas Cámaras.",
                "organo": "Comisión Mixta",
                "tipo": "resultado",
                "fuente": "https://tramitacion.senado.cl/senado/site/edic/base/port/comisiones.html"
            },
            {
                "f": "2026-08-10",
                "t": "La Cámara de Diputados se pronunció sobre las observaciones del Ejecutivo al proyecto.",
                "organo": "Cámara de Diputados",
                "tipo": "resultado",
                "fuente": "https://www.camara.cl/legislacion/proyectosdeley/tramitacion.aspx?prmBOLETIN=18216-05&prmID=18872"
            },
            {
                "f": "2026-08-12",
                "t": "El Senado aprobó los tres vetos supresivos del Ejecutivo: anatocismo, derecho al olvido financiero y reglas sobre plazos excepcionales de pago. Con ello terminó la tramitación del veto en el Congreso.",
                "organo": "Senado",
                "tipo": "resultado",
                "fuente": "https://www.senado.cl/comunicaciones/noticias/proyecto-de-reconstruccion-con-aprobacion-de-vetos-supresivos-camara-alta"
            },
            {
                "f": "2026-08-13",
                "t": "La ficha oficial de la Cámara registra el proyecto en trámite ante el Tribunal Constitucional.",
                "organo": "Tribunal Constitucional",
                "tipo": "tramitacion",
                "fuente": "https://www.camara.cl/legislacion/proyectosdeley/tramitacion.aspx?prmBOLETIN=18216-05&prmID=18872"
            }
        ],
        "agenda": [],
        "resumenes": [],
        "resumen_ejecutivo_bancario": "Proyecto de alta relevancia para ABIF por las normas financieras incorporadas durante su tramitación. Los vetos del Ejecutivo, aprobados por ambas Cámaras, suprimieron la prohibición absoluta del anatocismo, la regla de derecho al olvido financiero y la restricción a plazos excepcionales de pago. El seguimiento debe mantenerse hasta que concluya el control ante el Tribunal Constitucional y se determine el texto definitivo.",
        "categorias_abif": [
            "Crédito de consumo",
            "Datos personales financieros",
            "Cobranza y ejecución"
        ],
        "manual": "seguimiento_obligatorio"
    },
    {
        "id": "p100",
        "boletin": "18.524-05",
        "titulo": "Extiende vigencia y aumenta cobertura de la ley N° 21.748 y modifica normas que indica",
        "desc": "Mensaje del Ejecutivo, ingresado el 04/08/2026. Extiende el subsidio a la tasa de interés hipotecaria para viviendas nuevas: aumenta la cobertura en 30.000 viviendas hasta un total de 80.000, eleva el valor máximo de las propiedades a 6.000 UF y extiende el plazo para solicitar el beneficio hasta el 31/05/2028. También adecua el Programa de Garantías Apoyo a la Vivienda Nueva del FOGAES y autoriza aportes fiscales al fondo por hasta US$175 millones.",
        "camara": "Despachado por el Congreso",
        "etapa": "Segundo trámite concluido · en condiciones de convertirse en ley",
        "urgencia": "Discusión inmediata",
        "impacto": "Crédito hipotecario · Subsidio a la tasa · FOGAES · Demanda hipotecaria",
        "relevancia": "alta",
        "fecha": "2026-08-11",
        "hist": [
            {
                "f": "2026-08-04",
                "t": "Ingreso del mensaje con urgencia de discusión inmediata; pasa a la Comisión de Hacienda de la Cámara de Diputados.",
                "organo": "Cámara de Diputados",
                "tipo": "tramitacion",
                "fuente": "https://www.camara.cl/legislacion/comisiones/citaciones_todas.aspx"
            },
            {
                "f": "2026-08-05",
                "t": "La Cámara de Diputados aprobó el proyecto en primer trámite y lo despachó al Senado.",
                "organo": "Cámara de Diputados",
                "tipo": "resultado",
                "fuente": "https://www.camara.cl/legislacion/comisiones/informes.aspx?prmID=4890"
            },
            {
                "f": "2026-08-10",
                "t": "La Comisión de Hacienda del Senado trató el proyecto en segundo trámite constitucional.",
                "organo": "Senado",
                "tipo": "resultado",
                "fuente": "https://tramitacion.senado.cl/appsenado/index.php?ac=sesiones_celebradas&ano=2026&comi_nombre=de+Hacienda&fecha=10%2F08%2F2026&idcomision=188&idpunto=nada&idsesion=22814&mo=comisiones"
            },
            {
                "f": "2026-08-11",
                "t": "El Senado aprobó por unanimidad el proyecto. La iniciativa quedó en condiciones de convertirse en ley.",
                "organo": "Senado",
                "tipo": "resultado",
                "fuente": "https://www.senado.cl/comunicaciones/noticias/luz-verde-al-proyecto-que-extiende-el-subsidio-la-tasa-de-interes"
            }
        ],
        "agenda": [],
        "resumenes": [],
        "resumen_ejecutivo_bancario": "Alta relevancia para ABIF: amplía directamente el universo de operaciones hipotecarias beneficiadas por subsidio de tasa y garantía estatal. Extiende cobertura, rango de precio y vigencia del mecanismo, y ajusta FOGAES. El monitor debe seguir su promulgación, publicación y eventuales instrucciones operativas para las instituciones financieras.",
        "categorias_abif": [
            "Crédito hipotecario",
            "Regulación prudencial"
        ],
        "manual": "seguimiento_obligatorio"
    }
]


MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}

# ---------------------------------------------------------------------------
# UTILIDADES
# ---------------------------------------------------------------------------
def sin_tildes(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn")


def norm_text(s: str) -> str:
    s = sin_tildes(s or "").lower()
    s = re.sub(r"https?://\S+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_boletin(b: str) -> str:
    b = str(b or "").replace("N°", "").replace("Nº", "").replace("n°", "")
    b = re.sub(r"(?i)bolet[ií]n(?:es)?", "", b).replace(".", "")
    b = re.sub(r"\s+", "", b).strip().upper()
    m = re.search(r"(\d{4,5}-\d{2})", b)
    return m.group(1) if m else b


def fmt_boletin(b: str) -> str:
    n = norm_boletin(b)
    m = re.fullmatch(r"(\d{1,2})(\d{3})-(\d{2})", n)
    return f"{m.group(1)}.{m.group(2)}-{m.group(3)}" if m else str(b or "")


def boletines_proyecto(p: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for x in re.split(r"\s*/\s*|\s+y\s+|\s*,\s*", str(p.get("boletin") or "")):
        n = norm_boletin(x)
        if n and n not in out:
            out.append(n)
    return out


def boletines_en_texto(texto: str) -> List[str]:
    out: List[str] = []
    patron = re.compile(r"(?i)(?:bolet[ií]n(?:es)?\s*(?:n[°º]?\s*)?|\b)(\d{1,2}\.?\d{3}-\d{2})\b")
    for m in patron.finditer(texto or ""):
        b = norm_boletin(m.group(1))
        if b and b not in out:
            out.append(b)
    return out


def parse_fecha_any(s: str) -> Optional[str]:
    if not s:
        return None
    s0 = re.sub(r"\s+", " ", str(s)).strip()
    m = re.search(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b", s0)
    if m:
        y, mo, d = map(int, m.groups())
        try:
            return dt.date(y, mo, d).isoformat()
        except ValueError:
            pass
    m = re.search(r"\b(\d{1,2})[-/](\d{1,2})[-/](20\d{2})\b", s0)
    if m:
        d, mo, y = map(int, m.groups())
        try:
            return dt.date(y, mo, d).isoformat()
        except ValueError:
            pass
    sn = norm_text(s0)
    m = re.search(
        r"\b(\d{1,2})(?:\s+de)?\s+(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre)(?:\s+de)?\s+(20\d{2})\b",
        sn,
    )
    if m:
        d, mo, y = int(m.group(1)), MESES[m.group(2)], int(m.group(3))
        try:
            return dt.date(y, mo, d).isoformat()
        except ValueError:
            pass
    return None


def fecha_iso_valida(s: Optional[str]) -> bool:
    try:
        dt.date.fromisoformat(s or "")
        return True
    except Exception:
        return False


def request_text(url: str, params: Optional[dict] = None, timeout: int = 25) -> str:
    """Lee una URL con requests, curl_cffi opcional y Jina para páginas públicas."""
    last: Optional[Exception] = None
    for intento in range(2):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=timeout, allow_redirects=True)
            r.raise_for_status()
            if not r.encoding:
                r.encoding = "utf-8"
            return r.text
        except Exception as e:
            last = e
            time.sleep(0.7 + intento * 0.5)

    try:
        from curl_cffi import requests as crequests  # type: ignore
        r = crequests.get(url, params=params, headers=HEADERS, timeout=timeout, impersonate="chrome120")
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}")
        return r.text
    except Exception as e:
        last = e

    usar_jina = os.getenv("USAR_JINA_FALLBACK", "1").strip() != "0"
    publica = any(host in url for host in ["www.camara.cl/", "www.senado.cl/", "tramitacion.senado.cl/"])
    if usar_jina and publica and params is None:
        try:
            jurl = "https://r.jina.ai/" + url
            r = requests.get(jurl, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=max(timeout, 45))
            r.raise_for_status()
            return r.text
        except Exception as e:
            last = e
    raise RuntimeError(f"No se pudo leer {url}: {last}")


def plain_text(raw: str) -> str:
    if re.search(r"<html|<body|<table|<div|<span|<p|<br|<li", raw or "", re.I):
        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        return re.sub(r"\s+", " ", soup.get_text(" ")).strip()
    return re.sub(r"\s+", " ", raw or " ").strip()


def score_abif(texto: str) -> Tuple[int, List[str], List[str]]:
    score = 0
    hits: List[str] = []
    cats: List[str] = []
    for rx, peso, cat in REGLAS_ABIF:
        m = rx.search(texto or "")
        if m:
            score += peso
            hit = re.sub(r"\s+", " ", m.group(0)).strip()
            if hit and hit.lower() not in [x.lower() for x in hits]:
                hits.append(hit)
            if cat not in cats:
                cats.append(cat)
    return score, hits[:8], cats[:5]


def relevancia_max(a: str, b: str) -> str:
    orden = {"alta": 3, "media": 2, "baja": 1}
    return a if orden.get(a, 0) >= orden.get(b, 0) else b


def texto_mejor(a: Any, b: Any) -> str:
    aa, bb = str(a or "").strip(), str(b or "").strip()
    return bb if len(bb) > len(aa) else aa


def titulo_visible(p: Dict[str, Any]) -> str:
    return str(p.get("titulo") or "").split("\n\n", 1)[0].strip()


def tipo_evento_desde_texto(t: str) -> str:
    n = norm_text(t)
    if "urgencia" in n:
        return "urgencia"
    if any(x in n for x in ["citacion", "en agenda", "tabla semanal", "orden del dia", "en tabla"]):
        return "agenda"
    if any(x in n for x in ["resultado", "aprobado", "rechazado", "votacion", "discusion", "se despacho", "queda pendiente"]):
        return "resultado"
    if "informe de comision" in n:
        return "informe"
    if any(x in n for x in ["ingreso de proyecto", "cuenta de proyecto", "pasa a comision", "oficio"]):
        return "tramitacion"
    return "otro"


def inferir_organo(texto: str, default: str = "Congreso") -> str:
    n = norm_text(texto)
    if "comision mixta" in n:
        return "Comisión Mixta"
    if "senado" in n:
        return "Senado"
    if "camara" in n or "diputad" in n:
        return "Cámara de Diputados"
    return default or "Congreso"


def limpiar_evento_texto(texto: str) -> str:
    s = re.sub(r"\s*Fuente:\s*https?://\S+", "", str(texto or ""), flags=re.I)
    s = re.sub(r"\s+", " ", s).strip(" .;-")
    # La trazabilidad se conserva en campos separados (fuente/organo).
    # No se corta por cantidad fija de caracteres: el recorte arbitrario era
    # responsable de frases terminadas a la mitad en el monitor.
    s = re.sub(r"^\[(?:C[aá]mara|Senado)[^\]]*\]\s*", "", s, flags=re.I)
    return s


def fingerprint_texto(texto: str) -> str:
    n = norm_text(limpiar_evento_texto(texto))
    n = re.sub(r"\b(?:boletin|nro|numero|n)\b", " ", n)
    n = re.sub(r"\d{1,2}:\d{2}", " ", n)
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def eventos_similares(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    if a.get("f") != b.get("f"):
        return False
    if (a.get("organo") or "") != (b.get("organo") or ""):
        return False
    if (a.get("tipo") or tipo_evento_desde_texto(a.get("t", ""))) != (b.get("tipo") or tipo_evento_desde_texto(b.get("t", ""))):
        return False
    fa, fb = fingerprint_texto(a.get("t", "")), fingerprint_texto(b.get("t", ""))
    if not fa or not fb:
        return False
    if fa == fb or fa in fb or fb in fa:
        return True
    return difflib.SequenceMatcher(None, fa[:500], fb[:500]).ratio() >= 0.88


def dedup_eventos(eventos: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for raw in sorted(eventos, key=lambda h: (h.get("f") or "", h.get("organo") or "", h.get("t") or "")):
        if not raw or not fecha_iso_valida(raw.get("f")) or not str(raw.get("t") or "").strip():
            continue
        h = copy.deepcopy(raw)
        h["t"] = limpiar_evento_texto(h.get("t", ""))
        h["tipo"] = h.get("tipo") or tipo_evento_desde_texto(h["t"])
        h["organo"] = h.get("organo") or inferir_organo(h["t"])
        if any(eventos_similares(h, x) for x in out):
            continue
        out.append(h)
    return out


def sanear_proyecto(p: Dict[str, Any]) -> Dict[str, Any]:
    hoy = HOY.isoformat()
    hist: List[Dict[str, Any]] = []
    agenda: List[Dict[str, Any]] = list(p.get("agenda") or [])
    for h in p.get("hist") or []:
        if not isinstance(h, dict):
            continue
        tipo = h.get("tipo") or tipo_evento_desde_texto(h.get("t", ""))
        hh = copy.deepcopy(h)
        hh["tipo"] = tipo
        hh["organo"] = hh.get("organo") or inferir_organo(hh.get("t", ""), p.get("camara") or "Congreso")
        # Cualquier evento futuro se trata como agenda aunque un scraper antiguo
        # lo haya insertado en hist.
        if fecha_iso_valida(hh.get("f")) and hh["f"] > hoy:
            agenda.append(hh)
        elif tipo == "agenda" and fecha_iso_valida(hh.get("f")) and hh["f"] >= hoy:
            agenda.append(hh)
        else:
            hist.append(hh)

    p["hist"] = dedup_eventos(hist)
    # Agenda pasada se elimina: una citación pasada no equivale a que la sesión
    # efectivamente haya ocurrido. Los resultados/ficha son los que alimentan hist.
    p["agenda"] = dedup_eventos([a for a in agenda if fecha_iso_valida(a.get("f")) and a["f"] >= hoy])
    pasados = [h for h in p["hist"] if h.get("f") and h["f"] <= hoy]
    p["fecha"] = max((h["f"] for h in pasados), default=p.get("fecha") if fecha_iso_valida(p.get("fecha")) and p.get("fecha") <= hoy else None)
    p.setdefault("resumenes", [])
    return p


def merge_proyectos(base: Dict[str, Any], otro: Dict[str, Any]) -> Dict[str, Any]:
    base = sanear_proyecto(copy.deepcopy(base))
    otro = sanear_proyecto(copy.deepcopy(otro))
    # Conserva el título curado más corto/limpio, siempre que no sea demasiado genérico.
    tb, to = titulo_visible(base), titulo_visible(otro)
    if (not tb) or (to and len(to) >= 25 and len(to) < len(tb)):
        base["titulo"] = otro.get("titulo")
    base["desc"] = texto_mejor(base.get("desc"), otro.get("desc"))
    base["resumen_ejecutivo_bancario"] = texto_mejor(base.get("resumen_ejecutivo_bancario"), otro.get("resumen_ejecutivo_bancario"))
    base["impacto"] = texto_mejor(base.get("impacto"), otro.get("impacto"))
    base["relevancia"] = relevancia_max(base.get("relevancia", "baja"), otro.get("relevancia", "baja"))

    cats = []
    for x in list(base.get("categorias_abif") or []) + list(otro.get("categorias_abif") or []):
        if x and x not in cats:
            cats.append(x)
    if cats:
        base["categorias_abif"] = cats

    # Para estado/órgano, prioriza el registro con fecha factual más reciente,
    # pero nunca reemplaza un valor útil por "Por confirmar".
    fb, fo = base.get("fecha") or "0000-00-00", otro.get("fecha") or "0000-00-00"
    reciente = otro if fo > fb else base
    for campo in ["camara", "etapa", "urgencia"]:
        v = reciente.get(campo)
        if v and norm_text(v) not in {"por confirmar", ""}:
            base[campo] = v
    if outro_id := otro.get("manual"):
        base["manual"] = base.get("manual") or outro_id

    # Metadatos de comisiones del Senado descubiertos desde citaciones. Son claves
    # para poder consultar luego los resultados server-rendered por comisión.
    sc = []
    seen_sc = set()
    for c in list(base.get("senado_comisiones") or []) + list(otro.get("senado_comisiones") or []):
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id") or "").strip()
        if not cid or cid in seen_sc:
            continue
        seen_sc.add(cid)
        sc.append(copy.deepcopy(c))
    if sc:
        base["senado_comisiones"] = sc

    base["hist"] = dedup_eventos(list(base.get("hist") or []) + list(otro.get("hist") or []))
    base["agenda"] = dedup_eventos(list(base.get("agenda") or []) + list(otro.get("agenda") or []))

    res = list(base.get("resumenes") or []) + list(otro.get("resumenes") or [])
    seen = set(); rr = []
    for r in res:
        k = (r.get("f"), r.get("org"), norm_text(r.get("t", ""))[:180])
        if k in seen:
            continue
        seen.add(k); rr.append(r)
    base["resumenes"] = rr
    return sanear_proyecto(base)


def consolidar_proyectos(proyectos: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    mapa: Dict[str, Dict[str, Any]] = {}
    sin_clave: List[Dict[str, Any]] = []
    merges = 0
    for raw in proyectos:
        p = sanear_proyecto(copy.deepcopy(raw))
        bs = sorted(boletines_proyecto(p))
        key = "/".join(bs)
        if not key:
            sin_clave.append(p)
            continue
        if key in mapa:
            mapa[key] = merge_proyectos(mapa[key], p)
            merges += 1
        else:
            mapa[key] = p
    return list(mapa.values()) + sin_clave, merges


def pmap_all(proyectos: Sequence[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    for p in proyectos:
        for b in boletines_proyecto(p):
            out.setdefault(b, []).append(p)
    return out


def add_evento(p: Dict[str, Any], evento: Dict[str, Any], agenda: bool = False) -> bool:
    if not fecha_iso_valida(evento.get("f")) or not evento.get("t"):
        return False
    e = copy.deepcopy(evento)
    e["t"] = limpiar_evento_texto(e["t"])
    e["tipo"] = e.get("tipo") or ("agenda" if agenda else tipo_evento_desde_texto(e["t"]))
    e["organo"] = e.get("organo") or inferir_organo(e["t"], p.get("camara") or "Congreso")
    target = "agenda" if agenda or e["f"] > HOY.isoformat() else "hist"
    p.setdefault(target, [])
    if any(eventos_similares(e, x) for x in p[target]):
        return False
    p[target].append(e)
    p[target] = dedup_eventos(p[target])
    if target == "hist" and (not p.get("fecha") or e["f"] > p["fecha"]):
        p["fecha"] = e["f"]
    return True


def get_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"version": "inicial", "proyectos": [], "candidatos": []}
    d = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(d, list):
        return {"version": "importado", "proyectos": d, "candidatos": []}
    return d


def save_json(path: Path, data: Dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def asegurar_proyectos_seguimiento_obligatorio(data: Dict[str, Any]) -> int:
    """Inyecta proyectos que ABIF decidió seguir, aunque su ingreso sea anterior al
    corte de la bandeja. El corte 01-06-2026 aplica solo a NUEVOS CANDIDATOS.
    """
    proyectos = data.setdefault("proyectos", [])
    presentes = set(pmap_all(proyectos))
    agregados = 0
    for raw in PROYECTOS_SEGUIMIENTO_OBLIGATORIO:
        bs = boletines_proyecto(raw)
        if bs and bs[0] not in presentes:
            proyectos.append(copy.deepcopy(raw))
            presentes.update(bs)
            agregados += 1
    return agregados


# ---------------------------------------------------------------------------
# METADATOS DE FICHA SENADO (sirve para ambos orígenes)
# ---------------------------------------------------------------------------
def ficha_senado_metadata(boletin: str, cache: Dict[str, Optional[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    b = norm_boletin(boletin)
    if b in cache:
        return cache[b]
    url = SENADO_FICHA.format(bol=b)
    try:
        raw = request_text(url)
        soup = BeautifulSoup(raw, "html.parser")
        text = plain_text(raw)
        if not boletines_en_texto(text) and b not in norm_boletin(text):
            cache[b] = None
            return None

        def val_despues(label: str) -> str:
            # HTML clásico: etiqueta y valor en celdas hermanas.
            s = soup.find(string=re.compile(label, re.I))
            if s:
                par = s.parent
                if par:
                    nxt = par.find_next(["td", "div", "span", "p"])
                    if nxt:
                        v = re.sub(r"\s+", " ", nxt.get_text(" ", strip=True)).strip()
                        if v and norm_text(label) not in norm_text(v):
                            return v
            # fallback sobre texto plano
            m = re.search(label + r"\s*:?\s*(.{1,250}?)(?=\s+(?:Fecha de Ingreso|C[aá]mara de Origen|Iniciativa|Tipo de Proyecto|Etapa|Refundido|Link para compartir)\s*:|$)", text, re.I)
            return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""

        titulo = val_despues(r"T[ií]tulo")
        fi_txt = val_despues(r"Fecha\s+de\s+Ingreso")
        cam_origen = val_despues(r"C[aá]mara\s+de\s+Origen")
        etapa = val_despues(r"Etapa")
        meta = {
            "boletin": b,
            "titulo": titulo,
            "fecha_ingreso": parse_fecha_any(fi_txt),
            "camara_origen": cam_origen,
            "etapa": etapa,
            "url": url,
        }
        cache[b] = meta
        return meta
    except Exception:
        cache[b] = None
        return None


def historial_senado_ficha(boletin: str) -> Tuple[List[Dict[str, Any]], Optional[str], Optional[str]]:
    """Extrae movimientos de la ficha individual, por lo que no puede mezclar boletines."""
    b = norm_boletin(boletin)
    url = SENADO_FICHA.format(bol=b)
    try:
        raw = request_text(url)
        soup = BeautifulSoup(raw, "html.parser")
        eventos: List[Dict[str, Any]] = []
        for fila in soup.find_all("tr"):
            celdas = [re.sub(r"\s+", " ", c.get_text(" ", strip=True)).strip() for c in fila.find_all(["td", "th"])]
            if len(celdas) < 2:
                continue
            f = parse_fecha_any(celdas[0])
            if not f:
                # Algunas tablas incluyen mes/año en otra celda; no inventar fecha.
                continue
            desc = " · ".join(x for x in celdas[1:] if x).strip()
            if len(desc) < 4:
                continue
            eventos.append({
                "f": f,
                "t": desc,
                "organo": "Senado",
                "fuente": url,
                "tipo": tipo_evento_desde_texto(desc),
            })

        text = plain_text(raw)
        etapa = None
        m = re.search(r"\bEtapa\s*:?\s*(.{1,180}?)(?=\s+(?:Link para compartir|Fecha de Ingreso|C[aá]mara de Origen|Iniciativa|Tipo de Proyecto|Refundido)\s*:|$)", text, re.I)
        if m:
            etapa = re.sub(r"\s+", " ", m.group(1)).strip()
        return dedup_eventos(eventos), etapa, url
    except Exception:
        return [], None, url


# ---------------------------------------------------------------------------
# CANDIDATOS
# ---------------------------------------------------------------------------
def candidato_valido(fecha_ingreso: Optional[str], score: int) -> bool:
    if not fecha_iso_valida(fecha_ingreso):
        return False
    return dt.date.fromisoformat(fecha_ingreso) >= CANDIDATOS_DESDE and score >= 4


def merge_candidate(candidatos: List[Dict[str, Any]], cand: Dict[str, Any], existentes: set) -> bool:
    b = norm_boletin(cand.get("boletin", ""))
    if not b or b in existentes:
        return False
    # Segundo control, por si un origen incompleto intentó saltarse el filtro.
    score = int(cand.get("score_abif") or 0)
    if not candidato_valido(cand.get("fecha_ingreso") or cand.get("fecha"), score):
        return False
    for c in candidatos:
        if norm_boletin(c.get("boletin", "")) == b:
            # Enriquecer sin duplicar.
            for k, v in cand.items():
                if v and not c.get(k):
                    c[k] = v
            return False
    candidatos.append(cand)
    existentes.add(b)
    return True


def crear_candidato_desde_boletin(
    b: str,
    contexto: str,
    fuente: str,
    meta_cache: Dict[str, Optional[Dict[str, Any]]],
) -> Optional[Dict[str, Any]]:
    score, hits, cats = score_abif(contexto)
    if score < 4:
        return None
    meta = ficha_senado_metadata(b, meta_cache)
    if not meta:
        return None
    titulo = meta.get("titulo") or contexto[:260]
    score2, hits2, cats2 = score_abif((titulo or "") + " " + contexto)
    fecha_ingreso = meta.get("fecha_ingreso")
    if not candidato_valido(fecha_ingreso, max(score, score2)):
        return None
    cats_all = []
    for c in cats2 + cats:
        if c not in cats_all:
            cats_all.append(c)
    return {
        "boletin": fmt_boletin(b),
        "titulo": titulo,
        "desc": "Detectado automáticamente. Revisar texto oficial antes de incorporar al seguimiento.",
        "camara": meta.get("camara_origen") or "Por confirmar",
        "etapa": meta.get("etapa") or "Ingreso / por revisar",
        "urgencia": "Sin urgencia",
        "impacto": " · ".join(cats_all[:3]) if cats_all else "Por evaluar",
        "fecha": fecha_ingreso,
        "fecha_ingreso": fecha_ingreso,
        "match": list(dict.fromkeys(hits2 + hits))[:8],
        "categorias_abif": cats_all[:5],
        "score_abif": max(score, score2),
        "fuente": fuente,
    }


# ---------------------------------------------------------------------------
# CÁMARA — DATOS ABIERTOS PARA NUEVOS PROYECTOS
# ---------------------------------------------------------------------------
def xml_records(xml_text: str) -> List[Dict[str, str]]:
    try:
        root = ET.fromstring(xml_text.encode("utf-8"))
    except Exception:
        root = ET.fromstring(xml_text)

    def tag(e):
        return e.tag.split("}", 1)[-1] if "}" in e.tag else e.tag

    recs: List[Dict[str, str]] = []
    for elem in root.iter():
        children = list(elem)
        if len(children) < 2:
            continue
        d: Dict[str, str] = {}
        for ch in children:
            t = " ".join(ch.itertext()).strip()
            if t:
                d[tag(ch)] = re.sub(r"\s+", " ", t)
        joined = " ".join(d.values())
        if boletines_en_texto(joined) and any(x in norm_text(" ".join(d.keys())) for x in ["titulo", "nombre", "materia"]):
            recs.append(d)
    seen = set(); out = []
    for r in recs:
        joined = " ".join(r.values())
        bs = boletines_en_texto(joined)
        key = (bs[0] if bs else "", norm_text(joined)[:160])
        if key not in seen:
            seen.add(key); out.append(r)
    return out


def call_camara_open(method: str, **params) -> List[Dict[str, str]]:
    return xml_records(request_text(f"{CAMARA_OPEN_LEG}/{method}", params=params))


def find_field(record: Dict[str, str], *needles: str) -> str:
    needles_n = [norm_text(n) for n in needles]
    for k, v in record.items():
        kn = norm_text(k)
        if any(n in kn for n in needles_n):
            return v
    return ""


def scan_open_data_camara(data: Dict[str, Any]) -> Tuple[int, List[str]]:
    proyectos = data.setdefault("proyectos", [])
    candidatos = data.setdefault("candidatos", [])
    existentes = set(pmap_all(proyectos)) | {norm_boletin(c.get("boletin", "")) for c in candidatos}
    logs: List[str] = []
    nuevos = 0
    # Por la regla solicitada no tiene sentido recorrer años anteriores.
    year = CANDIDATOS_DESDE.year
    for method, fuente in [
        ("retornarMocionesXAnno", "Cámara Datos Abiertos · Mociones"),
        ("retornarMensajesXAnno", "Cámara Datos Abiertos · Mensajes"),
    ]:
        try:
            recs = call_camara_open(method, prmAnno=str(year))
            logs.append(f"{fuente} {year}: {len(recs)} registros")
            for r in recs:
                joined = " ".join(r.values())
                bs = boletines_en_texto(joined)
                if not bs:
                    continue
                b = bs[0]
                if b in existentes:
                    continue
                titulo = find_field(r, "titulo", "nombre", "materia") or joined[:260]
                # Busca campos de fecha de ingreso/presentación, no cualquier fecha del registro.
                ftxt = find_field(r, "fecha ingreso", "fechaingreso", "fecha presentacion", "fechapresentacion")
                fecha_ingreso = parse_fecha_any(ftxt)
                score, hits, cats = score_abif(titulo + " " + joined)
                if not candidato_valido(fecha_ingreso, score):
                    continue
                cand = {
                    "boletin": fmt_boletin(b),
                    "titulo": titulo,
                    "desc": f"Detectado automáticamente desde {fuente}. Revisar texto oficial antes de incorporar.",
                    "camara": "Cámara de Diputados",
                    "etapa": "Ingreso / por revisar",
                    "urgencia": "Sin urgencia",
                    "impacto": " · ".join(cats[:3]) if cats else "Por evaluar",
                    "fecha": fecha_ingreso,
                    "fecha_ingreso": fecha_ingreso,
                    "match": hits,
                    "categorias_abif": cats,
                    "score_abif": score,
                    "fuente": fuente,
                }
                if merge_candidate(candidatos, cand, existentes):
                    nuevos += 1
        except Exception as e:
            logs.append(f"ERROR {fuente}: {e}")
    return nuevos, logs


# ---------------------------------------------------------------------------
# PÁGINAS SEMANALES CÁMARA
# ---------------------------------------------------------------------------
def recent_week_links(index_url: str, max_links: int = 16) -> List[str]:
    raw = request_text(index_url)
    soup = BeautifulSoup(raw, "html.parser")
    links: List[str] = []
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if "_semana.aspx" in href:
            links.append(urljoin(index_url, href))
    for m in re.finditer(r"https://www\.camara\.cl/legislacion/comisiones/(?:citaciones|resultados)_semana\.aspx\?prmSemana=20\d{2}-\d{1,2}", raw):
        links.append(m.group(0))
    out: List[str] = []
    for u in links:
        if u not in out:
            out.append(u)
    return out[:max_links]


def semana_urls_calculadas(
    tipo: str,
    desde: dt.date = CAMARA_HISTORIAL_DESDE,
    weeks_forward: int = 2,
) -> List[str]:
    """Genera todas las semanas Cámara desde una fecha base hasta dos semanas futuras.

    v7 usaba solo seis semanas hacia atrás. Eso hacía que un proyecto pudiera quedar
    incompleto si el índice semanal de Cámara fallaba durante una corrida. Desde v7.2
    se re-lee siempre el período completo desde 01-06-2026; la deduplicación impide
    que esto multiplique movimientos.
    """
    if tipo not in {"citaciones", "resultados"}:
        return []
    page = "citaciones_semana.aspx" if tipo == "citaciones" else "resultados_semana.aspx"
    lunes_inicio = desde - dt.timedelta(days=desde.weekday())
    lunes_fin = (HOY - dt.timedelta(days=HOY.weekday())) + dt.timedelta(weeks=weeks_forward)
    out: List[str] = []
    d = lunes_inicio
    while d <= lunes_fin:
        iso = d.isocalendar()
        out.append(f"https://www.camara.cl/legislacion/comisiones/{page}?prmSemana={iso.year}-{iso.week}")
        d += dt.timedelta(weeks=1)
    return out


def fechas_dia_en_texto(text: str) -> List[Tuple[int, str]]:
    rx = re.compile(
        r"(?i)(?:lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo),?\s+(\d{1,2})\s+de\s+"
        r"(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre)\s+de\s+(20\d{2})"
    )
    out = []
    for m in rx.finditer(text):
        try:
            out.append((m.start(), dt.date(int(m.group(3)), MESES[norm_text(m.group(2))], int(m.group(1))).isoformat()))
        except ValueError:
            pass
    return out


def fecha_para_pos(text: str, pos: int, fallback: Optional[str] = None) -> Optional[str]:
    f = fallback
    for p, ff in fechas_dia_en_texto(text):
        if p <= pos:
            f = ff
        else:
            break
    return f


def contexto_especifico_bloque(texto: str, boletin: str) -> str:
    """Devuelve una frase/bloque coherente referido al boletín pedido.

    Las páginas de comisiones suelen poner varios proyectos dentro de una misma
    fila. No debemos asignar toda esa fila a cada boletín. Se conserva la oración
    que contiene el boletín y, cuando es útil, la oración inmediatamente anterior.
    """
    t = re.sub(r"\s+", " ", texto or " ").strip()
    if not t:
        return ""
    b = norm_boletin(boletin)
    ocurr = list(re.finditer(r"(?i)(?:bolet[ií]n(?:es)?\s*(?:n[°º]?\s*)?)?(\d{1,2}\.?\d{3}-\d{2})", t))
    own = next((m for m in ocurr if norm_boletin(m.group(1)) == b), None)
    if not own:
        return t
    if len(ocurr) == 1:
        return t

    # Límites naturales de oración alrededor del boletín.
    antes = [t.rfind('. ', 0, own.start()), t.rfind('! ', 0, own.start()), t.rfind('? ', 0, own.start())]
    start = max(antes)
    start = start + 2 if start >= 0 else 0
    despues = [x for x in [t.find('. ', own.end()), t.find('! ', own.end()), t.find('? ', own.end())] if x >= 0]
    end = min(despues) + 1 if despues else len(t)

    # Nunca atravesar hacia el siguiente boletín distinto.
    idx = ocurr.index(own)
    if idx + 1 < len(ocurr) and end > ocurr[idx + 1].start():
        previo_punto = t.rfind('. ', own.end(), ocurr[idx + 1].start())
        end = previo_punto + 1 if previo_punto >= own.end() else ocurr[idx + 1].start()
    if idx > 0 and start < ocurr[idx - 1].end():
        punto = t.find('. ', ocurr[idx - 1].end(), own.start())
        start = punto + 2 if punto >= 0 else own.start()

    seg = t[start:end].strip()
    return seg or t


def contextos_por_boletin(raw: str, fuente_url: str) -> List[Tuple[str, str, Optional[str]]]:
    """Extrae contexto por boletín y conserva correctamente la fecha del día.

    Corrección v7.2: en las tablas semanales de Cámara la fecha suele estar en una
    fila/encabezado anterior, no dentro de la fila que contiene el boletín. v7
    encontraba el boletín pero devolvía fecha=None y luego descartaba el movimiento.
    Aquí ubicamos cada fila dentro del texto completo de la página y heredamos el
    último encabezado diario anterior (MARTES, 7 DE JULIO DE 2026, etc.).
    """
    page_text = plain_text(raw)
    soup = BeautifulSoup(raw, "html.parser") if re.search(r"<html|<table|<tr|<li|<div", raw or "", re.I) else None
    encontrados: List[Tuple[str, str, Optional[str]]] = []

    if soup:
        cursor = 0
        for node in soup.find_all(["tr", "li"]):
            txt = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
            bs = boletines_en_texto(txt)
            if not bs or len(txt) < 15:
                continue

            # 1) Fecha explícita en la propia fila.
            f = parse_fecha_any(txt)

            # 2) Encabezado HTML cercano, cuando existe.
            if not f:
                prev = node.find_previous(["h1", "h2", "h3", "h4", "h5", "strong", "b"])
                f = parse_fecha_any(prev.get_text(" ", strip=True) if prev else "")

            # 3) Método robusto: posición de esta fila en el texto completo y último
            #    encabezado diario anterior. Es el que corrige los resultados Cámara.
            if not f and page_text:
                needle = txt[:240]
                pos = page_text.find(needle, cursor)
                if pos < 0:
                    pos = page_text.find(needle)
                if pos < 0:
                    # Si la fila fue normalizada de forma distinta, basta ubicar el boletín.
                    muestras = [fmt_boletin(b) for b in bs] + list(bs)
                    posiciones = [page_text.find(x, cursor) for x in muestras if x]
                    posiciones = [x for x in posiciones if x >= 0]
                    pos = min(posiciones) if posiciones else -1
                if pos >= 0:
                    f = fecha_para_pos(page_text, pos, None)
                    cursor = max(cursor, pos + max(1, len(needle) // 2))

            for b in bs:
                encontrados.append((b, contexto_especifico_bloque(txt, b), f))

        # Si logramos extraer filas, las usamos. La mayoría ya tendrá fecha gracias al
        # método por posición. Para menciones aún sin fecha, el fallback de texto de
        # más abajo las complementa en vez de descartarlas silenciosamente.
        if encontrados and all(f for _, _, f in encontrados):
            return encontrados

    # Fallback texto/Markdown: límites entre boletines vecinos para no mezclar proyectos.
    text = page_text
    occ = list(re.finditer(r"(?i)(?:bolet[ií]n(?:es)?\s*(?:n[°º]?\s*)?)?(\d{1,2}\.?\d{3}-\d{2})", text))
    fallback: List[Tuple[str, str, Optional[str]]] = []
    for i, m in enumerate(occ):
        b = norm_boletin(m.group(1))
        prev_end = occ[i - 1].end() if i else 0
        next_start = occ[i + 1].start() if i + 1 < len(occ) else len(text)
        left = max(prev_end, m.start() - 420)
        right = min(next_start, m.end() + 760)
        ctx = re.sub(r"\s+", " ", text[left:right]).strip()
        ctx = contexto_especifico_bloque(ctx, b)
        f = fecha_para_pos(text, m.start(), parse_fecha_any(text[max(0, m.start()-260):m.start()+100]))
        fallback.append((b, ctx, f))

    # Combina priorizando las filas HTML con fecha; agrega fallback solo cuando aporta
    # una fecha que faltaba o una mención no vista.
    salida: List[Tuple[str, str, Optional[str]]] = []
    for item in encontrados + fallback:
        b, ctx, f = item
        key = (b, fingerprint_texto(ctx)[:220])
        idx = next((i for i, x in enumerate(salida) if (x[0], fingerprint_texto(x[1])[:220]) == key), None)
        if idx is None:
            salida.append(item)
        elif not salida[idx][2] and f:
            salida[idx] = item
    return salida


def scan_camara_semana(data: Dict[str, Any], meta_cache: Dict[str, Optional[Dict[str, Any]]]) -> Tuple[int, List[str]]:
    proyectos = data.setdefault("proyectos", [])
    candidatos = data.setdefault("candidatos", [])
    pm = pmap_all(proyectos)
    existentes = set(pm) | {norm_boletin(c.get("boletin", "")) for c in candidatos}
    logs: List[str] = []
    cambios = 0

    urls: List[Tuple[str, str]] = []
    for tipo, idx in [("citaciones", CAMARA_CITACIONES_TODAS), ("resultados", CAMARA_RESULTADOS_TODOS)]:
        try:
            urls += [(tipo, u) for u in recent_week_links(idx)]
        except Exception as e:
            logs.append(f"ERROR índice Cámara {tipo}: {e}")
        urls += [(tipo, u) for u in semana_urls_calculadas(tipo)]
    # Tabla semanal de Sala, si está accesible, se toma como agenda.
    urls.append(("tabla", CAMARA_TABLA))

    seen = set(); dedup = []
    for item in urls:
        if item not in seen:
            seen.add(item); dedup.append(item)

    for tipo, url in dedup:
        try:
            raw = request_text(url)
            contexts = contextos_por_boletin(raw, url)
            logs.append(f"Cámara {tipo}: {len(contexts)} menciones · {url}")
            for b, ctx, f in contexts:
                # Para una agenda sin fecha explícita no se inventa hoy; no sirve al reporte.
                if not f:
                    continue
                if b in pm:
                    es_futuro = f > HOY.isoformat()
                    # Citaciones futuras van a agenda. Una citación ya pasada se conserva
                    # como hecho documental ("fue citado para...") y luego se elimina
                    # si existe un resultado sustantivo del mismo día. Esto evita perder
                    # completamente una sesión cuando la página de Resultados falla.
                    if tipo == "resultados":
                        tipo_evento = "resultado"
                        a_agenda = False
                    elif tipo == "citaciones":
                        tipo_evento = "agenda" if es_futuro else "citacion"
                        a_agenda = es_futuro
                    else:  # tabla semanal de Sala
                        tipo_evento = "agenda"
                        a_agenda = f >= HOY.isoformat()
                        if not a_agenda:
                            continue

                    evento = {
                        "f": f,
                        "t": ctx,
                        "organo": "Cámara de Diputados",
                        "fuente": url,
                        "tipo": tipo_evento,
                    }
                    for p in pm[b]:
                        if add_evento(p, evento, agenda=a_agenda):
                            cambios += 1
                elif b not in existentes:
                    cand = crear_candidato_desde_boletin(b, ctx, url, meta_cache)
                    if cand and merge_candidate(candidatos, cand, existentes):
                        cambios += 1
        except Exception as e:
            logs.append(f"ERROR Cámara {tipo} {url}: {e}")
    return cambios, logs


# ---------------------------------------------------------------------------
# SENADO — FICHAS + AGENDA/RESULTADOS
# ---------------------------------------------------------------------------
def scan_senado_fichas(data: Dict[str, Any]) -> Tuple[int, List[str]]:
    proyectos = data.setdefault("proyectos", [])
    logs: List[str] = []
    cambios = 0
    # Una consulta por boletín primario único. Para refundidos, la ficha principal
    # suele contener el historial consolidado y evita multiplicar tráfico.
    vistos = set()
    for p in proyectos:
        bs = boletines_proyecto(p)
        if not bs:
            continue
        b = bs[0]
        if b in vistos:
            continue
        vistos.add(b)
        eventos, etapa, url = historial_senado_ficha(b)
        for pp in pmap_all(proyectos).get(b, [p]):
            if url:
                pp["url_tramitacion"] = url
            for ev in eventos:
                if add_evento(pp, ev, agenda=False):
                    cambios += 1
            if etapa and etapa != pp.get("etapa"):
                # La ficha del Senado es fuente útil de etapa incluso si el proyecto
                # está en la Cámara o Mixta, pero no reemplaza una etapa curada más
                # específica por un valor vacío/genérico.
                if len(etapa) >= 5:
                    pp["etapa"] = etapa
        logs.append(f"Senado ficha {fmt_boletin(b)}: {len(eventos)} movimientos")
        time.sleep(float(os.getenv("PAUSA_SENADO", "0.15")))
    return cambios, logs


def parse_senado_tabla(text: str) -> List[Tuple[str, str, Optional[str]]]:
    """La tabla semanal usa encabezados 'Sesión N°.. Martes 18 de Agosto de 2026'."""
    out: List[Tuple[str, str, Optional[str]]] = []
    ses = list(re.finditer(r"(?i)Sesi[oó]n\s+N[°º]?\s*\d+\s+(?:Lunes|Martes|Mi[eé]rcoles|Jueves|Viernes)[, ]+\s*(\d{1,2})\s+de\s+(\w+)\s+de\s+(20\d{2})", text))
    if not ses:
        return contextos_por_boletin(text, SENADO_TABLA)
    for i, sm in enumerate(ses):
        try:
            f = dt.date(int(sm.group(3)), MESES[norm_text(sm.group(2))], int(sm.group(1))).isoformat()
        except Exception:
            f = None
        block = text[sm.end(): ses[i + 1].start() if i + 1 < len(ses) else len(text)]
        occ = list(re.finditer(r"(?i)(?:Bol\.\s*N[°º]?\s*|Bolet[ií]n\s*N[°º]?\s*)?(\d{1,2}\.?\d{3}-\d{2})", block))
        for j, m in enumerate(occ):
            b = norm_boletin(m.group(1))
            next_start = occ[j + 1].start() if j + 1 < len(occ) else len(block)
            ctx = block[max(0, m.start()-80): min(next_start, m.end()+650)]
            ctx = re.sub(r"\s+", " ", ctx).strip()
            out.append((b, ctx, f))
    return out



def id_comision_desde_href(href: str) -> Optional[str]:
    """Extrae el id de comisión desde URLs modernas o legacy del Senado."""
    h = str(href or "")
    m = re.search(r"/actividad-legislativa/comisiones/(\d+)(?:/|$|[?#])", h)
    if m:
        return m.group(1)
    m = re.search(r"[?&]idcomision=(\d+)", h, re.I)
    return m.group(1) if m else None


def registrar_comision_senado(p: Dict[str, Any], cid: str, nombre: str = "", fuente: str = "") -> bool:
    cid = str(cid or "").strip()
    if not cid:
        return False
    arr = p.setdefault("senado_comisiones", [])
    for c in arr:
        if str(c.get("id") or "") == cid:
            cambio = False
            if nombre and not c.get("nombre"):
                c["nombre"] = nombre; cambio = True
            if fuente and c.get("fuente") != fuente:
                c["fuente"] = fuente; cambio = True
            c["ultima_mencion"] = HOY.isoformat()
            return cambio
    arr.append({
        "id": cid,
        "nombre": nombre or "Comisión del Senado",
        "fuente": fuente,
        "ultima_mencion": HOY.isoformat(),
    })
    return True


def descubrir_comisiones_senado(raw: str, proyectos: List[Dict[str, Any]], fuente_url: str) -> int:
    """Asocia boletines seguidos con el id de la comisión que aparece en citaciones.

    El martes el Senado puede publicar una citación futura y el jueves el resultado
    ya no está en esa misma vista. Guardar el id de comisión permite consultar
    directamente el historial de sesiones de esa comisión en cada corrida.
    """
    pm = pmap_all(proyectos)
    cambios = 0
    if not raw:
        return 0

    # HTML: busca el enlace de comisión en la fila/bloque o encabezado cercano.
    if re.search(r"<html|<table|<tr|<li|<div", raw, re.I):
        soup = BeautifulSoup(raw, "html.parser")
        for node in soup.find_all(["tr", "li", "div", "p"]):
            txt = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
            bs = [b for b in boletines_en_texto(txt) if b in pm]
            if not bs:
                continue
            candidatos = []
            cur = node
            for _ in range(4):
                if cur is None:
                    break
                candidatos.extend(cur.find_all("a", href=True))
                cur = cur.parent
            # Los encabezados de las citaciones modernas suelen estar antes de la materia.
            for prev in node.find_all_previous(["h2", "h3", "h4", "h5", "a"], limit=20):
                if getattr(prev, "name", None) == "a" and prev.get("href"):
                    candidatos.append(prev)
                else:
                    candidatos.extend(prev.find_all("a", href=True))

            visto = set()
            for a in candidatos:
                href = a.get("href", "")
                cid = id_comision_desde_href(href)
                if not cid or cid in visto:
                    continue
                visto.add(cid)
                nombre = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
                if not nombre:
                    # Si el enlace no tiene texto útil, rescata el encabezado más cercano.
                    h = a.find_parent(["h2", "h3", "h4", "h5"])
                    nombre = re.sub(r"\s+", " ", h.get_text(" ", strip=True)).strip() if h else ""
                for b in bs:
                    for p in pm[b]:
                        if registrar_comision_senado(p, cid, nombre, urljoin(fuente_url, href)):
                            cambios += 1
                # En una fila de citación basta el primer id de comisión razonable.
                break

    # Fallback Markdown/texto (Jina / página legacy): busca idcomision o URL moderna
    # en una ventana cercana a cada boletín.
    text = raw
    for m in re.finditer(r"(?i)(\d{1,2}\.?\d{3}-\d{2})", text):
        b = norm_boletin(m.group(1))
        if b not in pm:
            continue
        window = text[max(0, m.start()-2600): min(len(text), m.end()+2600)]
        ids = []
        ids += re.findall(r"[?&]idcomision=(\d+)", window, re.I)
        ids += re.findall(r"/actividad-legislativa/comisiones/(\d+)(?:/|$|[?#])", window)
        if ids:
            cid = ids[0]
            for p in pm[b]:
                if registrar_comision_senado(p, cid, "Comisión del Senado", fuente_url):
                    cambios += 1
    return cambios


def _resultado_senado_desde_celdas(celdas: List[str], fila, base_url: str, nombre_comision: str) -> Optional[Tuple[str, List[str], str, str]]:
    """Normaliza una fila del listado legacy de sesiones celebradas."""
    if len(celdas) < 4:
        return None
    f = parse_fecha_any(celdas[0])
    if not f:
        return None
    try:
        if dt.date.fromisoformat(f) < SENADO_HISTORIAL_DESDE:
            return None
    except Exception:
        return None
    bs = boletines_en_texto(" ".join(celdas[:4]))
    if not bs:
        return None
    tema = celdas[1].strip() if len(celdas) > 1 else ""
    aspectos = celdas[3].strip() if len(celdas) > 3 else ""
    acuerdos = celdas[4].strip() if len(celdas) > 4 else ""
    partes = []
    if nombre_comision:
        partes.append(nombre_comision)
    if tema:
        partes.append(tema)
    if aspectos:
        partes.append(f"Aspectos considerados: {aspectos}")
    if acuerdos:
        partes.append(f"Acuerdos: {acuerdos}")
    texto = ". ".join(x.rstrip(" .") for x in partes if x).strip() + "."
    fuente = base_url
    if fila is not None:
        for a in fila.find_all("a", href=True):
            href = a.get("href", "")
            if "sesiones_celebradas" in href or "idsesion=" in href or "actividad-legislativa/comisiones/" in href:
                fuente = urljoin(base_url, href)
                break
    return f, bs, texto, fuente




def _parse_fecha_resultado_senado(s: str) -> Optional[str]:
    """Fecha visible en la tabla moderna de resultados del Senado."""
    f = parse_fecha_any(s or "")
    if f:
        return f
    m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(20\d{2})\b", str(s or ""))
    if m:
        try:
            return dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
        except ValueError:
            return None
    return None


def _bloques_puntos_sesion_senado(texto: str) -> List[str]:
    """Divide una ficha moderna de sesión del Senado por Nº Punto.

    La ficha puede contener varios proyectos/temas. Trabajar por punto evita
    mezclar Aspectos considerados y Acuerdos de un proyecto con otro.
    """
    t = str(texto or "").replace("\r", "\n")
    # Conserva saltos razonables porque ayudan a encontrar etiquetas.
    t = re.sub(r"[ \t]+", " ", t)
    starts = [m.start() for m in re.finditer(r"(?im)^\s*N[º°o]?\s*Punto\s*:\s*\d+", t)]
    if not starts:
        # Variación frecuente: "Nº Punto: 1 | Nº Boletín: ..."
        starts = [m.start() for m in re.finditer(r"(?i)N[º°o]?\s*Punto\s*:\s*\d+", t)]
    if not starts:
        return [t]
    out = []
    for i, st in enumerate(starts):
        en = starts[i + 1] if i + 1 < len(starts) else len(t)
        b = t[st:en].strip()
        if b:
            out.append(b)
    return out or [t]


def _campo_ficha_senado(block: str, etiqueta: str, siguientes: Sequence[str]) -> str:
    """Extrae un campo de una ficha de sesión manteniendo la frase completa."""
    if not block:
        return ""
    labels = "|".join(re.escape(x) for x in siguientes)
    pat = rf"(?is){re.escape(etiqueta)}\s*:\s*(.*?)(?=(?:{labels})\s*:|$)" if labels else rf"(?is){re.escape(etiqueta)}\s*:\s*(.*)$"
    m = re.search(pat, block)
    if not m:
        return ""
    val = re.sub(r"\s+", " ", m.group(1)).strip(" |.-\n\t")
    return val


def _eventos_desde_ficha_moderna_senado(
    texto: str,
    fecha: str,
    comision: str,
    fuente: str,
    materia_indice: str,
    proyectos: Sequence[Dict[str, Any]],
) -> List[Tuple[str, Dict[str, Any]]]:
    """Convierte una ficha moderna de sesión en eventos por boletín exacto."""
    pm = pmap_all(proyectos)
    out: List[Tuple[str, Dict[str, Any]]] = []
    if not fecha_iso_valida(fecha):
        return out

    for block in _bloques_puntos_sesion_senado(texto):
        bs = [b for b in boletines_en_texto(block) if b in pm]
        tema = _campo_ficha_senado(block, "Tema", ["Aspectos Considerados", "Aspectos considerados", "Acuerdos", "Presentaciones ante Comisión"])
        aspectos = _campo_ficha_senado(block, "Aspectos Considerados", ["Acuerdos", "Presentaciones ante Comisión"])
        if not aspectos:
            aspectos = _campo_ficha_senado(block, "Aspectos considerados", ["Acuerdos", "Presentaciones ante Comisión"])
        acuerdos = _campo_ficha_senado(block, "Acuerdos", ["Presentaciones ante Comisión", "Parlamentarios Asistentes", "Asistencia"])

        # Algunas fichas modernas no repiten el número de boletín en el texto del
        # punto. En ese caso, se permite una asociación SOLO si la materia visible
        # de la tabla coincide fuertemente con un único proyecto seguido.
        if not bs:
            materia = norm_text((tema or "") + " " + (materia_indice or ""))
            candidatos: List[Tuple[float, str]] = []
            for p in proyectos:
                tit = norm_text(titulo_visible(p))
                if not tit or not materia:
                    continue
                # Combina similitud de secuencia y solapamiento de palabras largas.
                seq = difflib.SequenceMatcher(None, materia[:600], tit[:600]).ratio()
                a = {w for w in re.findall(r"[a-z0-9]+", materia) if len(w) >= 5}
                bset = {w for w in re.findall(r"[a-z0-9]+", tit) if len(w) >= 5}
                jac = len(a & bset) / max(1, len(a | bset))
                score = max(seq, jac * 1.7)
                if score >= 0.55:
                    for bol in boletines_proyecto(p):
                        candidatos.append((score, bol))
            candidatos.sort(reverse=True)
            if candidatos:
                best = candidatos[0][0]
                top = sorted({b for s, b in candidatos if s >= best - 0.05})
                if len(top) == 1:
                    bs = top

        if not bs:
            continue

        partes: List[str] = []
        # El contenido sustantivo es prioritario; evita encabezados genéricos como
        # "Comisión Mixta" sin explicar qué ocurrió.
        if aspectos:
            partes.append(aspectos)
        elif tema:
            partes.append(tema)
        elif materia_indice:
            partes.append(re.sub(r"\s+", " ", materia_indice).strip())
        if acuerdos:
            partes.append(f"Acuerdos: {acuerdos}")
        texto_evento = " ".join(x.strip() for x in partes if x.strip()).strip()
        if not texto_evento:
            continue

        org = "Comisión Mixta" if "mixta" in norm_text(comision) else "Senado"
        evento = {
            "f": fecha,
            "t": texto_evento,
            "organo": org,
            "comision": comision,
            "fuente": fuente,
            "tipo": "resultado",
            "origen": "senado_resultados_moderno",
        }
        for b in bs:
            out.append((b, copy.deepcopy(evento)))
    return out


def scan_senado_resultados_modernos(data: Dict[str, Any]) -> Tuple[int, List[str]]:
    """Lee TODOS LOS DÍAS la página oficial moderna de Resultados del Senado.

    Esa página carga sus filas con JavaScript. Por eso se renderiza con Chromium
    (Playwright), exactamente como un navegador real. Luego se abre la FICHA DE
    SESIÓN enlazada en cada resultado reciente y se extraen, por boletín exacto,
    los "Aspectos considerados" y "Acuerdos".

    La URL guardada en cada evento es la ficha oficial de sesión, no un listado
    genérico ni una comisión incorrecta.
    """
    proyectos = data.setdefault("proyectos", [])
    pm = pmap_all(proyectos)
    logs: List[str] = []
    cambios = 0
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception as e:
        return 0, [f"ERROR Senado resultados modernos: Playwright no disponible ({e})"]

    # Revisa un margen amplio por si el Senado publica/corrige el resultado con
    # retraso. En cada ejecución la página vuelve a consultarse desde cero.
    desde = HOY - dt.timedelta(days=int(os.getenv("SENADO_RESULTADOS_DIAS", "21")))
    hasta = HOY + dt.timedelta(days=1)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(
                viewport={"width": 1440, "height": 1100},
                user_agent=HEADERS["User-Agent"],
                locale="es-CL",
            )
            page.goto(SENADO_RESULTADOS, wait_until="domcontentloaded", timeout=60000)
            # La tabla es DataTables/dinámica. Espera a que se reemplace la fila
            # "No hay resultados..." por filas reales.
            try:
                page.wait_for_function(
                    """() => {
                      const rows=[...document.querySelectorAll('table tbody tr')];
                      return rows.some(r => r.querySelectorAll('td').length >= 4 && !/No hay resultados/i.test(r.innerText));
                    }""",
                    timeout=30000,
                )
            except Exception:
                # Un pequeño margen adicional permite capturar respuestas lentas.
                page.wait_for_timeout(5000)

            # Si DataTables ofrece selector de cantidad, usa la opción máxima
            # disponible para no perder sesiones del mismo día.
            try:
                selects = page.locator("select")
                for i in range(selects.count()):
                    sel = selects.nth(i)
                    opts = sel.locator("option").all()
                    vals = []
                    for op in opts:
                        try:
                            txt = op.inner_text().strip()
                            val = op.get_attribute("value") or txt
                            if re.fullmatch(r"\d+", txt):
                                vals.append((int(txt), val))
                        except Exception:
                            pass
                    if vals:
                        _, val = max(vals)
                        sel.select_option(value=val)
                        page.wait_for_timeout(800)
                        break
            except Exception:
                pass

            rows_data: List[Dict[str, Any]] = []
            rows = page.locator("table tbody tr")
            for i in range(rows.count()):
                row = rows.nth(i)
                try:
                    cells = [re.sub(r"\s+", " ", x).strip() for x in row.locator("td").all_inner_texts()]
                except Exception:
                    continue
                if len(cells) < 4 or any("No hay resultados que coincidan" in x for x in cells):
                    continue
                f = _parse_fecha_resultado_senado(cells[0])
                if not f:
                    continue
                try:
                    fd = dt.date.fromisoformat(f)
                except Exception:
                    continue
                if fd < desde or fd > hasta:
                    continue
                links = row.locator("a").evaluate_all("els => els.map(a => ({href:a.href, text:(a.innerText||'').trim()}))")
                ficha = ""
                # Preferir el enlace de la última columna / texto ficha-ver.
                for a in reversed(links):
                    href = str(a.get("href") or "")
                    at = norm_text(str(a.get("text") or ""))
                    if href and ("comisiones/" in href or "ficha" in at or at == "ver"):
                        ficha = href
                        break
                if not ficha:
                    continue
                rows_data.append({
                    "fecha": f,
                    "comision": cells[1],
                    "materia": cells[2],
                    "ficha": ficha,
                })

            # Dedup de fichas: una sesión puede aparecer repetida por varios puntos.
            uniq: Dict[str, Dict[str, Any]] = {}
            for r in rows_data:
                uniq.setdefault(r["ficha"], r)

            procesadas = 0
            relevantes = 0
            for ficha, meta in uniq.items():
                detail = browser.new_page(
                    viewport={"width": 1360, "height": 1000},
                    user_agent=HEADERS["User-Agent"],
                    locale="es-CL",
                )
                try:
                    detail.goto(ficha, wait_until="domcontentloaded", timeout=45000)
                    try:
                        detail.wait_for_load_state("networkidle", timeout=10000)
                    except Exception:
                        detail.wait_for_timeout(1200)
                    body = detail.locator("body").inner_text(timeout=10000)
                    procesadas += 1
                    eventos = _eventos_desde_ficha_moderna_senado(
                        body,
                        meta["fecha"],
                        meta["comision"],
                        ficha,
                        meta["materia"],
                        proyectos,
                    )
                    for b, evento in eventos:
                        if b not in pm:
                            continue
                        for p in pm[b]:
                            if add_evento(p, evento, agenda=False):
                                cambios += 1
                        relevantes += 1
                except Exception as e:
                    logs.append(f"ERROR ficha resultado Senado {ficha}: {e}")
                finally:
                    try:
                        detail.close()
                    except Exception:
                        pass
            browser.close()
            marcar_fuente(data, "senado_resultados_comisiones", "ok", f"{len(rows_data)} fila(s) recientes; {procesadas} ficha(s) leídas", paginas=1+procesadas, eventos=relevantes)
            logs.append(
                f"Senado resultados modernos: {len(rows_data)} fila(s) recientes; "
                f"{procesadas} ficha(s) leídas; {relevantes} evento(s) relevante(s) · {SENADO_RESULTADOS}"
            )
    except Exception as e:
        marcar_fuente(data, "senado_resultados_comisiones", "error", str(e), paginas=0)
        logs.append(f"ERROR Senado resultados modernos {SENADO_RESULTADOS}: {e}")
    return cambios, logs


def depurar_resultados_senado_preferir_modernos(data: Dict[str, Any]) -> int:
    """Si existe ficha moderna oficial, elimina el resultado vago del mismo día."""
    quitados = 0
    for p in data.get("proyectos") or []:
        hist = list(p.get("hist") or [])
        modernas = {
            h.get("f")
            for h in hist
            if h.get("origen") == "senado_resultados_moderno"
            or re.search(r"www\.senado\.cl/actividad-legislativa/comisiones/\d+/\d+", str(h.get("fuente") or ""), re.I)
        }
        if not modernas:
            continue
        nuevo = []
        for h in hist:
            mismo_dia = h.get("f") in modernas
            tipo = h.get("tipo") or tipo_evento_desde_texto(h.get("t", ""))
            fuente = str(h.get("fuente") or "")
            es_moderno = h.get("origen") == "senado_resultados_moderno" or bool(re.search(r"www\.senado\.cl/actividad-legislativa/comisiones/\d+/\d+", fuente, re.I))
            es_senado_resultado = tipo == "resultado" and (h.get("organo") in {"Senado", "Comisión Mixta"} or "senado" in fuente.lower())
            es_generico = (
                fuente.rstrip("/") == SENADO_RESULTADOS.rstrip("/")
                or "sesiones_celebradas" in fuente
                or len(limpiar_evento_texto(h.get("t", ""))) < 80
            )
            if mismo_dia and es_senado_resultado and es_generico and not es_moderno:
                quitados += 1
                continue
            nuevo.append(h)
        p["hist"] = dedup_eventos(nuevo)
        sanear_proyecto(p)
    return quitados

def scan_senado_resultados_por_comision(data: Dict[str, Any]) -> Tuple[int, List[str]]:
    """Consulta resultados reales por comisión en el endpoint legacy del Senado.

    La página moderna /actividad-legislativa/comisiones/resultados carga su tabla
    mediante JavaScript y una petición HTTP simple puede devolver 0 filas aunque
    existan resultados publicados. El listado legacy `sesiones_celebradas`, en
    cambio, es server-rendered y contiene Fecha, Boletín, Aspectos y Acuerdos.
    """
    proyectos = data.setdefault("proyectos", [])
    pm = pmap_all(proyectos)
    ids: Dict[str, Dict[str, Any]] = {}
    for p in proyectos:
        for c in p.get("senado_comisiones") or []:
            if not isinstance(c, dict):
                continue
            cid = str(c.get("id") or "").strip()
            if not cid:
                continue
            d = ids.setdefault(cid, {"nombre": c.get("nombre") or "Comisión del Senado", "boletines": set()})
            d["boletines"].update(boletines_proyecto(p))
            if c.get("nombre") and d.get("nombre") == "Comisión del Senado":
                d["nombre"] = c.get("nombre")

    cambios = 0
    logs: List[str] = []
    for cid, meta in sorted(ids.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 10**9):
        url = SENADO_SESIONES_COMISION.format(idcomision=cid)
        try:
            raw = request_text(url)
            encontrados = 0
            if re.search(r"<html|<table|<tr", raw or "", re.I):
                soup = BeautifulSoup(raw, "html.parser")
                for tr in soup.find_all("tr"):
                    celdas = [re.sub(r"\s+", " ", c.get_text(" ", strip=True)).strip() for c in tr.find_all(["td", "th"])]
                    res = _resultado_senado_desde_celdas(celdas, tr, url, str(meta.get("nombre") or ""))
                    if not res:
                        continue
                    f, bs, texto, fuente = res
                    for b in bs:
                        if b not in pm or b not in meta["boletines"]:
                            continue
                        evento = {
                            "f": f,
                            "t": texto,
                            "organo": "Comisión Mixta" if "mixta" in norm_text(str(meta.get("nombre") or "")) else "Senado",
                            "fuente": fuente,
                            "tipo": "resultado",
                        }
                        for p in pm[b]:
                            if add_evento(p, evento, agenda=False):
                                cambios += 1
                        encontrados += 1
            else:
                # Fallback Markdown de Jina: filas tipo tabla separadas por '|'.
                for line in (raw or "").splitlines():
                    if "|" not in line or not re.search(r"\d{1,2}/\d{1,2}/20\d{2}", line):
                        continue
                    cells = [re.sub(r"\s+", " ", x).strip() for x in line.strip().strip("|").split("|")]
                    res = _resultado_senado_desde_celdas(cells, None, url, str(meta.get("nombre") or ""))
                    if not res:
                        continue
                    f, bs, texto, fuente = res
                    for b in bs:
                        if b not in pm or b not in meta["boletines"]:
                            continue
                        evento = {
                            "f": f,
                            "t": texto,
                            "organo": "Comisión Mixta" if "mixta" in norm_text(str(meta.get("nombre") or "")) else "Senado",
                            "fuente": fuente,
                            "tipo": "resultado",
                        }
                        for p in pm[b]:
                            if add_evento(p, evento, agenda=False):
                                cambios += 1
                        encontrados += 1
            logs.append(f"Senado resultados comisión {cid}: {encontrados} resultado(s) relevante(s) · {url}")
        except Exception as e:
            logs.append(f"ERROR Senado resultados comisión {cid}: {e}")
    return cambios, logs


def scan_senado_paginas(data: Dict[str, Any], meta_cache: Dict[str, Optional[Dict[str, Any]]]) -> Tuple[int, List[str]]:
    proyectos = data.setdefault("proyectos", [])
    candidatos = data.setdefault("candidatos", [])
    pm = pmap_all(proyectos)
    existentes = set(pm) | {norm_boletin(c.get("boletin", "")) for c in candidatos}
    logs: List[str] = []
    cambios = 0

    fuentes = [
        ("citaciones", SENADO_CITACIONES, True),
        ("citaciones", SENADO_CITACIONES_ALT, True),
        ("resultados", SENADO_RESULTADOS, False),
        ("tabla", SENADO_TABLA, True),
        ("ultimos", SENADO_ULTIMOS, False),
    ]
    for tipo, url, es_agenda in fuentes:
        try:
            raw = request_text(url)
            if tipo == "citaciones":
                cambios += descubrir_comisiones_senado(raw, proyectos, url)
            text = plain_text(raw)
            contexts = parse_senado_tabla(text) if tipo == "tabla" else contextos_por_boletin(raw, url)
            logs.append(f"Senado {tipo}: {len(contexts)} menciones · {url}")
            for b, ctx, f in contexts:
                if b in pm:
                    if not f:
                        # Las páginas de citaciones sin fecha parseable no deben crear
                        # eventos falsamente fechados. La ficha individual cubrirá hechos.
                        continue
                    evento = {
                        "f": f,
                        "t": ctx,
                        "organo": "Senado",
                        "fuente": url,
                        "tipo": "agenda" if es_agenda else "resultado",
                    }
                    for p in pm[b]:
                        if add_evento(p, evento, agenda=es_agenda):
                            cambios += 1
                elif b not in existentes:
                    cand = crear_candidato_desde_boletin(b, ctx, url, meta_cache)
                    if cand and merge_candidate(candidatos, cand, existentes):
                        cambios += 1
        except Exception as e:
            logs.append(f"ERROR Senado {tipo} {url}: {e}")
    return cambios, logs


def depurar_citaciones_redundantes(data: Dict[str, Any]) -> int:
    """Quita la citación pasada si el mismo día ya existe un resultado sustantivo.

    Se conserva la citación como respaldo únicamente cuando Cámara no publicó/entregó
    un resultado procesable para esa fecha.
    """
    quitadas = 0
    for p in data.get("proyectos") or []:
        hist = list(p.get("hist") or [])
        sustantivos = {
            (h.get("f"), h.get("organo") or "")
            for h in hist
            if (h.get("tipo") or tipo_evento_desde_texto(h.get("t", ""))) in {"resultado", "informe", "tramitacion"}
        }
        nuevo = []
        for h in hist:
            tipo = h.get("tipo") or tipo_evento_desde_texto(h.get("t", ""))
            if tipo == "citacion" and (h.get("f"), h.get("organo") or "") in sustantivos:
                quitadas += 1
                continue
            nuevo.append(h)
        p["hist"] = dedup_eventos(nuevo)
        sanear_proyecto(p)
    return quitadas


# ---------------------------------------------------------------------------
# CALIDAD FINAL
# ---------------------------------------------------------------------------
def depurar_candidatos(data: Dict[str, Any]) -> int:
    proyectos = data.get("proyectos") or []
    seguidos = set(pmap_all(proyectos))
    out: Dict[str, Dict[str, Any]] = {}
    eliminados = 0
    for c in data.get("candidatos") or []:
        b = norm_boletin(c.get("boletin", ""))
        f = c.get("fecha_ingreso") or c.get("fecha")
        score = int(c.get("score_abif") or score_abif((c.get("titulo") or "") + " " + (c.get("desc") or ""))[0])
        if b in seguidos or not candidato_valido(f, score):
            eliminados += 1
            continue
        cc = copy.deepcopy(c)
        cc["boletin"] = fmt_boletin(b)
        cc["fecha"] = f
        cc["fecha_ingreso"] = f
        cc["score_abif"] = score
        if b not in out or score > int(out[b].get("score_abif") or 0):
            out[b] = cc
    data["candidatos"] = sorted(out.values(), key=lambda c: ((c.get("fecha") or ""), int(c.get("score_abif") or 0)), reverse=True)
    return eliminados


def limpiar_historial_con_boletin_correcto(data: Dict[str, Any]) -> int:
    """Última barrera contra eventos automáticos que nombran solo otro boletín."""
    quitados = 0
    for p in data.get("proyectos") or []:
        propios = set(boletines_proyecto(p))
        for campo in ["hist", "agenda"]:
            ok = []
            for h in p.get(campo) or []:
                fuente = str(h.get("fuente") or "")
                texto = str(h.get("t") or "")
                bs = set(boletines_en_texto(texto))
                automatico = bool(fuente) or texto.startswith("[")
                if automatico and bs and not (bs & propios):
                    quitados += 1
                    continue
                ok.append(h)
            p[campo] = dedup_eventos(ok)
        sanear_proyecto(p)
    return quitados



# ---------------------------------------------------------------------------
# COBERTURA OBLIGATORIA Y FUENTES COMPLEMENTARIAS v8
# ---------------------------------------------------------------------------
def _estado_store(data: Dict[str, Any]) -> Dict[str, Any]:
    return data.setdefault("_estado_fuentes_v8", {})


def marcar_fuente(data: Dict[str, Any], clave: str, estado: str, detalle: str = "", *, paginas: int = 1, eventos: int = 0) -> None:
    spec = FUENTES_OBLIGATORIAS.get(clave, {})
    _estado_store(data)[clave] = {
        "camara": spec.get("camara", ""),
        "nombre": spec.get("nombre", clave),
        "url": spec.get("url", ""),
        "modo": spec.get("modo", ""),
        "estado": estado,
        "revisado": dt.datetime.now(TZ).replace(microsecond=0).isoformat(),
        "paginas_revisadas": int(paginas or 0),
        "eventos_relevantes": int(eventos or 0),
        "detalle": str(detalle or "")[:1000],
    }


def fecha_cercana(texto: str, pos: int, ventana: int = 360) -> Optional[str]:
    """Fecha más próxima ANTES del boletín, útil en listados de Sala/votaciones."""
    left = max(0, pos - ventana)
    trozo = texto[left:pos + 80]
    matches = []
    rx = re.compile(
        r"(?i)(\d{1,2})(?:\s+de)?\s+(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre)(?:\s+de)?\s+(20\d{2})"
    )
    for m in rx.finditer(trozo):
        try:
            matches.append(dt.date(int(m.group(3)), MESES[norm_text(m.group(2))], int(m.group(1))).isoformat())
        except Exception:
            pass
    for m in re.finditer(r"\b(\d{1,2})[/-](\d{1,2})[/-](20\d{2})\b", trozo):
        try:
            matches.append(dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat())
        except Exception:
            pass
    return matches[-1] if matches else fecha_para_pos(texto, pos, None)


def contextos_fecha_amplia(raw: str, max_ctx: int = 1300) -> List[Tuple[str, str, Optional[str]]]:
    """Extrae boletín/contexto/fecha desde páginas heterogéneas de Sala."""
    text = plain_text(raw)
    occ = list(re.finditer(r"(?i)(?:bolet[ií]n(?:es)?\s*(?:n[°º]?\s*)?|Bol\.\s*N[°º]?\s*)?(\d{1,2}\.?\d{3}-\d{2})", text))
    out: List[Tuple[str, str, Optional[str]]] = []
    for i, m in enumerate(occ):
        b = norm_boletin(m.group(1))
        prev_end = occ[i-1].end() if i else 0
        next_start = occ[i+1].start() if i+1 < len(occ) else len(text)
        left = max(prev_end, m.start()-320)
        # Si entre el boletín anterior y este aparece una nueva fecha de listado,
        # esa fecha marca el inicio natural del bloque y evita arrastrar el resultado
        # del proyecto precedente.
        pre = text[prev_end:m.start()]
        fechas_pos = list(re.finditer(
            r"(?i)(?:lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo)?[,]?\s*(\d{1,2})(?:\s+de)?\s+(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre)(?:\s+de)?\s+(20\d{2})",
            pre
        ))
        if fechas_pos:
            left = max(left, prev_end + fechas_pos[-1].start())
        right = min(next_start, m.end()+max_ctx)
        ctx = re.sub(r"\s+", " ", text[left:right]).strip()
        ctx = contexto_especifico_bloque(ctx, b)
        out.append((b, ctx, fecha_cercana(text, m.start())))
    # dedup
    seen, res = set(), []
    for x in out:
        k=(x[0], x[2], fingerprint_texto(x[1])[:240])
        if k not in seen:
            seen.add(k); res.append(x)
    return res


def _procesar_contextos_seguidos(
    data: Dict[str, Any], contexts: Sequence[Tuple[str, str, Optional[str]]], *,
    organo: str, fuente: str, tipo: str, agenda: bool = False,
    desde: Optional[dt.date] = None,
) -> int:
    pm = pmap_all(data.setdefault("proyectos", []))
    cambios = 0
    hoy = HOY.isoformat()
    for b, ctx, f in contexts:
        if b not in pm or not f or not fecha_iso_valida(f):
            continue
        fd = dt.date.fromisoformat(f)
        if desde and fd < desde:
            continue
        a_agenda = agenda and f >= hoy
        if agenda and not a_agenda:
            # Una cita pasada NO se convierte por sí sola en resultado.
            continue
        ev = {
            "f": f,
            "t": limpiar_evento_texto(ctx),
            "organo": "Comisión Mixta" if "mixta" in norm_text(ctx) else organo,
            "fuente": fuente,
            "tipo": "agenda" if a_agenda else tipo,
        }
        for p in pm[b]:
            if add_evento(p, ev, agenda=a_agenda):
                cambios += 1
    return cambios


def renderizar_con_playwright(url: str, espera_ms: int = 3200) -> Tuple[str, str, List[Dict[str, str]]]:
    from playwright.sync_api import sync_playwright  # type: ignore
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width":1440,"height":1100}, user_agent=HEADERS["User-Agent"], locale="es-CL")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            page.wait_for_timeout(espera_ms)
        html = page.content()
        txt = page.locator("body").inner_text(timeout=10000)
        links = page.locator("a").evaluate_all("els => els.map(a => ({href:a.href||'', text:(a.innerText||'').trim()}))")
        browser.close()
    return html, txt, links


def scan_senado_dinamico_generico(
    data: Dict[str, Any], clave: str, url: str, *, tipo: str, agenda: bool,
    organo: str, seguir: str = "", max_detalles: int = 35,
) -> Tuple[int, List[str]]:
    """Revisión con Chromium para Citaciones/Votaciones/Sesiones modernas del Senado."""
    logs: List[str] = []
    cambios = 0
    paginas = 1
    relevantes = 0
    try:
        html, txt, links = renderizar_con_playwright(url)
        base_contexts = contextos_fecha_amplia(html)
        n = _procesar_contextos_seguidos(data, base_contexts, organo=organo, fuente=url, tipo=tipo, agenda=agenda, desde=HOY-dt.timedelta(days=35))
        cambios += n; relevantes += n

        # En Citaciones y Sesiones se entra a los enlaces de detalle/comisión. De esta
        # forma el landing no es considerado suficiente si la materia vive dentro.
        if seguir:
            vistos = set()
            candidatos = []
            for a in links:
                href = str(a.get("href") or "")
                if not href or href in vistos or href.rstrip('/') == url.rstrip('/'):
                    continue
                if seguir not in href:
                    continue
                # evita navegación global; solo páginas internas de actividad legislativa
                if "senado.cl/actividad-legislativa/" not in href:
                    continue
                vistos.add(href); candidatos.append(href)
            candidatos = candidatos[:max_detalles]
            if candidatos:
                from playwright.sync_api import sync_playwright  # type: ignore
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    p = browser.new_page(viewport={"width":1360,"height":1000}, user_agent=HEADERS["User-Agent"], locale="es-CL")
                    for href in candidatos:
                        try:
                            p.goto(href, wait_until="domcontentloaded", timeout=45000)
                            try: p.wait_for_load_state("networkidle", timeout=8000)
                            except Exception: p.wait_for_timeout(900)
                            body = p.locator("body").inner_text(timeout=10000)
                            paginas += 1
                            ctxs = contextos_fecha_amplia(body)
                            nn = _procesar_contextos_seguidos(data, ctxs, organo=organo, fuente=href, tipo=tipo, agenda=agenda, desde=HOY-dt.timedelta(days=35))
                            cambios += nn; relevantes += nn
                        except Exception as e:
                            logs.append(f"WARN detalle {clave} {href}: {e}")
                    browser.close()
        marcar_fuente(data, clave, "ok", f"Página dinámica renderizada; {paginas} página(s) revisadas", paginas=paginas, eventos=relevantes)
        logs.append(f"{clave}: OK · {paginas} página(s) · {relevantes} evento(s)")
    except Exception as e:
        marcar_fuente(data, clave, "error", str(e), paginas=0, eventos=0)
        logs.append(f"ERROR {clave} {url}: {e}")
    return cambios, logs


def scan_camara_votaciones_v8(data: Dict[str, Any]) -> Tuple[int, List[str]]:
    logs=[]; cambios=0
    try:
        raw=request_text(CAMARA_VOTACIONES)
        contexts=contextos_fecha_amplia(raw)
        # Para cada bloque, intenta usar el enlace de detalle de votación como fuente.
        soup=BeautifulSoup(raw, "html.parser")
        pm=pmap_all(data.get("proyectos") or [])
        enlaces_por_b={}
        for a in soup.find_all("a", href=True):
            parent=a
            for _ in range(5):
                if parent is None: break
                txt=re.sub(r"\s+", " ", parent.get_text(" ", strip=True))
                bs=[b for b in boletines_en_texto(txt) if b in pm]
                if bs and "votacion_detalle" in str(a.get("href") or ""):
                    for b in bs: enlaces_por_b.setdefault(b, urljoin(CAMARA_VOTACIONES, a["href"]))
                    break
                parent=parent.parent
        hoy_menos=HOY-dt.timedelta(days=35)
        for b,ctx,f in contexts:
            if b not in pm or not f or dt.date.fromisoformat(f)<hoy_menos: continue
            fuente=enlaces_por_b.get(b,CAMARA_VOTACIONES)
            ev={"f":f,"t":limpiar_evento_texto(ctx),"organo":"Sala de la Cámara de Diputados","fuente":fuente,"tipo":"votacion"}
            for p in pm[b]:
                if add_evento(p,ev,agenda=False): cambios+=1
        marcar_fuente(data,"camara_votaciones_sala","ok",f"{len(contexts)} menciones analizadas",eventos=cambios)
        logs.append(f"Cámara votaciones: {len(contexts)} menciones · {cambios} cambios")
    except Exception as e:
        marcar_fuente(data,"camara_votaciones_sala","error",str(e),paginas=0)
        logs.append(f"ERROR Cámara votaciones: {e}")
    return cambios,logs


def scan_camara_sesiones_v8(data: Dict[str, Any]) -> Tuple[int, List[str]]:
    """Revisa la portada de Sesiones de Sala y cualquier enlace de detalle disponible."""
    logs=[]; cambios=0; paginas=1
    try:
        raw=request_text(CAMARA_SESIONES_SALA)
        contexts=contextos_fecha_amplia(raw)
        cambios += _procesar_contextos_seguidos(data,contexts,organo="Sala de la Cámara de Diputados",fuente=CAMARA_SESIONES_SALA,tipo="sesion",agenda=False,desde=HOY-dt.timedelta(days=35))
        soup=BeautifulSoup(raw,"html.parser")
        links=[];seen=set()
        for a in soup.find_all("a",href=True):
            h=urljoin(CAMARA_SESIONES_SALA,a["href"])
            if h in seen: continue
            if "/legislacion/sesiones_sala/" in h and h.rstrip('/')!=CAMARA_SESIONES_SALA.rstrip('/') and any(x in h.lower() for x in ["sintesis","tabla","sesion"]):
                seen.add(h);links.append(h)
        # Limita detalles para no sobrecargar Cámara.
        for h in links[:12]:
            try:
                rr=request_text(h); paginas+=1
                ctx=contextos_fecha_amplia(rr)
                cambios += _procesar_contextos_seguidos(data,ctx,organo="Sala de la Cámara de Diputados",fuente=h,tipo="sesion",agenda=False,desde=HOY-dt.timedelta(days=35))
            except Exception as e:
                logs.append(f"WARN detalle sesión Cámara {h}: {e}")
        marcar_fuente(data,"camara_sesiones_sala","ok",f"Portada + {paginas-1} detalle(s)",paginas=paginas,eventos=cambios)
    except Exception as e:
        marcar_fuente(data,"camara_sesiones_sala","error",str(e),paginas=0)
        logs.append(f"ERROR Cámara sesiones: {e}")
    return cambios,logs


def revisar_directorio_comisiones_camara(data: Dict[str, Any]) -> Tuple[int, List[str]]:
    logs=[]; paginas=1
    try:
        raw=request_text(CAMARA_COMISIONES_PERMANENTES)
        soup=BeautifulSoup(raw,"html.parser")
        links=[]; seen=set()
        for a in soup.find_all("a",href=True):
            h=urljoin(CAMARA_COMISIONES_PERMANENTES,a["href"])
            if h in seen: continue
            if "camara.cl/legislacion/comisiones/" in h and any(x in h.lower() for x in ["comision","sesiones","citaciones","resultados"]):
                seen.add(h);links.append(h)
        # Revisar efectivamente las páginas de comisión accesibles, pero sin crear
        # movimientos por mero texto contextual: los hechos salen de Citaciones/Resultados.
        ok_det=0
        for h in links[:45]:
            try:
                rr=request_text(h,timeout=18)
                if len(rr)>300: ok_det+=1
                paginas+=1
            except Exception:
                pass
        marcar_fuente(data,"camara_comisiones_permanentes","ok",f"Directorio revisado; {ok_det} página(s) de comisión accesibles",paginas=paginas)
        logs.append(f"Cámara comisiones permanentes: {ok_det} detalles accesibles")
    except Exception as e:
        marcar_fuente(data,"camara_comisiones_permanentes","error",str(e),paginas=0)
        logs.append(f"ERROR directorio comisiones Cámara: {e}")
    return 0,logs


def auditar_fuentes_estaticas_v8(data: Dict[str, Any]) -> List[str]:
    """Garantiza que las 13 URLs queden registradas aunque otro scanner no halle eventos."""
    logs=[]
    # Solo fuentes cuya lectura por HTTP es representativa. Las dinámicas del Senado
    # se marcan desde sus scanners Playwright.
    checks=[
        ("camara_proyectos_ley",CAMARA_PROYECTOS),
        ("camara_citaciones_comisiones",CAMARA_CITACIONES_TODAS),
        ("camara_resultados_comisiones",CAMARA_RESULTADOS_TODOS),
        ("camara_tabla_semanal",CAMARA_TABLA),
        ("senado_tabla_semanal",SENADO_TABLA),
    ]
    for clave,url in checks:
        try:
            raw=request_text(url,timeout=25)
            estado="ok" if len(raw)>250 else "parcial"
            marcar_fuente(data,clave,estado,f"Respuesta oficial leída ({len(raw)} bytes)")
        except Exception as e:
            marcar_fuente(data,clave,"error",str(e),paginas=0)
            logs.append(f"ERROR auditoría {clave}: {e}")
    return logs


def cerrar_estado_fuentes_v8(data: Dict[str, Any]) -> None:
    store=_estado_store(data)
    # Fichas Senado: si scan_senado_fichas terminó, main la marca explícitamente.
    for clave,spec in FUENTES_OBLIGATORIAS.items():
        if clave not in store:
            store[clave]={
                "camara":spec["camara"],"nombre":spec["nombre"],"url":spec["url"],"modo":spec["modo"],
                "estado":"no_revisada","revisado":dt.datetime.now(TZ).replace(microsecond=0).isoformat(),
                "paginas_revisadas":0,"eventos_relevantes":0,"detalle":"La ejecución no alcanzó esta fuente."
            }
    vals=list(store.values())
    ok=sum(1 for x in vals if x.get("estado")=="ok")
    parcial=sum(1 for x in vals if x.get("estado")=="parcial")
    error=sum(1 for x in vals if x.get("estado") in {"error","no_revisada"})
    data["estado_fuentes"]={
        "generado":dt.datetime.now(TZ).replace(microsecond=0).isoformat(),
        "total_obligatorias":len(FUENTES_OBLIGATORIAS),
        "ok":ok,"parcial":parcial,"error_o_no_revisada":error,
        "fuentes":store,
        "nota":"Sin actividad no equivale a fuente no revisada. Si una fuente falla, el monitor conserva el dato anterior y registra el error.",
    }
    data.pop("_estado_fuentes_v8",None)

def main(argv: Optional[Sequence[str]] = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="proyectos.json")
    ap.add_argument("--output", default="proyectos.json")
    ap.add_argument("--skip-senado-fichas", action="store_true", help="Omite consulta individual a fichas del Senado")
    args = ap.parse_args(argv)

    inp, out = Path(args.input), Path(args.output)
    data = get_json(inp)
    data.setdefault("proyectos", [])
    data.setdefault("candidatos", [])

    obligatorios_agregados = asegurar_proyectos_seguimiento_obligatorio(data)

    # Consolidación ANTES de scrapear: evita actualizar dos tarjetas del mismo boletín.
    data["proyectos"], merges_iniciales = consolidar_proyectos(data["proyectos"])
    meta_cache: Dict[str, Optional[Dict[str, Any]]] = {}
    logs: List[str] = [f"Seguimiento obligatorio: {obligatorios_agregados} proyecto(s) agregado(s)", f"Consolidación inicial: {merges_iniciales} duplicado(s) fusionado(s)"]
    cambios = 0

    # Auditoría base: las URLs obligatorias quedan registradas aunque no tengan actividad.
    logs += auditar_fuentes_estaticas_v8(data)

    n, lg = scan_open_data_camara(data); cambios += n; logs += lg
    n, lg = revisar_directorio_comisiones_camara(data); cambios += n; logs += lg
    n, lg = scan_camara_semana(data, meta_cache); cambios += n; logs += lg
    n, lg = scan_camara_votaciones_v8(data); cambios += n; logs += lg
    n, lg = scan_camara_sesiones_v8(data); cambios += n; logs += lg

    n, lg = scan_senado_paginas(data, meta_cache); cambios += n; logs += lg
    # Citaciones modernas: entra al landing y a páginas de comisión/detalle.
    n, lg = scan_senado_dinamico_generico(data, "senado_citaciones_comisiones", SENADO_CITACIONES, tipo="citacion", agenda=True, organo="Senado", seguir="/actividad-legislativa/comisiones/"); cambios += n; logs += lg
    # Votaciones y sesiones de Sala modernas son dinámicas y se revisan con Chromium.
    n, lg = scan_senado_dinamico_generico(data, "senado_votaciones_sala", SENADO_VOTACIONES, tipo="votacion", agenda=False, organo="Sala del Senado", seguir="/actividad-legislativa/sala/"); cambios += n; logs += lg
    n, lg = scan_senado_dinamico_generico(data, "senado_sesiones_sala", SENADO_SESIONES_SALA, tipo="sesion", agenda=False, organo="Sala del Senado", seguir="/actividad-legislativa/sala-de-sesiones/"); cambios += n; logs += lg
    # FUENTE PRIMARIA DE RESULTADOS DE COMISIONES DEL SENADO: la página moderna
    # indicada por ABIF. Se renderiza con navegador porque su tabla carga por JS.
    # La ficha de sesión enlazada desde cada fila aporta Aspectos considerados y
    # Acuerdos, y esa ficha exacta queda como enlace del movimiento.
    n, lg = scan_senado_resultados_modernos(data); cambios += n; logs += lg
    # Respaldo adicional: listado server-rendered por comisión. No sustituye a la
    # página moderna; sirve si el front del Senado tiene una caída temporal.
    n, lg = scan_senado_resultados_por_comision(data); cambios += n; logs += lg
    if not args.skip_senado_fichas:
        n, lg = scan_senado_fichas(data); cambios += n; logs += lg
        errores_ficha = sum(1 for x in lg if str(x).startswith("ERROR"))
        marcar_fuente(data, "senado_fichas_tramitacion", "ok" if errores_ficha == 0 else "parcial", f"Fichas revisadas; errores registrados: {errores_ficha}", paginas=len({b for p in data.get("proyectos",[]) for b in boletines_proyecto(p)}), eventos=n)
    else:
        marcar_fuente(data, "senado_fichas_tramitacion", "no_revisada", "Omitida por --skip-senado-fichas", paginas=0)

    data["proyectos"], merges_finales = consolidar_proyectos(data["proyectos"])
    resultados_senado_vagos = depurar_resultados_senado_preferir_modernos(data)
    citaciones_redundantes = depurar_citaciones_redundantes(data)
    quitados_cruzados = limpiar_historial_con_boletin_correcto(data)
    candidatos_eliminados = depurar_candidatos(data)

    generado = dt.datetime.now(TZ).replace(microsecond=0).isoformat()
    data["generado"] = generado
    data["version"] = dt.datetime.now(TZ).strftime("%Y-%m-%d-%H%M-abif-v8.0")
    data["total"] = len(data["proyectos"])
    data["cambios_detectados"] = cambios
    data["calendario"] = {
        "timezone": "America/Santiago",
        "semanas_distritales": SEMANAS_DISTRITALES_2026,
    }
    data["politica_bandeja"] = {
        "fecha_minima_ingreso": CANDIDATOS_DESDE.isoformat(),
        "requiere_fecha_ingreso_confirmada": True,
        "puntaje_minimo_abif": 4,
        "nota": "El corte aplica solo a candidatos nuevos; los proyectos de seguimiento obligatorio pueden ser anteriores.",
    }
    cerrar_estado_fuentes_v8(data)
    data["fuentes_revision"] = {
        "obligatorias": [v["url"] for v in FUENTES_OBLIGATORIAS.values()],
        "camara_auxiliares": [CAMARA_OPEN_LEG],
        "senado_auxiliares": [SENADO_CITACIONES_ALT, SENADO_SESIONES_COMISION, SENADO_ULTIMOS],
        "duplicados_fusionados": merges_iniciales + merges_finales,
        "movimientos_cruzados_descartados": quitados_cruzados,
        "resultados_senado_vagos_reemplazados": resultados_senado_vagos,
        "citaciones_pasadas_redundantes_descartadas": citaciones_redundantes,
        "candidatos_antiguos_o_invalidos_descartados": candidatos_eliminados,
        "logs": logs[-120:],
    }

    save_json(out, data)
    print(json.dumps({
        "ok": True,
        "output": str(out),
        "proyectos": len(data["proyectos"]),
        "candidatos": len(data.get("candidatos") or []),
        "cambios_detectados": cambios,
        "duplicados_fusionados": merges_iniciales + merges_finales,
        "movimientos_cruzados_descartados": quitados_cruzados,
        "resultados_senado_vagos_reemplazados": resultados_senado_vagos,
        "citaciones_pasadas_redundantes_descartadas": citaciones_redundantes,
        "bandeja_desde": CANDIDATOS_DESDE.isoformat(),
        "seguimiento_obligatorio": [p["boletin"] for p in PROYECTOS_SEGUIMIENTO_OBLIGATORIO],
        "cobertura_fuentes": {k: data.get("estado_fuentes", {}).get(k) for k in ["total_obligatorias","ok","parcial","error_o_no_revisada"]},
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
