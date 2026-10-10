"""Rodízio de produtos: o feed não repete sempre o produto mais novo."""
import datetime
import os
import tempfile
import unittest

from tests import stubs
stubs.instalar()

from fabrica_produtos import catalogo  # noqa: E402

LINK = "https://pay.cakto.com.br/{}"


def D(dia, hora=12):
    return datetime.datetime(2026, 10, dia, hora)


class Rodizio(unittest.TestCase):
    def setUp(self):
        os.environ.pop("PRODUTO_DA_VEZ_ID", None)
        self.addCleanup(os.environ.pop, "PRODUTO_DA_VEZ_ID", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.arq = os.path.join(self.tmp.name, "c.json")
        produtos = [{"id": f"p{i}", "nome": f"N{i}", "status": "pronto", "link_compra": LINK.format(f"a{i}"),
                     "preco_texto": "R$ 8,90"} for i in range(4)]
        produtos.append({"id": "pX", "nome": "NX", "status": "aguardando_cadastro", "link_compra": ""})
        catalogo.salvar({"produtos": produtos}, self.arq) if hasattr(catalogo, "salvar") else \
            open(self.arq, "w").write(__import__("json").dumps({"produtos": produtos}))

    def vez(self, ag):
        os.environ.pop("PRODUTO_DA_VEZ_ID", None)
        return catalogo.produto_da_vez(self.arq, ag)["id"]

    def test_so_produtos_prontos_com_link_e_gira_entre_todos(self):
        ids = {self.vez(D(d, h)) for d in range(1, 5) for h in (9, 21)}
        self.assertEqual(ids, {"p0", "p1", "p2", "p3"})

    def test_fatias_do_mesmo_dia_nao_repetem(self):
        self.assertNotEqual(self.vez(D(5, 9)), self.vez(D(5, 21)))

    def test_mesma_fatia_mesmo_produto(self):
        self.assertEqual(self.vez(D(5, 9)), self.vez(D(5, 12)))

    def test_env_forca_o_produto_e_a_primeira_chamada_o_fixa(self):
        os.environ["PRODUTO_DA_VEZ_ID"] = "p2"
        self.assertEqual(catalogo.produto_da_vez(self.arq, D(5))["id"], "p2")
        primeiro = self.vez(D(6))
        self.assertEqual(os.environ["PRODUTO_DA_VEZ_ID"], primeiro)
        self.assertEqual(catalogo.produto_da_vez(self.arq, D(20))["id"], primeiro)

    def test_sem_prontos_devolve_none(self):
        open(self.arq, "w").write('{"produtos": []}')
        self.assertIsNone(catalogo.produto_da_vez(self.arq, D(5)))


if __name__ == "__main__":
    unittest.main()
