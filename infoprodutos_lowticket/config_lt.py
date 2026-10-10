"""Configuração exclusiva do crew low ticket. Variáveis de ambiente usam prefixo LT_
para não colidir com as dos outros robôs."""
import os

try:  # lê o .env local (no GitHub Actions as variáveis já vêm do ambiente)
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# --- Marca (Instagram + TikTok) ---
# Nome da página/perfil: "Fábrica de Produtos Digitais". Sem LT_BRAND_HANDLE, o rodapé dos cartões usa o nome.
# Para trocar sem editar o código: defina LT_BRAND_NAME e LT_BRAND_HANDLE.
BRAND_NAME = os.getenv("LT_BRAND_NAME", "").strip() or "Fábrica de Produtos Digitais"
BRAND_HANDLE = os.getenv("LT_BRAND_HANDLE", "").strip()  # ex.: @seuperfil (vazio = usa o nome da marca)
BRAND_TONE = "direto, prático, humano, sem promessas de enriquecimento fácil"

# --- Produto ---
OFFER_PRICE = "R$ 7,00"
OFFER_LINK = os.getenv("LT_OFFER_LINK", "").strip() or "https://SEU-LINK-DE-CHECKOUT"  # vazio no workflow = sem link real

# Catálogo único (fabrica_produtos): se houver um produto 'pronto' (com link de compra válido),
# ele manda no preço, no link e no que o anúncio pode afirmar. Sem produto pronto, o post de OFERTA
# é bloqueado em modo real (travas.py), porque não há o que vender nem o que conferir.
try:
    from fabrica_produtos import catalogo as _catalogo
    PRODUTO = _catalogo.produto_da_vez()
except Exception:  # catálogo ausente ou ilegível: segue sem produto
    PRODUTO = None
if PRODUTO:
    OFFER_PRICE = PRODUTO["preco_texto"]
    OFFER_LINK = PRODUTO["link_compra"]

NICHE = os.getenv("LT_NICHE", "infoprodutos digitais sobre assuntos diversos, sempre com temas atuais e em alta")

# --- LLM (Gemini por padrão; OpenRouter como reserva; Groq opcional) ---
# LT_LLM_PROVIDER=gemini (padrão), groq ou openrouter
LLM_PROVIDER = os.getenv("LT_LLM_PROVIDER", "gemini").strip().lower() or "gemini"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "").strip() or "qwen/qwen3-coder:free"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
GROQ_MODEL = os.getenv("GROQ_MODEL", "").strip() or "openai/gpt-oss-120b"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"

if LLM_PROVIDER == "openrouter":
    LLM_MODEL = OPENROUTER_MODEL
elif LLM_PROVIDER == "gemini":
    LLM_MODEL = (os.getenv("LT_LLM_MODEL", "").strip()
                 or os.getenv("GEMINI_MODEL", "").strip()
                 or "gemini/gemini-3.8-flash")
else:  # groq (API compatível com a da OpenAI)
    LLM_MODEL = GROQ_MODEL

# --- Publicação ---
DRY_RUN = os.getenv("LT_DRY_RUN", "true").lower() == "true"  # começa SEM publicar de verdade
IG_USER_ID = os.getenv("LT_IG_USER_ID", "")
IG_ACCESS_TOKEN = os.getenv("LT_IG_ACCESS_TOKEN", "")
TIKTOK_ACCESS_TOKEN = os.getenv("LT_TIKTOK_ACCESS_TOKEN", "")
# URL pública onde as imagens geradas ficam hospedadas (Instagram exige URL pública)
PUBLIC_IMAGE_BASE_URL = os.getenv("LT_PUBLIC_IMAGE_BASE_URL", "").rstrip("/")
OUTPUT_DIR = os.getenv("LT_OUTPUT_DIR", "output_lowticket")

# --- Meta (Facebook + Instagram) reaproveitando os Secrets do robô principal ---
META_TOKEN = os.getenv("META_LONG_LIVED_TOKEN", "")
FB_PAGE_ID = os.getenv("FB_PAGE_ID", "")
IG_ACCOUNT_ID = os.getenv("INSTAGRAM_ACCOUNT_ID", "") or IG_USER_ID
GRAPH_VERSION = os.getenv("META_GRAPH_API_VERSION", "").strip() or "v26.0"
META_CONFIGURADA = bool(META_TOKEN and FB_PAGE_ID and IG_ACCOUNT_ID)

# --- Foto de fundo (Unsplash) ---
UNSPLASH_API_KEY = os.getenv("UNSPLASH_API_KEY", "")
UNSPLASH_QUERY_PADRAO = os.getenv("LT_UNSPLASH_QUERY", "learning laptop notebook")

# TikTok só é usado se houver token E URL pública de imagens (exigência da API do TikTok)
TIKTOK_ATIVO = bool(TIKTOK_ACCESS_TOKEN and PUBLIC_IMAGE_BASE_URL)

# --- Roteiro diário ---
SLOTS = {
    "manha": {
        "tipo": "VALOR",
        "descricao": "Conteúdo de alto valor prático / quebra de objeção. Sem venda direta; CTA leve (seguir/salvar).",
    },
    "tarde": {
        "tipo": "OFERTA",
        "descricao": f"Oferta direta do infoproduto de {OFFER_PRICE}. CTA claro para o link. Sem exageros nem garantias de ganho.",
    },
}
