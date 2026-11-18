"""Interface de linha de comando: ``qnetlab <comando> -t topologies/ring4.yaml``."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List

from .bench import saturation, write_report
from .capture import summarize
from .errors import QNetLabError
from .faults import NODE_ACTIONS
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

    # ---- experimentos: falhas, captura, medição de custo
    link_sel = argparse.ArgumentParser(add_help=False)
    link_sel.add_argument("a", help="nó do enlace, opcionalmente com interface (ex.: MKT11 ou MKT11:ether2)")
    link_sel.add_argument("b", nargs="?", help="nó da outra ponta (opcional se A[:iface] já for único)")
    link_sel.add_argument("--kind", choices=["kme-access", "ring", "sibling", "node", "kma", "nat"],
                          help="filtra pelo tipo de enlace (ex.: kma = KME<->KME)")

    ft = sub.add_parser("fault", help="injeta falhas em enlaces e nós (requer o laboratório no ar)")
    fsub = ft.add_subparsers(dest="fault_cmd", required=True)
    fsub.add_parser("list", parents=[common], help="lista enlaces com filtros, suspensão e captura ativos")
    fl = fsub.add_parser("link", parents=[common, link_sel], help="aplica atraso/perda/corrupção/BPF (substitui filtros)")
    fl.add_argument("--delay", type=float, metavar="MS", help="atraso (ms), 0-32767")
    fl.add_argument("--jitter", type=float, default=0, metavar="MS", help="jitter ± (ms); exige --delay")
    fl.add_argument("--loss", type=float, metavar="PCT", help="perda aleatória (%%), 0-100")
    fl.add_argument("--corrupt", type=float, metavar="PCT", help="corrupção (%%), 0-100")
    fl.add_argument("--drop-every", type=int, metavar="N", help="descarta 1 a cada N pacotes (-1 = todos)")
    fl.add_argument("--bpf", action="append", metavar="EXPR", help="descarta o que casar com a expressão BPF (repetível)")
    fsub.add_parser("down", parents=[common, link_sel], help="suspende o enlace (queda do cabo)")
    fsub.add_parser("up", parents=[common, link_sel], help="religa o enlace suspenso")
    fc = fsub.add_parser("clear", parents=[common], help="remove filtros e religa enlaces")
    fc.add_argument("a", nargs="?", help="enlace específico (omita com --all)")
    fc.add_argument("b", nargs="?")
    fc.add_argument("--kind", choices=["kme-access", "ring", "sibling", "node", "kma", "nat"])
    fc.add_argument("--all", action="store_true", help="restaura todos os enlaces alterados")
    fn = fsub.add_parser("node", parents=[common], help="para/inicia/reinicia/pausa um nó (CHR, KME, NODE)")
    fn.add_argument("name", help="nome do nó (ex.: KME1, MKT21, NODE3)")
    fn.add_argument("action", choices=sorted(NODE_ACTIONS))
    fn.add_argument("--no-wait", action="store_true", help="não espera o estado final")

    cp = sub.add_parser("capture", help="captura de pacotes nos enlaces do GNS3")
    csub = cp.add_subparsers(dest="capture_cmd", required=True)
    csub.add_parser("list", parents=[common], help="enlaces com captura ativa")
    cr = csub.add_parser("run", parents=[common, link_sel], help="captura por --duration segundos, grava em captures/ e resume")
    cr.add_argument("--duration", type=float, default=30.0)
    cr.add_argument("--label", default="")
    cs = csub.add_parser("summary", help="resume um .pcap (protocolos, ESP/IKE, trechos em claro)")
    cs.add_argument("pcap")
    cs.add_argument("--needle", help="texto procurado nos bytes (ex.: endereço interno, assinatura da sonda)")
    cs.add_argument("-v", "--verbose", action="count", default=0)

    bn = sub.add_parser("bench", help="custo do emulador: tempo por etapa e consumo de CPU/RAM")
    bsub = bn.add_subparsers(dest="bench_cmd", required=True)
    bs = bsub.add_parser("sample", parents=[common], help="amostra recursos por --duration segundos")
    bs.add_argument("--duration", type=float, default=60.0)
    bs.add_argument("--interval", type=float, default=2.0)
    bs.add_argument("--label", default="sample")
    bs.add_argument("--no-docker", action="store_true", help="não consulta 'docker stats'")
    bu = bsub.add_parser("up", parents=[common], help="'up' cronometrado e com amostragem de recursos")
    bu.add_argument("--recreate", action="store_true")
    bu.add_argument("--skip-images", action="store_true")
    bu.add_argument("--interval", type=float, default=2.0)
    bu.add_argument("--label", default="up")
    sub.add_parser("destroy", parents=[common], help="para e remove o projeto do GNS3")
    return p


def _print_links(rows: List[Dict[str, Any]]) -> None:
    for r in rows:
        flags = []
        if r["suspended"]:
            flags.append("SUSPENSO")
        if r["filters"]:
            flags.append("filtros=" + json.dumps(r["filters"], separators=(",", ":")))
        if r["capturing"]:
            flags.append("CAPTURANDO")
        print(f"  {r['a']}:{r['a_if']:<7} <-> {r['b']}:{r['b_if']:<7} {r['kind']:<10} {' '.join(flags)}")


def _run_fault(lab: Lab, args: argparse.Namespace) -> int:
    inj = lab.faults()
    fc = args.fault_cmd
    if fc == "list":
        _print_links(inj.link_state())
    elif fc == "node":
        print(json.dumps(inj.node(args.name, args.action, wait=not args.no_wait)))
    elif fc == "clear":
        if args.all or not args.a:
            print(f"{inj.clear_all()} enlace(s) restaurado(s)")
        else:
            inj.clear(inj.links.one(args.a, args.b, args.kind))
    else:
        ref = inj.links.one(args.a, args.b, args.kind)
        if fc == "link":
            if all(v is None for v in (args.delay, args.loss, args.corrupt, args.drop_every, args.bpf)):
                raise QNetLabError("Informe ao menos um de --delay, --loss, --corrupt, --drop-every, --bpf "
                                   "(para remover filtros use 'fault clear').")
            print(json.dumps(inj.set_link(ref, delay_ms=args.delay, jitter_ms=args.jitter, loss_pct=args.loss,
                                          corrupt_pct=args.corrupt, drop_every=args.drop_every, bpf=args.bpf)))
        elif fc == "down":
            inj.down(ref)
        elif fc == "up":
            inj.up(ref)
    return 0


def _run_capture(lab: Lab, args: argparse.Namespace) -> int:
    cap = lab.capture()
    cc = args.capture_cmd
    if cc == "list":
        print("\n".join(cap.active()) or "nenhuma captura ativa")
        return 0
    ref = cap.links.one(args.a, args.b, args.kind)
    path = cap.run(ref, args.duration, args.label)
    print(json.dumps(summarize(path), indent=2))
    return 0


def _run_bench(lab: Lab, args: argparse.Namespace) -> int:
    sampler = lab.sampler(args.interval, use_docker=not getattr(args, "no_docker", False))
    data: Dict[str, Any] = {"label": args.label, "kind": args.bench_cmd, "environment": lab.environment()}
    rc = 0
    if args.bench_cmd == "sample":
        with sampler:
            time.sleep(args.duration)
    else:
        with sampler:
            rep = lab.up(recreate=args.recreate, build_images=not args.skip_images)
        data["verify_ok"] = rep.get("_ok")
        rc = 0 if rep.get("_ok") else 3
        up_reports = sorted(lab.ws.bench_dir.glob("up-*.json"))
        if up_reports:
            data["stages"] = json.loads(up_reports[-1].read_text())["stages"]
    data["resources"] = sampler.summary()
    data["saturation"] = saturation(data["resources"])
    data["samples"] = sampler.samples
    path = write_report(lab.ws, args.label, data)
    brief = {"report": str(path), "saturation": data["saturation"], "stages": data.get("stages"),
             "headline": {k: v for k, v in data["resources"].items()
                          if k in ("host.cpu_pct", "host.mem_used_mb", "chr_total.rss_mb", "containers_total.mem_mb")}}
    print(json.dumps(brief, indent=2))
    return rc


def main(argv: List[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(getattr(args, "verbose", 0))
    try:
        if args.cmd == "capture" and args.capture_cmd == "summary":      # offline: não precisa de laboratório
            print(json.dumps(summarize(Path(args.pcap), args.needle.encode() if args.needle else None), indent=2))
            return 0
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
        elif cmd == "fault":
            return _run_fault(lab, args)
        elif cmd == "capture":
            return _run_capture(lab, args)
        elif cmd == "bench":
            return _run_bench(lab, args)
    except QNetLabError as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        log.error("Interrompido pelo usuário.")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
