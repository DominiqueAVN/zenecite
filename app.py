# -*- coding: utf-8 -*-
from flask import Flask, request, render_template_string, session, jsonify, Response, abort
import requests
from difflib import SequenceMatcher
import re
import time
import ipaddress
import socket
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup
import random
import functools
import os
import secrets
import json
from flask_caching import Cache
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

CONFIG = {
    "email_contacto": os.environ.get("CONTACT_EMAIL", "tu_correo@unsa.edu.pe"),
    "modo_debug": os.environ.get("FLASK_DEBUG", "False").lower() == "true",
    "puerto": int(os.environ.get("PORT", 5000)),
    "anthropic_api_key": os.environ.get("ANTHROPIC_API_KEY", ""),
}
_CACHE_DIR = os.environ.get("CACHE_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache"))
_REDIS_URL = os.environ.get("REDIS_URL", "")

if _REDIS_URL:
    cache = Cache(config={
        "CACHE_TYPE": "RedisCache",
        "CACHE_REDIS_URL": _REDIS_URL,
        "CACHE_DEFAULT_TIMEOUT": 600,
    })
else:
    # Fallback local para desarrollo sin Redis
    cache = Cache(config={
        "CACHE_TYPE": "FileSystemCache",
        "CACHE_DIR": _CACHE_DIR,
        "CACHE_DEFAULT_TIMEOUT": 600,
        "CACHE_THRESHOLD": 5000,
    })

def obtener_query_ingles(tema, idioma_usuario):
    if idioma_usuario == "en":
        return tema
    try:
        url = f"https://api.mymemory.translated.net/get?q={requests.utils.quote(tema)}&langpair={idioma_usuario}|en"
        r = requests.get(url, timeout=3)
        data = r.json()
        en_text = data.get("responseData", {}).get("translatedText")
        if en_text and en_text.lower() != tema.lower():
            return en_text
    except Exception:
        pass
    return tema

def traducir_titulo(titulo, idioma_destino):
    origen = None
    if re.search(r'[\u3040-\u30ff]+', titulo): origen = "ja"
    elif re.search(r'[\u4e00-\u9fff]+', titulo): origen = "zh"
    elif re.search(r'[\u0400-\u04FF]+', titulo): origen = "ru"
    if not origen and idioma_destino != "en":
        origen = "en"
    if not origen or origen == idioma_destino:
        return None
    try:
        url = f"https://api.mymemory.translated.net/get?q={requests.utils.quote(titulo)}&langpair={origen}|{idioma_destino}"
        r = requests.get(url, timeout=3)
        data = r.json()
        return data.get("responseData", {}).get("translatedText")
    except Exception:
        return None

def generar_cita(paper, formato="apa"):
    autores = paper.get("autores", "Anon.")
    anio = paper.get("año", "s.f.")
    titulo = paper.get("titulo", "Sin título")
    doi = paper.get("doi", "")
    enlace = paper.get("enlace", "")
    if formato == "apa":
        if doi and doi != "sin DOI": return f"{autores} ({anio}). {titulo}. https://doi.org/{doi}"
        else: return f"{autores} ({anio}). {titulo}. {enlace}"
    elif formato == "vancouver":
        if doi and doi != "sin DOI": return f"{autores}. {titulo}. {anio}. doi:{doi}"
        else: return f"{autores}. {titulo}. {anio}. Disponible en: {enlace}"
    elif formato == "ieee":
        if doi and doi != "sin DOI": return f'{autores}, "{titulo}," {anio}. doi: {doi}.'
        else: return f'{autores}, "{titulo}," {anio}. [Online]. Available: {enlace}'
    elif formato == "mla":
        if doi and doi != "sin DOI": return f'{autores}. "{titulo}." ({anio}). doi:{doi}.'
        else: return f'{autores}. "{titulo}." {anio}, {enlace}.'
    return ""

app = Flask(__name__, static_folder="static")
cache.init_app(app)

limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=["200 per hour"],
    storage_uri=_REDIS_URL or "memory://",
)

app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("FORCE_HTTPS", "True").lower() == "true",
)
from flask import g

@app.before_request
def generar_nonce():
    g.csp_nonce = secrets.token_urlsafe(16)
@app.after_request
def agregar_cabeceras_seguridad(resp):
    nonce = getattr(g, "csp_nonce", "")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    resp.headers["Content-Security-Policy"] = (
        f"default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}' https://cdn.jsdelivr.net; "
        f"style-src 'self' 'unsafe-inline'; "  # el CSS embebido queda igual por ahora
        f"img-src 'self' data: https:; "
        f"frame-src 'self' https://docs.google.com; "
        f"connect-src 'self' https: https://*.supabase.co; "
        f"object-src 'none'; "
        f"base-uri 'self';"
    )
    return resp

def obtener_pais_por_ip():
    try:
        ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        if not ip or ip == '127.0.0.1':
            return 'es'
        r = requests.get(f"http://ipapi.co/{ip}/json/", timeout=1)
        data = r.json()
        country_code = data.get("country_code", "PE")
        mapa = {
            "ES": "es", "MX": "es", "AR": "es", "CO": "es", "PE": "es",
            "CL": "es", "VE": "es", "EC": "es", "BO": "es", "PY": "es",
            "UY": "es", "CR": "es", "PA": "es", "DO": "es", "GT": "es",
            "HN": "es", "SV": "es", "NI": "es", "CU": "es", "PR": "es",
            "US": "en", "GB": "en", "CA": "en", "AU": "en", "NZ": "en", "IE": "en",
            "CN": "zh", "TW": "zh", "HK": "zh",
        }
        return mapa.get(country_code, "es")
    except Exception:
        return "es"

TEXTOS = {
    "es": {
        "titulo_pagina": "ZENECITE",
        "subtitulo": "Verifica, aprende y cita bien, sin importar de la universidad o país que provengas",
        "nav_verificar": "Verificar", "nav_scan": "Escaner Bibliografia", "nav_buscar": "Buscar fuentes",
        "nav_extracto": "Mi extracto", "nav_guia": "Guia APA", "nav_constructor": "Constructor", "nav_biblio": "Mi Bibliografía",
        "modo_oscuro": "Modo oscuro", "modo_claro": "Modo claro",
        "seccion1_titulo": "Verifica citas que ya tienes", "placeholder_cita": "Pega aqui tus citas, una por linea...",
        "boton_verificar": "Verificar citas", "scan_titulo": "Escaner de Bibliografia Completa",
        "scan_desc": "Pega toda tu lista de referencias (ej. 20 citas) para auditarla en lote.", "boton_scan": "Escanear Bibliografia",
        "seccion2_titulo": "Busca fuentes reales sobre tu tema", "placeholder_tema": "Ej: indice de peroxidos en aceites",
        "boton_buscar_todo": "Buscar en todas las fuentes", "resultados_para": "Resultados para",
        "papel_pagina_web": "Ver ficha", "papel_pdf": "Descargar PDF", "papel_previsualizar": "Previsualizar",
        "papel_sin_acceso": "Sin acceso abierto", "papel_en_espanol": "En español",
        "seccion3_titulo": "Pega un extracto de tu trabajo y te sugerimos fuentes", "placeholder_extracto": "Pega aqui un parrafo de tu introduccion, marco teorico, etc...",
        "boton_extracto": "Sugerir fuentes", "guia_titulo": "Guia rapida: como citar en APA 7",
        "guia_texto_intext": "Citar dentro del texto (narrativa y parentetica)", "guia_texto_revista": "Lista de referencias: articulo de revista",
        "guia_texto_libro": "Lista de referencias: libro", "guia_texto_tesis": "Lista de referencias: tesis",
        "guia_texto_web": "Lista de referencias: pagina web", "guia_texto_ppt": "Citar en diapositivas o presentaciones (PPT)",
        "guia_texto_vancouver": "Formato Vancouver (ciencias de la salud)",
        "constructor_titulo": "Constructor de Citas", "constructor_desc": "Arma la cita en formato APA haciendo clic en las piezas, en el orden correcto. Es como programar: el orden importa.",
        "constructor_pool": "Piezas disponibles (clic para colocar):", "constructor_zona": "Tu cita (clic para quitar una pieza):",
        "constructor_verificar": "Verificar orden", "constructor_siguiente": "Siguiente cita", "constructor_perfecto": "Perfecto! Cita 100% correcta.",
        "texto_puntaje": "Puntaje", "detecta_pais": "Idioma:", "usar_correccion": "Usar esta version",
        "cita_ver_desplegable": "Ver citas (APA y Vancouver)", "cita_apa_label": "APA 7ma:", "cita_vancouver_label": "Vancouver:",
        "cita_ieee_label": "IEEE:", "cita_mla_label": "MLA:", "copiar_boton": "Copiar",
        "biblio_titulo": "Tu Bibliografía Guardada", "biblio_desc": "Aquí están las referencias que has guardado. Copia todas en el formato que necesites para tu trabajo.",
        "guardar_boton": "Guardar", "copiar_todo": "Copiar Todo", "biblio_vacia": "Aún no has guardado ninguna referencia. Ve a 'Buscar fuentes', haz tu búsqueda y haz clic en 'Guardar'.",
        "filtro_todos": "Todos", "filtro_pdf": "Con PDF", "filtro_espanol": "En español", "filtro_reciente": "Últimos 5 años", "filtro_repo": "Repositorios",
        "buscar_tambien": "Buscar también en", "loader_buscando": "Buscando fuentes académicas...", "loader_paso": "Paso",
        "seccion_pdf": "Papers con acceso abierto", "seccion_academico": "Fuentes académicas indexadas", "seccion_repos": "Repositorios universitarios",
        "preview_error": "No se pudo cargar la vista previa de este PDF. Se abrirá en una pestaña nueva.",
        "preview_cargando": "Cargando documento...",
    },
    "en": {
        "titulo_pagina": "ZENECITE", "subtitulo": "Verify, learn and cite well, no matter your university or country",
        "nav_verificar": "Verify", "nav_scan": "Bibliography Scanner", "nav_buscar": "Find sources",
        "nav_extracto": "My excerpt", "nav_guia": "APA Guide", "nav_constructor": "Builder", "nav_biblio": "My Bibliography",
        "modo_oscuro": "Dark mode", "modo_claro": "Light mode",
        "seccion1_titulo": "Verify citations you already have", "placeholder_cita": "Paste your citations here, one per line...",
        "boton_verificar": "Verify citations", "scan_titulo": "Full Bibliography Scanner",
        "scan_desc": "Paste your entire reference list to batch audit it.", "boton_scan": "Scan Bibliography",
        "seccion2_titulo": "Search real sources on your topic", "placeholder_tema": "E.g: peroxide value in oils",
        "boton_buscar_todo": "Search all sources", "resultados_para": "Results for",
        "papel_pagina_web": "View record", "papel_pdf": "Download PDF", "papel_previsualizar": "Preview",
        "papel_sin_acceso": "No open access", "papel_en_espanol": "In Spanish",
        "seccion3_titulo": "Paste an excerpt of your work and we'll suggest sources", "placeholder_extracto": "Paste a paragraph from your introduction, theoretical framework, etc...",
        "boton_extracto": "Suggest sources", "guia_titulo": "Quick guide: how to cite in APA 7",
        "guia_texto_intext": "In-text citation (narrative and parenthetical)", "guia_texto_revista": "Reference list: journal article",
        "guia_texto_libro": "Reference list: book", "guia_texto_tesis": "Reference list: thesis",
        "guia_texto_web": "Reference list: web page", "guia_texto_ppt": "Citing in slides or presentations (PPT)",
        "guia_texto_vancouver": "Vancouver style (health sciences)",
        "constructor_titulo": "Citation Builder", "constructor_desc": "Build the APA citation by clicking the pieces in the correct order. It's like coding: order matters.",
        "constructor_pool": "Available pieces (click to place):", "constructor_zona": "Your citation (click to remove a piece):",
        "constructor_verificar": "Check order", "constructor_siguiente": "Next citation", "constructor_perfecto": "Perfect! 100% correct citation.",
        "texto_puntaje": "Score", "detecta_pais": "Language:", "usar_correccion": "Use this version",
        "cita_ver_desplegable": "View citations (APA & Vancouver)", "cita_apa_label": "APA 7th:", "cita_vancouver_label": "Vancouver:",
        "cita_ieee_label": "IEEE:", "cita_mla_label": "MLA:", "copiar_boton": "Copy",
        "biblio_titulo": "Your Saved Bibliography", "biblio_desc": "Here are the references you have saved. Copy all in the format you need for your work.",
        "guardar_boton": "Save", "copiar_todo": "Copy All", "biblio_vacia": "You haven't saved any references yet. Go to 'Find sources', search and click 'Save'.",
        "filtro_todos": "All", "filtro_pdf": "With PDF", "filtro_espanol": "In Spanish", "filtro_reciente": "Last 5 years", "filtro_repo": "Repositories",
        "buscar_tambien": "Also search in", "loader_buscando": "Searching academic sources...", "loader_paso": "Step",
        "seccion_pdf": "Open access papers", "seccion_academico": "Indexed academic sources", "seccion_repos": "University repositories",
        "preview_error": "Couldn't load this PDF's preview. It will open in a new tab instead.",
        "preview_cargando": "Loading document...",
    },
}

BANDERAS_PAIS = {
    "PE": "🇵🇪 Perú", "US": "🇺🇸 Estados Unidos", "BR": "🇧🇷 Brasil", "RU": "🇷🇺 Rusia",
    "MX": "🇲🇽 México", "AR": "🇦🇷 Argentina", "CO": "🇨🇴 Colombia", "CL": "🇨🇱 Chile",
    "ES": "🇪🇸 España", "CN": "🇨🇳 China", "GB": "🇬🇧 Reino Unido", "DE": "🇩🇪 Alemania",
    "VE": "🇻🇪 Venezuela", "EC": "🇪🇨 Ecuador", "BO": "🇧🇴 Bolivia", "PY": "🇵🇾 Paraguay",
    "UY": "🇺🇾 Uruguay", "CR": "🇨🇷 Costa Rica", "PA": "🇵🇦 Panamá", "DO": "🇩🇴 Rep. Dominicana",
    "GT": "🇬🇹 Guatemala", "HN": "🇭🇳 Honduras", "SV": "🇸🇻 El Salvador", "NI": "🇳🇮 Nicaragua",
    "CU": "🇨🇺 Cuba", "PR": "🇵🇷 Puerto Rico",
    "FR": "🇫🇷 Francia", "IT": "🇮🇹 Italia", "PT": "🇵🇹 Portugal", "NL": "🇳🇱 Países Bajos",
    "BE": "🇧🇪 Bélgica", "CH": "🇨🇭 Suiza", "AT": "🇦🇹 Austria", "SE": "🇸🇪 Suecia",
    "NO": "🇳🇴 Noruega", "DK": "🇩🇰 Dinamarca", "FI": "🇫🇮 Finlandia", "PL": "🇵🇱 Polonia",
    "GR": "🇬🇷 Grecia", "IE": "🇮🇪 Irlanda", "CZ": "🇨🇿 Chequia", "HU": "🇭🇺 Hungría",
    "RO": "🇷🇴 Rumanía", "UA": "🇺🇦 Ucrania", "TR": "🇹🇷 Turquía",
    "JP": "🇯🇵 Japón", "KR": "🇰🇷 Corea del Sur", "IN": "🇮🇳 India", "ID": "🇮🇩 Indonesia",
    "TH": "🇹🇭 Tailandia", "VN": "🇻🇳 Vietnam", "PH": "🇵🇭 Filipinas", "MY": "🇲🇾 Malasia",
    "SG": "🇸🇬 Singapur", "TW": "🇹🇼 Taiwán", "HK": "🇭🇰 Hong Kong", "IL": "🇮🇱 Israel",
    "SA": "🇸🇦 Arabia Saudita", "AE": "🇦🇪 Emiratos Árabes Unidos",
    "ZA": "🇿🇦 Sudáfrica", "NG": "🇳🇬 Nigeria", "EG": "🇪🇬 Egipto", "KE": "🇰🇪 Kenia",
    "AU": "🇦🇺 Australia", "NZ": "🇳🇿 Nueva Zelanda", "CA": "🇨🇦 Canadá",
}

def bandera_de(codigo_pais):
    if not codigo_pais:
        return None
    return BANDERAS_PAIS.get(codigo_pais.upper())

@app.before_request
def detectar_idioma():
    if session.get("idioma"):
        return
    lang_from_qs = request.args.get("idioma")
    if lang_from_qs in TEXTOS:
        session["idioma"] = lang_from_qs
        return
    session["idioma"] = obtener_pais_por_ip()

def intentar_corregir_cita(cita_raw):
    s = re.sub(r'\s+', ' ', cita_raw.strip())
    s = re.sub(r'\.{2,}', '.', s)
    def reemplazo_anio(m):
        dentro = m.group(1)
        if re.fullmatch(r'\d{2}', dentro):
            anio = int(dentro)
            anio_corregido = 2000 + anio if anio < 50 else 1900 + anio
            return f'({anio_corregido})'
        return m.group(0)
    s = re.sub(r'\((\d{2})\)', reemplazo_anio, s)
    s = re.sub(r'(\d{4})\s*\.', r'\1. ', s)
    s = re.sub(r'(\d{4})\s+', r'\1. ', s)
    s = s.rstrip('. ')
    if not s.endswith('.'):
        s += '.'
    return s

def extraer_palabras_clave(texto, max_palabras=8):
    stop_words = {"el", "la", "los", "las", "un", "una", "unos", "unas", "y", "o", "pero", "por", "para", "con", "sin", "sobre", "bajo", "entre", "hacia", "desde", "hasta", "mientras", "cuando", "que", "quien", "cual", "es", "esta", "esto", "ese", "esos", "esa", "esas", "del", "al", "lo", "le", "les", "se", "me", "te", "nos", "os", "ya", "tambien", "aunque", "sino", "como", "mas", "menos", "muy", "poco", "mucho", "todo", "cada", "alguno", "ninguno", "uno", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve", "diez"}
    tokens = re.findall(r"[a-záéíóúñ]+", texto.lower())
    filtrados = [t for t in tokens if t not in stop_words and len(t) > 3]
    vistos = set()
    unicos = []
    for t in filtrados:
        if t not in vistos:
            vistos.add(t)
            unicos.append(t)
    return " ".join(unicos[:max_palabras])

def _parsear_json_seguro(respuesta):
    try:
        return respuesta.json()
    except Exception:
        return {}

def detectar_idioma_titulo(titulo):
    palabras_es = {"el", "la", "los", "las", "un", "una", "de", "del", "en", "y", "para", "con", "por", "que", "como"}
    palabras_lower = set(titulo.lower().split())
    coincidencias = sum(1 for p in palabras_lower if p in palabras_es)
    return coincidencias >= 2

_PATRON_DOI = re.compile(r'10\.\d{4,9}/[^\s"\'<>,]+')

def extraer_doi(texto):
    m = _PATRON_DOI.search(texto)
    if not m:
        return None
    return m.group(0).rstrip('.').rstrip(')').rstrip(']')

@cache.memoize(timeout=1800)
def obtener_trabajo_por_doi(doi):
    """Resuelve un DOI directamente contra CrossRef (distinto de la búsqueda
    bibliográfica: aquí consultamos el registro exacto del DOI)."""
    try:
        r = requests.get(f"https://api.crossref.org/works/{doi}", timeout=6)
        if r.status_code != 200:
            return None
        return _parsear_json_seguro(r).get("message")
    except Exception:
        return None

@cache.memoize(timeout=1800)
def verificar_retractacion(doi):
    """Revisa si un DOI aparece marcado como retractado en los metadatos de
    CrossRef (campo 'update-to' con tipo Retraction, o relación
    'is-retracted-by'). Devuelve un dict con el detalle o None si no hay
    indicios de retractación."""
    trabajo = obtener_trabajo_por_doi(doi)
    if not trabajo:
        return None
    for actualizacion in trabajo.get("update-to", []):
        if str(actualizacion.get("type", "")).lower() in ("retraction", "retract"):
            return {
                "retractado": True,
                "fecha": actualizacion.get("updated", {}).get("date-time", "s.f."),
                "doi_retraccion": actualizacion.get("DOI", ""),
            }
    relaciones = trabajo.get("relation", {})
    if "is-retracted-by" in relaciones:
        return {"retractado": True, "fecha": "s.f.", "doi_retraccion": ""}
    return None

def verificar(cita):
    doi_en_texto = extraer_doi(cita)
    if doi_en_texto:
        trabajo = obtener_trabajo_por_doi(doi_en_texto)
        if trabajo:
            titulo_real = (trabajo.get("title") or ["(sin titulo)"])[0]
            similitud_doi = SequenceMatcher(None, cita.lower(), titulo_real.lower()).ratio()
            retraccion = verificar_retractacion(doi_en_texto)
            if similitud_doi < 0.4:
                alternativas = []
                try:
                    alternativas = buscar_por_tema(cita)
                except Exception:
                    pass
                return {
                    "original": cita, "titulo": titulo_real, "doi": doi_en_texto,
                    "similitud": f"{similitud_doi:.2f}",
                    "mensaje": "⚠ Cita fantasma probable: el DOI es real pero pertenece a un artículo distinto al que describe la cita. Esto es típico de referencias inventadas por IA.",
                    "alerta": "hallucination", "alternativas": alternativas,
                }
            if retraccion:
                return {
                    "original": cita, "titulo": titulo_real, "doi": doi_en_texto,
                    "similitud": f"{similitud_doi:.2f}",
                    "mensaje": "🚫 Este artículo fue retractado. No se recomienda citarlo, aunque el DOI y el título coincidan.",
                    "alerta": "retraction", "alternativas": [],
                }

    url = "https://api.crossref.org/works"
    parametros = {"query.bibliographic": cita, "rows": 1}
    try:
        respuesta = requests.get(url, params=parametros, timeout=6)
        datos = _parsear_json_seguro(respuesta)
    except Exception:
        datos = {}
    items = datos.get("message", {}).get("items", []) if datos else []
    if len(items) == 0:
        alternativas = []
        try:
            alternativas = buscar_por_tema(cita)
        except Exception:
            pass
        return {
            "original": cita, "titulo": "(ninguno)", "doi": "-", "similitud": "0.00",
            "mensaje": "No encontramos esta cita. Aquí tienes alternativas reales sobre el mismo tema:",
            "alerta": "not_found", "alternativas": alternativas,
        }
    primer_resultado = items[0]
    titulo = primer_resultado.get("title", ["(sin titulo)"])[0]
    doi = primer_resultado.get("DOI", "sin DOI")
    similitud = SequenceMatcher(None, cita.lower(), titulo.lower()).ratio()
    if similitud > 0.65:
        retraccion = verificar_retractacion(doi) if doi and doi != "sin DOI" else None
        if retraccion:
            return {
                "original": cita, "titulo": titulo, "doi": doi, "similitud": f"{similitud:.2f}",
                "mensaje": "🚫 Este artículo fue retractado. No se recomienda citarlo.",
                "alerta": "retraction", "alternativas": [],
            }
        return {
            "original": cita, "titulo": titulo, "doi": doi, "similitud": f"{similitud:.2f}",
            "mensaje": "Es probable que esta cita SI exista.", "alerta": "ok", "alternativas": [],
        }
    else:
        cita_corregida = intentar_corregir_cita(cita)
        alternativas = []
        try:
            alternativas = buscar_por_tema(cita_corregida)
        except Exception:
            pass
        return {
            "original": cita, "titulo": titulo, "doi": doi, "similitud": f"{similitud:.2f}",
            "mensaje": "No pudimos confirmar esta cita exacta. ¿Quieres usar esta versión corregida?",
            "alerta": "unsure", "cita_corregida": cita_corregida, "alternativas": alternativas,
        }

@cache.memoize(timeout=600)
def buscar_por_tema(tema):
    url = "https://api.crossref.org/works"
    parametros = {"query": tema, "rows": 6}
    try:
        respuesta = requests.get(url, params=parametros, timeout=6)
        datos = _parsear_json_seguro(respuesta)
    except Exception:
        return []
    items = datos.get("message", {}).get("items", []) if datos else []
    papers = []
    for item in items:
        try:
            titulo = item.get("title", ["(sin titulo)"])[0]
            if not titulo or not titulo.strip():
                continue
            autores_lista = item.get("author", [])
            autores = ", ".join(f"{a.get('given', '')} {a.get('family', '')}".strip() for a in autores_lista) if autores_lista else "Anon."
            fecha = item.get("published-print") or item.get("published-online") or {}
            partes_fecha = fecha.get("date-parts", [[None]])
            anio = partes_fecha[0][0] if partes_fecha else None
            doi = item.get("DOI", "sin DOI")
            pdf_gratis = obtener_pdf_gratis(doi) if doi != "sin DOI" else None
            papers.append({
                "titulo": titulo, "autores": autores, "año": anio, "doi": doi,
                "enlace": f"https://doi.org/{doi}" if doi != "sin DOI" else "#",
                "pdf_gratis": pdf_gratis, "en_espanol": detectar_idioma_titulo(titulo),
                "fuente": "CrossRef", "region": None,
                "tipo": "academico"
            })
        except Exception:
            continue
    return papers

def buscar_en_google_scholar(tema):
    return []

def buscar_fuentes_google_scholar(tema):
    return []

@cache.memoize(timeout=600)
def buscar_en_semantic_scholar(tema):
    url = "https://api.semanticscholar.org/graph/v1/paper/search"
    params = {
        "query": tema,
        "fields": "title,authors,year,openAccessPdf,externalIds",
        "limit": 6
    }
    headers = {"User-Agent": "Mozilla/5.0 (AcademicCiteBot/1.0)"}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=6)
        if r.status_code != 200:
            return []
        data = r.json()
        papers = []
        for item in data.get("data", []):
            autores_raw = item.get("authors", [])
            autores = ", ".join([a.get("name", "") for a in autores_raw[:3]]) if autores_raw else "Anon."
            pdf_url = None
            oa = item.get("openAccessPdf")
            if oa and isinstance(oa, dict):
                pdf_url = oa.get("url")
            doi = ""
            ext = item.get("externalIds", {})
            if ext and isinstance(ext, dict):
                doi = ext.get("DOI", "")
            papers.append({
                "titulo": item.get("title", "Sin título"),
                "autores": autores,
                "año": item.get("year", "s.f."),
                "doi": doi,
                "enlace": f"https://www.semanticscholar.org/paper/{item.get('paperId', '')}",
                "pdf_gratis": pdf_url,
                "en_espanol": detectar_idioma_titulo(item.get("title", "")),
                "fuente": "Semantic Scholar", "region": None,
                "tipo": "academico"
            })
        return papers
    except Exception as e:
        print(f"Error Semantic Scholar: {e}")
        return []

@cache.memoize(timeout=600)
def buscar_en_openalex(tema):
    url = "https://api.openalex.org/works"
    parametros = {"search": tema, "per-page": 6, "mailto": CONFIG["email_contacto"]}
    try:
        respuesta = requests.get(url, params=parametros, timeout=6)
        datos = _parsear_json_seguro(respuesta)
    except Exception:
        return []
    resultados = []
    for item in datos.get("results", []):
        try:
            titulo = item.get("title") or "(sin titulo)"
            anio = item.get("publication_year", "?")
            doi = item.get("doi") or ""
            doi_limpio = doi.replace("https://doi.org/", "") if doi else ""
            openalex_id = item.get("id", "")
            autorships = item.get("authorships", [])
            autores = ", ".join(a.get("author", {}).get("display_name", "") for a in autorships[:3]) if autorships else "Anon."
            codigo_pais = None
            if autorships:
                instituciones = autorships[0].get("institutions", [])
                if instituciones:
                    codigo_pais = instituciones[0].get("country_code")
            acceso_abierto = item.get("open_access", {})
            pdf_gratis = acceso_abierto.get("oa_url") if acceso_abierto.get("is_oa") else None
            resultados.append({
                "titulo": titulo, "autores": autores, "año": anio, "doi": doi_limpio,
                "enlace": openalex_id if openalex_id else "#", "pdf_gratis": pdf_gratis,
                "en_espanol": detectar_idioma_titulo(titulo),
                "fuente": "OpenAlex", "region": bandera_de(codigo_pais),
                "tipo": "academico"
            })
        except Exception:
            continue
    return resultados

def obtener_pdf_gratis(doi):
    if not doi or doi == "sin DOI":
        return None
    url = f"https://api.unpaywall.org/v2/{doi}"
    parametros = {"email": CONFIG["email_contacto"]}
    try:
        respuesta = requests.get(url, params=parametros, timeout=5)
        datos = _parsear_json_seguro(respuesta)
        mejor_ubicacion = datos.get("best_oa_location")
        if mejor_ubicacion:
            return mejor_ubicacion.get("url_for_pdf")
    except Exception:
        return None
    return None

REPOSITORIOS_DSPACE = [
    {"nombre": "UNSA", "base_url": "https://repositorio.unsa.edu.pe", "region": "🇵🇪 Perú"},
    {"nombre": "UNAM", "base_url": "https://repositorio.unam.mx", "region": "🇲🇽 México"},
    {"nombre": "CONICET", "base_url": "https://ri.conicet.gov.ar", "region": "🇦🇷 Argentina"},
    {"nombre": "UNAL", "base_url": "https://repositorio.unal.edu.co", "region": "🇨🇴 Colombia"},
    {"nombre": "UChile", "base_url": "https://repositorio.uchile.cl", "region": "🇨🇱 Chile"},
]

def _buscar_en_dspace(base_url, tema, region_label, nombre_repo):
    try:
        url = f"{base_url}/server/api/discover/search/objects"
        params = {"query": tema, "dsoType": "item", "size": 3}
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        r = requests.get(url, params=params, headers=headers, timeout=5)
        datos = _parsear_json_seguro(r)
        objetos = datos.get("_embedded", {}).get("searchResult", {}).get("_embedded", {}).get("objects", [])
        resultados = []
        for obj in objetos:
            try:
                indexable = obj.get("_embedded", {}).get("indexableObject", {})
                titulo = indexable.get("name", "(sin titulo)")
                handle = indexable.get("handle", "")
                enlace = f"{base_url}/handle/{handle}" if handle else "#"
                meta = indexable.get("metadata", {})
                autores_lista = meta.get("dc.contributor.author", [])
                autores = ", ".join(a.get("value", "") for a in autores_lista) if autores_lista else "Anon."
                fecha = meta.get("dc.date.issued", [])
                anio = fecha[0]["value"][:4] if fecha else "?"
                resultados.append({
                    "titulo": titulo, "autores": autores, "año": anio, "enlace": enlace,
                    "en_espanol": detectar_idioma_titulo(titulo), "region": region_label,
                    "fuente": f"Repositorio {nombre_repo}",
                    "tipo": "repositorio", "pdf_gratis": None, "doi": ""
                })
            except Exception:
                continue
        return resultados
    except Exception:
        return []

def buscar_en_repositorios(tema):
    resultados = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(_buscar_en_dspace, repo["base_url"], tema, repo["region"], repo["nombre"]): repo for repo in REPOSITORIOS_DSPACE}
        for future in as_completed(futures, timeout=6):
            try:
                resultados.extend(future.result())
            except Exception:
                continue
    return resultados

def _tema_es_valido(tema):
    if not tema or len(tema) > 300:
        return False
    return bool(re.match(r'^[\w\s.,;:()áéíóúñÁÉÍÓÚÑ\-\'"¿?]+$', tema, re.UNICODE))

def ejecutar_busqueda_completa(tema, idioma):
    tema_en = obtener_query_ingles(tema, idioma)
    tema_busqueda_global = f"{tema} {tema_en}" if tema_en != tema else tema

    with ThreadPoolExecutor(max_workers=4) as executor:
        f_crossref = executor.submit(buscar_por_tema, tema_busqueda_global)
        f_openalex = executor.submit(buscar_en_openalex, tema_busqueda_global)
        f_semantic = executor.submit(buscar_en_semantic_scholar, tema_busqueda_global)
        f_repos = executor.submit(buscar_en_repositorios, tema)

        futures = {
            f_crossref: "crossref",
            f_openalex: "openalex",
            f_semantic: "semantic",
            f_repos: "repositorios"
        }

        sugerencias = []
        resultados_openalex = []
        resultados_semantic = []
        resultados_repositorios = []

        for futuro, nombre in futures.items():
            try:
                res = futuro.result(timeout=8)
                if nombre == "crossref":
                    sugerencias = res
                elif nombre == "openalex":
                    resultados_openalex = res
                elif nombre == "semantic":
                    resultados_semantic = res
                elif nombre == "repositorios":
                    resultados_repositorios = res
            except Exception as e:
                print(f"Error en fuente {nombre}: {e}")

    todos_papers = sugerencias + resultados_openalex + resultados_semantic + resultados_repositorios

    for p in todos_papers:
        p["cita_apa"] = generar_cita(p, "apa")
        p["cita_vancouver"] = generar_cita(p, "vancouver")
        p["cita_ieee"] = generar_cita(p, "ieee")
        p["cita_mla"] = generar_cita(p, "mla")

    def score(p):
        tiene_pdf = 1 if p.get("pdf_gratis") else 0
        anio = int(p.get("año")) if p.get("año") and str(p.get("año")).isdigit() else 0
        return (tiene_pdf, anio)

    todos_papers.sort(key=score, reverse=True)
    return todos_papers


_TAMANO_MAX_PDF = 30 * 1024 * 1024  # 30 MB

_DOMINIOS_PDF_PERMITIDOS = (
    "doi.org", "unpaywall.org", "semanticscholar.org",
    "ncbi.nlm.nih.gov", "arxiv.org", "core.ac.uk",
    "researchgate.net", "crossref.org", "openalex.org",
    "repositorio.unsa.edu.pe", "repositorio.unam.mx",
    "ri.conicet.gov.ar", "repositorio.unal.edu.co", "repositorio.uchile.cl",
)

def _url_es_segura(url):
    try:
        partes = urlparse(url)
    except Exception:
        return False
    if partes.scheme not in ("http", "https"):
        return False
    host = (partes.hostname or "").lower()
    if not host:
        return False
    if not any(host == d or host.endswith("." + d) for d in _DOMINIOS_PDF_PERMITIDOS):
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True

@app.route("/api/preview-pdf")
@limiter.limit("60 per minute")
def api_preview_pdf():
    url = request.args.get("url", "").strip()
    if not url or not _url_es_segura(url):
        abort(400, description="URL no permitida.")
    try:
        r = requests.get(
            url,
            timeout=8,
            stream=True,
            headers={"User-Agent": "Mozilla/5.0 (AcademicCiteBot Preview/1.0)"},
            allow_redirects=True,
        )
    except Exception:
        abort(502, description="No se pudo obtener el documento.")

    content_type = r.headers.get("Content-Type", "")
    if r.status_code != 200 or "pdf" not in content_type.lower():
        abort(415, description="El recurso solicitado no es un PDF válido.")

    contenido = bytearray()
    for chunk in r.iter_content(chunk_size=65536):
        contenido.extend(chunk)
        if len(contenido) > _TAMANO_MAX_PDF:
            abort(413, description="El PDF supera el tamaño máximo permitido para la vista previa.")

    respuesta = Response(bytes(contenido), mimetype="application/pdf")
    respuesta.headers["Content-Disposition"] = "inline; filename=documento.pdf"
    respuesta.headers["X-Content-Type-Options"] = "nosniff"
    respuesta.headers["Cache-Control"] = "private, max-age=300"
    return respuesta


@app.route("/")
def splash():
    return render_template_string(SPLASH_ZENECITE, csp_nonce=g.csp_nonce)

@app.route("/app", methods=["GET", "POST"])
@limiter.limit("30 per minute")
def inicio():
    idioma = request.args.get("idioma") or session.get("idioma", "es")
    session["idioma"] = idioma
    t = TEXTOS[idioma]

    resultados = None
    sugerencias = []
    tema_buscado = None
    sugerencias_extracto = None
    tab_activa = "verificar"

    if request.method == "POST":
        accion = request.form.get("accion")

        if accion == "verificar" or accion == "scan_bibliography":
            texto = request.form.get("cita") or request.form.get("bibliography") or ""
            lineas = texto.split("\n")
            resultados = []
            for linea in lineas:
                linea_limpia = linea.strip()
                if linea_limpia != "":
                    resultados.append(verificar(linea_limpia))
            tab_activa = "verificar" if accion == "verificar" else "scan"

        elif accion == "buscar_todo":
            tema = request.form.get("tema", "").strip()
            tab_activa = "buscar"
            if tema:
                tema_buscado = tema
                sugerencias = ejecutar_busqueda_completa(tema, idioma)

        elif accion == "analizar_extracto":
            extracto = request.form.get("extracto", "").strip()
            if extracto:
                tema_para_buscar = extraer_palabras_clave(extracto)
                if not tema_para_buscar:
                    tema_para_buscar = extracto
                sugerencias_extracto = buscar_por_tema(tema_para_buscar) if tema_para_buscar else []
                tab_activa = "extracto"
                session["ultimo_tema_extracto"] = tema_para_buscar

    return render_template_string(
    PLANTILLA, t=t, resultados=resultados, sugerencias=sugerencias,
    tema_buscado=tema_buscado, sugerencias_extracto=sugerencias_extracto,
    tab_activa=tab_activa, idioma_actual=idioma, csp_nonce=g.csp_nonce,
)

@app.route("/api/buscar", methods=["POST"])
@limiter.limit("20 per minute")
def api_buscar():
    idioma = session.get("idioma", "es")
    tema = request.form.get("tema", "").strip()
    if not tema or not _tema_es_valido(tema):
        return jsonify({"error": "tema_invalido"}), 400
    papers = ejecutar_busqueda_completa(tema, idioma)
    return jsonify({"tema": tema, "papers": papers})

@app.route("/api/suggest-tema")
@limiter.limit("60 per minute")
def api_suggest_tema():
    query = request.args.get("tema", "").strip()
    if len(query) < 2:
        return jsonify([])
    papers = buscar_por_tema(query)
    return jsonify([{"titulo": p["titulo"], "año": p.get("año")} for p in papers[:5]])
SPLASH_ZENECITE = r"""
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ZENECITE — Verificador Académico Global</title>
    <style>
        /* ==================== ACCESIBILIDAD GLOBAL ==================== */
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            cursor: auto !important;
            background-color: #0B0F19;
            display: flex;
            justify-content: center;
            align-items: center;
            height: 100vh;
            margin: 0;
            flex-direction: column;
            font-family: 'Segoe UI', sans-serif;
            overflow: hidden;
            position: relative;
        }

        /* Foco visible SOLO con teclado */
        a:focus-visible,
        button:focus-visible,
        input:focus-visible,
        textarea:focus-visible,
        summary:focus-visible,
        [tabindex]:focus-visible {
            outline: 3px solid #34D399 !important;
            outline-offset: 2px !important;
            border-radius: 4px !important;
        }

        .sr-only {
            position: absolute;
            width: 1px !important;
            height: 1px !important;
            padding: 0 !important;
            margin: -1px !important;
            overflow: hidden !important;
            clip: rect(0, 0, 0, 0) !important;
            white-space: nowrap !important;
            border: 0 !important;
        }

        /* Skip link: visible SOLO al enfocarlo con Tab */
        .skip-link {
            position: absolute;
            left: -9999px;
            top: 0;
            z-index: 10000;
            background: #10B981;
            color: #0B1120;
            padding: 10px 18px;
            font-weight: 700;
            border-radius: 0 0 8px 0;
            text-decoration: none;
        }
        .skip-link:focus {
            left: 0;
        }

        /* ===== REDUCED MOTION: cubre TODAS las animaciones ===== */
        @media (prefers-reduced-motion: reduce) {
            *,
            *::before,
            *::after {
                animation-duration: 0.001ms !important;
                animation-iteration-count: 1 !important;
                transition-duration: 0.001ms !important;
                scroll-behavior: auto !important;
            }
            .brand-name,
            .tagline,
            .enter-btn {
                animation: none !important;
                opacity: 1 !important;
                background-position: 0 0 !important;
            }
        }

        #particle-canvas {
            position: absolute;
            top: 0; left: 0;
            width: 100%; height: 100%;
            z-index: 0;
        }
        .content-layer {
            position: relative;
            z-index: 10;
            display: flex;
            flex-direction: column;
            align-items: center;
            text-align: center;
            pointer-events: none;
        }
        .brand-name {
            font-size: 80px;
            font-weight: 900;
            letter-spacing: 14px;
            color: transparent;
            background: linear-gradient(120deg, #FFFFFF 0%, #A7F3D0 30%, #10B981 55%, #A7F3D0 80%, #FFFFFF 100%);
            background-size: 300% 100%;
            -webkit-background-clip: text;
            background-clip: text;
            font-family: 'Cambria', 'Georgia', serif;
            text-shadow: 0 0 60px rgba(16, 185, 129, 0.4), 0 0 120px rgba(16, 185, 129, 0.15);
            animation: fadeInScale 1.2s ease-out, brilloTexto 4s linear infinite 1.2s;
            margin-bottom: 10px;
        }
        .tagline {
            font-size: 14px;
            color: #7F8C8D;
            letter-spacing: 8px;
            text-transform: uppercase;
            margin-top: 10px;
            opacity: 0;
            animation: fadeUp 1s ease 0.8s forwards;
        }
        .enter-btn {
            margin-top: 50px;
            padding: 16px 50px;
            background: transparent;
            border: 2px solid #10B981;
            color: #10B981;
            font-family: 'Segoe UI', sans-serif;
            font-size: 14px;
            letter-spacing: 4px;
            text-transform: uppercase;
            cursor: auto;
            border-radius: 6px;
            opacity: 0;
            animation: fadeUp 1s ease 1.4s forwards;
            transition: all 0.3s ease;
            text-decoration: none;
            display: inline-block;
            pointer-events: auto;
            font-weight: 700;
            position: relative;
            overflow: hidden;
        }
        .enter-btn::before {
            content: "";
            position: absolute;
            top: 0; left: -100%;
            width: 100%; height: 100%;
            background: linear-gradient(90deg, transparent, rgba(16,185,129,0.35), transparent);
            transition: left 0.5s ease;
        }
        .enter-btn:hover::before { left: 100%; }
        .enter-btn:hover {
            background: rgba(16, 185, 129, 0.12);
            box-shadow: 0 0 40px rgba(16, 185, 129, 0.35);
            transform: scale(1.06);
            border-color: #34D399;
            color: #34D399;
        }
        @keyframes fadeInScale {
            from { opacity: 0; transform: scale(0.85); }
            to { opacity: 1; transform: scale(1); }
        }
        @keyframes fadeUp {
            from { opacity: 0; transform: translateY(20px); }
            to { opacity: 1; transform: translateY(0); }
        }
        @keyframes brilloTexto {
            0% { background-position: 0% 50%; }
            100% { background-position: 300% 50%; }
        }
        body::before {
            content: "";
            position: absolute;
            top: 0; left: 0; right: 0; bottom: 0;
            background: repeating-linear-gradient(
                0deg, transparent, transparent 2px,
                rgba(0,0,0,0.15) 2px, rgba(0,0,0,0.15) 4px
            );
            pointer-events: none;
            z-index: 20;
            opacity: 0.2;
        }
        @media (max-width: 600px) {
            .brand-name { font-size: 44px; letter-spacing: 8px; }
            .tagline { font-size: 11px; letter-spacing: 4px; }
            .enter-btn { padding: 12px 30px; font-size: 12px; }
        }
    .brand-icon-wrap img {
    transition: filter 0.4s ease;
}
.brand-icon-wrap:hover img {
    filter: drop-shadow(0 0 10px rgba(245,158,11,0.6)) drop-shadow(0 0 10px rgba(59,130,246,0.6));
}
    </style>
</head>
<body>
    <a href="#main-content" class="skip-link">Saltar al contenido principal</a>
    <canvas id="particle-canvas" aria-hidden="true"></canvas>

    <main id="main-content" class="content-layer">
        <img src="/static/logo-zenecite.svg" alt="Logo Zenecite" width="120" style="margin-bottom: 20px; filter: drop-shadow(0 0 30px rgba(16,185,129,0.4));">
        <h1 class="brand-name">ZENECITE</h1>
        <p class="tagline">Verificador Académico Global</p>
        <a href="/app" class="enter-btn">Entrar al Verificador →</a>
    </main>

    <script nonce="{{ csp_nonce }}">
    (function() {
        if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
            document.getElementById('particle-canvas').style.display = 'none';
            return;
        }
        const canvas = document.getElementById("particle-canvas");
        const ctx = canvas.getContext("2d");
        let W, H;
        function resize() {
            W = canvas.width = window.innerWidth;
            H = canvas.height = window.innerHeight;
        }
        resize();
        window.addEventListener("resize", resize);

        const PARTICLES = 80;
        const CONNECT_DIST = 120;
        const COLOR = "16, 185, 129";
        const particles = [];

        for (let i = 0; i < PARTICLES; i++) {
            particles.push({
                x: Math.random() * W,
                y: Math.random() * H,
                vx: (Math.random() - 0.5) * 0.8,
                vy: (Math.random() - 0.5) * 0.8,
                r: 1.5 + Math.random() * 2.5,
                alpha: 0.2 + Math.random() * 0.5,
                pulse: Math.random() * Math.PI * 2
            });
        }

        function dist(a, b) {
            return Math.sqrt((a.x - b.x)*(a.x - b.x) + (a.y - b.y)*(a.y - b.y));
        }

        function draw() {
            ctx.clearRect(0, 0, W, H);

            ctx.lineWidth = 0.8;
            for (let i = 0; i < particles.length; i++) {
                for (let j = i + 1; j < particles.length; j++) {
                    var d = dist(particles[i], particles[j]);
                    if (d < CONNECT_DIST) {
                        var opacity = (1 - d / CONNECT_DIST) * 0.25 * Math.min(particles[i].alpha, particles[j].alpha);
                        ctx.strokeStyle = "rgba(" + COLOR + "," + opacity + ")";
                        ctx.beginPath();
                        ctx.moveTo(particles[i].x, particles[i].y);
                        ctx.lineTo(particles[j].x, particles[j].y);
                        ctx.stroke();
                    }
                }
            }

            particles.forEach(function(p) {
                p.x += p.vx;
                p.y += p.vy;
                p.pulse += 0.04;
                if (p.x < 0 || p.x > W) p.vx *= -1;
                if (p.y < 0 || p.y > H) p.vy *= -1;

                var g = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, p.r * 3);
                g.addColorStop(0, "rgba(" + COLOR + "," + (p.alpha * 0.6) + ")");
                g.addColorStop(1, "rgba(" + COLOR + ",0)");
                ctx.fillStyle = g;
                ctx.beginPath();
                ctx.arc(p.x, p.y, p.r * 3, 0, Math.PI * 2);
                ctx.fill();

                ctx.fillStyle = "rgba(255,255,255," + p.alpha + ")";
                ctx.beginPath();
                ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
                ctx.fill();
            });

            if (Math.random() < 0.02) {
                var a = particles[Math.floor(Math.random() * particles.length)];
                var b = particles[Math.floor(Math.random() * particles.length)];
                if (dist(a, b) < 100) {
                    ctx.strokeStyle = "rgba(" + COLOR + ",0.5)";
                    ctx.lineWidth = 1.2;
                    ctx.shadowBlur = 8;
                    ctx.shadowColor = "#10B981";
                    ctx.beginPath();
                    ctx.moveTo(a.x, a.y);
                    ctx.lineTo(b.x, b.y);
                    ctx.stroke();
                    ctx.shadowBlur = 0;
                }
            }

            requestAnimationFrame(draw);
        }
        draw();
    })();
    </script>
</body>
</html>
"""
PLANTILLA = r"""
<!DOCTYPE html>
<html lang="{{ idioma_actual }}">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ t.titulo_pagina }}</title>
    <style>
        /* ==================== ACCESIBILIDAD GLOBAL ==================== */
        :root {
            --bg-dark: #0B1120;
            --bg-card: rgba(20, 30, 45, 0.75);
            --text-main: #e2e8f0;
            --text-muted: #94a3b8;
            --primary-color: #10B981;
            --primary-hover: #059669;
            --border-light: rgba(255, 255, 255, 0.08);
            --sidebar-bg: rgba(11, 17, 32, 0.98);
            --danger: #EF4444;
            --warning: #F59E0B;
            --info: #3B82F6;
        }
        * { box-sizing: border-box; }
        body {
            margin: 0;
            font-family: 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
            background-color: var(--bg-dark);
            background-image:
                radial-gradient(circle at 85% 15%, rgba(16, 185, 129, 0.06) 0%, transparent 40%),
                radial-gradient(circle at 15% 85%, rgba(59, 130, 246, 0.06) 0%, transparent 40%);
            background-attachment: fixed;
            color: var(--text-main);
            min-height: 100vh;
            overflow-x: hidden;
            cursor: auto !important;
        }

        /* Foco visible SOLO con teclado */
        a:focus-visible,
        button:focus-visible,
        input:focus-visible,
        textarea:focus-visible,
        summary:focus-visible,
        [tabindex]:focus-visible {
            outline: 3px solid #34D399 !important;
            outline-offset: 2px !important;
            border-radius: 4px !important;
        }

        .sr-only {
            position: absolute;
            width: 1px !important;
            height: 1px !important;
            padding: 0 !important;
            margin: -1px !important;
            overflow: hidden !important;
            clip: rect(0, 0, 0, 0) !important;
            white-space: nowrap !important;
            border: 0 !important;
        }

        .skip-link {
            position: absolute;
            left: -9999px;
            top: 0;
            z-index: 10000;
            background: #10B981;
            color: #0B1120;
            padding: 10px 18px;
            font-weight: 700;
            border-radius: 0 0 8px 0;
            text-decoration: none;
        }
        .skip-link:focus {
            left: 0;
        }

        /* ===== REDUCED MOTION: cubre TODAS las animaciones ===== */
        @media (prefers-reduced-motion: reduce) {
            *,
            *::before,
            *::after {
                animation-duration: 0.001ms !important;
                animation-iteration-count: 1 !important;
                transition-duration: 0.001ms !important;
                scroll-behavior: auto !important;
            }
            .brand-name, .tagline, .enter-btn,
            .brand-text, .brand-icon-wrap::before, .brand-icon-wrap .icon,
            .skeleton, .loader-canvas-container canvas, #loaderCanvas {
                animation: none !important;
                opacity: 1 !important;
                background-position: 0 0 !important;
            }
            .nav-boton::before {
                transform: scaleY(1) !important;
            }
        }

        /* ===== ICONOS ===== */
        .icon {
            display: inline-block;
            width: 1em;
            height: 1em;
            vertical-align: -0.15em;
            background-color: currentColor;
            -webkit-mask-repeat: no-repeat;
            mask-repeat: no-repeat;
            -webkit-mask-position: center;
            mask-position: center;
            -webkit-mask-size: contain;
            mask-size: contain;
            flex-shrink: 0;
        }
        .icon-lg { width: 1.3em; height: 1.3em; }
        .icon-search { -webkit-mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23000' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='11' cy='11' r='7'/%3E%3Cline x1='21' y1='21' x2='16.2' y2='16.2'/%3E%3C/svg%3E"); mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23000' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='11' cy='11' r='7'/%3E%3Cline x1='21' y1='21' x2='16.2' y2='16.2'/%3E%3C/svg%3E"); }
        .icon-bolt { -webkit-mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='%23000'%3E%3Cpath d='M13 2 3 14h7l-1 8 10-13h-7l1-7z'/%3E%3C/svg%3E"); mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='%23000'%3E%3Cpath d='M13 2 3 14h7l-1 8 10-13h-7l1-7z'/%3E%3C/svg%3E"); }
        .icon-file { -webkit-mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23000' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M6 2h9l5 5v15H6z'/%3E%3Cpath d='M15 2v5h5'/%3E%3Cline x1='9' y1='13' x2='15' y2='13'/%3E%3Cline x1='9' y1='17' x2='15' y2='17'/%3E%3C/svg%3E"); mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23000' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M6 2h9l5 5v15H6z'/%3E%3Cpath d='M15 2v5h5'/%3E%3Cline x1='9' y1='13' x2='15' y2='13'/%3E%3Cline x1='9' y1='17' x2='15' y2='17'/%3E%3C/svg%3E"); }
        .icon-book { -webkit-mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23000' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M4 4.5A2.5 2.5 0 0 1 6.5 2H20v17H6.5A2.5 2.5 0 0 0 4 21.5z'/%3E%3Cpath d='M4 19.5A2.5 2.5 0 0 1 6.5 17H20'/%3E%3C/svg%3E"); mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23000' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M4 4.5A2.5 2.5 0 0 1 6.5 2H20v17H6.5A2.5 2.5 0 0 0 4 21.5z'/%3E%3Cpath d='M4 19.5A2.5 2.5 0 0 1 6.5 17H20'/%3E%3C/svg%3E"); }
        .icon-grid { -webkit-mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23000' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Crect x='3' y='3' width='7' height='7' rx='1'/%3E%3Crect x='14' y='3' width='7' height='7' rx='1'/%3E%3Crect x='3' y='14' width='7' height='7' rx='1'/%3E%3Crect x='14' y='14' width='7' height='7' rx='1'/%3E%3C/svg%3E"); mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23000' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Crect x='3' y='3' width='7' height='7' rx='1'/%3E%3Crect x='14' y='3' width='7' height='7' rx='1'/%3E%3Crect x='3' y='14' width='7' height='7' rx='1'/%3E%3Crect x='14' y='14' width='7' height='7' rx='1'/%3E%3C/svg%3E"); }
        .icon-star { -webkit-mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='%23000'%3E%3Cpath d='M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01z'/%3E%3C/svg%3E"); mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='%23000'%3E%3Cpath d='M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01z'/%3E%3C/svg%3E"); }
        .icon-shield { -webkit-mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23000' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M12 2l8 3v6c0 5-3.4 8.7-8 11-4.6-2.3-8-6-8-11V5z'/%3E%3Cpath d='M9 12l2 2 4-4'/%3E%3C/svg%3E"); mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23000' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M12 2l8 3v6c0 5-3.4 8.7-8 11-4.6-2.3-8-6-8-11V5z'/%3E%3Cpath d='M9 12l2 2 4-4'/%3E%3C/svg%3E"); }
        .icon-download { -webkit-mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23000' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M12 3v12'/%3E%3Cpath d='M7 10l5 5 5-5'/%3E%3Cpath d='M4 21h16'/%3E%3C/svg%3E"); mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23000' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M12 3v12'/%3E%3Cpath d='M7 10l5 5 5-5'/%3E%3Cpath d='M4 21h16'/%3E%3C/svg%3E"); }
        .icon-eye { -webkit-mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23000' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z'/%3E%3Ccircle cx='12' cy='12' r='3'/%3E%3C/svg%3E"); mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23000' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z'/%3E%3Ccircle cx='12' cy='12' r='3'/%3E%3C/svg%3E"); }
        .icon-copy { -webkit-mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23000' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Crect x='9' y='9' width='12' height='12' rx='2'/%3E%3Cpath d='M5 15V5a2 2 0 0 1 2-2h10'/%3E%3C/svg%3E"); mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23000' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Crect x='9' y='9' width='12' height='12' rx='2'/%3E%3Cpath d='M5 15V5a2 2 0 0 1 2-2h10'/%3E%3C/svg%3E"); }
        .icon-check { -webkit-mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23000' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='12' cy='12' r='9'/%3E%3Cpath d='M8 12l3 3 5-6'/%3E%3C/svg%3E"); mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23000' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='12' cy='12' r='9'/%3E%3Cpath d='M8 12l3 3 5-6'/%3E%3C/svg%3E"); }
        .icon-alert { -webkit-mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23000' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M12 2 1 21h22z'/%3E%3Cline x1='12' y1='9' x2='12' y2='14'/%3E%3Ccircle cx='12' cy='17.5' r='0.6' fill='%23000' stroke='none'/%3E%3C/svg%3E"); mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23000' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M12 2 1 21h22z'/%3E%3Cline x1='12' y1='9' x2='12' y2='14'/%3E%3Ccircle cx='12' cy='17.5' r='0.6' fill='%23000' stroke='none'/%3E%3C/svg%3E"); }
        .icon-globe { -webkit-mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23000' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='12' cy='12' r='9'/%3E%3Cellipse cx='12' cy='12' rx='4' ry='9'/%3E%3Cline x1='3' y1='12' x2='21' y2='12'/%3E%3C/svg%3E"); mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23000' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='12' cy='12' r='9'/%3E%3Cellipse cx='12' cy='12' rx='4' ry='9'/%3E%3Cline x1='3' y1='12' x2='21' y2='12'/%3E%3C/svg%3E"); }
        .icon-building { -webkit-mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23000' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Crect x='4' y='3' width='16' height='18'/%3E%3Cline x1='9' y1='7' x2='9' y2='7'/%3E%3Cline x1='15' y1='7' x2='15' y2='7'/%3E%3Cline x1='9' y1='11' x2='9' y2='11'/%3E%3Cline x1='15' y1='11' x2='15' y2='11'/%3E%3Cline x1='9' y1='15' x2='9' y2='15'/%3E%3Cline x1='15' y1='15' x2='15' y2='15'/%3E%3Cpath d='M9 21v-4h6v4'/%3E%3C/svg%3E"); mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23000' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Crect x='4' y='3' width='16' height='18'/%3E%3Cline x1='9' y1='7' x2='9' y2='7'/%3E%3Cline x1='15' y1='7' x2='15' y2='7'/%3E%3Cline x1='9' y1='11' x2='9' y2='11'/%3E%3Cline x1='15' y1='11' x2='15' y2='11'/%3E%3Cline x1='9' y1='15' x2='9' y2='15'/%3E%3Cline x1='15' y1='15' x2='15' y2='15'/%3E%3Cpath d='M9 21v-4h6v4'/%3E%3C/svg%3E"); }
        .icon-x { -webkit-mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23000' stroke-width='2.5' stroke-linecap='round'%3E%3Cline x1='5' y1='5' x2='19' y2='19'/%3E%3Cline x1='19' y1='5' x2='5' y2='19'/%3E%3C/svg%3E"); mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23000' stroke-width='2.5' stroke-linecap='round'%3E%3Cline x1='5' y1='5' x2='19' y2='19'/%3E%3Cline x1='19' y1='5' x2='5' y2='19'/%3E%3C/svg%3E"); }
        .icon-doc-mini { -webkit-mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23000' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M6 2h9l5 5v15H6z'/%3E%3Cpath d='M15 2v5h5'/%3E%3C/svg%3E"); mask-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23000' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M6 2h9l5 5v15H6z'/%3E%3Cpath d='M15 2v5h5'/%3E%3C/svg%3E"); }

        /* ===== LAYOUT ===== */
        .layout-wrapper { display: flex; min-height: 100vh; }
        .sidebar {
            width: 260px; position: fixed; top: 0; left: 0; height: 100vh;
            background: var(--sidebar-bg); backdrop-filter: blur(20px);
            border-right: 1px solid var(--border-light);
            display: flex; flex-direction: column; padding: 25px 15px; z-index: 1000;
        }
        .brand {
            font-weight: 800; font-size: 1.3rem; color: var(--primary-color);
            display: flex; align-items: center; gap: 10px; padding: 0 10px 20px 10px;
            border-bottom: 1px solid var(--border-light); margin-bottom: 20px;
            position: relative;
        }
        .brand-icon-wrap {
            position: relative; display: flex; align-items: center; justify-content: center;
            width: 28px; height: 28px; flex-shrink: 0;
        }
        .brand-icon-wrap::before {
            content: ""; position: absolute; inset: -6px; border-radius: 50%;
            background: radial-gradient(circle, rgba(16,185,129,0.55) 0%, transparent 70%);
            animation: pulsoLogo 2.4s ease-in-out infinite;
        }
        .brand-icon-wrap .icon {
            position: relative; z-index: 1;
            animation: girarSuaveLogo 6s ease-in-out infinite;
        }
        @keyframes pulsoLogo {
            0%, 100% { opacity: 0.35; transform: scale(0.85); }
            50% { opacity: 0.9; transform: scale(1.15); }
        }
        @keyframes girarSuaveLogo {
            0%, 100% { transform: rotate(-4deg) scale(1); }
            50% { transform: rotate(4deg) scale(1.08); }
        }
        .brand-text {
            background: linear-gradient(120deg, var(--primary-color) 0%, #A7F3D0 25%, var(--primary-color) 50%, #34D399 75%, var(--primary-color) 100%);
            background-size: 250% 100%;
            -webkit-background-clip: text; background-clip: text; -webkit-text-fill-color: transparent;
            animation: brilloLogo 5s linear infinite;
        }
        @keyframes brilloLogo {
            0% { background-position: 0% 50%; }
            100% { background-position: 250% 50%; }
        }
        .sidebar-nav { display: flex; flex-direction: column; gap: 5px; flex-grow: 1; }
        .nav-boton {
            padding: 12px 15px; border-radius: 8px; border: none; background: transparent;
            color: var(--text-muted); font-weight: 600; font-size: 0.95rem; cursor: pointer;
            transition: all 0.25s ease; text-align: left; display: flex; align-items: center; gap: 12px;
            position: relative; overflow: hidden;
        }
        .nav-boton::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 3px; background: var(--primary-color); transform: scaleY(0); transition: transform 0.25s ease; }
        .nav-boton::after {
            content: ""; position: absolute; inset: 0; border-radius: 8px;
            background: radial-gradient(circle at var(--mx, 50%) var(--my, 50%), rgba(16,185,129,0.18), transparent 60%);
            opacity: 0; transition: opacity 0.25s ease; pointer-events: none;
        }
        .nav-boton:hover::after { opacity: 1; }
        .nav-boton .icon { transition: transform 0.25s ease; }
        .nav-boton:hover .icon { transform: scale(1.15) translateX(1px); }
        .nav-boton:hover { background: rgba(16, 185, 129, 0.08); color: #fff; transform: translateX(4px); }
        .nav-boton:hover::before { transform: scaleY(1); }
        .nav-boton.activo { background: rgba(16, 185, 129, 0.12); color: var(--primary-color); }
        .nav-boton.activo::before { transform: scaleY(1); }
        .sidebar-footer { margin-top: auto; padding-top: 20px; border-top: 1px solid var(--border-light); display: flex; flex-direction: column; gap: 15px; }
        .lang-switch a { color: var(--text-muted); text-decoration: none; font-size: 0.85rem; margin-right: 10px; transition: color 0.2s; }
        .lang-switch a:hover { color: var(--primary-color); }
        .theme-btn { background: rgba(255,255,255,0.05); border: 1px solid var(--border-light); color: var(--text-main); padding: 8px 15px; border-radius: 8px; cursor: pointer; font-size: 0.9rem; width: 100%; text-align: left; transition: background 0.2s; }
        .theme-btn:hover { background: rgba(255,255,255,0.1); }
        .main-content { margin-left: 260px; flex: 1; padding: 40px; max-width: 1400px; }
        .top-hero { margin-bottom: 30px; }
        .top-hero h1 { font-size: 2.4rem; margin: 0 0 8px 0; letter-spacing: -0.5px; background: linear-gradient(90deg, #fff, var(--primary-color)); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        .top-hero p { font-size: 1.1rem; color: var(--text-muted); margin: 0; }
        .glass { background: var(--bg-card); backdrop-filter: blur(16px); box-shadow: 0 8px 32px 0 rgba(0,0,0,0.4); border: 1px solid var(--border-light); border-radius: 16px; transition: border-color 0.3s ease, box-shadow 0.3s ease; }
        .glass:hover { border-color: rgba(16, 185, 129, 0.18); }
        .seccion { padding: 30px; margin-bottom: 30px; }
        .seccion h2 { margin-top: 0; color: #fff; border-bottom: 1px solid var(--border-light); padding-bottom: 12px; font-size: 1.5rem; display: flex; align-items: center; gap: 10px; }
        textarea, input[type="text"] {
            width: 100%; padding: 15px; border: 1px solid var(--border-light); border-radius: 10px;
            font-size: 16px; font-family: inherit; background: rgba(0,0,0,0.3); color: #fff;
            transition: all 0.3s; margin-bottom: 15px; resize: vertical; cursor: auto;
        }
        textarea::placeholder, input::placeholder { color: var(--text-muted); }
        textarea:focus, input[type="text"]:focus { outline: none; border-color: var(--primary-color); box-shadow: 0 0 0 3px rgba(16, 185, 129, 0.15); }
        button.accion {
            background: var(--primary-color); color: #0B1120; border: none; padding: 12px 28px;
            border-radius: 10px; font-size: 16px; font-weight: 700; cursor: pointer;
            transition: all 0.3s ease; display: inline-flex; align-items: center; gap: 8px;
            position: relative; overflow: hidden;
        }
        button.accion::before {
            content: ""; position: absolute; top: 0; left: -120%; width: 100%; height: 100%;
            background: linear-gradient(100deg, transparent, rgba(255,255,255,0.35), transparent);
            transition: left 0.5s ease;
        }
        button.accion:hover::before { left: 120%; }
        button.accion:hover { transform: translateY(-2px); box-shadow: 0 8px 25px rgba(16, 185, 129, 0.35); background: var(--primary-hover); }
        .grid-resultados { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 20px; margin-top: 20px; }
        .tarjeta {
            padding: 22px; border-radius: 14px; background: rgba(255,255,255,0.03);
            border: 1px solid var(--border-light); box-shadow: 0 4px 15px rgba(0,0,0,0.15);
            transition: transform 0.3s ease, background 0.3s ease, border-color 0.3s ease, box-shadow 0.3s ease;
            position: relative; overflow: hidden;
        }
        .tarjeta:hover { transform: translateY(-4px); background: rgba(255,255,255,0.06); border-color: rgba(16,185,129,0.2); box-shadow: 0 12px 30px rgba(0,0,0,0.3); }
        .tarjeta.si-existe { border-left: 4px solid var(--primary-color); }
        .tarjeta.no-existe { border-left: 4px solid var(--danger); }
        .tarjeta p { margin: 6px 0; color: var(--text-muted); line-height: 1.5; }
        .tarjeta p strong { color: #fff; }
        .titulo-clickable { color: var(--primary-color); text-decoration: none; font-weight: 700; font-size: 1.05rem; cursor: pointer; line-height: 1.4; display: block; margin-bottom: 6px; transition: color 0.2s ease; }
        .titulo-clickable:hover { text-decoration: underline; color: #34D399; }
        .badge-region { display: inline-block; font-size: 11px; font-weight: bold; padding: 4px 10px; border-radius: 6px; margin-bottom: 8px; background: rgba(59, 130, 246, 0.22); color: #60a5fa; border: 1px solid rgba(59, 130, 246, 0.25); }
        .badge-fuente { display: inline-flex; align-items: center; gap: 5px; font-size: 11px; font-weight: bold; padding: 4px 10px; border-radius: 6px; margin-bottom: 8px; margin-right: 6px; background: rgba(16, 185, 129, 0.22); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.25); }
        .badge-es { display: inline-block; background: rgba(245, 158, 11, 0.22); color: #fbbf24; font-size: 10px; font-weight: bold; padding: 3px 8px; border-radius: 4px; margin-left: 6px; border: 1px solid rgba(245,158,11,0.2); }
        .badge-pdf { display: inline-flex; align-items: center; gap: 4px; background: rgba(16, 185, 129, 0.22); color: #34d399; font-size: 10px; font-weight: bold; padding: 3px 8px; border-radius: 4px; margin-left: 6px; border: 1px solid rgba(16,185,129,0.2); }
        .enlaces-paper { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 14px; align-items: center; }
        .enlace-web, .enlace-pdf, .enlace-preview, .guardar-btn { display: inline-flex; align-items: center; gap: 6px; cursor: pointer; }
        .enlace-web { background: rgba(59, 130, 246, 0.1); color: #60a5fa !important; padding: 7px 14px; border-radius: 8px; font-size: 13px; text-decoration: none !important; border: 1px solid rgba(59, 130, 246, 0.3); font-weight: 600; transition: all 0.2s; }
        .enlace-web:hover { background: rgba(59, 130, 246, 0.2); transform: translateY(-1px); }
        .enlace-pdf { background: rgba(16, 185, 129, 0.1); color: var(--primary-color) !important; padding: 7px 14px; border-radius: 8px; font-size: 13px; text-decoration: none !important; border: 1px solid rgba(16, 185, 129, 0.3); font-weight: 600; transition: all 0.2s; }
        .enlace-pdf:hover { background: rgba(16, 185, 129, 0.2); transform: translateY(-1px); }
        .enlace-preview { background: rgba(139, 92, 246, 0.1); color: #a78bfa !important; padding: 7px 14px; border-radius: 8px; font-size: 13px; text-decoration: none !important; border: 1px solid rgba(139, 92, 246, 0.3); font-weight: 600; transition: all 0.2s; }
        .enlace-preview:hover { background: rgba(139, 92, 246, 0.2); transform: translateY(-1px); }
        .tab-content { display: none; }
        .tab-content.active { display: block; animation: fadeIn 0.4s ease; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
        .skeleton {
            background: linear-gradient(90deg, rgba(255,255,255,0.04) 25%, rgba(255,255,255,0.09) 50%, rgba(255,255,255,0.04) 75%);
            background-size: 200% 100%; animation: loading 1.5s infinite;
            border-radius: 14px; height: 160px; border: 1px solid var(--border-light);
        }
        @keyframes loading { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }
        .filtros-bar { display: flex; gap: 10px; margin: 20px 0; flex-wrap: wrap; }
        .filtro-btn {
            display: inline-flex; align-items: center; gap: 6px;
            background: rgba(255,255,255,0.05); border: 1px solid var(--border-light);
            color: var(--text-muted); padding: 8px 16px; border-radius: 20px;
            cursor: pointer; font-size: 0.9rem; font-weight: 600; transition: all 0.2s;
        }
        .filtro-btn:hover { background: rgba(255,255,255,0.1); color: #fff; }
        .filtro-btn.activo { background: var(--primary-color); color: #0B1120; border-color: var(--primary-color); }
        .seccion-resultados { margin: 30px 0; }
        .seccion-resultados h3 { color: var(--text-muted); font-size: 1rem; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 15px; display: flex; align-items: center; gap: 8px; }
        .buscar-tambien { display: flex; gap: 12px; margin-top: 30px; flex-wrap: wrap; align-items: center; }
        .buscar-tambien span { color: var(--text-muted); font-size: 0.95rem; }
        .buscar-tambien a {
            background: rgba(255,255,255,0.05); border: 1px solid var(--border-light);
            color: var(--text-main); padding: 8px 16px; border-radius: 8px;
            text-decoration: none; font-size: 0.9rem; font-weight: 600; transition: all 0.2s; cursor: pointer;
        }
        .buscar-tambien a:hover { background: rgba(16, 185, 129, 0.1); border-color: var(--primary-color); color: var(--primary-color); transform: translateY(-2px); }
        .modal-pdf {
            display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
            background: rgba(0,0,0,0.85); backdrop-filter: blur(8px); z-index: 2000;
            justify-content: center; align-items: center; padding: 20px;
        }
        .modal-pdf.active { display: flex; }
        .modal-contenido {
            background: var(--bg-card); border: 1px solid var(--border-light);
            border-radius: 16px; width: 90%; height: 90%; max-width: 1200px;
            display: flex; flex-direction: column; overflow: hidden;
            box-shadow: 0 25px 50px rgba(0,0,0,0.5);
        }
        .modal-header {
            padding: 15px 25px; border-bottom: 1px solid var(--border-light);
            display: flex; justify-content: space-between; align-items: center;
        }
        .modal-header h3 { margin: 0; color: #fff; font-size: 1.1rem; display: flex; align-items: center; gap: 8px; }
        .modal-cerrar {
            background: var(--danger); color: #fff; border: none; width: 32px; height: 32px;
            border-radius: 8px; cursor: pointer; font-size: 18px; font-weight: bold;
            display: flex; align-items: center; justify-content: center;
        }
        .modal-cerrar .icon { background-color: #fff; }
        .modal-body { position: relative; flex: 1; }
        .modal-iframe { border: none; width: 100%; height: 100%; background: #fff; }
        .modal-estado {
            position: absolute; inset: 0; display: flex; flex-direction: column; gap: 12px;
            align-items: center; justify-content: center; background: var(--bg-card); color: var(--text-muted);
            font-size: 0.95rem; text-align: center; padding: 20px;
        }
        .modal-estado.oculto { display: none; }
        .spinner {
            width: 34px; height: 34px; border-radius: 50%;
            border: 3px solid rgba(255,255,255,0.15); border-top-color: var(--primary-color);
            animation: girar 0.8s linear infinite;
        }
        @keyframes girar { to { transform: rotate(360deg); } }
        .modal-estado a.abrir-externo { color: var(--primary-color); font-weight: 600; text-decoration: none; }
        .modal-estado a.abrir-externo:hover { text-decoration: underline; }
        .loader-canvas-container {
            position: relative; width: 100%; height: 200px; margin: 20px 0;
            background: rgba(0,0,0,0.2); border-radius: 16px; border: 1px solid var(--border-light);
            overflow: hidden; display: none;
        }
        .loader-canvas-container.active { display: block; }
        #loaderCanvas { width: 100%; height: 100%; }
        .loader-texto {
            position: absolute; bottom: 20px; left: 50%; transform: translateX(-50%);
            color: var(--primary-color); font-weight: 700; font-size: 1rem;
            text-shadow: 0 2px 10px rgba(0,0,0,0.5); pointer-events: none;
            text-align: center;
        }
        .sugerencia-item { padding: 12px; cursor: pointer; border-bottom: 1px solid var(--border-light); color: var(--text-main); transition: background 0.15s; display: flex; align-items: center; gap: 10px; }
        .sugerencia-item:hover { background: rgba(16, 185, 129, 0.1); }
        .sugerencia-item .icon { color: var(--text-muted); }
        .aviso-caja { display: flex; gap: 12px; align-items: flex-start; padding: 15px; margin-bottom: 20px; border-radius: 10px; }
        .aviso-caja .icon { margin-top: 2px; }

        /* Toast visual */
        #toast-visual {
            display: none;
            position: fixed;
            bottom: 20px;
            right: 20px;
            background: #10B981;
            color: #0B1120;
            padding: 12px 24px;
            border-radius: 8px;
            font-weight: 600;
            z-index: 9999;
            max-width: 400px;
            box-shadow: 0 8px 25px rgba(0,0,0,0.3);
        }
        #toast-visual.warning { background: #F59E0B; }
        #toast-visual.error { background: #EF4444; color: white; }

        @media (max-width: 768px) {
            .sidebar { width: 100%; height: auto; position: relative; }
            .main-content { margin-left: 0; padding: 20px; }
            .layout-wrapper { flex-direction: column; }
            .grid-resultados { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>
    <a href="#main-content" class="skip-link">Saltar al contenido principal</a>

    <!-- Toast accesible (solo lector de pantalla) -->
    <div id="toast" class="sr-only" role="status" aria-live="polite"></div>

    <!-- Toast visual (para usuarios videntes) -->
    <div id="toast-visual"></div>

    <!-- Anuncio de resultados (aria-live) -->
    <div id="anuncio-resultados" class="sr-only" aria-live="polite"></div>

    <!-- MODAL PDF -->
    <div class="modal-pdf" id="modalPdf" role="dialog" aria-modal="true" aria-label="Vista previa del documento PDF">
        <div class="modal-contenido">
            <div class="modal-header">
                <h3><span class="icon icon-file" aria-hidden="true"></span> Previsualización del documento</h3>
                <button class="modal-cerrar" id="btnCerrarModal" type="button" aria-label="Cerrar vista previa"><span class="icon icon-x" aria-hidden="true"></span></button>
            </div>
            <div class="modal-body">
                <div class="modal-estado oculto" id="modalCargando">
                    <div class="spinner"></div>
                    <span>{{ t.preview_cargando }}</span>
                </div>
                <div class="modal-estado oculto" id="modalError">
                    <span class="icon icon-alert icon-lg" aria-hidden="true" style="color:var(--warning);"></span>
                    <span id="modalErrorTexto">{{ t.preview_error }}</span>
                    <a class="abrir-externo" id="modalAbrirExterno" href="#" target="_blank" rel="noopener">Abrir en pestaña nueva →</a>
                </div>
                <iframe class="modal-iframe" id="iframePdf" src="about:blank" title="Visor de PDF"></iframe>
            </div>
        </div>
    </div>

    <div class="layout-wrapper">
        <aside class="sidebar">
            <div class="brand">
                <span class="brand-icon-wrap">
                  <img src="/static/logo-zenecite.svg" alt="" aria-hidden="true" width="28" height="28" style="display:block; filter: drop-shadow(0 0 6px rgba(16,185,129,0.5));">
                </span>
                <span class="brand-text">ZENECITE</span>
            </div>
            <nav class="sidebar-nav" aria-label="Navegación principal">
                <button type="button" class="nav-boton" id="nav-verificar" data-tab="verificar"><span class="icon icon-check" aria-hidden="true"></span> {{ t.nav_verificar }}</button>
                <button type="button" class="nav-boton" id="nav-scan" data-tab="scan"><span class="icon icon-bolt" aria-hidden="true"></span> {{ t.nav_scan }}</button>
                <button type="button" class="nav-boton" id="nav-buscar" data-tab="buscar"><span class="icon icon-search" aria-hidden="true"></span> {{ t.nav_buscar }}</button>
                <button type="button" class="nav-boton" id="nav-extracto" data-tab="extracto"><span class="icon icon-doc-mini" aria-hidden="true"></span> {{ t.nav_extracto }}</button>
                <button type="button" class="nav-boton" id="nav-guia" data-tab="guia"><span class="icon icon-book" aria-hidden="true"></span> {{ t.nav_guia }}</button>
                <button type="button" class="nav-boton" id="nav-constructor" data-tab="constructor"><span class="icon icon-grid" aria-hidden="true"></span> {{ t.nav_constructor }}</button>
                <button type="button" class="nav-boton" id="nav-biblio" data-tab="biblio"><span class="icon icon-star" aria-hidden="true"></span> {{ t.nav_biblio }}</button>
            </nav>
            <div class="sidebar-footer">
                <div class="lang-switch">
                    <a href="?idioma=es" aria-label="Cambiar a español">ES</a>
                    <a href="?idioma=en" aria-label="Switch to English">EN</a>
                    <a href="?idioma=zh" aria-label="切换到中文">中文</a>
                </div>
            </div>
        </aside>

        <main class="main-content" id="main-content">
            <header class="top-hero">
                <h1>{{ t.titulo_pagina }}</h1>
                <p>{{ t.subtitulo }}</p>
            </header>

            <div class="tab-content {% if tab_activa == 'verificar' %}active{% endif %}" id="tab-verificar">
                <div class="seccion glass">
                    <h2><span class="icon icon-check" aria-hidden="true"></span> {{ t.seccion1_titulo }}</h2>
                    <form method="POST">
                        <label for="cita-textarea" class="sr-only">{{ t.placeholder_cita }}</label>
                        <textarea id="cita-textarea" name="cita" rows="6" placeholder="{{ t.placeholder_cita }}"></textarea>
                        <button type="submit" class="accion" name="accion" value="verificar">{{ t.boton_verificar }}</button>
                    </form>
                    {% if resultados and tab_activa == 'verificar' %}
                        <div class="grid-resultados">
                        {% for resultado in resultados %}
                            <div class="tarjeta {% if 'SI exista' in resultado.mensaje %}si-existe{% else %}no-existe{% endif %}">
                                <p><strong>Cita:</strong> {{ resultado.original }}</p>
                                {% if resultado.cita_corregida %}
                                    <p><strong>Sugerencia:</strong> {{ resultado.cita_corregida }}</p>
                                    <button type="button" class="accion corregir-btn" data-original="{{ resultado.original|e }}" data-corregida="{{ resultado.cita_corregida|e }}" style="background:var(--warning); padding:6px 18px; font-size:14px;">{{ t.usar_correccion }}</button>
                                {% endif %}
                                <p><strong>Título:</strong> <a href="https://doi.org/{{ resultado.doi }}" target="_blank" class="titulo-clickable">{{ resultado.titulo }}</a></p>
                                <p><strong>DOI:</strong> {{ resultado.doi }} · <strong>Similitud:</strong> {{ resultado.similitud }}</p>
                                <p>{{ resultado.mensaje }}</p>
                            </div>
                        {% endfor %}
                        </div>
                    {% endif %}
                </div>
            </div>

            <div class="tab-content {% if tab_activa == 'scan' %}active{% endif %}" id="tab-scan">
                <div class="seccion glass">
                    <h2><span class="icon icon-bolt" aria-hidden="true"></span> {{ t.scan_titulo }}</h2>
                    <p>{{ t.scan_desc }}</p>
                    <div class="aviso-caja" style="background: rgba(245, 158, 11, 0.1); border: 1px solid rgba(245, 158, 11, 0.3); border-left: 4px solid var(--warning); color:#fbbf24;">
                        <span class="icon icon-alert icon-lg" aria-hidden="true"></span>
                        <span><strong>¿Usaste IA para tu tarea?</strong> Las IA suelen inventar citas bibliográficas. Pega tu lista aquí para auditarla antes de entregar.</span>
                    </div>
                    <form method="POST">
                        <label for="bibliography-textarea" class="sr-only">Pega aquí tu bibliografía</label>
                        <textarea id="bibliography-textarea" name="bibliography" rows="10" placeholder="Pega aquí toda tu lista de referencias, una por línea..."></textarea>
                        <button type="submit" class="accion" name="accion" value="scan_bibliography">{{ t.boton_scan }}</button>
                    </form>
                    {% if resultados and tab_activa == 'scan' %}
                        <div class="grid-resultados">
                        {% for resultado in resultados %}
                            <div class="tarjeta {% if 'SI exista' in resultado.mensaje %}si-existe{% else %}no-existe{% endif %}">
                                <p><strong>Cita:</strong> {{ resultado.original }}</p>
                                {% if resultado.cita_corregida %}<p><strong>Sugerencia:</strong> {{ resultado.cita_corregida }}</p>{% endif %}
                                <p><strong>Título:</strong> <a href="https://doi.org/{{ resultado.doi }}" target="_blank" class="titulo-clickable">{{ resultado.titulo }}</a></p>
                                <p><strong>DOI:</strong> {{ resultado.doi }} · <strong>Similitud:</strong> {{ resultado.similitud }}</p>
                                <p>{{ resultado.mensaje }}</p>
                            </div>
                        {% endfor %}
                        </div>
                    {% endif %}
                </div>
            </div>

            <div class="tab-content {% if tab_activa == 'buscar' %}active{% endif %}" id="tab-buscar">
                <div class="seccion glass">
                    <h2><span class="icon icon-search" aria-hidden="true"></span> {{ t.seccion2_titulo }}</h2>
                    <form id="buscar-form" method="POST">
                        <div style="position:relative; width:100%; margin-bottom:15px;">
                            <label for="search-input" class="sr-only">{{ t.placeholder_tema }}</label>
                            <input type="text" name="tema" id="search-input" placeholder="{{ t.placeholder_tema }}" autocomplete="off" style="margin-bottom:0; padding-right: 40px;">
                            <div id="suggestions-dropdown" role="listbox" style="position:absolute; top:100%; left:0; right:0; background: #1E293B; border:1px solid var(--border-light); border-top:none; max-height:250px; overflow-y:auto; z-index:1000; display:none; border-radius: 0 0 12px 12px; box-shadow: 0 10px 25px rgba(0,0,0,0.3);"></div>
                        </div>
                        <button type="submit" class="accion" name="accion" value="buscar_todo" id="btn-buscar-submit">{{ t.boton_buscar_todo }}</button>
                    </form>

                    <div class="loader-canvas-container" id="loaderContainer">
                        <canvas id="loaderCanvas" aria-hidden="true"></canvas>
                        <div class="loader-texto" id="loaderTexto">{{ t.loader_buscando }}</div>
                    </div>

                    <div id="skeleton-container" class="grid-resultados" style="display:none;">
                        <div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>
                    </div>

                    <p id="buscar-sin-resultados" style="display:none; opacity:0.75; margin-top:20px;">No encontramos resultados para ese tema. Prueba con otras palabras o menos específicas.</p>
                    <p id="buscar-error" style="display:none; color:var(--danger); margin-top:20px;"><span class="icon icon-alert" aria-hidden="true"></span> Ocurrió un error al buscar. Intenta de nuevo en unos segundos.</p>

                    <div id="resultados-buscar-wrapper" style="display:none; margin-top:25px;">
                        <p style="font-size:1.1rem; margin-bottom:15px;">{{ t.resultados_para }} <strong id="resultados-para-tema" style="color:var(--primary-color);"></strong></p>

                        <div class="filtros-bar">
                            <button class="filtro-btn activo" data-filtro="todos" type="button">{{ t.filtro_todos }}</button>
                            <button class="filtro-btn" data-filtro="pdf" type="button"><span class="icon icon-download" aria-hidden="true"></span> {{ t.filtro_pdf }}</button>
                            <button class="filtro-btn" data-filtro="espanol" type="button">{{ t.filtro_espanol }}</button>
                            <button class="filtro-btn" data-filtro="reciente" type="button">{{ t.filtro_reciente }}</button>
                            <button class="filtro-btn" data-filtro="repositorio" type="button"><span class="icon icon-building" aria-hidden="true"></span> {{ t.filtro_repo }}</button>
                        </div>

                        <div class="seccion-resultados" id="seccion-pdf">
                            <h3><span class="icon icon-file" aria-hidden="true"></span> {{ t.seccion_pdf }}</h3>
                            <div class="grid-resultados" id="grid-pdf"></div>
                        </div>

                        <div class="seccion-resultados" id="seccion-academico">
                            <h3><span class="icon icon-globe" aria-hidden="true"></span> {{ t.seccion_academico }}</h3>
                            <div class="grid-resultados" id="grid-academico"></div>
                        </div>

                        <div class="seccion-resultados" id="seccion-repos">
                            <h3><span class="icon icon-building" aria-hidden="true"></span> {{ t.seccion_repos }}</h3>
                            <div class="grid-resultados" id="grid-repos"></div>
                        </div>

                        <div class="buscar-tambien" id="buscar-tambien-links"></div>
                    </div>

                    {% if tema_buscado %}
                    <script id="datos-papers" type="application/json">
                    {"tema": {{ tema_buscado|tojson }}, "papers": {{ sugerencias|tojson }}}
                    </script>
                    {% endif %}
                </div>
            </div>

            <div class="tab-content {% if tab_activa == 'extracto' %}active{% endif %}" id="tab-extracto">
                <div class="seccion glass">
                    <h2><span class="icon icon-doc-mini" aria-hidden="true"></span> {{ t.seccion3_titulo }}</h2>
                    <form method="POST">
                        <label for="extracto-textarea" class="sr-only">{{ t.placeholder_extracto }}</label>
                        <textarea id="extracto-textarea" name="extracto" rows="6" placeholder="{{ t.placeholder_extracto }}"></textarea>
                        <button type="submit" class="accion" name="accion" value="analizar_extracto">{{ t.boton_extracto }}</button>
                    </form>
                    {% if sugerencias_extracto %}
                        <div class="grid-resultados">
                        {% for paper in sugerencias_extracto %}
                            <div class="tarjeta">
                                <span class="badge-fuente">{{ paper.fuente or 'CrossRef' }}</span>
                                {% if paper.region %}<span class="badge-region">{{ paper.region }}</span>{% endif %}
                                <p><strong><a href="{{ paper.enlace }}" target="_blank" class="titulo-clickable">{{ paper.titulo }}</a></strong></p>
                                <p>{{ paper.autores }} ({{ paper.año }})</p>
                                <div class="enlaces-paper">
                                    <a class="enlace-web" href="{{ paper.enlace }}" target="_blank">{{ t.papel_pagina_web }}</a>
                                    {% if paper.pdf_gratis %}<a class="enlace-pdf" href="{{ paper.pdf_gratis }}" target="_blank"><span class="icon icon-download" aria-hidden="true"></span> {{ t.papel_pdf }}</a>{% endif %}
                                </div>
                            </div>
                        {% endfor %}
                        </div>
                    {% endif %}
                </div>
            </div>

            <div class="tab-content" id="tab-guia">
                <div class="seccion glass">
                    <h2><span class="icon icon-book" aria-hidden="true"></span> {{ t.guia_titulo }}</h2>
                    <details><summary><strong>{{ t.guia_texto_intext }}</strong></summary><p>Narrativa: García (2021)... Parentética: (García, 2021).</p></details>
                    <details><summary><strong>{{ t.guia_texto_revista }}</strong></summary><p>Apellido, A. A. (Año). Título. <em>Revista, volumen</em>(número), páginas. DOI</p></details>
                    <details><summary><strong>{{ t.guia_texto_libro }}</strong></summary><p>Apellido, A. A. (Año). <em>Título</em>. Editorial.</p></details>
                    <details><summary><strong>{{ t.guia_texto_tesis }}</strong></summary><p>Apellido, A. A. (Año). <em>Título</em> [Tesis, Universidad]. URL</p></details>
                    <details><summary><strong>{{ t.guia_texto_web }}</strong></summary><p>Autor. (Año). <em>Título</em>. Sitio. URL</p></details>
                    <details><summary><strong>{{ t.guia_texto_vancouver }}</strong></summary><p>(1) en texto · 1. Autor. Título. Revista. Año;Vol(Num):pág.</p></details>
                </div>
            </div>

            <div class="tab-content" id="tab-constructor">
                <div class="seccion glass">
                    <h2><span class="icon icon-grid" aria-hidden="true"></span> {{ t.constructor_titulo }}</h2>
                    <p>{{ t.constructor_desc }}</p>
                    <p class="puntaje" style="font-size: 1.2rem; color: var(--primary-color);">{{ t.texto_puntaje }}: <span id="puntaje-constructor">0 / 0</span></p>
                    <p><strong>{{ t.constructor_pool }}</strong></p>
                    <div id="piezas-pool" style="display:flex; flex-wrap:wrap; gap:10px; padding:15px; border:1px dashed var(--border-light); border-radius:12px; min-height:60px; margin-bottom:20px; background:rgba(0,0,0,0.2);" role="group" aria-label="Piezas disponibles para construir la cita"></div>
                    <p><strong>{{ t.constructor_zona }}</strong></p>
                    <div id="zona-respuesta" style="display:flex; flex-wrap:wrap; gap:10px; padding:15px; border:1px dashed var(--primary-color); border-radius:12px; min-height:60px; margin-bottom:20px; background:rgba(16,185,129,0.05);" role="group" aria-label="Tu cita en construcción"></div>
                    <button type="button" class="accion" id="btnVerificarConstructor">{{ t.constructor_verificar }}</button>
                    <button type="button" class="accion" id="btnSiguienteConstructor" style="background:#334155; color:white;">{{ t.constructor_siguiente }}</button>
                    <div id="resultado-constructor" style="margin-top:20px;" role="status" aria-live="polite"></div>
                </div>
            </div>

            <div class="tab-content" id="tab-biblio">
                <div class="seccion glass">
                    <h2><span class="icon icon-star" aria-hidden="true"></span> {{ t.biblio_titulo }}</h2>
                    <p>{{ t.biblio_desc }}</p>
                    <div id="biblio-container" style="margin-top: 20px;"></div>
                </div>
            </div>
        </main>
    </div>
    <script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2" nonce="{{ csp_nonce }}"></script>
    <script nonce="{{ csp_nonce }}">
    // === Supabase (persistencia opcional) ===
    var supabaseClient = (typeof supabase !== 'undefined')
    ? supabase.createClient(
        'https://sgifygynovprbpskomkl.supabase.co',
        'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InNnaWZ5Z3lub3ZwcmJwc2tvbWtsIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODY3NjE0NTcsImV4cCI6MjEwMjMzNzQ1N30.LmIdeV7N43FBI3Og8ul2VE4pe2NYQTiw8U6l3YhnoSc'
    )
    : null;
    // ===== ICONOS SVG con aria-hidden =====
    function iconoSvg(nombre) {
        return '<span class="icon icon-' + nombre + '" aria-hidden="true"></span>';
    }

    // ===== TOAST ACCESIBLE (reemplaza alert()) =====
    function mostrarToast(msg, tipo) {
        var t = document.getElementById('toast');
        if (t) {
            t.textContent = msg;
        }
        var toastVis = document.getElementById('toast-visual');
        if (toastVis) {
            toastVis.textContent = msg;
            toastVis.className = '';
            if (tipo === 'warning') toastVis.classList.add('warning');
            else if (tipo === 'error') toastVis.classList.add('error');
            toastVis.style.display = 'block';
            clearTimeout(toastVis._timer);
            toastVis._timer = setTimeout(function() {
                toastVis.style.display = 'none';
            }, 4000);
        }
    }

    // ===== NAVEGACIÓN =====
    function mostrarTab(nombre) {
        document.querySelectorAll('.tab-content').forEach(function(el) { el.classList.remove('active'); });
        document.querySelectorAll('.nav-boton').forEach(function(el) { el.classList.remove('activo'); });
        var el = document.getElementById('tab-' + nombre);
        if (el) el.classList.add('active');
        var nav = document.getElementById('nav-' + nombre);
        if (nav) nav.classList.add('activo');
        localStorage.setItem('tabActiva', nombre);
        if (nombre === 'biblio') cargarBibliografia();
        if (nombre === 'constructor' && !window.juegoIniciado) { cargarCitaJuego(0); window.juegoIniciado = true; }
    }

    document.querySelectorAll('.nav-boton').forEach(function(btn) {
        btn.addEventListener('click', function() {
            mostrarTab(btn.dataset.tab);
        });
    });

    var tabInicial = localStorage.getItem('tabActiva') || '{{ tab_activa }}';
    mostrarTab(tabInicial);

    // ===== MODAL PDF con foco y Escape =====
    var ultimoFoco = null;

    function previsualizarPdf(urlOriginal) {
        var modal = document.getElementById('modalPdf');
        var iframe = document.getElementById('iframePdf');
        var cargando = document.getElementById('modalCargando');
        var error = document.getElementById('modalError');
        var abrirExterno = document.getElementById('modalAbrirExterno');

        ultimoFoco = document.activeElement;

        modal.setAttribute('role', 'dialog');
        modal.setAttribute('aria-modal', 'true');
        modal.setAttribute('aria-label', 'Vista previa del documento PDF');

        abrirExterno.href = urlOriginal;
        error.classList.add('oculto');
        cargando.classList.remove('oculto');
        iframe.style.visibility = 'hidden';
        modal.classList.add('active');

        setTimeout(function() {
            document.getElementById('btnCerrarModal').focus();
        }, 100);

        var urlProxy = '/api/preview-pdf?url=' + encodeURIComponent(urlOriginal);

        fetch(urlProxy, { method: 'GET' }).then(function(resp) {
            if (!resp.ok) throw new Error('preview_failed');
            cargando.classList.add('oculto');
            iframe.src = urlProxy;
            iframe.style.visibility = 'visible';
        }).catch(function() {
            cargando.classList.add('oculto');
            error.classList.remove('oculto');
            document.getElementById('modalAbrirExterno').focus();
        });
    }

    function cerrarModal() {
        document.getElementById('modalPdf').classList.remove('active');
        document.getElementById('iframePdf').src = 'about:blank';
        if (ultimoFoco) {
            ultimoFoco.focus();
            ultimoFoco = null;
        }
    }

    document.getElementById('btnCerrarModal').addEventListener('click', cerrarModal);
    document.getElementById('modalPdf').addEventListener('click', function(e) {
        if (e.target === document.getElementById('modalPdf')) cerrarModal();
    });

    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape' && document.getElementById('modalPdf').classList.contains('active')) {
            cerrarModal();
        }
    });

    document.getElementById('modalPdf').addEventListener('keydown', function(e) {
    if (e.key !== 'Tab') return;
    var focoseables = this.querySelectorAll('button, a[href], iframe');
    if (!focoseables.length) return;
    var primero = focoseables[0];
    var ultimo = focoseables[focoseables.length - 1];
    if (e.shiftKey && document.activeElement === primero) {
        e.preventDefault();
        ultimo.focus();
    } else if (!e.shiftKey && document.activeElement === ultimo) {
        e.preventDefault();
        primero.focus();
    }
});

    // ===== LOADER del buscador con reduced-motion =====
    function iniciarLoader() {
        var prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        var canvas = document.getElementById('loaderCanvas');
        var container = document.getElementById('loaderContainer');
        if (!canvas || !container) return;

        if (prefersReducedMotion) {
            canvas.style.display = 'none';
            document.getElementById('loaderTexto').textContent = '{{ t.loader_buscando }}';
            return;
        }

        canvas.style.display = 'block';
        var ctx = canvas.getContext('2d');
        var W = container.offsetWidth;
        var H = 200;
        canvas.width = W; canvas.height = H;

        var particulas = [];
        for (var i = 0; i < 22; i++) {
            particulas.push({
                x: Math.random() * W, y: Math.random() * H,
                vx: (Math.random() - 0.5) * 1.2, vy: (Math.random() - 0.5) * 1.2,
                r: 2 + Math.random() * 3,
                alpha: 0.25 + Math.random() * 0.5
            });
        }

        var frases = [
            "Desempolvando repositorios...",
            "Consultando bases académicas...",
            "Conectando con universidades...",
            "Analizando metadatos...",
            "Buscando PDFs de acceso abierto...",
            "Verificando DOIs...",
            "Traduciendo títulos...",
            "Ordenando resultados..."
        ];
        var fraseIdx = 0;
        var intervaloFrase = setInterval(function() {
            var txt = document.getElementById('loaderTexto');
            if (!txt || !document.getElementById('loaderContainer').classList.contains('active')) {
                clearInterval(intervaloFrase);
                return;
            }
            fraseIdx = (fraseIdx + 1) % frases.length;
            txt.textContent = frases[fraseIdx];
        }, 2000);

        function distancia(a, b) { var dx = a.x-b.x, dy = a.y-b.y; return Math.sqrt(dx*dx+dy*dy); }

        function animar() {
            if (!document.getElementById('loaderContainer').classList.contains('active')) return;
            ctx.clearRect(0, 0, W, H);
            particulas.forEach(function(p) {
                p.x += p.vx; p.y += p.vy;
                if (p.x < 0 || p.x > W) p.vx *= -1;
                if (p.y < 0 || p.y > H) p.vy *= -1;
            });
            ctx.strokeStyle = 'rgba(16,185,129,0.15)';
            ctx.lineWidth = 1;
            for (var i = 0; i < particulas.length; i++) {
                for (var j = i + 1; j < particulas.length; j++) {
                    if (distancia(particulas[i], particulas[j]) < 90) {
                        ctx.beginPath();
                        ctx.moveTo(particulas[i].x, particulas[i].y);
                        ctx.lineTo(particulas[j].x, particulas[j].y);
                        ctx.stroke();
                    }
                }
            }
            particulas.forEach(function(p) {
                ctx.beginPath();
                ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
                ctx.fillStyle = 'rgba(16,185,129,' + p.alpha + ')';
                ctx.fill();
            });
            requestAnimationFrame(animar);
        }
        animar();
    }

    // ===== CREAR TARJETA DE RESULTADO =====
    function crearTarjetaResultado(p) {
        var div = document.createElement('div');
        div.className = 'tarjeta';
        div.dataset.pdf = p.pdf_gratis ? '1' : '0';
        div.dataset.espanol = p.en_espanol ? '1' : '0';
        div.dataset.anio = p.año || 0;
        div.dataset.repo = p.tipo === 'repositorio' ? '1' : '0';

        var titulo = escaparHtml(p.titulo || 'Sin título');
        var autores = escaparHtml(p.autores || 'Anon.');
        var anio = escaparHtml(p.año || 's.f.');
        var fuente = escaparHtml(p.fuente || 'Fuente');
        var region = p.region ? escaparHtml(p.region) : '';
        var enlace = (p.enlace && p.enlace !== '#') ? encodeURI(p.enlace) : '';
        var pdfUrl = p.pdf_gratis ? encodeURI(p.pdf_gratis) : '';

        var html = '<span class="badge-fuente">' + fuente + '</span>';
        if (region) html += '<span class="badge-region">' + region + '</span>';
        if (p.en_espanol) html += '<span class="badge-es">{{ t.papel_en_espanol }}</span>';
        if (p.pdf_gratis) html += '<span class="badge-pdf">' + iconoSvg('download') + ' PDF</span>';
        html += '<a href="' + (enlace || '#') + '" target="_blank" rel="noopener" class="titulo-clickable">' + titulo + '</a>';
        html += '<p>' + autores + ' (' + anio + ')</p>';
        html += '<div class="enlaces-paper">';
        if (enlace) html += '<a class="enlace-web" href="' + enlace + '" target="_blank" rel="noopener">' + iconoSvg('globe') + ' {{ t.papel_pagina_web }}</a>';
        if (pdfUrl) {
            html += '<a class="enlace-pdf" href="' + pdfUrl + '" target="_blank" rel="noopener">' + iconoSvg('download') + ' {{ t.papel_pdf }}</a>';
            html += '<button type="button" class="enlace-preview" data-pdf-url="' + encodeURIComponent(p.pdf_gratis) + '">' + iconoSvg('eye') + ' {{ t.papel_previsualizar }}</button>';
        }
        html += '<button type="button" class="guardar-btn" style="background: var(--warning); color: #0B1120; border: none; padding: 7px 14px; border-radius: 8px; font-size: 13px; font-weight: 600; cursor: pointer;" data-titulo="' + encodeURIComponent(p.titulo || '') + '" data-autores="' + encodeURIComponent(p.autores || '') + '" data-anio="' + (p.año || '') + '" data-doi="' + (p.doi || '') + '" data-enlace="' + encodeURIComponent(p.enlace || '') + '">' + iconoSvg('star') + ' {{ t.guardar_boton }}</button>';
        html += '</div>';
        html += '<details style="margin-top:15px; background:rgba(0,0,0,0.2); padding:12px; border-radius:8px; border:1px solid var(--border-light);">';
        html += '<summary style="cursor:pointer; color:var(--primary-color); font-weight:600; font-size:0.9rem;">' + iconoSvg('file') + ' {{ t.cita_ver_desplegable }}</summary>';
        html += '<div style="margin-top:10px; font-size:0.85rem;">';
        var formatos = [
            {id:'apa', label:'{{ t.cita_apa_label }}', val:p.cita_apa},
            {id:'vancouver', label:'{{ t.cita_vancouver_label }}', val:p.cita_vancouver},
            {id:'ieee', label:'{{ t.cita_ieee_label }}', val:p.cita_ieee},
            {id:'mla', label:'{{ t.cita_mla_label }}', val:p.cita_mla}
        ];
        formatos.forEach(function(fmt) {
            if (fmt.val) {
                var valEsc = escaparHtml(fmt.val);
                html += '<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;"><strong style="color:var(--primary-color);">' + fmt.label + '</strong>';
                html += '<button type="button" class="copiar-btn" data-cita="' + encodeURIComponent(fmt.val) + '" style="background:var(--primary-color); color:#0B1120; border:none; padding:4px 10px; border-radius:4px; font-size:0.75rem; font-weight:bold; cursor:pointer; display:inline-flex; align-items:center; gap:4px;">' + iconoSvg('copy') + ' {{ t.copiar_boton }}</button></div>';
                html += '<p style="margin:0 0 10px 0; word-break:break-word; opacity:0.9; border-left:2px solid var(--border-light); padding-left:8px;">' + valEsc + '</p>';
            }
        });
        html += '</div></details>';
        div.innerHTML = html;
        return div;
    }

    function escaparHtml(texto) {
        if (texto === null || texto === undefined) return '';
        var div = document.createElement('div');
        div.textContent = String(texto);
        return div.innerHTML;
    }

    function actualizarBuscarTambien(tema) {
        var cont = document.getElementById('buscar-tambien-links');
        var q = encodeURIComponent(tema);
        cont.innerHTML = '<span>{{ t.buscar_tambien }}:</span>' +
            '<a href="https://scholar.google.com/scholar?q=' + q + '" target="_blank" rel="noopener">Google Scholar</a>' +
            '<a href="https://www.semanticscholar.org/search?q=' + q + '" target="_blank" rel="noopener">Semantic Scholar</a>' +
            '<a href="https://pubmed.ncbi.nlm.nih.gov/?term=' + q + '" target="_blank" rel="noopener">PubMed</a>' +
            '<a href="https://core.ac.uk/search?q=' + q + '" target="_blank" rel="noopener">CORE</a>' +
            '<a href="https://www.researchgate.net/search/publication?q=' + q + '" target="_blank" rel="noopener">ResearchGate</a>';
    }

    function renderizarResultadosBusqueda(tema, papers) {
        var gridPdf = document.getElementById('grid-pdf');
        var gridAcad = document.getElementById('grid-academico');
        var gridRepos = document.getElementById('grid-repos');
        var seccionPdf = document.getElementById('seccion-pdf');
        var seccionAcad = document.getElementById('seccion-academico');
        var seccionRepos = document.getElementById('seccion-repos');

        gridPdf.innerHTML = ''; gridAcad.innerHTML = ''; gridRepos.innerHTML = '';
        document.getElementById('resultados-para-tema').textContent = '"' + tema + '"';

        papers.forEach(function(p) {
            var card = crearTarjetaResultado(p);
            if (p.pdf_gratis) {
                gridPdf.appendChild(card);
            } else if (p.tipo === 'repositorio') {
                gridRepos.appendChild(card);
            } else {
                gridAcad.appendChild(card);
            }
        });

        seccionPdf.style.display = gridPdf.children.length ? 'block' : 'none';
        seccionAcad.style.display = gridAcad.children.length ? 'block' : 'none';
        seccionRepos.style.display = gridRepos.children.length ? 'block' : 'none';

        actualizarBuscarTambien(tema);

        document.querySelectorAll('.filtro-btn').forEach(function(b) { b.classList.remove('activo'); });
        var btnTodos = document.querySelector('.filtro-btn[data-filtro="todos"]');
        if (btnTodos) btnTodos.classList.add('activo');

        window.filtrarResultados = function(tipo, btn) {
            document.querySelectorAll('.filtro-btn').forEach(function(b) { b.classList.remove('activo'); });
            btn.classList.add('activo');
            var cards = document.querySelectorAll('.tarjeta');
            var anioLimite = new Date().getFullYear() - 5;
            cards.forEach(function(card) {
                var mostrar = true;
                if (tipo === 'pdf' && card.dataset.pdf !== '1') mostrar = false;
                if (tipo === 'espanol' && card.dataset.espanol !== '1') mostrar = false;
                if (tipo === 'reciente') {
                    var a = parseInt(card.dataset.anio);
                    if (!a || a < anioLimite) mostrar = false;
                }
                if (tipo === 'repositorio' && card.dataset.repo !== '1') mostrar = false;
                card.style.display = mostrar ? 'block' : 'none';
            });
            ['grid-pdf','grid-academico','grid-repos'].forEach(function(id, i) {
                var g = document.getElementById(id);
                var vis = Array.from(g.children).some(function(c) { return c.style.display !== 'none'; });
                var secs = [seccionPdf, seccionAcad, seccionRepos];
                secs[i].style.display = vis ? 'block' : 'none';
            });
        };

        document.getElementById('resultados-buscar-wrapper').style.display = 'block';
        document.getElementById('buscar-sin-resultados').style.display = 'none';

        // Anuncio accesible
        var anuncio = document.getElementById('anuncio-resultados');
        if (anuncio) {
            anuncio.textContent = papers.length + ' resultados encontrados para ' + tema;
        }
    }

    function mostrarLoaderBusqueda() {
        document.getElementById('resultados-buscar-wrapper').style.display = 'none';
        document.getElementById('buscar-sin-resultados').style.display = 'none';
        document.getElementById('buscar-error').style.display = 'none';
        document.getElementById('loaderContainer').classList.add('active');
        document.getElementById('skeleton-container').style.display = 'grid';
        iniciarLoader();
    }

    function ocultarLoaderBusqueda() {
        document.getElementById('loaderContainer').classList.remove('active');
        document.getElementById('skeleton-container').style.display = 'none';
    }

    // ===== BÚSQUEDA AJAX =====
    var buscarForm = document.getElementById('buscar-form');
    if (buscarForm) {
        buscarForm.addEventListener('submit', function(e) {
            e.preventDefault();
            var tema = document.getElementById('search-input').value.trim();
            if (!tema) return;
            var btn = document.getElementById('btn-buscar-submit');
            btn.disabled = true;
            mostrarLoaderBusqueda();
            fetch('/api/buscar', {
                method: 'POST',
                headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                body: 'tema=' + encodeURIComponent(tema)
            }).then(function(resp) {
                if (!resp.ok) throw new Error('HTTP ' + resp.status);
                return resp.json();
            }).then(function(data) {
                ocultarLoaderBusqueda();
                btn.disabled = false;
                if (!data.papers || data.papers.length === 0) {
                    document.getElementById('buscar-sin-resultados').style.display = 'block';
                    return;
                }
                renderizarResultadosBusqueda(data.tema, data.papers);
            }).catch(function(err) {
                console.error('Error en la búsqueda:', err);
                ocultarLoaderBusqueda();
                btn.disabled = false;
                document.getElementById('buscar-error').style.display = 'block';
                mostrarToast('Error al buscar: ' + err.message, 'error');
            });
        });
    }

    // Carga inicial de resultados
    (function() {
        var datosEl = document.getElementById('datos-papers');
        if (!datosEl) return;
        try {
            var inicial = JSON.parse(datosEl.textContent);
            ocultarLoaderBusqueda();
            if (inicial.papers && inicial.papers.length) {
                renderizarResultadosBusqueda(inicial.tema, inicial.papers);
            } else {
                document.getElementById('buscar-sin-resultados').style.display = 'block';
            }
        } catch (err) {
            console.error('Error al cargar resultados iniciales:', err);
        }
    })();

    // ===== AUTOCOMPLETE =====
    var searchInput = document.getElementById('search-input');
    var suggestionsBox = document.getElementById('suggestions-dropdown');

    function debounce(fn, delay) {
        var timer;
        return function () {
            var args = arguments;
            clearTimeout(timer);
            timer = setTimeout(function() { fn.apply(null, args); }, delay);
        };
    }

    async function fetchSugerencias(query) {
        try {
            var resp = await fetch('/api/suggest-tema?tema=' + encodeURIComponent(query));
            if (!resp.ok) return [];
            return await resp.json();
        } catch (e) { return []; }
    }

    function mostrarSugerencias(lista) {
        suggestionsBox.innerHTML = '';
        if (lista.length === 0) { suggestionsBox.style.display = 'none'; return; }
        suggestionsBox.innerHTML = lista.map(function(item) {
            var anio = item.año ? ' (' + escaparHtml(item.año) + ')' : '';
            return '<div class="sugerencia-item" data-valor="' + escaparHtml(item.titulo) + '" role="option">' + iconoSvg('doc-mini') + '<span>' + escaparHtml(item.titulo) + anio + '</span></div>';
        }).join('');
        suggestionsBox.style.display = 'block';
        document.querySelectorAll('.sugerencia-item').forEach(function(el) {
            el.addEventListener('click', function() {
                searchInput.value = el.dataset.valor;
                suggestionsBox.style.display = 'none';
            });
        });
    }

    if (searchInput) {
        var debouncedInput = debounce(async function() {
            var q = searchInput.value.trim();
            if (q.length < 3) { suggestionsBox.style.display = 'none'; return; }
            mostrarSugerencias(await fetchSugerencias(q));
        }, 300);
        searchInput.addEventListener('input', debouncedInput);
        document.addEventListener('click', function(e) {
            if (!searchInput.contains(e.target) && !suggestionsBox.contains(e.target)) {
                suggestionsBox.style.display = 'none';
            }
        });
    }

    if (searchInput) {
    var indiceActivo = -1;
    searchInput.addEventListener('keydown', function(e) {
        var items = suggestionsBox.querySelectorAll('.sugerencia-item');
        if (!items.length || suggestionsBox.style.display === 'none') return;
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            indiceActivo = Math.min(indiceActivo + 1, items.length - 1);
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            indiceActivo = Math.max(indiceActivo - 1, 0);
        } else if (e.key === 'Enter' && indiceActivo >= 0) {
            e.preventDefault();
            searchInput.value = items[indiceActivo].dataset.valor;
            suggestionsBox.style.display = 'none';
            indiceActivo = -1;
            return;
        } else if (e.key === 'Escape') {
            suggestionsBox.style.display = 'none';
            indiceActivo = -1;
            return;
        } else {
            return;
        }
        items.forEach(function(it, i) {
            it.style.background = i === indiceActivo ? 'rgba(16,185,129,0.15)' : '';
            it.setAttribute('aria-selected', i === indiceActivo ? 'true' : 'false');
        });
        items[indiceActivo].scrollIntoView({ block: 'nearest' });
    });
}

    // ===== UTILIDADES =====
    function usarCitaCorregida(original, corregida) {
        var textarea = document.querySelector('textarea[name="cita"]');
        if (!textarea) return;
        var lineas = textarea.value.split('\n');
        var nuevas = lineas.map(function(l) { return l.trim() === original ? corregida : l; });
        textarea.value = nuevas.join('\n');
        textarea.focus();
    }

    function copiarTexto(btn, texto) {
        navigator.clipboard.writeText(texto).then(function() {
            var originalHtml = btn.innerHTML;
            btn.innerHTML = iconoSvg('check');
            mostrarToast('Texto copiado al portapapeles', 'success');
            setTimeout(function() { btn.innerHTML = originalHtml; }, 2000);
        });
    }

    function generarCitaJS(paper, formato) {
        var autores = paper.autores || "Anon.";
        var anio = paper.año || "s.f.";
        var titulo = paper.titulo || "Sin título";
        var doi = paper.doi || "";
        var enlace = paper.enlace || "";
        if (formato === "apa") {
            return doi && doi !== "sin DOI" ? autores + " (" + anio + "). " + titulo + ". https://doi.org/" + doi : autores + " (" + anio + "). " + titulo + ". " + enlace;
        } else if (formato === "vancouver") {
            return doi && doi !== "sin DOI" ? autores + ". " + titulo + ". " + anio + ". doi:" + doi : autores + ". " + titulo + ". " + anio + ". Disponible en: " + enlace;
        } else if (formato === "ieee") {
            return doi && doi !== "sin DOI" ? autores + ', "' + titulo + '," ' + anio + ". doi: " + doi + "." : autores + ', "' + titulo + '," ' + anio + ". [Online]. Available: " + enlace;
        } else if (formato === "mla") {
            return doi && doi !== "sin DOI" ? autores + '. "' + titulo + '." (' + anio + "). doi:" + doi + "." : autores + '. "' + titulo + '." ' + anio + ", " + enlace + ".";
        }
        return "";
    }

    function guardarReferencia(btn, paper) {
        var refs = JSON.parse(localStorage.getItem('userBiblio') || '[]');
        if (refs.some(function(r) { return r.titulo === paper.titulo; })) {
            mostrarToast('Esta referencia ya está en tu bibliografía.', 'warning');
            return;
        }
        refs.push(paper);
        localStorage.setItem('userBiblio', JSON.stringify(refs));
        btn.style.background = '#10B981';
        btn.innerHTML = iconoSvg('check') + ' Guardado';
        mostrarToast('Referencia guardada correctamente.', 'success');
        setTimeout(function() {
            btn.style.background = '#F59E0B';
            btn.innerHTML = iconoSvg('star') + ' {{ t.guardar_boton }}';
        }, 2000);
    }

    function cargarBibliografia() {
        var container = document.getElementById('biblio-container');
        if (!container) return;
        var refs = JSON.parse(localStorage.getItem('userBiblio') || '[]');
        if (refs.length === 0) {
            container.innerHTML = "<p style='opacity: 0.7; text-align: center; padding: 20px;'>{{ t.biblio_vacia }}</p>";
            return;
        }
        var formatos = [
            { id: 'apa', nombre: '{{ t.cita_apa_label }}' },
            { id: 'vancouver', nombre: '{{ t.cita_vancouver_label }}' },
            { id: 'ieee', nombre: '{{ t.cita_ieee_label }}' },
            { id: 'mla', nombre: '{{ t.cita_mla_label }}' }
        ];
        var html = '<div style="display: grid; gap: 20px;">';
        formatos.forEach(function(fmt) {
            html += '<div class="tarjeta" style="background: rgba(255,255,255,0.05);">';
            html += '<div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px; border-bottom: 1px solid var(--border-light); padding-bottom: 10px;">';
            html += '<h3 style="margin: 0; color: var(--primary-color);">' + fmt.nombre + '</h3>';
            html += '<button type="button" class="copiar-todo-btn" data-formato="' + fmt.id + '" style="background: var(--primary-color); color: #0B1120; border: none; padding: 8px 15px; border-radius: 6px; font-weight: bold; cursor: pointer; display:inline-flex; align-items:center; gap:6px;">' + iconoSvg('copy') + ' {{ t.copiar_todo }}</button>';
            html += '</div>';
            refs.forEach(function(paper, index) {
                var cita = escaparHtml(generarCitaJS(paper, fmt.id));
                html += '<p style="margin: 0 0 12px 0; word-break: break-word; opacity: 0.9; border-left: 2px solid var(--border-light); padding-left: 10px;">' + (index + 1) + '. ' + cita + '</p>';
            });
            html += '</div>';
        });
        html += '</div>';
        container.innerHTML = html;
    }

    function copiarTodo(formato) {
        var refs = JSON.parse(localStorage.getItem('userBiblio') || '[]');
        var texto = refs.map(function(paper, index) {
            return (index + 1) + ". " + generarCitaJS(paper, formato);
        }).join('\n\n');
        navigator.clipboard.writeText(texto).then(function() {
            mostrarToast('Bibliografía copiada al portapapeles!', 'success');
        });
    }

    // ===== CONSTRUCTOR DE CITAS =====
    var citasJuego = [
        { piezas: ["García, J. A.", "(2021).", "Efecto de la temperatura en la oxidación de aceites vegetales.", "Revista de Ciencia de los Alimentos,", "15(3),", "45-58."] },
        { piezas: ["López, M., & Pérez, R.", "(2019).", "Análisis del índice de peróxidos en aceite de oliva.", "Grasas y Aceites,", "70(2),", "112-120."] },
        { piezas: ["UNESCO.", "(2023).", "Alfabetización mediática e informacional para la era digital.", "Ediciones UNESCO,", "1(1),", "10-25."] },
        { piezas: ["Smith, J.", "(2022).", "Impacto de la inteligencia artificial en la educación superior.", "Journal of EdTech,", "8(4),", "112-128."] },
        { piezas: ["Chen, L.", "(2020).", "Desarrollo de vacunas de ARNm frente a pandemias globales.", "Nature Medicine,", "26(5),", "450-460."] },
        { piezas: ["Torres, A.", "(2018).", "Sostenibilidad y energías renovables en zonas rurales.", "Revista de Ingeniería Ambiental,", "12(1),", "33-41."] }
    ];
    var citaActual = 0;
    var piezasDisponibles = [];
    var respuestaUsuario = [];

    function barajar(arr) {
        var c = arr.slice();
        for (var i = c.length - 1; i > 0; i--) {
            var j = Math.floor(Math.random() * (i + 1));
            var t = c[i]; c[i] = c[j]; c[j] = t;
        }
        return c;
    }

    function cargarCitaJuego(i) {
        citaActual = i;
        respuestaUsuario = [];
        piezasDisponibles = barajar(citasJuego[i].piezas);
        dibujarJuego();
        document.getElementById('resultado-constructor').innerHTML = '';
    }

    function siguienteCitaJuego() { cargarCitaJuego((citaActual + 1) % citasJuego.length); }

    function dibujarJuego() {
        var pool = document.getElementById('piezas-pool');
        var zona = document.getElementById('zona-respuesta');
        pool.innerHTML = ''; zona.innerHTML = '';

        piezasDisponibles.forEach(function(p, idx) {
            var b = document.createElement('button');
            b.type = 'button';
            b.className = 'pieza-bloque';
            b.textContent = p;
            b.setAttribute('aria-label', 'Colocar pieza: ' + p);
            b.style.cssText = 'background:#334155; color:white; border:1px solid #475569; padding:10px 18px; border-radius:6px; cursor:pointer; font-size:14px; font-weight:500; transition:background 0.2s;';
            b.onmouseenter = function() { b.style.background = '#475569'; };
            b.onmouseleave = function() { b.style.background = '#334155'; };
            b.onclick = function() { respuestaUsuario.push(p); piezasDisponibles.splice(idx, 1); dibujarJuego(); };
            pool.appendChild(b);
        });

        respuestaUsuario.forEach(function(p, idx) {
            var b = document.createElement('button');
            b.type = 'button';
            b.className = 'pieza-bloque';
            b.textContent = p;
            b.setAttribute('aria-label', 'Quitar pieza: ' + p);
            b.style.cssText = 'background:var(--primary-color); color:#0B1120; border:1px solid var(--primary-color); padding:10px 18px; border-radius:6px; cursor:pointer; font-size:14px; font-weight:600; transition:transform 0.2s;';
            b.onclick = function() { piezasDisponibles.push(p); respuestaUsuario.splice(idx, 1); dibujarJuego(); };
            zona.appendChild(b);
        });
    }

    function verificarConstructor() {
        var c = citasJuego[citaActual].piezas;
        var a = 0;
        var h = '<p><strong>Resultado:</strong></p><p style="line-height:2;">';
        for (var i = 0; i < c.length; i++) {
            var ok = respuestaUsuario[i] === c[i];
            if (ok) a++;
            h += '<span style="display:inline-block; margin:2px; padding:4px 8px; border-radius:4px; color:' + (ok ? '#0B1120' : '#fff') + '; background:' + (ok ? 'var(--primary-color)' : 'var(--danger)') + '; font-weight:bold;">' + (respuestaUsuario[i] || '___') + '</span> ';
        }
        h += '</p>';
        var tot = parseInt(localStorage.getItem('constructorTotal') || '0');
        var aci = parseInt(localStorage.getItem('constructorAciertos') || '0');
        localStorage.setItem('constructorTotal', tot + 1);
        if (a === c.length) {
            localStorage.setItem('constructorAciertos', aci + 1);
            h += '<p style="color:var(--primary-color); font-weight:bold; font-size:1.1rem;">{{ t.constructor_perfecto }}</p>';
            mostrarToast('¡Cita perfecta!', 'success');
        } else {
            h += '<p style="color:var(--text-muted);">' + a + ' / ' + c.length + ' correctas</p>';
        }
        document.getElementById('resultado-constructor').innerHTML = h;
        document.getElementById('puntaje-constructor').textContent = localStorage.getItem('constructorAciertos') + ' / ' + localStorage.getItem('constructorTotal');
    }

    document.getElementById('btnVerificarConstructor').addEventListener('click', verificarConstructor);
    document.getElementById('btnSiguienteConstructor').addEventListener('click', siguienteCitaJuego);

    // ===== EVENTOS GLOBALES =====
    document.addEventListener('click', function(e) {
        var target = e.target.closest ? e.target.closest('.corregir-btn, .copiar-btn, .guardar-btn, .enlace-preview, .copiar-todo-btn, .filtro-btn') : null;
        if (!target) return;
        if (target.classList.contains('corregir-btn')) {
            var original = target.getAttribute('data-original');
            var corregida = target.getAttribute('data-corregida');
            usarCitaCorregida(original, corregida);
        }
        if (target.classList.contains('copiar-btn')) {
            var cita = decodeURIComponent(target.getAttribute('data-cita'));
            copiarTexto(target, cita);
        }
        if (target.classList.contains('guardar-btn')) {
            var paper = {
                titulo: decodeURIComponent(target.getAttribute('data-titulo')),
                autores: decodeURIComponent(target.getAttribute('data-autores')),
                año: target.getAttribute('data-anio'),
                doi: target.getAttribute('data-doi'),
                enlace: decodeURIComponent(target.getAttribute('data-enlace'))
            };
            guardarReferencia(target, paper);
        }
        if (target.classList.contains('enlace-preview')) {
            var url = decodeURIComponent(target.getAttribute('data-pdf-url'));
            previsualizarPdf(url);
        }
        if (target.classList.contains('copiar-todo-btn')) {
            copiarTodo(target.getAttribute('data-formato'));
        }
        if (target.classList.contains('filtro-btn')) {
            filtrar(target.getAttribute('data-filtro'), target);
        }
    });

    function filtrar(tipo, btn) {
        if (window.filtrarResultados) window.filtrarResultados(tipo, btn);
    }

    // Efecto de seguimiento de mouse en botones de nav
    document.querySelectorAll('.nav-boton').forEach(function(btn) {
        btn.addEventListener('mousemove', function(e) {
            var r = btn.getBoundingClientRect();
            btn.style.setProperty('--mx', ((e.clientX - r.left) / r.width * 100) + '%');
            btn.style.setProperty('--my', ((e.clientY - r.top) / r.height * 100) + '%');
        });
    });

    </script>
</body>
</html>
"""

if __name__ == "__main__":
    if os.environ.get("RENDER") or os.environ.get("VERCEL"):
        CONFIG["modo_debug"] = False
    app.run(host="0.0.0.0", port=CONFIG["puerto"], debug=CONFIG["modo_debug"])