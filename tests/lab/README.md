# Testes de laboratório (N1/N2)

Rodam contra um laboratório **já no ar** (`make up`). São opt-in: sem `QNETLAB_TOPOLOGY` todos aparecem como *skipped*.

```bash
export QNETLAB_TOPOLOGY=topologies/ring4.yaml     # obrigatório
export QNETLAB_WORKDIR=build                      # opcional (padrão: build)
python -m pytest tests/lab -m "lab and not disruptive" -v -rs   # só leitura/captura (também é o que `make test` roda)
python -m pytest tests/lab -m disruptive -v -rs   # derruba enlaces e para o KME1; só rode quando for isso que você quer
python -m pytest tests/lab -k seg01               # um experimento
```

Os testes **disruptivos são pulados por padrão** (inclusive no `make test` e em `pytest` puro, mesmo com `QNETLAB_TOPOLOGY`
definido): só rodam com `-m disruptive` ou `QNETLAB_DISRUPTIVE=1`. Depois deles, confirme o laboratório com `qnetlab verify`.

Controle positivo do SEG-01 (precisa de um enlace sem IPsec): use `topologies/ring4-control.yaml` (enlace 2 em claro; nome
`qkd-ring4-control`). O procedimento (`make destroy`, `make up TOPO=…`, rodar os testes) está no cabeçalho do arquivo.

Cada teste grava uma **evidência** em `build/<lab>/evidence/<ID>-<UTC>.json` (ambiente, resultados, hash dos arquivos
anexados, aprovado/reprovado), no formato pedido pelo plano (`docs/14-validation-test-plan.md`, §10.2).
Capturas ficam em `build/<lab>/captures/`.

* `@pytest.mark.plan("SEG-01")` liga o teste ao ID do plano.
* `@pytest.mark.disruptive` marca os que param nós ou derrubam enlaces (o *teardown* restaura tudo; pulados por padrão).
* Os critérios ficam **no docstring do teste, escritos antes** da execução.

## Quando tudo falha logo no início

* `Laboratório fora do ar: O GNS3 não conhece o projeto ...` → o `state.json` aponta para um projeto que não existe (ou o GNS3
  foi reiniciado/trocado). Rode `make up`. Confira com `curl -s localhost:3080/v2/projects`.
* `test_baseline_verify_is_ok` com `192.168.122.x: falha REST` → o projeto existe, mas o host não alcança a rede de
  gerenciamento dos CHRs (NAT do GNS3/`virbr0`, nós parados): veja `docs/11-troubleshooting.md`. Sem REST nos CHRs, os
  testes de enlace/captura não têm como concluir.
