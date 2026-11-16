"""Interface de linha de comando: ``qnetlab <comando> -t topologies/ring4.yaml``."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any, Callable, Dict, List

from .errors import QNetLabError
from .lab import Lab
from .logging_utils import setup_logging

log = logging.getLogger("qnetlab")


def _print_plan(lab: Lab, as_json: bool) -> None:
    plan, topo = lab.plan, lab.topo
    data: Dict[str, Any] = {
        "name": topo.name, "ring_size": topo.ring_size,
        "mgmt": {"network": str(plan.mgmt_network), "gateway": str(plan.mgmt_gateway)},
        "nodes": {}, "links": [], "tunnels": [],
    }
    for sid, sp in plan.sites.items():
        for c in sp.chrs:
            data["nodes"][c.name] = {"mgmt": str(c.mgmt_ip) if c.mgmt_ip else None, "router_id": str(c.router_id),
                                     "ifaces": {i.name: i.cidr for i in c.ifaces.values()}}
        data["nodes"][sp.kme.name] = {"kme_id": sp.kme.kme_id, "ifaces": {i.name: i.cidr for i in sp.kme.ifaces.values()}}
        data["nodes"][sp.node.name] = {"ifaces": {i.name: i.cidr for i in sp.node.ifaces.values()},
                                       "routes": [f"{n} via {gw} dev {dev}" for n, gw, dev in sp.node.routes]}
    for l in plan.links:
        data["links"].append({"kind": l.kind, "net": str(l.network) if l.network else None,
                              "a": f"{l.a}:{l.a_if}", "b": f"{l.b}:{l.b_if}"})
    for t in plan.tunnels:
        data["tunnels"].append({"name": t.name, "sa": f"{t.a_addr} <-> {t.b_addr}", "protected": t.protected,
                                "protects": f"{t.a_net} <-> {t.b_net}", "peer_sae": f"{t.a} <-> {t.b}"})
    if as_json:
        print(json.dumps(data, indent=2))
        return
    print(f"Laboratório: {topo.name} (anel de {topo.ring_size} sites)   mgmt/NAT: {plan.mgmt_network} (gw {plan.mgmt_gateway})")
    print("\nNós:")
    for n, d in data["nodes"].items():
        ifs = " ".join(f"{k}={v}" for k, v in d["ifaces"].items())
        extra = f" mgmt={d['mgmt']}" if d.get("mgmt") else ""
        print(f"  {n:<6}{extra:<22} {ifs}")
    print("\nEnlaces:")
    for l in data["links"]:
        print(f"  {l['kind']:<10} {l['a']:<14} <-> {l['b']:<14} {l['net'] or ''}")
    print("\nTúneis IPsec/QKD:" + ("" if data["tunnels"] else " (nenhum)"))
    for t in data["tunnels"]:
        print(f"  {t['name']:<12} SA {t['sa']:<26} {'protege' if t['protected'] else 'EM CLARO'} {t['protects']}")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-t", "--topology", required=True, help="arquivo YAML/JSON da topologia")
    common.add_argument("-w", "--workdir", default="build", help="diretório de artefatos (padrão: build)")
    common.add_argument("-v", "--verbose", action="count", default=0, help="mais logs (-v = debug)")

    p = argparse.ArgumentParser(prog="qnetlab", description="Framework IaC para laboratórios QKD/IPsec no GNS3")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("validate", parents=[common], help="valida a topologia e o plano de endereçamento")
    pl = sub.add_parser("plan", parents=[common], help="mostra endereçamento, enlaces e túneis")
    pl.add_argument("--json", action="store_true")
    sub.add_parser("render", parents=[common], help="gera PKI, .rsc e docker-compose offline (sem GNS3)")
    pk = sub.add_parser("pki", parents=[common], help="gera/atualiza Root CA e certificados")
    pk.add_argument("--force", action="store_true", help="reemite tudo (inclui nova CA)")
    sub.add_parser("images", parents=[common], help="docker build da base e de cada KME/APP")
    dp = sub.add_parser("deploy", parents=[common], help="cria projeto/nós/enlaces no GNS3 e inicia")
    dp.add_argument("--recreate", action="store_true", help="remove e recria o projeto se existir")
    sub.add_parser("bootstrap", parents=[common], help="IP de gerenciamento/REST nos CHR via console")
    sub.add_parser("upgrade", parents=[common], help="atualiza o RouterOS dos CHRs se < router.min_version (Internet via NAT)")
    sub.add_parser("configure", parents=[common], help="aplica OSPF/PKI/IPsec/firewall nos roteadores")
    vf = sub.add_parser("verify", parents=[common], help="verifica OSPF, IPsec e QKD")
    vf.add_argument("--timeout", type=float, default=240.0)
    up = sub.add_parser("up", parents=[common], help="pki -> images -> deploy -> bootstrap -> configure -> verify")
    up.add_argument("--recreate", action="store_true")
    up.add_argument("--skip-images", action="store_true")
    sub.add_parser("load", parents=[common], help="executa a carga nos APPs")
    co = sub.add_parser("collect", parents=[common], help="coleta logs/métricas, gera gráficos e output.tar.gz")
    co.add_argument("--offline", action="store_true", help="apenas consolida o que já está em metrics/ e logs/")
    sub.add_parser("run", parents=[common], help="load + collect")
    sub.add_parser("destroy", parents=[common], help="para e remove o projeto do GNS3")
    return p


def main(argv: List[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    try:
        lab = Lab(args.topology, args.workdir)
        cmd = args.cmd
        if cmd == "validate":
            print(f"OK: '{lab.topo.name}' ({len(lab.topo.sites)} sites, {len(lab.plan.chrs())} CHRs, {len(lab.plan.links)} enlaces, "
                  f"{len(lab.plan.tunnels)} túneis)")
        elif cmd == "plan":
            _print_plan(lab, args.json)
        elif cmd == "render":
            print(json.dumps(lab.render(), indent=2))
        elif cmd == "pki":
            print("Emitidos:", lab.pki(force=args.force) or "nada a fazer (tudo atualizado)")
        elif cmd == "images":
            print("\n".join(lab.images()))
        elif cmd == "deploy":
            nodes = lab.deploy(recreate=args.recreate)
            print(f"{len(nodes)} nós criados no projeto '{lab.topo.project_name}'")
        elif cmd == "bootstrap":
            lab.bootstrap()
        elif cmd == "upgrade":
            for n, v in sorted(lab.upgrade().items()):
                print(f"{n}: RouterOS {v}")
        elif cmd == "configure":
            lab.configure()
        elif cmd == "verify":
            rep = lab.verify(args.timeout)
            print(json.dumps(rep, indent=2, default=str))
            return 0 if rep.get("_ok") else 3
        elif cmd == "up":
            rep = lab.up(recreate=args.recreate, build_images=not args.skip_images)
            print(json.dumps(rep, indent=2, default=str))
            return 0 if rep.get("_ok") else 3
        elif cmd == "load":
            print(json.dumps(lab.load(), indent=2))
        elif cmd == "collect":
            print(json.dumps(lab.collect(live=not args.offline), indent=2, default=str))
        elif cmd == "run":
            print(json.dumps(lab.run_experiment(), indent=2, default=str))
        elif cmd == "destroy":
            lab.destroy()
    except QNetLabError as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        log.error("Interrompido pelo usuário.")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
