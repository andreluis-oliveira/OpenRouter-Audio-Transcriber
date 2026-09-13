import sys
import os
import re
import time
import random
import shutil
import subprocess
import tempfile
from openai import OpenAI, RateLimitError, APIStatusError, APIConnectionError

if len(sys.argv) < 2:
    print("Uso: python transcrever_openrouter_websec.py <audio.mp3> [--manter-termos-tecnicos]")
    sys.exit(1)

audio_path = sys.argv[1]
manter_termos = "--manter-termos-tecnicos" in sys.argv or True  # Ativado por padrão para WebSec

api_key = os.environ.get("OPENROUTER_API_KEY")
if not api_key:
    print("Erro: defina OPENROUTER_API_KEY nas variáveis de ambiente.")
    sys.exit(1)

if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
    print("Erro: precisa de ffmpeg e ffprobe instalados e no PATH (usados pra fatiar o áudio).")
    sys.exit(1)

client = OpenAI(
    api_key=api_key,
    base_url="https://openrouter.ai/api/v1",
    timeout=90.0,
    default_headers={
        "HTTP-Referer": "https://github.com/andreluis-oliveira/OpenRouter-Audio-Transcriber",
        "X-Title": "OpenRouter Audio Transcriber",
    },
)

# --- Configuração ---
# qwen/qwen3-asr-1.7b ou microsoft/mai-transcribe-2
MODELO_TRANSCRICAO = "qwen/qwen3-asr-1.7b"
# Chunks curtos (45s): garante que qualquer divisão proporcional tenha no máximo segundos de drift,
# sem acumular descompasso e sem gerar parágrafos gigantes na tela.
DURACAO_CHUNK_SEGUNDOS = 45

# Limites de formatação padrão para legendas SRT de vídeo:
MAX_CHARS_POR_LEGENDA = 80  # máx ~2 linhas legíveis na tela
MAX_SEG_DURACAO = 7.0       # máx 7 segundos exibidos por legenda

MODELO_TRADUCAO = "meta/muse-spark-1.3-contributor"
TAMANHO_LOTE = 25          
DELAY_ENTRE_LOTES = 0.4    
MAX_TENTATIVAS = 5
BACKOFF_BASE = 2.0
MAX_RETENTATIVAS_QA = 2

# ============================================================
# GLOSSÁRIO E REGRAS (tradução)
# ============================================================
GLOSSARIO_WEBSEC = (
    "GLOSSÁRIO — NÃO traduza os termos abaixo (mantenha em inglês, por "
    "serem de uso consagrado em cursos/certificações de segurança "
    "ofensiva no Brasil, como OffSec WEB-300/OSWE, eWPT, eWPTX, eCPPT, "
    "PNPT e labs da PortSwigger Web Security Academy):\n"
    "- Vulnerabilidades/técnicas: XSS, SQL Injection, SSRF, XXE, CSRF, "
    "Server-Side Template Injection (SSTI), Prototype Pollution, "
    "Deserialization, Insecure Deserialization, Buffer Overflow, "
    "Sandbox Escape, Path Traversal, Race Condition, Privilege Escalation\n"
    "- Exploração: exploit, payload, shell, reverse shell, bind shell, "
    "webshell, backdoor, gadget chain, Proof of Concept (PoC), wordlist, "
    "fuzzing, brute force\n"
    "- Arquitetura web: endpoint, framework, backend, frontend, request, "
    "response, header, cookie, session, token, query string, route, "
    "callback, hook, listener, sandbox\n"
    "- Metodologia: black-box, white-box, gray-box, code review, bypass\n"
    "- Siglas técnicas em geral: HTTP, JSON, API, JWT, ORM, etc.\n"
    "Termos do dia a dia de TI com tradução consolidada (arquivo, servidor, "
    "usuário, senha, navegador) DEVEM ser traduzidos normalmente. Na dúvida, "
    "prefira manter em inglês se for o uso comum em cursos como OffSec/eWPT."
)

CONTEXTO_ASR_WEBSEC = (
    "XSS, SQL Injection, SSRF, XXE, CSRF, SSTI, Prototype Pollution, "
    "Deserialization, Buffer Overflow, Sandbox Escape, Path Traversal, "
    "Race Condition, Privilege Escalation, exploit, payload, shell, "
    "reverse shell, webshell, backdoor, wordlist, fuzzing, JWT, API, ORM"
)

_TERMOS_GLOSSARIO = {
    t.strip().lower() for t in re.findall(r"[A-Za-z][A-Za-z .-]*[A-Za-z]", GLOSSARIO_WEBSEC)
    if len(t.strip()) > 1
}

REGRAS_BASE = (
    "REGRAS:\n"
    "1) Traduza de forma concisa, natural e fluente, como legenda de tela (máximo 1 a 2 linhas).\n"
    "2) Preserve tags <i></i>, números, código e nomes próprios/ferramentas sem tradução.\n"
    "3) Não censure termos técnicos nem palavrões se houver.\n"
    "4) Mantenha os termos do glossário exatamente como estão.\n"
    "5) Responda EXATAMENTE no formato:\n[1] tradução 1\n[2] tradução 2\n...\n"
    "Mesma quantidade de linhas da entrada, sem comentários extras, sem markdown."
)


def montar_glossario() -> str:
    return GLOSSARIO_WEBSEC if manter_termos else ""


def chamar_com_retry(func, *args, **kwargs):
    for tentativa in range(1, MAX_TENTATIVAS + 1):
        try:
            return func(*args, **kwargs)
        except RateLimitError as e:
            espera = BACKOFF_BASE * (2 ** (tentativa - 1))
            try:
                retry_after = float(e.response.headers.get("retry-after"))
                if retry_after:
                    espera = retry_after + 1
            except:
                pass
            espera += random.uniform(0, 1)
            print(f" [Rate limit] tentativa {tentativa}/{MAX_TENTATIVAS} -> esperando {espera:.1f}s")
            time.sleep(espera)
        except APIStatusError as e:
            if e.status_code == 429:
                espera = BACKOFF_BASE * (2 ** (tentativa - 1))
                print(f" [429] tentativa {tentativa} -> {espera:.1f}s")
                time.sleep(espera)
            else:
                raise
        except APIConnectionError as e:
            espera = BACKOFF_BASE * (2 ** (tentativa - 1))
            print(f" [Conexão/timeout] tentativa {tentativa}/{MAX_TENTATIVAS}: {e} -> esperando {espera:.1f}s")
            time.sleep(espera)
    raise RuntimeError("Falha após várias tentativas (rate limit ou conexão).")


def format_time(seconds: float):
    ms = int((seconds - int(seconds)) * 1000)
    s = int(seconds) % 60
    m = (int(seconds) // 60) % 60
    h = int(seconds) // 3600
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def quebrar_frases_curtas(texto: str) -> list[str]:
    """Divide blocos grandes em orações curtas adequadas para legendas normais de vídeo."""
    texto = texto.strip().replace("\r\n", " ").replace("\n", " ")
    if not texto:
        return []
    # Separa por pontuações que indicam pausa natural (ponto, exclamação, interrogação, vírgula ou dois-pontos)
    raw_partes = re.split(r"(?<=[.!?])\s+|(?<=[,;:])\s+", texto)
    partes = [p.strip() for p in raw_partes if p.strip()]

    resultado = []
    for parte in partes:
        # Se uma sentença ainda for muito longa (>80 chars), fatia por palavras
        if len(parte) > MAX_CHARS_POR_LEGENDA:
            palavras = parte.split()
            atual = []
            atual_len = 0
            for w in palavras:
                if atual_len + len(w) + 1 > MAX_CHARS_POR_LEGENDA and atual:
                    resultado.append(" ".join(atual))
                    atual = [w]
                    atual_len = len(w)
                else:
                    atual.append(w)
                    atual_len += len(w) + 1
            if atual:
                resultado.append(" ".join(atual))
        else:
            resultado.append(parte)

    return [r for r in resultado if r.strip()]


def distribuir_tempo_legenda(frases: list[str], inicio: float, fim: float) -> list[dict]:
    """Distribui as frases curtas ao longo do tempo do áudio com limites de exibição de tela."""
    if not frases:
        return []
    duracao_total = max(fim - inicio, 0.1)
    total_chars = sum(len(f) for f in frases) or 1
    segmentos = []
    cursor = inicio
    for f in frases:
        proporcao = len(f) / total_chars
        dur = duracao_total * proporcao
        # Evita que uma única legenda passe de MAX_SEG_DURACAO segundos na tela
        dur = min(dur, MAX_SEG_DURACAO)
        seg_fim = min(cursor + max(dur, 1.2), fim)
        segmentos.append({"text": f, "start": cursor, "end": seg_fim})
        cursor = seg_fim
    if segmentos:
        segmentos[-1]["end"] = fim
    return segmentos


def parece_ingles(texto: str) -> bool:
    if not texto.strip():
        return False
    palavras_en = {"the", "and", "you", "that", "with", "this", "have",
                   "what", "your", "are", "for", "was", "were", "they"}
    palavras = re.findall(r"[a-zA-Zà-úÀ-Ú']+", texto.lower())
    if not palavras:
        return False
    palavras_relevantes = [p for p in palavras if p not in _TERMOS_GLOSSARIO]
    hits_en = sum(1 for p in palavras_relevantes if p in palavras_en)
    tem_acento = bool(re.search(r"[áàâãéêíóôõúçÁÀÂÃÉÊÍÓÔÕÚÇ]", texto))
    return hits_en >= 1 and not tem_acento


def retraduzir_linha_unica(texto_en: str) -> str:
    glossario = montar_glossario()
    system_prompt = (
        "Você é um tradutor técnico profissional de legendas EN -> PT-BR, "
        "nível 9/10, especializado em cibersegurança e segurança web. "
        f"{glossario}"
        "Traduza a legenda abaixo de forma concisa e natural (máximo 1 ou 2 linhas). "
        "Preserve tags <i></i>, números, código e termos técnicos. Responda APENAS "
        "com a tradução, sem numeração e sem quebras extras."
    )
    resp = chamar_com_retry(
        client.chat.completions.create,
        model=MODELO_TRADUCAO,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": texto_en}
        ],
        temperature=0.15,
        top_p=0.9,
        extra_body={
            "reasoning": {"effort": "medium"},
            "provider": {"sort": "price", "allow_fallbacks": True}
        },
    )
    return resp.choices[0].message.content.strip()


def traduzir_lote_curso(textos_en: list[str]) -> list[str]:
    numerado = "\n".join([f"[{i+1}] {t}" for i, t in enumerate(textos_en)])
    glossario = montar_glossario()

    system_prompt = (
        "Você é um tradutor técnico profissional de legendas EN -> PT-BR, "
        "nível 9/10, especializado em cibersegurança e segurança web. "
        f"{glossario}"
        f"{REGRAS_BASE}"
    )

    resp = chamar_com_retry(
        client.chat.completions.create,
        model=MODELO_TRADUCAO,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": numerado}
        ],
        temperature=0.15,
        top_p=0.9,
        extra_body={
            "reasoning": {"effort": "medium"},
            "provider": {"sort": "price", "allow_fallbacks": True}
        },
    )
    saida = resp.choices[0].message.content.strip()

    traducoes = {}
    for linha in saida.splitlines():
        if "]" not in linha:
            continue
        try:
            idx_str, trad = linha.split("]", 1)
            idx = int(re.sub(r"\D", "", idx_str))
            # Remove quebras de linha acidentais que poluem a tela do player
            traducoes[idx] = trad.strip().replace("\n", " ")
        except:
            continue

    resultado = [traducoes.get(i + 1, textos_en[i]) for i in range(len(textos_en))]

    for tentativa in range(1, MAX_RETENTATIVAS_QA + 1):
        indices_com_erro = [i for i, t in enumerate(resultado) if parece_ingles(t)]
        if not indices_com_erro:
            break
        for pos, i in enumerate(indices_com_erro, 1):
            nova_trad = retraduzir_linha_unica(textos_en[i])
            if not parece_ingles(nova_trad):
                resultado[i] = nova_trad.replace("\n", " ")
            time.sleep(0.4)

    return resultado


# ============================================================
# TRANSCRIÇÃO (Chunks de 45s com subdivisão de tela)
# ============================================================

def obter_duracao_audio(caminho: str) -> float:
    saida = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", caminho],
        capture_output=True, text=True, check=True,
    )
    return float(saida.stdout.strip())


def extrair_chunk(caminho_origem: str, inicio_s: float, duracao_s: float, caminho_saida: str):
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", caminho_origem,
         "-ss", str(inicio_s), "-t", str(duracao_s),
         "-ac", "1", "-ar", "16000", "-b:a", "64k", caminho_saida],
        check=True,
    )


def transcrever_chunk(caminho_chunk: str, inicio_chunk: float, duracao_chunk: float) -> list[dict]:
    with open(caminho_chunk, "rb") as f:
        arquivo = (os.path.basename(caminho_chunk), f.read())

    resp = chamar_com_retry(
        client.audio.transcriptions.create,
        file=arquivo,
        model=MODELO_TRANSCRICAO,
        language="en",
        response_format="json",
    )

    texto_bruto = getattr(resp, "text", "") or (resp.get("text", "") if isinstance(resp, dict) else str(resp))
    texto_bruto = texto_bruto.strip()
    if not texto_bruto:
        return []

    # Quebra o bloco de texto retornado pelo ASR em linhas curtas de legenda de vídeo
    frases = quebrar_frases_curtas(texto_bruto)
    return distribuir_tempo_legenda(frases, inicio_chunk, inicio_chunk + duracao_chunk)


def transcrever_audio_openrouter(caminho_audio: str) -> list[dict]:
    duracao_total = obter_duracao_audio(caminho_audio)
    total_chunks = int(duracao_total // DURACAO_CHUNK_SEGUNDOS) + 1
    print(f"Áudio com {duracao_total/60:.1f}min, dividido em {total_chunks} chunk(s) de até {DURACAO_CHUNK_SEGUNDOS}s...")

    segmentos = []
    with tempfile.TemporaryDirectory() as tmpdir:
        ini = 0.0
        chunk_num = 0
        while ini < duracao_total:
            chunk_num += 1
            dur = min(DURACAO_CHUNK_SEGUNDOS, duracao_total - ini)
            caminho_chunk = os.path.join(tmpdir, f"chunk_{chunk_num:04d}.mp3")
            extrair_chunk(caminho_audio, ini, dur, caminho_chunk)

            segs = transcrever_chunk(caminho_chunk, ini, dur)
            segmentos.extend(segs)

            print(f" Chunk {chunk_num}/{total_chunks} ({ini/60:.1f}m - {(ini+dur)/60:.1f}m | {len(segs)} legendas)")
            ini += DURACAO_CHUNK_SEGUNDOS
            time.sleep(0.6)

    return segmentos


# --- Execução Principal ---
inicio_exec = time.time()
print(f"Transcrevendo via OpenRouter ({MODELO_TRANSCRICAO}): {audio_path}")

segmentos = transcrever_audio_openrouter(audio_path)
segmentos = [s for s in segmentos if s["text"].strip()]

if not segmentos:
    print("Nenhum segmento de fala reconhecido.")
    sys.exit(1)

nome_base = os.path.splitext(audio_path)[0]
arquivo_srt_en = f"{nome_base}_en.srt"
arquivo_srt = f"{nome_base}_ptbr.srt"

with open(arquivo_srt_en, "w", encoding="utf-8") as f:
    for i, seg in enumerate(segmentos, 1):
        f.write(f"{i}\n{format_time(seg['start'])} --> {format_time(seg['end'])}\n{seg['text'].strip()}\n\n")
print(f"Legenda em inglês salva: {arquivo_srt_en}")

print(f"Transcrição OK: {len(segmentos)} legendas de tamanho normal. Traduzindo em lotes de {TAMANHO_LOTE} (modelo: {MODELO_TRADUCAO})...")

with open(arquivo_srt, "w", encoding="utf-8") as f:
    for ini in range(0, len(segmentos), TAMANHO_LOTE):
        lote = segmentos[ini:ini + TAMANHO_LOTE]
        textos_en = [s["text"].strip() for s in lote]

        textos_pt = traduzir_lote_curso(textos_en)

        for j, seg in enumerate(lote):
            n = ini + j + 1
            f.write(f"{n}\n{format_time(seg['start'])} --> {format_time(seg['end'])}\n{textos_pt[j]}\n\n")

        total_lotes = (len(segmentos) - 1) // TAMANHO_LOTE + 1
        print(f" Lote {ini // TAMANHO_LOTE + 1}/{total_lotes} traduzido")
        time.sleep(DELAY_ENTRE_LOTES)

fim_exec = time.time()
print(f"\nSucesso! {arquivo_srt_en} (EN) e {arquivo_srt} (PT-BR) gerados em {round(fim_exec - inicio_exec, 1)}s | {len(segmentos)} legendas geradas.")