"""
Ponto de entrada da Infoproduct Factory.

Uso automático (GitHub Actions, cron ou workflow_dispatch sem inputs):
    python main.py
    -> se PRODUTO_TOPICO não estiver definido, escolhe um nicho do rodízio (NICHOS).
    -> se IMAGEM_PADRAO_URL não estiver definida, busca uma imagem no Unsplash
       (UNSPLASH_API_KEY).

Uso manual local, sobrepondo os valores padrão só nesta execução:
    python main.py --topic "Como organizar finanças pessoais em 30 dias" \
        --image-url https://meusite.com/assets/capa.jpg

Requer um arquivo .env válido na raiz do projeto ou as mesmas variáveis
definidas como Secrets do GitHub Actions.
"""
import argparse
import datetime
import logging
import os
import sys

import requests
from dotenv import load_dotenv

# --- WORKAROUND PARA O GROQ ---
# Evita que o CrewAI injete 'cache_breakpoint' nas mensagens do sistema,
# o que gera BadRequestError na API do Groq.
try:
    import crewai.llms.cache as _crewai_cache
    _crewai_cache.mark_cache_breakpoint = lambda msg: msg
except (ImportError, AttributeError):
    pass
# -----------------------------

from config import get_settings
from crew import InfoprodutoFactoryCrew
from fabrica_produtos import catalogo, marcas

TOPICO_PADRAO = "Produto em destaque do catálogo desta semana"

# (nicho para os agentes, busca em inglês para o Unsplash)
# Edite à vontade: um nicho por rodada, em rodízio.
NICHOS = [
    ("Finanças pessoais para iniciantes", "personal finance budget"),
    ("Produtividade e organização da rotina", "productivity planner desk"),
    ("Receitas fit e alimentação saudável", "healthy food meal prep"),
    ("Inglês para o dia a dia", "learning english notebook"),
    ("Marketing digital para pequenos negócios", "small business marketing laptop"),
    ("Renda extra com habilidades online", "working from home laptop"),
    ("Redação e copywriting", "writing notebook coffee"),
    ("Estudos e concentração", "student studying desk"),
]


def indice_da_rodada():
    """Muda a cada execução: 3 rodadas por dia (cron 8h/14h/20h UTC) sem repetir."""
    agora = datetime.datetime.utcnow()
    return (agora.date().toordinal() * 3 + agora.hour // 6)


def escolher_nicho():
    return NICHOS[indice_da_rodada() % len(NICHOS)]


def buscar_imagem_unsplash(consulta, logger):
    chave = os.getenv("UNSPLASH_API_KEY", "").strip()
    if not chave:
        logger.warning("UNSPLASH_API_KEY não configurada.")
        return None
    try:
        r = requests.get(
            "https://api.unsplash.com/search/photos",
            params={"query": consulta, "per_page": 5, "orientation": "squarish"},
            headers={"Authorization": f"Client-ID {chave}"},
            timeout=20,
        )
        r.raise_for_status()
        resultados = r.json().get("results", [])
        if not resultados:
            logger.warning("Unsplash não retornou imagens para: %s", consulta)
            return None
        # varia a foto a cada rodada, entre os primeiros resultados
        escolhida = resultados[indice_da_rodada() % len(resultados)]
        return escolhida["urls"]["raw"] + "&w=1080&h=1080&fit=crop&fm=jpg&q=80"
    except Exception as e:
        logger.warning("Unsplash falhou: %s", e)
        return None


def descrever_produto(produto):
    """Texto do produto REAL do catálogo, sem chaves {} (o CrewAI as interpreta nas tasks)."""
    if not produto:
        return ("NENHUM produto 'pronto' no catálogo. Isto é apenas uma SIMULAÇÃO: crie uma ideia de produto "
                "fictícia só para o teste e diga claramente no relatório que ela é fictícia.")
    txt = (f"NOME: {produto['nome']}. PRECO: {produto['preco_texto']}. PROMESSA: {produto['promessa']}. "
           f"CONTEUDOS: {'; '.join(produto['conteudos'])}.")
    return txt.replace("{", "(").replace("}", ")")


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("infoproduct_factory")

    settings = get_settings()

    parser = argparse.ArgumentParser(description="Infoproduct Factory - CrewAI")
    parser.add_argument(
        "--topic",
        type=str,
        default=settings.produto_topico,
        help="Nicho/tema da rodada (default: PRODUTO_TOPICO ou rodízio de nichos).",
    )
    parser.add_argument(
        "--image-url",
        type=str,
        default=settings.imagem_padrao_url,
        help="URL pública de imagem (default: IMAGEM_PADRAO_URL; senão, Unsplash).",
    )
    args = parser.parse_args()

    # Nicho: se ninguém definiu um tema, usa o rodízio do dia.
    consulta_imagem = args.topic
    if not args.topic or args.topic.strip() == TOPICO_PADRAO:
        args.topic, consulta_imagem = escolher_nicho()
        logger.info("Nicho da rodada (rodízio): %s", args.topic)

    # Produto: a campanha é SEMPRE de um produto real do catálogo (status 'pronto', com link de compra).
    produto = catalogo.produto_da_vez()
    if produto is None and not settings.dry_run:
        logger.warning(
            "Nenhum produto 'pronto' no catálogo: sem produto real não há o que anunciar. "
            "Nada foi publicado. Rode a Fábrica de Produtos e libere o produto (verificar ou definir-link)."
        )
        marcas.marcar_bloqueio("sem produto pronto no catálogo")  # o workflow não repete a tentativa
        return
    if produto is not None:
        args.topic = consulta_imagem = produto["nome"]
        logger.info("Produto da rodada (catálogo): %s (%s)", produto["nome"], produto["preco_texto"])

    # Imagem: URL fixa tem prioridade; senão, busca no Unsplash.
    if not args.image_url:
        args.image_url = buscar_imagem_unsplash(consulta_imagem, logger)

    if not args.image_url:
        logger.error(
            "Sem imagem. Defina UNSPLASH_API_KEY (busca automática), "
            "IMAGEM_PADRAO_URL (URL fixa) ou passe --image-url. "
            "A Meta Graph API exige uma URL pública de imagem para publicar."
        )
        sys.exit(1)

    logger.info("Imagem da rodada: %s", args.image_url)

    if not settings.meta_configurada:
        logger.warning(
            "Credenciais da Meta (META_LONG_LIVED_TOKEN/FB_PAGE_ID/INSTAGRAM_ACCOUNT_ID) "
            "incompletas. O conteúdo será gerado normalmente, mas a etapa de publicação "
            "vai reportar 'não configurado' em vez de publicar de verdade."
        )

    logger.info("Gerando campanha para: %s", args.topic)

    resultado = InfoprodutoFactoryCrew().crew().kickoff(
        inputs={"produto_topico": args.topic, "imagem_padrao_url": args.image_url,
                "produto_catalogo": descrever_produto(produto)}
    )

    print("\n" + "=" * 80)
    print("RESULTADO FINAL")
    print("=" * 80)
    print(resultado)


if __name__ == "__main__":
    main()
