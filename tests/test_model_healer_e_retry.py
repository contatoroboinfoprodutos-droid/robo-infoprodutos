"""model_healer (modelo gratuito do OpenRouter), retry por rede, hashtags de 5 no Instagram e rodízio de nichos."""
import importlib
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import requests

from tests import stubs
stubs.instalar()

from agents import model_healer as mh  # noqa: E402
from fabrica_produtos import catalogo, config_fabrica as cfg, fabrica, marcas, travas  # noqa: E402
from tests.fixtures import produto_bom  # noqa: E402
from tests.test_integracao import LINK, carregar_lowticket  # noqa: E402


class Resp:
    def __init__(self, status=200, corpo=None, texto=""):
        self.status_code, self._c, self.text = status, corpo if corpo is not None else {}, texto or str(corpo)

    def json(self):
        return self._c

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


LISTA = {"data": [
    {"id": "qwen/qwen-2.5-coder-32b-instruct:free", "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "google/gemini-2.0-flash-exp:free", "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "meta-llama/llama-3.2-3b-instruct:free", "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "mistralai/mistral-7b-instruct:free", "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "qwen/qwen3-coder", "pricing": {"prompt": "0.0000003", "completion": "0.0000012"}},   # pago: nunca
    {"id": "algum/modelo:free", "pricing": {"prompt": "0.000001", "completion": "0"}},            # ':free' mas cobra
    {"id": "outro/gratis:free", "pricing": {"prompt": "0", "completion": "0"}},
]}


class Sessao:
    """Falso requests: GET lista modelos; POST responde conforme o modelo pedido."""

    def __init__(self, ok=(), lista=LISTA, status_modelo=None):
        self.ok, self.lista, self.status_modelo = set(ok), lista, status_modelo or {}
        self.posts = []

    def get(self, url, **k):
        return Resp(200, self.lista)

    def post(self, url, json=None, **k):
        self.posts.append(json["model"])
        if json["model"] in self.status_modelo:
            return Resp(*self.status_modelo[json["model"]])
        return Resp(200, {}) if json["model"] in self.ok else Resp(404, {}, "No endpoints found")


class Healer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = os.path.join(self.tmp.name, "free_models.json")
        p = mock.patch.object(mh, "CACHE_ARQUIVO", self.cache)
        p.start()
        self.addCleanup(p.stop)

    def test_so_modelos_gratis_de_verdade(self):
        achados = mh.listar_modelos_gratis(Sessao())
        self.assertNotIn("qwen/qwen3-coder", achados)
        self.assertNotIn("algum/modelo:free", achados)
        self.assertIn("outro/gratis:free", achados)

    def test_ordem_de_preferencia(self):
        self.assertEqual(mh.ordenar(mh.listar_modelos_gratis(Sessao()))[:4], list(mh.PREFERIDOS))

    def test_pega_o_primeiro_preferido_que_responde(self):
        s = Sessao(ok={"google/gemini-2.0-flash-exp:free", "meta-llama/llama-3.2-3b-instruct:free"})
        self.assertEqual(mh.get_free_model("k", sessao=s), "google/gemini-2.0-flash-exp:free")
        self.assertEqual(s.posts, ["google/gemini-2.0-flash-exp:free"])

    def test_pula_o_que_nao_responde(self):
        s = Sessao(ok={"qwen/qwen-2.5-coder-32b-instruct:free"})
        self.assertEqual(mh.get_free_model("k", sessao=s), "qwen/qwen-2.5-coder-32b-instruct:free")
        self.assertEqual(s.posts[:2], ["google/gemini-2.0-flash-exp:free", "meta-llama/llama-3.2-3b-instruct:free"])

    def test_cache_de_6h(self):
        s = Sessao(ok={"google/gemini-2.0-flash-exp:free"})
        mh.get_free_model("k", sessao=s, agora=1000.0)
        s2 = Sessao()
        self.assertEqual(mh.get_free_model("k", sessao=s2, agora=1000.0 + 3600), "google/gemini-2.0-flash-exp:free")
        self.assertEqual(s2.posts, [])                                   # nem testou: veio do cache
        s3 = Sessao(ok={"outro/gratis:free"})
        self.assertEqual(mh.get_free_model("k", sessao=s3, agora=1000.0 + 7 * 3600), "outro/gratis:free")

    def test_nenhum_responde_devolve_none_e_nunca_pago(self):
        s = Sessao()
        self.assertIsNone(mh.get_free_model("k", sessao=s))
        self.assertNotIn("qwen/qwen3-coder", s.posts)

    def test_sem_rede_devolve_none(self):
        class Quebrada:
            def get(self, *a, **k):
                raise requests.ConnectionError("sem rede")
        self.assertIsNone(mh.get_free_model("k", sessao=Quebrada()))

    def test_resolver_troca_modelo_404_e_avisa(self):
        s = Sessao(ok={"google/gemini-2.0-flash-exp:free"},
                   status_modelo={"qwen/qwen3-coder:free": (404, {}, "qwen/qwen3-coder:free not found: This model is "
                                                                     "unavailable for free. Use slug qwen/qwen3-coder")})
        with self.assertLogs("fabrica.model_healer", "WARNING") as logs:
            novo = mh.resolver_modelo("qwen/qwen3-coder:free", "k", s)
        self.assertEqual(novo, "google/gemini-2.0-flash-exp:free")
        self.assertTrue(any("AUTO-HEAL: OPENROUTER_MODEL desatualizado, usando google/gemini-2.0-flash-exp:free" in m
                            for m in logs.output))

    def test_resolver_mantem_modelo_que_funciona_ou_com_limite_de_uso(self):
        self.assertEqual(mh.resolver_modelo("a/b:free", "k", Sessao(ok={"a/b:free"})), "a/b:free")
        s = Sessao(status_modelo={"a/b:free": (429, {}, "rate limit")})
        self.assertEqual(mh.resolver_modelo("a/b:free", "k", s), "a/b:free")   # 429 não é motivo de troca

    def test_resolver_sem_nenhum_gratis_devolve_none(self):
        self.assertIsNone(mh.resolver_modelo("a/b:free", "k", Sessao()))

    def test_variable_so_e_atualizada_com_token_de_variables(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GH_PAT_VARIABLES", None)
            self.assertFalse(mh.atualizar_variable("x/y:free", executar=mock.Mock()))
        chamadas = mock.Mock(return_value=mock.Mock(returncode=0))
        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "o/r", "GH_PAT_VARIABLES": "t"}):
            self.assertTrue(mh.atualizar_variable("x/y:free", executar=chamadas))
        args = chamadas.call_args[0][0]
        self.assertIn("PATCH", args)
        self.assertIn("repos/o/r/actions/variables/OPENROUTER_MODEL", args)

    def test_proteger_llm_troca_o_modelo_no_meio_da_execucao_e_repete_uma_vez(self):
        class LLM:
            model = "openai/qwen/qwen3-coder:free"
            chamadas = []

            def call(self, *a, **k):
                self.chamadas.append(self.model)
                if self.model == "openai/qwen/qwen3-coder:free":
                    raise RuntimeError("404 qwen/qwen3-coder:free not found: This model is unavailable for free")
                return "ok"
        s = Sessao(ok={"google/gemini-2.0-flash-exp:free"})
        llm = mh.proteger_llm(LLM(), "k", sessao=s)
        self.assertEqual(llm.call("oi"), "ok")
        self.assertEqual(llm.chamadas, ["openai/qwen/qwen3-coder:free", "openai/google/gemini-2.0-flash-exp:free"])

    def test_proteger_llm_nao_mexe_em_outros_erros(self):
        class LLM:
            model = "openai/a/b:free"

            def call(self, *a, **k):
                raise RuntimeError("429 rate limit")
        llm = mh.proteger_llm(LLM(), "k", sessao=Sessao())
        with self.assertRaises(RuntimeError):
            llm.call("x")

    def test_resolver_publico_em_tools_model_resolver(self):
        from tools import model_resolver as mr
        self.assertIs(mr.get_free_model, mh.get_free_model)
        self.assertEqual(len(mr.PREFERIDOS), 4)
        self.assertEqual(mr.PREFERIDOS[3], "mistralai/mistral-7b-instruct:free")

    def test_indisponivel(self):
        self.assertTrue(mh.indisponivel("404 qwen/qwen3-coder:free not found: This model is unavailable for free"))
        self.assertFalse(mh.indisponivel("429 rate limit"))


class Retry(unittest.TestCase):
    def setUp(self):
        _, self.tools = carregar_lowticket("true")
        os.environ.pop("LT_DRY_RUN", None)
        p = mock.patch.object(self.tools.time, "sleep")
        self.sleep = p.start()
        self.addCleanup(p.stop)

    def test_repete_so_em_5xx_429_ou_rede_com_espera_crescente(self):
        resp = [Resp(500), Resp(429), Resp(200)]
        r = self.tools._com_tentativas(lambda: resp.pop(0))
        self.assertEqual(r.status_code, 200)
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [4.0, 8.0])

    def test_4xx_nao_repete(self):
        n = []
        r = self.tools._com_tentativas(lambda: (n.append(1), Resp(400))[1])
        self.assertEqual((r.status_code, len(n)), (400, 1))

    def test_desiste_depois_de_3(self):
        n = []
        r = self.tools._com_tentativas(lambda: (n.append(1), Resp(503))[1])
        self.assertEqual((r.status_code, len(n)), (503, 3))

    def test_excecao_de_rede_repete_e_depois_propaga(self):
        def falha():
            raise requests.ConnectionError("x")
        with self.assertRaises(requests.ConnectionError):
            self.tools._com_tentativas(falha)
        self.assertEqual(self.sleep.call_count, 2)


class InstagramCincoHashtags(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg.CATALOGO_PATH = os.path.join(self.tmp.name, "catalogo.json")
        os.environ["POST_MARKER_DIR"] = os.path.join(self.tmp.name, "m")
        self.addCleanup(os.environ.pop, "POST_MARKER_DIR", None)
        stubs.instalar_config(dry_run=False)
        sys.modules.pop("tools.crewai_meta_tools", None)
        self.mod = importlib.import_module("tools.crewai_meta_tools")
        p = mock.patch.object(self.mod.time, "sleep")
        p.start()
        self.addCleanup(p.stop)
        r = catalogo.adicionar(produto_bom(), "aprovado", "x")
        catalogo.atualizar(r["id"], "ok", status="pronto", link_compra=LINK, plataforma="cakto")

    def test_recusa_por_hashtags_repete_com_5(self):
        api = mock.MagicMock()
        api.publish_instagram_post.side_effect = [self.mod.MetaGraphAPIError("Too many hashtags (limit 5)"),
                                                  mock.Mock(post_id="IG1")]
        with mock.patch.object(self.mod, "MetaGraphAPI", return_value=api):
            r = self.mod.PublishToInstagramTool()._run(
                image_url="https://u/i.jpg", caption="Guia de rotina em PDF por R$ 7,00, com passos práticos para o dia a dia.")
        self.assertIn("sucesso", r)
        primeira, segunda = [c.kwargs["caption"] for c in api.publish_instagram_post.call_args_list]
        self.assertEqual(len(__import__("re").findall(r"#\w+", primeira)), 20)
        self.assertEqual(len(__import__("re").findall(r"#\w+", segunda)), 5)
        self.assertEqual(marcas.estado(("instagram",)), "completo")

    def test_erro_de_rede_repete_so_o_instagram_sem_duplicar(self):
        api = mock.MagicMock()
        api.publish_instagram_post.side_effect = [self.mod.MetaGraphAPIError("HTTP 500"), mock.Mock(post_id="IG1")]
        with mock.patch.object(self.mod, "MetaGraphAPI", return_value=api):
            r = self.mod.PublishToInstagramTool()._run(
                image_url="https://u/i.jpg", caption="Guia de rotina em PDF por R$ 7,00, com passos práticos para o dia a dia.")
        self.assertIn("sucesso", r)
        self.assertEqual(api.publish_instagram_post.call_count, 2)
        api.publish_facebook_post.assert_not_called()

    def test_falha_3x_devolve_erro(self):
        api = mock.MagicMock()
        api.publish_instagram_post.side_effect = self.mod.MetaGraphAPIError("HTTP 500")
        with mock.patch.object(self.mod, "MetaGraphAPI", return_value=api):
            r = self.mod.PublishToInstagramTool()._run(
                image_url="https://u/i.jpg", caption="Guia de rotina em PDF por R$ 7,00, com passos práticos para o dia a dia.")
        self.assertTrue(r.startswith("ERRO"))
        self.assertEqual(api.publish_instagram_post.call_count, 3)


class Rodizio(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg.CATALOGO_PATH = os.path.join(self.tmp.name, "catalogo.json")

    def test_tres_rodadas_seguidas_dao_tres_nichos_diferentes_sem_repetir_os_ultimos_5(self):
        escolhidos = []
        for i in range(6):
            tema = fabrica.tema_da_rodada("")
            escolhidos.append(tema)
            catalogo.adicionar(produto_bom(nome=f"Guia {tema} {i}", nicho=tema), "aprovado", "x")
        self.assertEqual(len(set(escolhidos)), 6, escolhidos)

    def test_novos_nichos_existem(self):
        for n in ("Finanças domésticas", "Organização da casa", "Renda extra"):
            self.assertTrue(any(x.startswith(n) for x in cfg.NICHOS), n)

    def test_nicho_com_nome_ja_usado_e_pulado_mesmo_sem_campo_nicho(self):
        catalogo.adicionar(produto_bom(nome="Guia Prático de Finanças Pessoais para iniciantes"), "aprovado", "x")
        proximo = fabrica.tema_da_rodada("")
        self.assertNotIn("Finanças pessoais", proximo)


if __name__ == "__main__":
    unittest.main()
