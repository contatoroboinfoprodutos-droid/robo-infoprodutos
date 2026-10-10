"""
Monta o Crew da Infoproduct Factory a partir de config/agents.yaml e
config/tasks.yaml (padrão @CrewBase do CrewAI) — integrando com o Groq
(primário) e Gemini (fallback automático) para tolerar Rate Limit e
falhas de conexão sem derrubar o pipeline.

Provedores aceitos em LLM_PROVIDER: "gemini", "groq" ou "openrouter".
O workflow escolhe o provedor disponível antes de rodar.
"""
import re
import time
import logging
from typing import ClassVar, Optional, Tuple

import litellm
from pydantic import PrivateAttr
from crewai import LLM, Agent, Crew, Process, Task
from crewai.project import CrewBase, agent, crew, task

from agents import model_healer
from config import get_settings
from tools.crewai_meta_tools import PublishToFacebookTool, PublishToInstagramTool

logger = logging.getLogger(__name__)


def _aguardar_reset_rate_limit(output):
    time.sleep(20)


# Exceções que consideramos "críticas" o suficiente para acionar o failover
# depois de esgotadas as tentativas na Groq.
RETRYABLE_ERRORS: Tuple[type, ...] = (
    litellm.exceptions.RateLimitError,
    litellm.exceptions.APIConnectionError,
    litellm.exceptions.ServiceUnavailableError,
    litellm.exceptions.Timeout,
)


class GroqWithGeminiFailoverLLM(LLM):
    """
    LLM primário na Groq com:
      1. Retry inteligente (lê o "try again in Xs" da própria Groq).
      2. Failover automático para o Gemini se a Groq esgotar as tentativas
         ou falhar de forma persistente (rate limit, timeout, conexão).
    Uma vez acionado o failover, a instância passa a usar o Gemini
    diretamente nas chamadas seguintes (evita ficar re-tentando a Groq
    a cada task dentro da mesma execução do crew).
    """

    MAX_ATTEMPTS: ClassVar[int] = 6
    FALLBACK_BASE_DELAY: ClassVar[float] = 8.0

    # Atributos privados (não são campos Pydantic do modelo LLM)
    _fallback_llm: Optional[LLM] = PrivateAttr(default=None)
    _using_fallback: bool = PrivateAttr(default=False)

    def _build_fallback_llm(self) -> Optional[LLM]:
        settings = get_settings()
        if not settings.gemini_api_key:
            return None
        return LLM(
            model=settings.gemini_model,
            api_key=settings.gemini_api_key,
            temperature=0.7,
            timeout=240,
            max_tokens=1024,
        )

    def call(self, *args, **kwargs):
        # Se já mudamos para o Gemini nesta instância, vai direto nele.
        if self._using_fallback and self._fallback_llm is not None:
            return self._fallback_llm.call(*args, **kwargs)

        last_error: Exception | None = None
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            try:
                return super().call(*args, **kwargs)
            except RETRYABLE_ERRORS as e:
                last_error = e
                wait_time = self._extract_wait_time(str(e))
                if wait_time is None:
                    wait_time = self.FALLBACK_BASE_DELAY * (2 ** (attempt - 1))

                logger.warning(
                    "[Groq] Tentativa %d/%d falhou (%s) — aguardando %.1fs.",
                    attempt, self.MAX_ATTEMPTS, type(e).__name__, wait_time,
                )
                time.sleep(wait_time + 0.5)

        # Groq esgotou as tentativas -> tenta failover para Gemini
        logger.warning(
            "[Failover] Groq falhou após %d tentativas (%s). Tentando Gemini...",
            self.MAX_ATTEMPTS, type(last_error).__name__,
        )

        fallback = self._build_fallback_llm()
        if fallback is None:
            logger.error(
                "Sem GEMINI_API_KEY configurada — não é possível fazer failover. "
                "Propagando o erro original da Groq."
            )
            raise last_error

        try:
            result = fallback.call(*args, **kwargs)
        except Exception as fallback_error:
            logger.error(
                "[Failover] Gemini também falhou: %s. Pipeline será interrompido.",
                fallback_error,
            )
            raise

        # Sucesso no Gemini: fixa o fallback para o resto da execução desta instância
        self._fallback_llm = fallback
        self._using_fallback = True
        logger.info("[Failover] Ativado com sucesso — usando Gemini (%s) a partir de agora.", fallback.model)
        return result

    @staticmethod
    def _extract_wait_time(error_message: str) -> Optional[float]:
        match = re.search(r"try again in ([\d.]+)s", error_message)
        return float(match.group(1)) if match else None


def get_llm() -> LLM:
    settings = get_settings()
    provider = (settings.llm_provider or '').strip().lower()

    # OpenRouter: compatível com a API da OpenAI. O prefixo "openai/" + base_url
    # funciona em qualquer versão do CrewAI (nativa ou via LiteLLM).
    if provider == 'openrouter':
        if not settings.openrouter_api_key:
            raise RuntimeError('OPENROUTER_API_KEY não configurada (LLM_PROVIDER=openrouter).')
        modelo = model_healer.resolver_modelo(settings.openrouter_model, settings.openrouter_api_key)
        if modelo:
            return model_healer.proteger_llm(LLM(
                model="openai/" + modelo,
                base_url="https://openrouter.ai/api/v1",
                api_key=settings.openrouter_api_key,
                temperature=0.7,
                timeout=240,
                max_tokens=2048,
            ), settings.openrouter_api_key)
        # Nenhum modelo gratuito respondeu: segue com Gemini ou Groq se houver chave (nunca um modelo pago).
        logger.warning("Nenhum modelo gratuito do OpenRouter respondeu; usando GEMINI/GROQ se houver chave.")
        provider = 'groq' if (settings.groq_api_key and not settings.gemini_api_key) else 'gemini'
        if not (settings.gemini_api_key or settings.groq_api_key):
            raise RuntimeError('Sem modelo gratuito no OpenRouter e sem GEMINI_API_KEY/GROQ_API_KEY.')

    # Groq: também compatível com a API da OpenAI (mesmo truque do OpenRouter).
    if provider == 'groq' and settings.groq_api_key:
        modelo = settings.model or "openai/gpt-oss-120b"
        if modelo.startswith("groq/"):
            modelo = modelo[len("groq/"):]
        return LLM(
            model="openai/" + modelo,
            base_url="https://api.groq.com/openai/v1",
            api_key=settings.groq_api_key,
            temperature=0.7,
            timeout=240,
            max_tokens=2048,
        )

    if provider == 'gemini' or not settings.groq_api_key:
        if not settings.gemini_api_key:
            raise RuntimeError('GEMINI_API_KEY não configurada (LLM_PROVIDER=gemini).')
        return LLM(
            model=settings.gemini_model,
            api_key=settings.gemini_api_key,
            temperature=0.7,
            timeout=240,
            max_tokens=2048,
        )
    return GroqWithGeminiFailoverLLM(
        model=settings.model,
        api_key=settings.groq_api_key,
        temperature=0.7,
        max_tokens=1024,
        num_retries=5,
    )


@CrewBase
class InfoprodutoFactoryCrew:
    """Crew completo: estratégia -> roteiro -> visual -> segmentação -> publicação."""

    agents_config = "config/agents.yaml"
    tasks_config = "config/tasks.yaml"

    # -- Agentes (sem mudanças na assinatura, só continuam usando get_llm()) --
    @agent
    def conteudo_estrategista(self) -> Agent:
        return Agent(
            config=self.agents_config["conteudo_estrategista"],
            llm=get_llm(),
            verbose=True,
            allow_delegation=False,
            cache=False,
        )

    @agent
    def redator_senior(self) -> Agent:
        return Agent(
            config=self.agents_config["redator_senior"],
            llm=get_llm(),
            verbose=True,
            allow_delegation=False,
            cache=False,
        )

    @agent
    def diretor_criativo(self) -> Agent:
        return Agent(
            config=self.agents_config["diretor_criativo"],
            llm=get_llm(),
            verbose=True,
            allow_delegation=False,
            cache=False,
        )

    @agent
    def segmentador_publicos(self) -> Agent:
        return Agent(
            config=self.agents_config["segmentador_publicos"],
            llm=get_llm(),
            verbose=True,
            allow_delegation=False,
            cache=False,
        )

    @agent
    def publicador_redes(self) -> Agent:
        return Agent(
            config=self.agents_config["publicador_redes"],
            llm=get_llm(),
            tools=[PublishToFacebookTool(), PublishToInstagramTool()],
            verbose=True,
            allow_delegation=False,
            cache=False,
        )

    # -- Tasks (sem mudanças) --
    @task
    def planejar_campanha(self) -> Task:
        return Task(config=self.tasks_config["planejar_campanha"], callback=_aguardar_reset_rate_limit)

    @task
    def criar_roteiro_e_legenda(self) -> Task:
        return Task(config=self.tasks_config["criar_roteiro_e_legenda"], callback=_aguardar_reset_rate_limit)

    @task
    def desenvolver_diretrizes_visuais(self) -> Task:
        return Task(config=self.tasks_config["desenvolver_diretrizes_visuais"], callback=_aguardar_reset_rate_limit)

    @task
    def direcionar_para_grupos(self) -> Task:
        return Task(config=self.tasks_config["direcionar_para_grupos"], callback=_aguardar_reset_rate_limit)

    @task
    def publicar_no_facebook_e_instagram(self) -> Task:
        return Task(config=self.tasks_config["publicar_no_facebook_e_instagram"])

    # -- Crew --
    @crew
    def crew(self) -> Crew:
        return Crew(
            agents=self.agents,
            tasks=self.tasks,
            process=Process.sequential,
            max_rpm=6,
            verbose=True,
        )
