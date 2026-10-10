import os
from crewai import Agent, LLM
from . import config_lt as cfg
from .tools_lt import lt_render_card, lt_publicar_meta, lt_publicar_tiktok

if cfg.LLM_PROVIDER == "openrouter":
    _api_key = cfg.OPENROUTER_API_KEY
elif cfg.LLM_PROVIDER == "gemini":
    _api_key = cfg.GEMINI_API_KEY
else:
    _api_key = cfg.GROQ_API_KEY
if not _api_key:
    raise RuntimeError(f"Chave do LLM ausente para LT_LLM_PROVIDER={cfg.LLM_PROVIDER} "
                       "(defina GEMINI_API_KEY, OPENROUTER_API_KEY ou GROQ_API_KEY).")

if cfg.LLM_PROVIDER == "openrouter":
    # OpenRouter e Groq são compatíveis com a API da OpenAI: o prefixo "openai/" + base_url funciona em qualquer versão do CrewAI.
    # O slug gratuito pode sair do ar (404 "unavailable for free"): o healer troca por outro gratuito e, se nenhum
    # responder, cai para o Gemini (nunca para modelo pago).
    from agents import model_healer
    _modelo = model_healer.resolver_modelo(cfg.LLM_MODEL, _api_key) if not cfg.DRY_RUN else cfg.LLM_MODEL
    if _modelo:
        _llm = model_healer.proteger_llm(LLM(model="openai/" + _modelo, base_url=cfg.OPENROUTER_BASE_URL,
                                             api_key=_api_key, temperature=0.7, max_tokens=2048, timeout=240), _api_key)
    elif cfg.GEMINI_API_KEY:
        _llm = LLM(model=os.getenv("GEMINI_MODEL", "").strip() or "gemini/gemini-3.8-flash", api_key=cfg.GEMINI_API_KEY,
                   temperature=0.7, max_tokens=2048, timeout=240)
    else:
        raise RuntimeError("Nenhum modelo gratuito do OpenRouter respondeu e não há GEMINI_API_KEY de reserva.")
elif cfg.LLM_PROVIDER == "groq":
    _llm = LLM(model="openai/" + cfg.LLM_MODEL, base_url=cfg.GROQ_BASE_URL,
               api_key=_api_key, temperature=0.7, max_tokens=2048, timeout=240)
else:
    _llm = LLM(model=cfg.LLM_MODEL, api_key=_api_key, temperature=0.7, max_tokens=2048, timeout=240)

trend_scout_agent = Agent(
    role="Pesquisador de dores e ganchos de baixo custo",
    goal=f"Identificar dores reais e ganchos de atenção no nicho '{cfg.NICHE}' que combinem com um produto de {cfg.OFFER_PRICE}.",
    backstory="Analista de audiência que transforma dúvidas comuns de iniciantes em ideias de conteúdo. Nunca inventa dados nem estatísticas.",
    llm=_llm, allow_delegation=False, verbose=True)

copywriter_lowticket_agent = Agent(
    role="Redator de conversão rápida para infoprodutos de R$7",
    goal=f"Escrever texto do card e legenda em tom {cfg.BRAND_TONE}, em português do Brasil.",
    backstory="Copywriter de resposta direta, especialista em ofertas de entrada. Evita promessas de ganho garantido e não usa linguagem de spam.",
    llm=_llm, allow_delegation=False, verbose=True)

visual_director_agent = Agent(
    role="Diretor visual de imagens fixas",
    goal="Transformar o copy em um card visual limpo, com foto de fundo, e gerar o arquivo de imagem.",
    backstory="Designer de posts estáticos: título curto, contraste alto, identidade consistente entre Instagram e TikTok.",
    tools=[lt_render_card], llm=_llm, allow_delegation=False, verbose=True)

publisher_infoproduto_agent = Agent(
    role="Publicador de infoprodutos",
    goal="Publicar o post aprovado no Facebook e no Instagram (e no TikTok, se configurado) e reportar o resultado de cada um.",
    backstory="Operador de publicação. Só publica o que recebeu pronto e reporta erros com clareza.",
    tools=[lt_publicar_meta] + ([lt_publicar_tiktok] if cfg.TIKTOK_ATIVO else []),
    llm=_llm, allow_delegation=False, verbose=True)
