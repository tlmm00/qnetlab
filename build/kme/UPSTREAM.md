# KME vendorizado

`main.py` é uma cópia **sem alterações** do KME de `KMS-sim/src/main.py`
(Flask, ETSI GS QKD 014 + sub-camadas KMA/KSA da ITU-T Y.3803).

* origem: `/home/tlmm/Codes/KMS-sim/src/main.py` (máquina `tlmm-pc`)
* sha256: `943675ea5ef2886149f518d757ff9c8582474bc5c92f74dc403c96308fd462f8`
* execução: `python3 main.py --config kme_config.yaml`
* o `kme_config.yaml` de cada KME é **gerado** pelo framework (`templates/docker/kme_config.yaml.j2`)

Para usar uma versão mais nova do seu repositório sem re-vendorizar, aponte
`containers.kme_source_dir` para a pasta que contém o `main.py` (ex.: `~/Codes/KMS-sim/src`).
