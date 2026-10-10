"""Escolha dos modelos do criador e do orientador.

Regra: o orientador usa um provedor DIFERENTE do criador sempre que houver mais de uma chave.
Um revisor do mesmo modelo que escreveu o texto costuma aprovar o que ele mesmo gerou.
As chaves são lidas só de variáveis de ambiente (Secrets do GitHub); nada é impresso.
"""
import logging
import os

logger = logging.getLogger("fabrica")

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def _env(nome: str) -> str:
    return os.getenv(nome, "").strip()


def provedores_disponiveis() -> list[str]:
    ordem = []
    if _env("GEMINI_API_KEY"):
        ordem.append("gemini")
    if _env("GROQ_API_KEY"):
        ordem.append("groq")
    if _env("OPENROUTER_API_KEY"):
        ordem.append("openrouter")
    return ordem


def modelo_padrao(provedor: str, alternativo: bool = False) -> str:
    if provedor == "gemini":
        principal = _env("GEMINI_MODEL") or "gemini/gemini-3.8-flash"
        if not principal.startswith("gemini/"):
            principal = "gemini/" + principal
        if alternativo:
            return "gemini/gemini-3.7-flash" if principal != "gemini/gemini-3.7-flash" \
                else "gemini/gemini-3.8-flash"
        return principal
    if provedor == "groq":
        m = _env("GROQ_MODEL") or "openai/gpt-oss-120b"
        return m[len("groq/"):] if m.startswith("groq/") else m
    return _env("OPENROUTER_MODEL") or "qwen/qwen3-coder:free"


def candidatos(papel: str, evitar: str | None = None) -> list[dict]:
    """Lista ordenada de {provedor, modelo} para o papel ('criador' ou 'orientador').
    Para o orientador, `evitar` é o provedor que o criador usou: ele só entra por último."""
    disp = provedores_disponiveis()
    if not disp:
        raise RuntimeError("Nenhuma chave de LLM definida (GEMINI_API_KEY, GROQ_API_KEY ou OPENROUTER_API_KEY).")
    if papel == "orientador" and evitar:
        outros = [p for p in disp if p != evitar]
        lista = [{"provedor": p, "modelo": modelo_padrao(p)} for p in outros]
        # mesmo provedor do criador só como último recurso, com modelo alternativo quando existir
        lista.append({"provedor": evitar, "modelo": modelo_padrao(evitar, alternativo=True)})
        if not outros:
            logger.warning("Só há um provedor de LLM: o orientador usará o mesmo provedor do criador "
                           "(modelo alternativo quando possível). Cadastre outra chave para uma revisão mais independente.")
        return lista
    return [{"provedor": p, "modelo": modelo_padrao(p)} for p in disp]


def construir_llm(provedor: str, modelo: str, max_tokens: int = 6000, temperature: float = 0.7,
                  timeout: float | None = None):
    """Cria o LLM do CrewAI (mesmo padrão usado nos outros robôs do repositório)."""
    from crewai import LLM
    from . import config_fabrica as _cfg

    timeout = timeout or _cfg.LLM_TIMEOUT

    if provedor == "gemini":
        return LLM(model=modelo, api_key=_env("GEMINI_API_KEY"), temperature=temperature, max_tokens=max_tokens, timeout=timeout)
    if provedor == "groq":
        return LLM(model="openai/" + modelo, base_url=GROQ_BASE_URL, api_key=_env("GROQ_API_KEY"),
                   temperature=temperature, max_tokens=max_tokens, timeout=timeout)
    if provedor == "openrouter":
        llm = LLM(model="openai/" + modelo, base_url=OPENROUTER_BASE_URL, api_key=_env("OPENROUTER_API_KEY"),
                  temperature=temperature, max_tokens=max_tokens, timeout=timeout)
        try:  # slug gratuito que saiu do ar (404): troca por outro gratuito, na partida e no meio da execução
            from agents import model_healer
            novo = model_healer.resolver_modelo(modelo, _env("OPENROUTER_API_KEY"))
            if novo and novo != modelo:
                llm = LLM(model="openai/" + novo, base_url=OPENROUTER_BASE_URL, api_key=_env("OPENROUTER_API_KEY"),
                          temperature=temperature, max_tokens=max_tokens, timeout=timeout)
            model_healer.proteger_llm(llm, _env("OPENROUTER_API_KEY"))
        except Exception as e:  # o healer nunca pode derrubar a fábrica
            logger.warning("model_healer indisponível: %s", e)
        return llm
    raise ValueError(f"provedor desconhecido: {provedor}")


def aplicar_workaround_groq() -> None:
    """Mesmo workaround do main.py: evita 'cache_breakpoint' nas mensagens, que a Groq rejeita."""
    try:
        import crewai.llms.cache as _cache
        _cache.mark_cache_breakpoint = lambda msg: msg
    except (ImportError, AttributeError):
        pass
