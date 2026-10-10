"""Ponto de entrada do auto-healer de modelo gratuito do OpenRouter (a implementação está em agents/model_healer.py).

    from tools.model_resolver import get_free_model, resolver_modelo, proteger_llm

- get_free_model(): modelo `:free` com preço zero que responde agora (lista pública do OpenRouter, ordem de
  preferência, teste de 1 token, cache de 6 h). Nunca devolve modelo pago.
- resolver_modelo(slug, chave): o slug, se funciona; senão um gratuito novo (log `AUTO-HEAL`); None se nada serve.
- proteger_llm(llm, chave): se um 404 'unavailable for free' aparecer durante a execução, troca e repete a chamada.
"""
from agents.model_healer import (  # noqa: F401
    PREFERIDOS, atualizar_variable, curar, get_free_model, indisponivel, listar_modelos_gratis, proteger_llm,
    resolver_modelo, testar_modelo,
)
