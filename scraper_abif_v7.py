#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Scraper ABIF v7.2 — seguimiento legislativo coordinado Cámara + Senado.

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
    python scraper_abif_v7.py --input proyectos.json --output proyectos.json

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
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# CONFIGURACIÓN
# ---------------------------------------------------------------------------
TZ = dt.timezone(dt.timedelta(hours=-4))
HOY = dt.datetime.now(TZ).date()
CANDIDATOS_DESDE = dt.date(2026, 6, 1)
CAMARA_HISTORIAL_DESDE = dt.date(2026, 6, 1)

CAMARA_BASE = "https://www.camara.cl/"
CAMARA_CITACIONES_TODAS = "https://www.camara.cl/legislacion/comisiones/citaciones_todas.aspx"
CAMARA_RESULTADOS_TODOS = "https://www.camara.cl/legislacion/comisiones/resultados_todos.aspx"
CAMARA_TABLA = "https://www.camara.cl/verDoc.aspx?prmId=0&prmTipo=TABLASEMANAL"
CAMARA_OPEN_LEG = "https://opendata.camara.cl/camaradiputados/WServices/WSLegislativo.asmx"

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
SENADO_ULTIMOS = "https://tramitacion.senado.cl/appsenado/index.php?ac=ultimos_vistos&etc=&mo=tramitacion"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ABIF-Monitor-Legislativo/7.2; +https://github.com/)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-CL,es;q=0.9,en;q=0.5",
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
    # El prefijo técnico aporta trazabilidad vía campos fuente/organo y no debe
    # repetirse en el informe al usuario.
    s = re.sub(r"^\[(?:C[aá]mara|Senado)[^\]]*\]\s*", "", s, flags=re.I)
    return s[:900].rstrip() + ("…" if len(s) > 900 else "")


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
                encontrados.append((b, txt[:1200], f))

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

    n, lg = scan_open_data_camara(data); cambios += n; logs += lg
    n, lg = scan_camara_semana(data, meta_cache); cambios += n; logs += lg
    n, lg = scan_senado_paginas(data, meta_cache); cambios += n; logs += lg
    if not args.skip_senado_fichas:
        n, lg = scan_senado_fichas(data); cambios += n; logs += lg

    data["proyectos"], merges_finales = consolidar_proyectos(data["proyectos"])
    citaciones_redundantes = depurar_citaciones_redundantes(data)
    quitados_cruzados = limpiar_historial_con_boletin_correcto(data)
    candidatos_eliminados = depurar_candidatos(data)

    generado = dt.datetime.now(TZ).replace(microsecond=0).isoformat()
    data["generado"] = generado
    data["version"] = dt.datetime.now(TZ).strftime("%Y-%m-%d-%H%M-abif-v7.2")
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
    data["fuentes_revision"] = {
        "camara": [CAMARA_CITACIONES_TODAS, CAMARA_RESULTADOS_TODOS, CAMARA_TABLA, CAMARA_OPEN_LEG],
        "senado": [SENADO_CITACIONES, SENADO_CITACIONES_ALT, SENADO_RESULTADOS, SENADO_TABLA, SENADO_FICHA],
        "duplicados_fusionados": merges_iniciales + merges_finales,
        "movimientos_cruzados_descartados": quitados_cruzados,
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
        "citaciones_pasadas_redundantes_descartadas": citaciones_redundantes,
        "bandeja_desde": CANDIDATOS_DESDE.isoformat(),
        "seguimiento_obligatorio": [p["boletin"] for p in PROYECTOS_SEGUIMIENTO_OBLIGATORIO],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
