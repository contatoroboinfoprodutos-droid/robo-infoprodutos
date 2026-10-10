"""Cura sozinha o modelo do OpenRouter quando o slug configurado sai do ar (404 "unavailable for free").

Só escolhe modelos GRATUITOS (id terminando em ":free" e preço zero na lista pública do OpenRouter): nunca um pago.
Fluxo: lista os modelos -> filtra -> segue a ordem de preferência -> testa 1 token -> guarda 6h em cache.
Se nada gratuito responder, devolve None e o chamador cai para GEMINI_API_KEY / GROQ_API_KEY.

Obs.: o slug mora na Variable OPENROUTER_MODEL do repositório, não no código. O GITHUB_TOKEN padrão NÃO pode gravar
Variables; por isso a troca vale para a execução em andamento e a Variable só é atualizada se houver um token com
permissão (GH_PAT_VARIABLES) — senão o log avisa para trocar a Variable à mão.
"""
import json
import logging
import os
import subprocess
import tempfile
import time

import requests

logger = logging.getLogger("fabrica.model_healer")

MODELOS_URL = "https://openrouter.ai/api/v1/models"
CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
CACHE_ARQUIVO = os.getenv("FREE_MODELS_CACHE", os.path.join(tempfile.gettempdir(), "free_models.json"))
CACHE_VALIDADE_S = 6 * 3600
# Ordem de preferência pedida. Os slugs gratuitos mudam: o que não existir mais na lista é ignorado.
PREFERIDOS = (
    "google/gemini-2.0-flash-exp:free",
    "meta-llama/llama-3.2-3b-instruct:free",
    "qwen/qwen-2.5-coder-32b-instruct:free",
    "mistralai/mistral-7b-instruct:free",
)
MAX_TESTES = 6


def _zero(valor) -> bool:
    try:
        return float(valor) == 0.0
    except (TypeError, ValueError):
        return False


def listar_modelos_gratis(sessao=requests) -> list[str]:
    """Ids ':free' com preço de prompt (e de resposta, se informado) igual a zero."""
    r = sessao.get(MODELOS_URL, timeout=30)
    r.raise_for_status()
    achados = []
    for m in r.json().get("data", []):
        mid = str(m.get("id", ""))
        preco = m.get("pricing") or {}
        if mid.endswith(":free") and _zero(preco.get("prompt")) and _zero(preco.get("completion", "0")):
            achados.append(mid)
    return achados


def ordenar(modelos: list[str]) -> list[str]:
    pref = [m for m in PREFERIDOS if m in modelos]
    return pref + sorted(m for m in modelos if m not in pref)


def testar_modelo(modelo: str, api_key: str, sessao=requests) -> bool:
    """Pede 1 token ao modelo. True só com HTTP 200."""
    try:
        r = sessao.post(CHAT_URL, timeout=45,
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                        json={"model": modelo, "max_tokens": 1, "messages": [{"role": "user", "content": "ok"}]})
        return r.status_code == 200
    except requests.RequestException:
        return False


def indisponivel(erro) -> bool:
    """O erro diz que o modelo saiu do ar (não é limite de uso nem rede)?"""
    t = str(erro).lower()
    return ("404" in t or "not found" in t or "no endpoints" in t) and \
        ("unavailable for free" in t or "not found" in t or "no endpoints" in t or "404" in t)


def _ler_cache(agora: float):
    try:
        with open(CACHE_ARQUIVO, encoding="utf-8") as f:
            c = json.load(f)
        if agora - float(c["em"]) < CACHE_VALIDADE_S and c.get("modelo"):
            return c["modelo"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _gravar_cache(modelo: str, agora: float) -> None:
    try:
        with open(CACHE_ARQUIVO, "w", encoding="utf-8") as f:
            json.dump({"modelo": modelo, "em": agora}, f)
    except OSError:
        pass


def get_free_model(api_key: str | None = None, evitar: str | None = None, sessao=requests, agora=None) -> str | None:
    """Um modelo gratuito que responde agora, ou None. Usa o cache de 6h (revalidado com 1 token)."""
    api_key = api_key or os.getenv("OPENROUTER_API_KEY", "")
    agora = time.time() if agora is None else agora
    em_cache = _ler_cache(agora)
    if em_cache and em_cache != evitar:
        return em_cache
    try:
        candidatos = [m for m in ordenar(listar_modelos_gratis(sessao)) if m != evitar]
    except Exception as e:  # rede, JSON, 5xx
        logger.warning("model_healer: não consegui listar os modelos do OpenRouter (%s)", e)
        return None
    for modelo in candidatos[:MAX_TESTES]:
        if testar_modelo(modelo, api_key, sessao):
            _gravar_cache(modelo, agora)
            return modelo
        logger.info("model_healer: %s não respondeu, tentando o próximo", modelo)
    return None


def atualizar_variable(novo: str, executar=subprocess.run) -> bool:
    """Tenta gravar OPENROUTER_MODEL na Variable do repositório (precisa de token com permissão de Variables)."""
    repo, token = os.getenv("GITHUB_REPOSITORY"), os.getenv("GH_PAT_VARIABLES")
    if not (repo and token):
        logger.warning("model_healer: sem GH_PAT_VARIABLES, a Variable OPENROUTER_MODEL não foi atualizada "
                       "(troque para %s em Settings > Variables)", novo)
        return False
    try:
        r = executar(["gh", "api", "-X", "PATCH", f"repos/{repo}/actions/variables/OPENROUTER_MODEL",
                      "-f", "name=OPENROUTER_MODEL", "-f", f"value={novo}"],
                     env={**os.environ, "GH_TOKEN": token}, capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False


def curar(modelo_atual: str, api_key: str, sessao=requests) -> str | None:
    """Troca o modelo desatualizado por um gratuito que funciona. Loga o aviso e tenta atualizar a Variable."""
    novo = get_free_model(api_key, evitar=modelo_atual, sessao=sessao)
    if novo:
        logger.warning("⚠️ AUTO-HEAL: OPENROUTER_MODEL desatualizado, usando %s", novo)
        print(f"⚠️ AUTO-HEAL: OPENROUTER_MODEL desatualizado, usando {novo}", flush=True)
        if atualizar_variable(novo):
            logger.info("model_healer: Variable OPENROUTER_MODEL atualizada para %s", novo)
    return novo


def resolver_modelo(modelo_atual: str, api_key: str, sessao=requests) -> str | None:
    """Devolve o modelo a usar: o atual, se responde; um gratuito novo, se o atual saiu do ar; None se nada serve.
    Falha de rede ou limite de uso (429) não é motivo para trocar: mantém o atual."""
    if not (modelo_atual or "").strip():  # Variable apagada: o Actions entrega "" (não "ausente"); vai direto a um gratuito
        return get_free_model(api_key, sessao=sessao)
    try:
        r = sessao.post(CHAT_URL, timeout=45,
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                        json={"model": modelo_atual, "max_tokens": 1, "messages": [{"role": "user", "content": "ok"}]})
    except requests.RequestException:
        return modelo_atual
    if r.status_code == 200 or r.status_code in (401, 402, 429) or r.status_code >= 500:
        return modelo_atual
    if indisponivel(f"{r.status_code} {r.text}") or r.status_code == 404:
        return curar(modelo_atual, api_key, sessao)
    return modelo_atual


def proteger_llm(llm, api_key: str, prefixo: str = "openai/", sessao=requests):
    """Troca o modelo NO MEIO da execução: se uma chamada ao LLM voltar 404 'unavailable for free' (o slug saiu do ar
    depois do teste inicial, ou ninguém testou), escolhe outro gratuito, atualiza `llm.model` e repete a chamada UMA vez.
    Qualquer outro erro passa como está. Devolve o próprio `llm`."""
    original = getattr(llm, "call", None)
    if original is None:
        return llm

    def call(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        except Exception as e:
            if not indisponivel(e):
                raise
            atual = str(getattr(llm, "model", "") or "")
            novo = curar(atual[len(prefixo):] if atual.startswith(prefixo) else atual, api_key, sessao)
            if not novo:
                raise
            llm.model = prefixo + novo
            return original(*args, **kwargs)
    try:
        llm.call = call
    except (AttributeError, TypeError):  # classe que não aceita atributo novo: segue sem proteção em tempo de execução
        logger.warning("model_healer: não consegui proteger o LLM em tempo de execução")
    return llm
