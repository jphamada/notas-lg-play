import os
import re
import datetime
import requests
import feedparser
from bs4 import BeautifulSoup
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.proxies import WebshareProxyConfig, GenericProxyConfig
from google import genai
from dotenv import load_dotenv

# Cargar variables de entorno desde el archivo .env
load_dotenv()

# ==========================================
# CONFIGURACIÓN
# ==========================================
CHANNEL_HANDLE = "@lagacetadetucuman"
CHANNEL_ID = os.environ.get("CHANNEL_ID", "UCowbI8idnvQTl2sFxkOvnKw")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

client = genai.Client(api_key=GEMINI_API_KEY)

SYSTEM_PROMPT = """Sos un redactor periodístico profesional de agencia de noticias. Tu tarea es convertir transcripciones o contenidos de videos de YouTube en notas periodísticas rigurosas, precisas y listas para publicación interna.

REGLAS DE TRABAJO:
1. Basate estrictamente en los hechos, datos y declaraciones presentes en el contenido proporcionado.
2. Está terminantemente prohibido inventar, inferir intenciones no explícitas, extrapolar o rellenar con información externa. Si un dato no figura en el video, no existe.
3. Estructura de pirámide invertida: lo más relevante y noticioso debe ir al inicio.
4. Tono informativo neutro, formal y objetivo.
5. Incluí citas textuales entrecomilladas atribuidas con precisión al interlocutor correspondiente. Referenciá el momento aproximado de la cita usando marcas de tiempo (ej. [03:45]).
6. Extensión obligatoria del cuerpo de la noticia: aproximadamente 500 palabras.
7. Formato de salida: TEXTO PLANO sin etiquetas HTML ni Markdown complejo (no uses asteriscos dobles, numerales ni bloques de código).

FORMATO EXACTO DE RESPUESTA:

TITULO:
[Titular informativo, conciso y de alto impacto periodístico]

SUMARIO:
[Bajada/copete de 2 a 3 oraciones que sintetice el qué, quién, cuándo y dónde]

CUERPO:
[Desarrollo completo de la noticia organizado en párrafos claros. Incluye el contexto inmediato, desarrollo del hecho y declaraciones textuales entrecomilladas con su marca temporal. Extensión cercana a 500 palabras]"""

def obtener_channel_id(handle):
    """Extrae el ID nativo UC... a partir del @handle de YouTube"""
    url = f"https://www.youtube.com/{handle}"
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"})
    match = re.search(r'channel_id=([a-zA-Z0-9_-]+)', r.text)
    if match:
        return match.group(1)
    
    # Búsqueda alternativa en metatags
    soup = BeautifulSoup(r.text, 'html.parser')
    meta = soup.find('meta', itemprop='identifier')
    if meta:
        return meta['content']
    raise ValueError(f"No se pudo resolver el Channel ID para {handle}")

def obtener_ultimos_videos(channel_id, limite=15):
    """Lee los últimos videos del canal ordenados del más reciente al más antiguo"""
    rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    feed = feedparser.parse(rss_url)
    return [
        {"id": entry.yt_videoid, "titulo": entry.title, "link": entry.link}
        for entry in feed.entries[:limite]
    ]

def obtener_ytt_api():
    """Inicializa la API de transcripción configurando proxies si están presentes"""
    webshare_user = os.environ.get("WEBSHARE_USER")
    webshare_pass = os.environ.get("WEBSHARE_PASSWORD")
    proxy_url = os.environ.get("PROXY_URL")

    if webshare_user and webshare_pass:
        print("[i] Usando proxy Webshare para evitar bloqueo de IP...")
        proxy_config = WebshareProxyConfig(
            proxy_username=webshare_user,
            proxy_password=webshare_pass
        )
        return YouTubeTranscriptApi(proxy_config=proxy_config)
    elif proxy_url:
        print("[i] Usando proxy genérico...")
        proxy_config = GenericProxyConfig(http_url=proxy_url, https_url=proxy_url)
        return YouTubeTranscriptApi(proxy_config=proxy_config)
    else:
        return YouTubeTranscriptApi()

def procesar_video(video_id, titulo):
    """Descarga transcripción, filtra por duración (<10 min) y genera la nota"""
    try:
        ytt = obtener_ytt_api()
        if hasattr(ytt, 'fetch'):
            transcript_list = ytt.fetch(video_id, languages=['es', 'es-419'])
        else:
            transcript_list = ytt.get_transcript(video_id, languages=['es', 'es-419'])
    except Exception as e:
        print(f"[-] Sin transcripción disponible para '{titulo}' ({e})")
        return None, "sin_transcripcion"

    if not transcript_list:
        print(f"[-] Transcripción vacía para '{titulo}'")
        return None, "sin_transcripcion"

    ultimo_fragmento = transcript_list[-1]
    start_fin = getattr(ultimo_fragmento, 'start', None) if hasattr(ultimo_fragmento, 'start') else ultimo_fragmento.get('start', 0)
    duration_fin = getattr(ultimo_fragmento, 'duration', None) if hasattr(ultimo_fragmento, 'duration') else ultimo_fragmento.get('duration', 0)
    duracion_segundos = start_fin + duration_fin
    
    # Filtro estricto: máximo 10 minutos (600 segundos)
    if duracion_segundos > 600:
        print(f"[i] Ignorado por duración: '{titulo}' ({int(duracion_segundos // 60)}m {int(duracion_segundos % 60)}s)")
        return None, "excede_duracion"

    # Formatear transcripción con timecodes
    lineas_formateadas = []
    for item in transcript_list:
        t_start = getattr(item, 'start', None) if hasattr(item, 'start') else item.get('start', 0)
        t_text = getattr(item, 'text', '') if hasattr(item, 'text') else item.get('text', '')
        lineas_formateadas.append(f"[{int(t_start // 60):02d}:{int(t_start % 60):02d}] {t_text}")

    texto_con_tiempos = "\n".join(lineas_formateadas)

    user_content = f"TÍTULO DEL VIDEO: {titulo}\nTRANSCRIPCIÓN:\n{texto_con_tiempos}"

    # Llamada al SDK oficial de Gemini con captura de errores temporales (ej. 503 / cuota)
    try:
        response = client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=user_content,
            config=genai.types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.2, # Baja temperatura para máxima fidelidad fáctica
            )
        )
        return response.text, "ok"
    except Exception as e:
        print(f"[-] Error al generar nota con Gemini ({e})")
        return None, "error_gemini"

ULTIMO_VIDEO_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ultimo_video.txt")

def leer_videos_procesados():
    """Lee el registro de videos ya procesados desde ultimo_video.txt"""
    if not os.path.exists(ULTIMO_VIDEO_FILE):
        return set()
    with open(ULTIMO_VIDEO_FILE, "r", encoding="utf-8") as f:
        return set(linea.strip() for linea in f if linea.strip())

def marcar_video_procesado(video_id):
    """Guarda el nuevo ID en el historial de ultimo_video.txt manteniendo los últimos 30 videos"""
    video_id = video_id.strip()
    if not video_id:
        return

    ids_existentes = []
    if os.path.exists(ULTIMO_VIDEO_FILE):
        with open(ULTIMO_VIDEO_FILE, "r", encoding="utf-8") as f:
            for linea in f:
                v = linea.strip()
                if v and v not in ids_existentes:
                    ids_existentes.append(v)

    if video_id in ids_existentes:
        ids_existentes.remove(video_id)
    ids_existentes.append(video_id)

    # Conservar los últimos 30 videos procesados
    ultimos = ids_existentes[-30:]
    with open(ULTIMO_VIDEO_FILE, "w", encoding="utf-8") as f:
        for vid in ultimos:
            f.write(f"{vid}\n")

def enviar_a_slack(texto):
    r = requests.post(SLACK_WEBHOOK_URL, json={"text": texto})
    if r.status_code != 200:
        print(f"[-] Error enviando a Slack: {r.status_code} - {r.text}")
        return False
    return True

# ==========================================
# EJECUCIÓN
# ==========================================
if __name__ == "__main__":
    if CHANNEL_ID:
        channel_id = CHANNEL_ID
        print(f"[+] Usando Channel ID configurado: {channel_id}")
    else:
        print("[+] Obteniendo ID del canal...")
        channel_id = obtener_channel_id(CHANNEL_HANDLE)
        print(f"[+] Channel ID detectado: {channel_id}")

    videos = obtener_ultimos_videos(channel_id, limite=15)
    if not videos:
        print("[-] No se encontraron videos en el canal.")
    else:
        procesados = leer_videos_procesados()
        video_enviado = False

        for v in videos:
            v_id = v["id"]
            v_titulo = v["titulo"]

            # 1. Si ya se procesó o evaluó previamente, ignorar
            if v_id in procesados:
                continue

            print(f"\n[+] Verificando video: '{v_titulo}' ({v['link']})")
            nota, estado = procesar_video(v_id, v_titulo)

            # 2. Si tiene transcripción y dura <= 10 min: enviar a Slack y terminar
            if estado == "ok" and nota:
                mensaje_slack = f"{nota}\n\nEnlace al video original: {v['link']}"
                if enviar_a_slack(mensaje_slack):
                    marcar_video_procesado(v_id)
                    print("[OK] Nota generada y enviada a Slack exitosamente.")
                    video_enviado = True
                    break

            # 3. Si supera los 10 minutos: descartar permanentemente y seguir con el anterior
            elif estado == "excede_duracion":
                marcar_video_procesado(v_id)
                print("[i] Supera los 10 min de duración. Marcado como evaluado. Buscando video anterior...")

            # 4. Si aún no tiene subtítulos disponibles: buscar el anterior sin marcar este en procesados
            elif estado == "sin_transcripcion":
                print("[i] Sin subtítulos listos todavía. Buscando video anterior...")

        if not video_enviado:
            print("\n[i] No hubo videos nuevos listos para procesar en esta ejecución.")