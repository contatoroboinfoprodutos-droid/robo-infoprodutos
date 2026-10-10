"""Catálogo único de produtos (JSON versionado no repositório). Biblioteca padrão apenas.

Status do produto:
  aprovado             criado, passou no orientador e nas travas; ainda não está à venda
  aguardando_cadastro  pacote pronto; falta cadastrar na plataforma (ou a plataforma não devolveu o link)
  pronto               tem link de compra válido: SÓ ESTE status libera anúncio de venda
  reprovado            não passou depois de todas as rodadas (o motivo fica no histórico)
"""
import datetime
import json
import os
import tempfile

from . import config_fabrica as cfg
from .texto import formatar_preco, normalizar, parse_preco
from .travas import link_ok

STATUS = ("aprovado", "aguardando_cadastro", "pronto", "reprovado")
_FUSO = datetime.timezone(datetime.timedelta(hours=-3))  # Brasília


def _caminho(caminho: str | None) -> str:
    return caminho or cfg.CATALOGO_PATH


def _agora() -> datetime.datetime:
    return datetime.datetime.now(_FUSO)


def carregar(caminho: str | None = None) -> dict:
    p = _caminho(caminho)
    if not os.path.exists(p):
        return {"versao": 1, "produtos": []}
    with open(p, encoding="utf-8") as f:
        cat = json.load(f)
    cat.setdefault("produtos", [])
    return cat


def salvar(cat: dict, caminho: str | None = None) -> None:
    """Grava de forma atômica (arquivo temporário + rename) para nunca deixar JSON pela metade."""
    p = _caminho(caminho)
    pasta = os.path.dirname(p) or "."
    os.makedirs(pasta, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=pasta, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cat, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _evento(produto: dict, texto: str) -> None:
    produto.setdefault("historico", []).append({"quando": _agora().isoformat(timespec="seconds"),
                                                "evento": texto})


def adicionar(produto: dict, status: str, evento: str, caminho: str | None = None) -> dict:
    """Acrescenta um produto ao catálogo, com id novo, e devolve o registro salvo."""
    cat = carregar(caminho)
    hoje = _agora().strftime("%Y%m%d")
    n = 1 + sum(1 for p in cat["produtos"] if str(p.get("id", "")).startswith(f"p{hoje}-"))
    preco = parse_preco(produto.get("preco"))
    reg = dict(produto)
    reg.update({
        "id": f"p{hoje}-{n}",
        "criado_em": _agora().isoformat(timespec="seconds"),
        "preco": preco,
        "preco_texto": formatar_preco(preco) if preco is not None else "",
        "status": status,
        "plataforma": "",
        "link_compra": "",
        "historico": [],
    })
    _evento(reg, evento)
    cat["produtos"].append(reg)
    salvar(cat, caminho)
    return reg


def obter(produto_id: str, caminho: str | None = None) -> dict | None:
    return next((p for p in carregar(caminho)["produtos"] if p.get("id") == produto_id), None)


def atualizar(produto_id: str, evento: str, caminho: str | None = None, **campos) -> dict:
    cat = carregar(caminho)
    for p in cat["produtos"]:
        if p.get("id") == produto_id:
            p.update(campos)
            _evento(p, evento)
            salvar(cat, caminho)
            return p
    raise KeyError(f"produto {produto_id} não existe no catálogo")


def por_status(*status: str, caminho: str | None = None) -> list[dict]:
    return [p for p in carregar(caminho)["produtos"] if p.get("status") in status]


def nomes_existentes(caminho: str | None = None) -> list[str]:
    return [p.get("nome", "") for p in carregar(caminho)["produtos"]]


def nome_ja_existe(nome: str, caminho: str | None = None) -> bool:
    alvo = normalizar(nome)
    return any(normalizar(n) == alvo for n in nomes_existentes(caminho))


def criados_hoje(caminho: str | None = None) -> int:
    """Produtos aprovados hoje (os reprovados não contam: podem ser refeitos no mesmo dia)."""
    hoje = _agora().strftime("%Y-%m-%d")
    return sum(1 for p in carregar(caminho)["produtos"]
               if str(p.get("criado_em", "")).startswith(hoje) and p.get("status") != "reprovado")


def produto_ativo(caminho: str | None = None) -> dict | None:
    """O produto mais recente com status 'pronto' E link de compra válido (é o que os robôs anunciam)."""
    for p in reversed(carregar(caminho)["produtos"]):
        if p.get("status") == "pronto" and link_ok(p.get("link_compra", "")):
            return p
    return None


def produtos_prontos(caminho: str | None = None) -> list[dict]:
    """Todos os produtos 'pronto' com link de compra válido, do mais antigo ao mais novo."""
    return [p for p in carregar(caminho)["produtos"]
            if p.get("status") == "pronto" and link_ok(p.get("link_compra", ""))]


def produto_da_vez(caminho: str | None = None, agora=None) -> dict | None:
    """Rodízio entre os produtos prontos, para o feed não repetir sempre a mesma capa.

    Cada dia tem 2 fatias (antes/depois das 20h UTC), então produtos consecutivos aparecem em
    sequência. PRODUTO_DA_VEZ_ID força um produto (usado para main.py, ferramentas e config_lt
    concordarem dentro da mesma execução). Sem produtos prontos devolve None."""
    prontos = produtos_prontos(caminho)
    if not prontos:
        return None
    forcado = os.getenv("PRODUTO_DA_VEZ_ID", "").strip()
    if forcado:
        for p in prontos:
            if p.get("id") == forcado:
                return p
    agora = agora or _agora()
    indice = (agora.toordinal() * 2 + (1 if agora.hour >= 20 else 0)) % len(prontos)
    escolhido = prontos[indice]
    os.environ["PRODUTO_DA_VEZ_ID"] = escolhido.get("id", "")
    return escolhido
