"""
Tools do CrewAI que expõem o MetaGraphAPI aos agentes.

Cada tool tem um schema de argumentos explícito (pydantic) para que o LLM
saiba exatamente quais campos preencher ao chamá-la.
"""
import time
from typing import Optional, Type

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from tools.meta_graph_api import MetaGraphAPI, MetaGraphAPIError
from config import get_settings
from fabrica_produtos import catalogo, marcas, travas


def _preparar(texto: str, rede: str | None = None):
    """Travas em código: o texto só pode afirmar o que está no catálogo. Devolve
    (produto, texto_corrigido, ajustes, problemas). Problemas = não publicar em modo real."""
    produto = catalogo.produto_da_vez()
    legenda, acoes, problemas = travas.preparar_legenda(produto, texto, "VITRINE", rede)
    return produto, legenda, acoes, problemas


def _nota_travas(acoes, problemas) -> str:
    nota = ""
    if acoes:
        nota += f" [TRAVAS] ajustes automáticos: {'; '.join(acoes)}."
    if problemas:
        nota += f" [AVISO] em modo real seria BLOQUEADO: {'; '.join(problemas)}."
    return nota


# ---------------------------------------------------------------------- #
# Facebook
# ---------------------------------------------------------------------- #
class FacebookPostInput(BaseModel):
    message: str = Field(..., description="Texto completo do post a ser publicado no Facebook.")
    link: Optional[str] = Field(
        None, description="URL opcional a ser anexada ao post (ex.: link do infoproduto)."
    )
    image_url: Optional[str] = Field(
        None,
        description="URL pública de uma imagem para publicar como foto com legenda. "
        "Se omitido, publica um post de texto simples.",
    )


class PublishToFacebookTool(BaseTool):
    name: str = "publish_to_facebook"
    description: str = (
        "Publica um post no feed da Página do Facebook configurada. "
        "Use para divulgar o infoproduto ou o conteúdo final aprovado."
    )
    args_schema: Type[BaseModel] = FacebookPostInput

    def _run(self, message: str, link: Optional[str] = None, image_url: Optional[str] = None) -> str:
        produto, message, acoes, problemas = _preparar(message, "facebook")
        # Simulação: sai ANTES de qualquer chamada de rede à Meta.
        if get_settings().dry_run:
            return (
                "[DRY_RUN] Facebook NÃO publicado (nenhuma chamada foi feita à Meta). "
                f"Público: {travas.rotulo_publico()} "
                f"image_url={image_url!r} message={message!r}" + _nota_travas(acoes, problemas)
            )
        if marcas.ja_publicado("facebook"):
            return "Facebook: já publicado nesta execução. Não repita: siga para o Instagram ou finalize."
        if problemas:
            if produto is None:
                marcas.marcar_bloqueio("sem produto pronto no catálogo")  # repetir não resolve
            return "ERRO: publicação no Facebook bloqueada pelas travas: " + "; ".join(problemas)
        link = produto["link_compra"]  # o link anexado é sempre o do catálogo
        api = MetaGraphAPI()
        ultimo = None
        for tentativa in range(3):  # só o Facebook é repetido; espera 4s, 8s entre as tentativas
            try:
                result = api.publish_facebook_post(message=message, link=link, image_url=image_url)
                marcas.marcar("facebook", {"post_id": result.post_id})
                return f"Publicado no Facebook com sucesso. post_id={result.post_id}"
            except MetaGraphAPIError as exc:
                ultimo = exc
                if tentativa < 2:
                    time.sleep(4 * (tentativa + 1))
        return f"ERRO ao publicar no Facebook: {ultimo}"


# ---------------------------------------------------------------------- #
# Instagram
# ---------------------------------------------------------------------- #
class InstagramPostInput(BaseModel):
    image_url: str = Field(
        ..., description="URL pública e acessível da imagem a ser publicada no Instagram."
    )
    caption: str = Field(..., description="Legenda completa do post, incluindo hashtags.")


class PublishToInstagramTool(BaseTool):
    name: str = "publish_to_instagram"
    description: str = (
        "Publica uma imagem com legenda na conta comercial do Instagram configurada, "
        "usando o fluxo de container (media -> media_publish) da Graph API."
    )
    args_schema: Type[BaseModel] = InstagramPostInput

    def _run(self, image_url: str, caption: str) -> str:
        produto, caption, acoes, problemas = _preparar(caption, "instagram")
        # Simulação: sai ANTES de qualquer chamada de rede à Meta.
        if get_settings().dry_run:
            return (
                "[DRY_RUN] Instagram NÃO publicado (nenhuma chamada foi feita à Meta). "
                f"Público: {travas.rotulo_publico()} "
                f"image_url={image_url!r} caption={caption!r}" + _nota_travas(acoes, problemas)
            )
        if marcas.ja_publicado("instagram"):
            return "Instagram: já publicado nesta execução. Não repita: finalize."
        if problemas:
            if produto is None:
                marcas.marcar_bloqueio("sem produto pronto no catálogo")  # repetir não resolve
            return "ERRO: publicação no Instagram bloqueada pelas travas: " + "; ".join(problemas)
        api = MetaGraphAPI()
        legenda, aviso, ultimo = caption, "", None
        for tentativa in range(3):  # só o Instagram é repetido; espera 4s, 8s entre as tentativas
            try:
                result = api.publish_instagram_post(image_url=image_url, caption=legenda)
                marcas.marcar("instagram", {"post_id": result.post_id})
                return f"Publicado no Instagram com sucesso. post_id={result.post_id}{aviso}"
            except MetaGraphAPIError as exc:
                ultimo = exc
                if travas.erro_de_hashtag(str(exc)) and legenda == caption:
                    legenda = travas.reduzir_hashtags(caption, 5)  # teto de hashtags do Instagram
                    aviso = " (legenda reduzida para as 5 hashtags principais)"
                    continue
                if tentativa < 2:
                    time.sleep(4 * (tentativa + 1))
        return f"ERRO ao publicar no Instagram: {ultimo}"
