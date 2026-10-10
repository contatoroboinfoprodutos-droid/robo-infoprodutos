"""Cliente da Cakto com a rede simulada (nenhuma chamada real é feita)."""
import json
import os
import tempfile
import unittest
from unittest import mock

from fabrica_produtos import catalogo, config_fabrica as cfg, plataformas, registrador
from tests.fixtures import produto_bom


class Resp:
    def __init__(self, status=200, corpo=None, headers=None):
        self.status_code, self._corpo, self.headers = status, corpo if corpo is not None else {}, headers or {}
        self.text = json.dumps(self._corpo)

    def json(self):
        return self._corpo


def produto_api(nome="Guia Prático de Rotina em 7 Dias", status="active", oferta_status="active", pid="uuid-1"):
    return {"id": pid, "name": nome, "status": status,
            "offers": [{"id": "OFERTA2", "default": False, "status": "active"},
                       {"id": "77BcHrY", "default": True, "status": oferta_status}]}


class Servidor:
    """Simula a API: guarda as chamadas e responde conforme `rotas`."""

    def __init__(self, rotas):
        self.rotas, self.chamadas = rotas, []

    def __call__(self, metodo, url, **kw):
        self.chamadas.append((metodo, url, kw))
        caminho = url.split("api.cakto.com.br")[1]
        item = self.rotas.get((metodo, caminho)) or Resp(404, {"detail": "Not found"})  # rota fora da doc = 404
        if isinstance(item, list):
            item = item.pop(0)
        return item(kw) if callable(item) else item


def cliente(rotas):
    os.environ["CAKTO_CLIENT_ID"], os.environ["CAKTO_CLIENT_SECRET"] = "id-falso", "segredo-falso"
    srv = Servidor({("POST", "/public_api/token/"): Resp(200, {"access_token": "tok", "expires_in": 36000}),
                    **rotas})
    return plataformas.Cakto(), srv


NOME_PADRAO = produto_bom()["nome"]


class ClienteCakto(unittest.TestCase):
    def setUp(self):
        for k in ("CAKTO_CLIENT_ID", "CAKTO_CLIENT_SECRET", "CAKTO_SALES_PAGE"):
            self.addCleanup(os.environ.pop, k, None)
        p = mock.patch.object(plataformas.time, "sleep")
        p.start()
        self.addCleanup(p.stop)

    def usar(self, srv):
        p = mock.patch.object(plataformas.requests, "request", srv)
        p.start()
        self.addCleanup(p.stop)

    def test_credenciais_faltando_so_mostra_nomes(self):
        for k in ("CAKTO_CLIENT_ID", "CAKTO_CLIENT_SECRET"):
            os.environ.pop(k, None)
        c = plataformas.Cakto()
        self.assertFalse(c.configurada())
        self.assertEqual(c.faltando(), ["CAKTO_CLIENT_ID", "CAKTO_CLIENT_SECRET"])

    def test_busca_exata_monta_link_da_oferta_padrao(self):
        c, srv = cliente({("GET", "/public_api/products/"): Resp(200, {"count": 2, "next": None, "results": [
                              {"id": "x", "name": "Guia Prático de Rotina em 7 Dias Avançado"},
                              {"id": "uuid-1", "name": "guia pratico de rotina em 7 dias"}]}),
                          ("GET", "/public_api/products/uuid-1/"): Resp(200, produto_api())})
        self.usar(srv)
        r = c.buscar_por_nome("Guia Prático de Rotina em 7 Dias")
        self.assertEqual(r["id"], "uuid-1")
        self.assertTrue(r["ativo"])
        self.assertEqual(r["link"], "https://pay.cakto.com.br/77BcHrY")  # oferta default, não a primeira

    def test_nao_ativo_nao_conta_como_ativo(self):
        c, srv = cliente({("GET", "/public_api/products/"): Resp(200, {"results": [{"id": "uuid-1", "name": "G"}]}),
                          ("GET", "/public_api/products/uuid-1/"): Resp(200, produto_api("G", status="waiting_config"))})
        self.usar(srv)
        self.assertFalse(c.buscar_por_nome("G")["ativo"])

    def test_sem_correspondencia_exata_devolve_none(self):
        c, srv = cliente({("GET", "/public_api/products/"): Resp(200, {"results": [{"id": "a", "name": "Outro"}]})})
        self.usar(srv)
        self.assertIsNone(c.buscar_por_nome("Guia"))

    def test_criar_sem_entrega_nasce_waiting_config(self):
        corpos = []
        c, srv = cliente({("GET", "/public_api/products/"): Resp(200, {"results": []}),
                          ("POST", "/public_api/products/"):
                              lambda kw: (corpos.append(kw["json"]),
                                          Resp(201, produto_api(status="waiting_config")))[1]})
        self.usar(srv)
        r = c.criar_produto({**produto_bom(), "preco": 7.0}, "x.pdf")
        corpo = corpos[0]
        self.assertEqual((corpo["price"], corpo["currency"], corpo["type"], corpo["status"]),
                         ("7.00", "BRL", "unique", "waiting_config"))
        self.assertNotIn("emailAccessLink", corpo)
        self.assertFalse(r["ativo"])  # waiting_config nunca é liberado
        self.assertEqual(r["link"], "https://pay.cakto.com.br/77BcHrY")

    def test_criar_com_entrega_e_pagina_de_vendas(self):
        os.environ["CAKTO_SALES_PAGE"] = "https://loja.exemplo.com.br/p"
        corpos = []
        com_imagem = {**produto_api(), "image": "https://x/capa.png"}
        c, srv = cliente({("GET", "/public_api/products/"): Resp(200, {"results": []}),
                          ("POST", "/public_api/products/"):
                              lambda kw: (corpos.append(kw["json"]), Resp(201, produto_api(status="waiting_config")))[1],
                          ("PUT", "/public_api/products/uuid-1/"): Resp(200, {}),
                          ("GET", "/public_api/products/uuid-1/"): [Resp(200, com_imagem), Resp(200, com_imagem)]})
        self.usar(srv)
        r = c.criar_produto({**produto_bom(), "preco": 7.0}, "x.pdf", url_entrega="https://drive.exemplo/pdf",
                            urls_imagem=["https://x/capa.png"])
        self.assertEqual(corpos[0]["status"], "waiting_config")   # só ativa depois da capa
        self.assertEqual(corpos[0]["contentDeliveries"], ["emailAccess"])
        self.assertEqual(corpos[0]["emailAccessLink"], "https://drive.exemplo/pdf")
        self.assertEqual(corpos[0]["salesPage"], "https://loja.exemplo.com.br/p")
        puts = [kw["json"] for m, u, kw in srv.chamadas if m == "PUT"]
        self.assertEqual(puts[0]["image"], "https://x/capa.png")
        self.assertEqual({k: puts[0][k] for k in ("name", "price")}, {"name": NOME_PADRAO, "price": "7.00"})
        self.assertIn("description", puts[0])
        self.assertEqual(puts[1], {"status": "active"})
        self.assertTrue(r["ativo"])

    def test_criar_nao_duplica_quando_ja_existe(self):
        c, srv = cliente({("GET", "/public_api/products/"): Resp(200, {"results": [{"id": "uuid-1", "name": produto_bom()["nome"]}]}),
                          ("GET", "/public_api/products/uuid-1/"): Resp(200, produto_api())})
        self.usar(srv)
        r = c.criar_produto({**produto_bom(), "preco": 7.0}, "x.pdf")
        self.assertTrue(r["existente"])
        self.assertFalse(any(m == "POST" and u.endswith("/products/") for m, u, _ in srv.chamadas))

    def test_401_renova_token_e_429_espera(self):
        c, srv = cliente({("GET", "/public_api/products/"): [Resp(401), Resp(429, headers={"Retry-After": "2"}),
                                                              Resp(200, {"results": []})]})
        self.usar(srv)
        self.assertEqual(c.listar_por_nome("x"), [])
        tokens = [1 for m, u, _ in srv.chamadas if u.endswith("/token/")]
        self.assertEqual(len(tokens), 2)  # autenticou de novo depois do 401
        plataformas.time.sleep.assert_called_with(2.0)

    def test_erros_nao_vazam_credenciais(self):
        c, srv = cliente({("GET", "/public_api/products/"): Resp(500)})
        self.usar(srv)
        with self.assertRaises(plataformas.ErroPlataforma) as ctx:
            c.listar_por_nome("x")
        self.assertNotIn("segredo-falso", str(ctx.exception))
        self.assertNotIn("tok", str(ctx.exception).replace("token", ""))
        srv2 = Servidor({("POST", "/public_api/token/"): Resp(401)})
        self.usar(srv2)
        with self.assertRaises(plataformas.ErroPlataforma) as ctx:
            plataformas.Cakto()._autenticar()
        self.assertIn("HTTP 401", str(ctx.exception))
        self.assertNotIn("segredo-falso", str(ctx.exception))

    def test_sondar_so_le(self):
        c, srv = cliente({("GET", "/public_api/products/"): Resp(200, {"count": 3, "results": [produto_api()]})})
        self.usar(srv)
        texto = "\n".join(c.sondar())
        self.assertIn("3 produto(s)", texto)
        self.assertNotIn("segredo-falso", texto)
        self.assertTrue(all(m in ("GET", "POST") and (m == "GET" or u.endswith("/token/"))
                            for m, u, _ in srv.chamadas))


class FakeCakto:
    nome = "cakto"

    def __init__(self, retorno=None, erros=0):
        self.retorno, self.erros, self.chamadas = retorno, erros, 0

    def configurada(self):
        return True

    def faltando(self):
        return []

    def criar_produto(self, p, pdf, url_entrega=None):
        self.chamadas += 1
        if self.chamadas <= self.erros:
            raise plataformas.ErroPlataforma("cakto: falha de rede")
        return self.retorno

    def buscar_por_nome(self, nome):
        return None


class RegistroComCriacao(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg.CATALOGO_PATH = os.path.join(self.tmp.name, "c.json")
        cfg.PACOTES_DIR = os.path.join(self.tmp.name, "pac")
        self.r = catalogo.adicionar(produto_bom(), "aprovado", "x")

    def registrar(self, fake):
        p = mock.patch.object(plataformas, "instanciar", lambda nomes: [fake])
        p.start()
        self.addCleanup(p.stop)
        return registrador.registrar_produto(self.r["id"], dry_run=False)

    def test_ativo_com_link_vira_pronto(self):
        fake = FakeCakto({"id": "u", "status": "active", "ativo": True, "link": "https://pay.cakto.com.br/77BcHrY",
                          "existente": False})
        self.registrar(fake)
        p = catalogo.obter(self.r["id"])
        self.assertEqual((p["status"], p["plataforma"], p["link_compra"]),
                         ("pronto", "cakto", "https://pay.cakto.com.br/77BcHrY"))

    def test_waiting_config_fica_aguardando_e_sem_link_liberado(self):
        fake = FakeCakto({"id": "u", "status": "waiting_config", "ativo": False,
                          "link": "https://pay.cakto.com.br/77BcHrY", "existente": False})
        linhas = self.registrar(fake)
        p = catalogo.obter(self.r["id"])
        self.assertEqual(p["status"], "aguardando_cadastro")
        self.assertEqual(p["link_compra"], "")  # link só entra no catálogo quando liberado
        self.assertIsNone(catalogo.produto_ativo())
        self.assertTrue(any("ainda não liberado" in l for l in linhas))

    def test_erro_de_rede_tenta_de_novo(self):
        fake = FakeCakto({"id": "u", "status": "active", "ativo": True, "link": "https://pay.cakto.com.br/A1",
                          "existente": True}, erros=1)
        self.registrar(fake)
        self.assertEqual(fake.chamadas, 2)
        self.assertEqual(catalogo.obter(self.r["id"])["status"], "pronto")

    def test_falha_persistente_nao_derruba(self):
        fake = FakeCakto(erros=5)
        self.registrar(fake)
        self.assertEqual(catalogo.obter(self.r["id"])["status"], "aguardando_cadastro")

    def test_verificar_so_libera_se_ativo(self):
        class F(FakeCakto):
            def buscar_por_nome(self, nome):
                return {"id": "u", "status": "waiting_config", "ativo": False,
                        "link": "https://pay.cakto.com.br/77BcHrY"}
        catalogo.atualizar(self.r["id"], "x", status="aguardando_cadastro")
        p = mock.patch.object(plataformas, "instanciar", lambda nomes: [F()])
        p.start()
        self.addCleanup(p.stop)
        linhas = registrador.verificar_links()
        self.assertEqual(catalogo.obter(self.r["id"])["status"], "aguardando_cadastro")
        self.assertTrue(any("ainda não está ativo" in l for l in linhas))


if __name__ == "__main__":
    unittest.main()
