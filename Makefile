TOPO ?= topologies/ring4.yaml
WORK ?= build
Q = qnetlab
A = -t $(TOPO) -w $(WORK)   # -t/-w pertencem ao subcomando: vêm DEPOIS dele

.PHONY: install test validate plan render pki images up deploy bootstrap configure verify load collect run destroy clean

install:        ## instala o pacote em modo editável + dependências opcionais
	pip install -e '.[dev]'

test:           ## testes unitários e ponta a ponta local (sem GNS3)
	python -m pytest -q

validate:       ## valida topologia + plano de IPs
	$(Q) validate $(A)

plan:           ## mostra endereçamento, enlaces /30 e túneis
	$(Q) plan $(A)

render:         ## gera PKI, .rsc e docker-compose offline
	$(Q) render $(A)

pki:
	$(Q) pki $(A)

images:         ## docker build (base + uma imagem por KME)
	$(Q) images $(A)

up:             ## pipeline completo: pki -> images -> deploy -> bootstrap -> configure -> verify
	$(Q) up $(A) --recreate

deploy:
	$(Q) deploy $(A) --recreate

bootstrap:
	$(Q) bootstrap $(A)

configure:
	$(Q) configure $(A)

verify:
	$(Q) verify $(A)

load:
	$(Q) load $(A)

collect:
	$(Q) collect $(A)

run:            ## carga + coleta + gráficos + output.tar.gz
	$(Q) run $(A)

destroy:
	$(Q) destroy $(A)

clean:          ## remove artefatos locais (NÃO remove o projeto do GNS3)
	rm -rf $(WORK)
