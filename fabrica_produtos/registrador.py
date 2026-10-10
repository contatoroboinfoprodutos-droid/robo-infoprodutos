"""Registrador: leva o produto aprovado até o status 'pronto' (com link de compra válido).

Caminhos, nesta ordem, sem nunca travar o fluxo:
  1. tenta criar o produto na plataforma por API (hoje: nenhuma confirmada, ver plataformas.py);
  2. gera o PACOTE (PDF + ficha de cadastro) e deixa o produto em 'aguardando_cadastro';
  3. a cada execução, procura o produto pelo NOME EXATO nas APIs e, se achar com link, libera ('pronto');
  4. se a API não devolver o link, `definir-link` grava o link manualmente.
Falha de uma plataforma nunca derruba as outras nem os outros produtos.
"""
import logging
import os
import re

from . import catalogo, config_fabrica as cfg, plataformas
from .drive import Drive, ErroDrive
from .pdf_produto import gerar_pdf
from .texto import normalizar
from .travas import link_ok

logger = logging.getLogger("fabrica")


def _slug(nome: str) -> str:
    return re.sub(r"\s+", "-", normalizar(nome))[:60] or "produto"


def pacote_dir(produto: dict) -> str:
    return os.path.join(cfg.PACOTES_DIR, produto["id"])


def _ficha(produto: dict, pdf_nome: str) -> str:
    itens = "\n".join(f"- {c}" for c in produto.get("conteudos") or [])
    return (
        f"# Ficha do produto: {produto['nome']}\n\n"
        f"Produto do catálogo `{produto['id']}`.\n\n"
        f"Se a criação por API estiver ligada (`FABRICA_DRY_RUN=false`), o produto já foi criado na Cakto "
        f"com estes dados e está em `waiting_config`. Se não, cadastre-o na Cakto com estes dados.\n\n"
        f"**O NOME precisa ficar exatamente assim**: o robô encontra o produto pelo nome (sem diferença de "
        f"acento ou maiúscula) e então libera o anúncio sozinho.\n\n"
        f"- Nome: {produto['nome']}\n"
        f"- Preço: {produto['preco_texto']}\n"
        f"- Tipo de entrega: arquivo digital (PDF)\n"
        f"- Arquivo da entrega: `{pdf_nome}` (nesta mesma pasta)\n\n"
        f"## Descrição da oferta (copie e cole)\n\n{produto.get('descricao_oferta', '')}\n\n"
        f"## O que vem dentro\n\n{itens}\n\n"
        f"## Para liberar o anúncio\n\n"
        f"1. Se o relatório da execução disser que o PDF foi hospedado no Drive, o produto já foi criado ativo e "
        f"com a entrega configurada: pule para o passo 2. Se não, no painel da Cakto abra o produto, configure "
        f"a entrega com o PDF acima e **ative** o produto.\n"
        f"2. Rode a ação `verificar` no GitHub (Actions → Fábrica de Produtos), ou espere a verificação "
        f"automática de 6 em 6 horas. O robô só libera se o produto estiver **ativo** e a API devolver o link.\n"
        f"3. Se a API não devolver o link, copie o link de compra do painel e rode a ação `definir-link` "
        f"com o id `{produto['id']}` e o link.\n"
        f"4. Só depois disso os robôs de anúncio passam a divulgar o produto.\n"
    )


def gerar_pacote(produto: dict) -> dict:
    pasta = pacote_dir(produto)
    os.makedirs(pasta, exist_ok=True)
    pdf_nome = f"{_slug(produto['nome'])}.pdf"
    pdf = gerar_pdf(produto, os.path.join(pasta, pdf_nome))
    ficha = os.path.join(pasta, "ficha_cadastro.md")
    with open(ficha, "w", encoding="utf-8") as f:
        f.write(_ficha(produto, pdf_nome))
    return {"pasta": pasta, "pdf": pdf, "ficha": ficha}


def gerar_capa_produto(produto: dict, linhas: list[str]) -> str | None:
    """Capa PNG em docs/capas/{id}.png (o commit do workflow publica no repositório). Nunca derruba o registro."""
    try:
        from . import capa
        caminho = capa.gerar_capa(produto)
        linhas.append(f"capa gerada em {caminho}")
        return caminho
    except Exception as e:  # sem Pillow/fonte: o produto segue sem capa, com o aviso no relatório
        linhas.append(f"CAKTO_IMAGEM_ERROR: não consegui gerar a capa ({type(e).__name__}: {e})")
        return None


def _acessivel(url: str) -> bool:
    """A URL da capa responde 200 com imagem? Não adianta mandar à Cakto um endereço que ainda não existe."""
    import requests
    try:
        r = requests.get(url, timeout=20, stream=True)
        ok = r.status_code == 200 and "image" in r.headers.get("Content-Type", "")
        r.close()
        return ok
    except requests.RequestException:
        return False


REPO_PADRAO = "fabricadeprodutosdigitais/fabrica-de-produtos-digitais"


def urls_da_capa(produto: dict, capa_path: str | None, linhas: list[str]) -> list[str]:
    """URLs públicas candidatas da capa, na ordem: GitHub Pages (Content-Type image/png garantido, site com .nojekyll) e
    GitHub raw. O Drive NÃO é usado: a conta de serviço não tem cota no Meu Drive. Cada URL só entra se já responder
    como imagem (ou seja, depois do commit do workflow). O teste é feito do runner do GitHub, não da Cakto: se a Cakto
    não consegue baixar, o `CAKTO_IMAGEM_DEBUG` da tentativa mostra o que a API devolveu."""
    repo = os.getenv("GITHUB_REPOSITORY", "").strip() or REPO_PADRAO
    dono, _, nome_repo = repo.partition("/")
    ref = os.getenv("GITHUB_REF_NAME", "").strip() or "main"
    candidatas = [f"https://{dono}.github.io/{nome_repo}/docs/capas/{produto['id']}.png",
                  f"https://raw.githubusercontent.com/{repo}/{ref}/docs/capas/{produto['id']}.png"]
    urls = []
    for u in candidatas:
        ok = _acessivel(u)
        print(f"CAKTO_IMAGEM_DEBUG url={u} responde_como_imagem_no_runner={ok}", flush=True)
        if ok:
            urls.append(u)
    if not urls:
        linhas.append(f"{produto['id']}: a capa ainda não responde como imagem no GitHub Pages nem no raw; "
                      "segue com o envio do arquivo e a próxima execução usa a URL")
    return urls


def enviar_capas(nomes: list[str] | None = None) -> list[str]:
    """Conserta produtos que já existem na Cakto 'Sem imagem': gera a capa, envia e, se o produto estava esperando
    só por isso (waiting_config com entrega configurada), ativa. Idempotente: produto que já tem imagem é pulado."""
    linhas = []
    ativas = [pl for pl in plataformas.instanciar(nomes or cfg.PLATAFORMAS_ALVO) if pl.configurada()]
    if not ativas:
        return ["nenhuma plataforma configurada: defina os Secrets CAKTO_CLIENT_ID e CAKTO_CLIENT_SECRET"]
    alvo = [p for p in catalogo.carregar()["produtos"] if p.get("status") in ("pronto", "aguardando_cadastro")]
    if not alvo:
        return ["nenhum produto na Cakto para conferir"]
    for p in alvo:
        for plat in ativas:
            try:
                achado = plat.buscar_por_nome(p["nome"])
                if not achado and p.get("status") == "aguardando_cadastro":
                    # nunca chegou na Cakto (ex.: p20261004-1): cria agora, sem entrega (o Drive não grava), em
                    # waiting_config; a capa sobe em seguida e a entrega do PDF você configura no painel
                    pacote = gerar_pacote(p)
                    criado = plat.criar_produto(p, pacote["pdf"])
                    linhas.append(f"{p['id']}: não existia na {plat.nome}; criado agora (status {criado.get('status')}, "
                                  "sem entrega do PDF: configure no painel e ative)")
                    achado = plat.buscar_por_nome(p["nome"]) or criado
                if not achado:
                    linhas.append(f"{p['id']}: não encontrado na {plat.nome} pelo nome")
                    continue
                if achado.get("imagem"):
                    linhas.append(f"{p['id']}: já tem imagem")
                    continue
                capa_path = gerar_capa_produto(p, linhas)
                try:
                    plat.enviar_imagem(achado["id"], urls_da_capa(p, capa_path, linhas), capa_path, p)
                except plataformas.ErroPlataforma as e:
                    linhas.append(f"CAKTO_IMAGEM_ERROR: {p['id']} http={e.codigo or '-'} {e}")
                    print(f"CAKTO_IMAGEM_ERROR produto={p['id']} http={e.codigo or '-'} {e}", flush=True)
                    continue
                linhas.append(f"{p['id']}: capa enviada")
                if achado.get("status") == "waiting_config" and achado.get("entrega"):
                    r = plat.ativar(achado["id"])
                    linhas.append(f"{p['id']}: ativado ({r.get('status')})")
            except plataformas.ErroPlataforma as e:
                linhas.append(f"{p['id']} / {plat.nome}: erro ({e})")
    return linhas


def publicar_pdf(produto: dict, pacote: dict, linhas: list[str]) -> str | None:
    """Hospeda o PDF no Drive e devolve o link, ou None (e explica no relatório) se não for possível.
    Nunca levanta exceção: sem link o produto simplesmente nasce em 'waiting_config'."""
    drive = Drive()
    if not drive.configurada():
        linhas.append(f"drive: não configurado (faltam {', '.join(drive.faltando())}); o PDF não foi hospedado")
        return None
    nome = f"{produto['id']}-{os.path.basename(pacote['pdf'])}"
    try:
        url = drive.publicar_pdf(pacote["pdf"], nome)
    except ErroDrive as e:
        linhas.append(f"{e}. O produto será criado sem a entrega (waiting_config)")
        return None
    linhas.append("drive: PDF hospedado e com leitura por link liberada")
    return url


def registrar_produto(produto_id: str, dry_run: bool | None = None, nomes: list[str] | None = None) -> list[str]:
    """Devolve as linhas do relatório. Não levanta exceção por falha de plataforma."""
    dry_run = cfg.DRY_RUN if dry_run is None else dry_run
    p = catalogo.obter(produto_id)
    if not p or p.get("status") != "aprovado":
        return [f"produto {produto_id}: nada a registrar (status {p.get('status') if p else 'inexistente'})"]
    pacote = gerar_pacote(p)
    linhas = [f"pacote gerado em {pacote['pasta']}"]
    capa_path = gerar_capa_produto(p, linhas)
    criado = None

    if dry_run:
        linhas.append("DRY_RUN: nenhuma criação foi feita nas plataformas (leitura continua permitida)")
    else:
        plats = [pl for pl in plataformas.instanciar(nomes or cfg.PLATAFORMAS_ALVO)]
        # Só hospeda o PDF se houver alguma plataforma pronta para usá-lo (evita envio à toa).
        url_entrega = publicar_pdf(p, pacote, linhas) if any(pl.configurada() for pl in plats) else None
        extra = {"url_entrega": url_entrega} if url_entrega else {}
        if url_entrega:
            extra.update({"urls_imagem": urls_da_capa(p, capa_path, linhas), "capa_path": capa_path})
        for plat in plats:
            if not plat.configurada():
                linhas.append(f"{plat.nome}: pulada (faltam variáveis: {', '.join(plat.faltando())})")
                continue
            # criar_produto procura pelo nome antes de criar: repetir a chamada após um erro nunca duplica.
            for tentativa in (1, 2):
                try:
                    r = plat.criar_produto(p, pacote["pdf"], **extra)
                    origem = "já existia" if r.get("existente") else "criado"
                    linhas.append(f"{plat.nome}: produto {origem} (id {r.get('id', '?')}, "
                                  f"status {r.get('status', '?')})")
                    if r.get("imagem_erro"):
                        linhas.append(f"CAKTO_IMAGEM_ERROR: {r['imagem_erro']}. O produto fica em 'waiting_config' até a capa "
                                      "subir (rode a ação `capas`)")
                    elif url_entrega:
                        linhas.append(f"{plat.nome}: capa enviada ({r.get('imagem', '')[:70] or 'ok'})")
                    if r.get("ativo") and link_ok(r.get("link", "")):
                        if criado is None:
                            criado = (plat.nome, r["link"])
                    else:
                        linhas.append(f"{plat.nome}: ainda não liberado para venda. Configure a entrega do PDF "
                                      "no painel e ative o produto; o robô libera sozinho em seguida")
                    break
                except plataformas.NaoSuportado as e:
                    linhas.append(str(e))
                    break
                except plataformas.ErroPlataforma as e:
                    linhas.append(f"{plat.nome}: erro na tentativa {tentativa}: {e}")

    if criado:
        catalogo.atualizar(p["id"], f"registrado na {criado[0]} por API; link recebido", status="pronto",
                           plataforma=criado[0], link_compra=criado[1])
        linhas.append(f"produto liberado (pronto) com o link da {criado[0]}")
    else:
        catalogo.atualizar(p["id"], "pacote pronto; aguardando cadastro na plataforma",
                           status="aguardando_cadastro")
        linhas.append("status: aguardando_cadastro (cadastre pelo pacote ou aguarde a criação por API)")
    linhas += verificar_links(nomes)
    return linhas


def verificar_links(nomes: list[str] | None = None) -> list[str]:
    """Somente leitura nas plataformas: procura cada produto pendente pelo nome e libera se achar o link."""
    linhas = []
    pendentes = catalogo.por_status("aguardando_cadastro")
    if not pendentes:
        return ["nenhum produto aguardando cadastro"]
    ativas = [pl for pl in plataformas.instanciar(nomes or cfg.PLATAFORMAS_ALVO) if pl.configurada()]
    if not ativas:
        return ["nenhuma plataforma configurada: defina os Secrets CAKTO_CLIENT_ID e CAKTO_CLIENT_SECRET"]
    for p in pendentes:
        liberado = False
        for plat in ativas:
            try:
                achado = plat.buscar_por_nome(p["nome"])
            except plataformas.ErroPlataforma as e:
                linhas.append(f"{p['id']} / {plat.nome}: não consegui consultar ({e})")
                continue
            if not achado:
                linhas.append(f"{p['id']} / {plat.nome}: produto ainda não encontrado pelo nome")
                continue
            if achado.get("ativo") and link_ok(achado.get("link", "")):
                catalogo.atualizar(p["id"], f"encontrado na {plat.nome}; link de compra recebido pela API",
                                   status="pronto", plataforma=plat.nome, link_compra=achado["link"])
                linhas.append(f"{p['id']}: LIBERADO com o link da {plat.nome}")
                liberado = True
                break
            if not achado.get("ativo"):
                aviso = (f"encontrado na {plat.nome}, mas ainda não está ativo "
                         f"(status {achado.get('status')}): configure a entrega do PDF e ative o produto")
            else:
                aviso = f"encontrado na {plat.nome}, mas a API não devolveu o link: use definir-link"
            if not p.get("historico") or p["historico"][-1].get("evento") != aviso:
                catalogo.atualizar(p["id"], aviso)
            linhas.append(f"{p['id']}: {aviso}")
        if liberado:
            continue
    return linhas


def definir_link(produto_id: str, link: str, plataforma: str = "") -> str:
    """Grava o link de compra copiado do painel da plataforma e libera o produto."""
    link = (link or "").strip()
    if not link_ok(link):
        raise ValueError("link inválido: precisa começar com https:// e não pode ser o link de exemplo")
    p = catalogo.obter(produto_id)
    if not p:
        raise KeyError(f"produto {produto_id} não existe no catálogo")
    if p.get("status") not in ("aprovado", "aguardando_cadastro", "pronto"):
        raise ValueError(f"produto {produto_id} está '{p.get('status')}' e não pode ser liberado")
    catalogo.atualizar(produto_id, "link de compra definido manualmente; produto liberado", status="pronto",
                       plataforma=plataforma or p.get("plataforma", ""), link_compra=link)
    return f"{produto_id} liberado com o link informado"
